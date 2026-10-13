"""Host-side speech normalization for Jibo's SSML-like TTS surface.

The gateway keeps the original message for conversation/event logs, while this
module produces a separate, bounded speech representation.  It removes
unhelpful code and URLs, turns common symbols into words, and uses the small
documented Jibo TTS vocabulary (``phoneme`` and ``say-as spell``) for common
technical acronyms.  The phoneme table deliberately uses ARPABET vowel stress
0/1 only; the deployed Speak-Tweak documentation warns that stress 2 is not
supported reliably.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
from collections.abc import Mapping
import re
from typing import Any

from .esml import sanitize_esml

MAX_SPOKEN_CHARS = 3000

# ARPABET symbols documented by the archived ESML reference.  Keeping this
# allow-list here prevents a future pronunciation config from accidentally
# sending an unsupported/unsafe token to the robot's XML parser.
_ARPABET = {
    "a", "aa", "ae", "ah", "ai", "au", "b", "d", "dj", "dt", "e", "ei",
    "f", "g", "h", "i", "ii", "j", "k", "l", "lf", "ls", "m", "ms", "n",
    "ng", "ns", "o", "oi", "or", "ou", "p", "r", "s", "sh", "t", "th",
    "tj", "u", "uo", "ur", "v", "w", "y", "z", "zh", "spau", "lpau",
}
_VOWELS = {
    "a", "aa", "ae", "ah", "ai", "au", "e", "ei", "i", "ii", "o", "oi",
    "or", "ou", "u", "uo", "ur",
}

# These are intentionally conservative.  Words whose pronunciation depends on
# context are left alone; the general acronym fallback uses say-as spelling.
DEFAULT_PHONEMES: dict[str, str] = {
    # These spellings use the Jibo/ESML inventory (for example ``ei`` and
    # ``ai`` rather than the similarly named symbols in desktop ARPABET).
    # Keep this table short: unknown acronyms use the documented spell mode.
    "AI": "ei1 ai0",
    "API": "ei1 p ii1 ai0",
    "CPU": "s ii1 p ii1 y uo0",
    "GPU": "dj ii1 p ii1 y uo0",
    "IP": "ai1 p ii0",
    "JSON": "dj ei1 s ah0 n",
    "ROM": "r ou1 m",
    "UI": "y uo1 ai0",
    "UX": "y uo1 ei1 k s",
}

# Uppercase terms that are clearer as letter names when no curated phoneme is
# available.  Do not spell every capitalized word: NASA, JIBO, and ordinary
# sentence initials should remain natural words.
SPELL_ACRONYMS = frozenset({
    "ACL", "ASCII", "ASR", "CLI", "DNS", "GUI", "HTML", "HTTP", "HTTPS", "ID",
    "JPEG", "LAN", "LED", "OS", "PDF", "PID", "PNG", "POS", "QR", "SDK", "SQL",
    "SSH", "STT", "TCP", "TLS", "TTS", "UDP", "URI", "URL", "USB", "UUID", "VPN",
    "WIFI", "XML", "YAML", "ZIP",
})

_CODE_FENCE_RE = re.compile(r"```[^\n`]*\n?[\s\S]*?```", re.IGNORECASE)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]\n]*\]\([^\)\n]*\)")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]+)\]\([^\)\n]*\)")
_URL_RE = re.compile(r"(?i)(?<![\w\"'=])(?:https?://|www\.)[^\s<>()[\]]+")
_EMAIL_RE = re.compile(r"(?i)(?<![\w])([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})(?![\w])")
_TAG_RE = re.compile(r"<[^>]*>")
_ACRONYM_RE = re.compile(r"(?<![A-Za-z])([A-Z]{2,}[0-9]*)(?![A-Za-z])")

_SYMBOL_WORDS = {
    "@": " at ",
    "#": " number ",
    "%": " percent ",
    "&": " and ",
    "+": " plus ",
    "=": " equals ",
    "/": " slash ",
    "\\": " backslash ",
    "|": " or ",
    "^": " caret ",
    "~": " tilde ",
    "_": " underscore ",
}


@dataclass(frozen=True)
class Pronunciation:
    """Raw/logging and speech-safe views of one message."""

    raw_text: str
    spoken_text: str
    esml: str


def _valid_phoneme_string(value: Any) -> bool:
    tokens = str(value or "").strip().lower().split()
    if not tokens:
        return False
    for token in tokens:
        stress = token[-1:] if token.endswith(("0", "1", "2")) else ""
        base = token[:-1] if stress else token
        if base not in _ARPABET:
            return False
        if base in _VOWELS and stress not in {"0", "1"}:
            return False
        if base not in _VOWELS and stress:
            return False
    return True


def _apply_plain_segments(value: str, transform) -> str:
    """Apply a text transform without touching XML tags already in the ESML."""
    out: list[str] = []
    cursor = 0
    for match in _TAG_RE.finditer(value):
        if match.start() > cursor:
            out.append(transform(value[cursor:match.start()]))
        out.append(match.group(0))
        cursor = match.end()
    if cursor < len(value):
        out.append(transform(value[cursor:]))
    return "".join(out)


def _replace_literals(value: str) -> str:
    """Remove content that is useful to text readers but poor TTS input."""
    value = _CODE_FENCE_RE.sub(" the code ", value)
    value = _INLINE_CODE_RE.sub(" the code ", value)
    value = _MARKDOWN_IMAGE_RE.sub("", value)
    value = _MARKDOWN_LINK_RE.sub(r"\1", value)
    value = _EMAIL_RE.sub(" the email address ", value)

    def replace_url(match: re.Match[str]) -> str:
        target = match.group(0)
        trailing = ""
        while target and target[-1] in ".,;:!?":
            trailing = target[-1] + trailing
            target = target[:-1]
        return " the link " + trailing

    return _URL_RE.sub(replace_url, value)


def _replace_symbols(value: str) -> str:
    def transform(segment: str) -> str:
        for symbol, word in _SYMBOL_WORDS.items():
            segment = segment.replace(symbol, word)
        return segment

    return _apply_plain_segments(value, transform)


def _merged_phonemes(custom: Mapping[str, Any] | None) -> dict[str, str]:
    result = dict(DEFAULT_PHONEMES)
    for key, value in (custom or {}).items():
        name = str(key or "").strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9]{1,15}", name):
            continue
        if _valid_phoneme_string(value):
            result[name] = " ".join(str(value).lower().split())
    return result


def _annotate_acronyms(value: str, phonemes: Mapping[str, str]) -> str:
    """Add only safe, documented TTS tags outside existing speech tags."""
    protected_depth = 0
    out: list[str] = []
    cursor = 0

    def annotate(segment: str) -> str:
        def replacement(match: re.Match[str]) -> str:
            visible = match.group(1)
            term = visible.upper()
            if term in phonemes:
                return f'<phoneme ph="{phonemes[term]}">{html.escape(visible)}</phoneme>'
            if term in SPELL_ACRONYMS:
                # Spell tags are self-closing in the archived reference; the
                # corresponding plain view is generated separately below.
                return f'<say-as spell="{html.escape(term)}"/>'
            return visible

        return _ACRONYM_RE.sub(replacement, segment)

    for match in _TAG_RE.finditer(value):
        if match.start() > cursor and protected_depth == 0:
            out.append(annotate(value[cursor:match.start()]))
        elif match.start() > cursor:
            out.append(value[cursor:match.start()])
        tag = match.group(0)
        out.append(tag)
        tag_name = re.match(r"<\s*(/?)\s*([A-Za-z][\w-]*)", tag)
        if tag_name:
            closing, name = tag_name.groups()
            if name.lower() in {"phoneme", "say-as"} and not tag.rstrip().endswith("/>"):
                protected_depth = max(0, protected_depth - 1) if closing else protected_depth + 1
        cursor = match.end()
    tail = value[cursor:]
    out.append(annotate(tail) if protected_depth == 0 else tail)
    return "".join(out)


def _plain_view(value: str, phonemes: Mapping[str, str]) -> str:
    """Create the plain text sent alongside ESML for native peers."""
    def expand_phonemes(match: re.Match[str]) -> str:
        visible = html.unescape(match.group(1))
        term = visible.upper()
        if term in phonemes or term in SPELL_ACRONYMS:
            return " ".join(visible)
        return visible

    # Curated phonemes carry the original visible word as a child, so remove
    # the tag while retaining a useful letter-by-letter fallback for a peer
    # that cannot consume ESML.
    value = re.sub(
        r"<phoneme\b[^>]*>([\s\S]*?)</phoneme\s*>",
        expand_phonemes,
        value,
        flags=re.IGNORECASE,
    )

    def expand_acronyms(segment: str) -> str:
        def replacement(match: re.Match[str]) -> str:
            term = match.group(1)
            upper = term.upper()
            if upper in phonemes or upper in SPELL_ACRONYMS:
                return " ".join(term)
            return term

        return _ACRONYM_RE.sub(replacement, segment)

    plain = _apply_plain_segments(value, expand_acronyms)
    # A say-as tag has no child text.  Retain its spelling in the plain view.
    plain = re.sub(
        r"<say-as\b[^>]*\bspell\s*=\s*['\"]([A-Za-z0-9]+)['\"][^>]*/\s*>",
        lambda match: " ".join(match.group(1)),
        plain,
        flags=re.IGNORECASE,
    )
    plain = _TAG_RE.sub(" ", plain)
    plain = re.sub(r"\s+", " ", html.unescape(plain)).strip()
    if len(plain) > MAX_SPOKEN_CHARS:
        plain = plain[: MAX_SPOKEN_CHARS - 1].rsplit(" ", 1)[0].rstrip() + "…"
    return plain


def normalize_for_speech(
    text: Any,
    *,
    custom_phonemes: Mapping[str, Any] | None = None,
) -> Pronunciation:
    """Return raw, safe plain, and ESML-enhanced views of *text*.

    This function never mutates or rewrites the caller's raw value.  The
    returned ``raw_text`` is intended for logs; only ``spoken_text`` and
    ``esml`` should be sent to a TTS/display operation.
    """
    raw_text = "" if text is None else str(text)
    value = _replace_literals(raw_text)
    value = _replace_symbols(value)
    # sanitize_esml supplies the existing private-content, markdown, URL,
    # Unicode, and supported-tag policy used by the legacy ROM path.
    value = sanitize_esml(value)
    phonemes = _merged_phonemes(custom_phonemes)
    esml = _annotate_acronyms(value, phonemes)
    esml = sanitize_esml(esml)
    return Pronunciation(
        raw_text=raw_text,
        spoken_text=_plain_view(esml, phonemes),
        esml=esml,
    )
