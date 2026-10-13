"""Small, conservative ESML preparation layer for Jibo ROM speech."""

from __future__ import annotations

import html
import re
import unicodedata

MAX_CHUNK_CHARS = 450
MAX_TOTAL_CHARS = 3000

_THINK_RE = re.compile(r"<think\b[^>]*>[\s\S]*?</think\s*>", re.I)
_SSA_RE = re.compile(r"<ssa\b[^>]*/\s*>", re.I)
_INTERNAL_META_LINE_RE = re.compile(
    r"^\s*(?:💾\s*)?(?:"
    r"self[- ]improvement(?:\s+review)?|"
    r"memory\s+(?:updated|saved)|"
    r"(?:i\s+)?(?:updated|saved)\s+(?:my\s+)?(?:memory|skill)"
    r")\b\s*:?",
    re.I,
)
_SSML_BREAK_RE = re.compile(
    r"<break\b[^>]*?(?:time\s*=\s*['\"]([0-9.]+)s?['\"])?[^>]*/\s*>",
    re.I,
)
_TAG_RE = re.compile(r"<[^>]*>")
_URL_RE = re.compile(r"https?://[^\s<]+", re.I)
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]\n]*\]\([^\)\n]*\)")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]+)\]\([^\)\n]+\)")
_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
_DOLLAR_AMOUNT_RE = re.compile(
    r"(?<![\w])(?P<sign>-?)\$\s*"
    r"(?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"(?!\d)"
)
_ALLOWED_TAGS = {
    "anim",
    "audio",
    "break",
    "duration",
    "emphasis",
    "es",
    "jiboji",
    "list",
    "phoneme",
    "pitch",
    "say-as",
    "sfx",
    "style",
    "tts",
}
_STATUS_ANIMATION = {
    "thinking": "confused",
    "progress": "confused",
    "error": "embarrassed",
}
_SUPPORTED_STYLES = {"neutral", "enthusiastic", "sheepish", "confused", "confident"}


def _strip_internal_meta(text: str) -> str:
    """Remove Hermes review/memory notes from spoken output."""
    kept: list[str] = []
    suppressing = False
    for line in str(text or "").splitlines():
        if _INTERNAL_META_LINE_RE.search(line):
            suppressing = True
            continue
        if suppressing:
            if not line.strip():
                suppressing = False
            continue
        kept.append(line)
    return "\n".join(kept)


def _strip_unknown_tags(value: str) -> str:
    """Remove markup that the documented Jibo speech surface does not know."""
    def replace(match: re.Match[str]) -> str:
        tag_name_match = re.match(r"</?\s*([A-Za-z][\w-]*)", match.group(0))
        if not tag_name_match:
            return ""
        return match.group(0) if tag_name_match.group(1).lower() in _ALLOWED_TAGS else ""

    return _TAG_RE.sub(replace, value)


def _rewrite_dollar_amounts(value: str) -> str:
    """Turn leading currency notation into speech-friendly number-first text."""
    def replace(match: re.Match[str]) -> str:
        sign = "negative " if match.group("sign") else ""
        return f"{sign}{match.group('amount')} dollars"

    out: list[str] = []
    plain: list[str] = []
    in_tag = False

    def flush_plain() -> None:
        if plain:
            out.append(_DOLLAR_AMOUNT_RE.sub(replace, "".join(plain)))
            plain.clear()

    for char in value:
        if char == "<":
            flush_plain()
            in_tag = True
            out.append(char)
        elif char == ">":
            in_tag = False
            out.append(char)
        elif in_tag:
            out.append(char)
        else:
            plain.append(char)
    flush_plain()
    return "".join(out)


def _rewrite_plain_symbols(value: str) -> str:
    """Make punctuation/symbols outside XML tags safe for the legacy TTS parser."""
    replacements = {
        "&": " and ",
        "@": " at ",
        "#": " ",
        "$": " dollars ",
        "%": " percent ",
        "/": " slash ",
        "\\": " backslash ",
        "|": " or ",
        "=": " equals ",
        "+": " plus ",
        "^": " caret ",
        "~": " ",
        "`": "",
        "[": " ",
        "]": " ",
        "{": " ",
        "}": " ",
    }
    out: list[str] = []
    in_tag = False
    for char in value:
        if char == "<":
            in_tag = True
            out.append(char)
        elif char == ">":
            in_tag = False
            out.append(char)
        elif not in_tag and char in replacements:
            out.append(replacements[char])
        else:
            out.append(char)
    return "".join(out)


def strip_thinking(text: str) -> str:
    """Remove reasoning blocks and obvious markdown preambles."""
    value = _THINK_RE.sub("", _strip_internal_meta(str(text or "")))
    # A leaked model preamble before an ESML tag is not useful speech. Keep
    # short introductions, but discard a long preamble that precedes markup.
    first_tag = re.search(r"<(?:anim|sfx|break|style|es)\b", value, re.I)
    if first_tag and first_tag.start() > 120:
        value = value[first_tag.start():]
    return value.strip()


def _remove_unsupported_unicode(value: str) -> str:
    out = []
    for char in value:
        category = unicodedata.category(char)
        # Keep normal ASCII plus XML punctuation. Drop emoji/symbols and
        # variation selectors that are commonly rejected by old ROM builds.
        if ord(char) < 128:
            out.append(char)
        elif category.startswith("M") and ord(char) in range(0xFE00, 0xFE10):
            continue
        elif category.startswith("S") or category in {"So", "Sk"}:
            continue
        elif char in "\u200d\u200c\ufe0e\ufe0f":
            continue
        else:
            # Preserve ordinary letters from non-ASCII languages only when
            # they are not a symbol; the legacy ROM path is safest with ASCII.
            if category[0] in {"L", "N", "P", "Z"}:
                out.append(char)
    return "".join(out)


def sanitize_esml(text: str) -> str:
    """Normalize model text while preserving safe Jibo tags."""
    value = strip_thinking(text)
    # Do not make Jibo read image alt text or spoiler delimiters.
    value = _MARKDOWN_IMAGE_RE.sub("", value)
    value = re.sub(r"\|\|", "", value)
    value = _CODE_FENCE_RE.sub("the code", value)
    value = _MARKDOWN_LINK_RE.sub(r"\1", value)
    value = _URL_RE.sub("the link", value)
    value = html.unescape(value)
    value = _strip_unknown_tags(value)
    value = _SSA_RE.sub("", value)
    value = _SSML_BREAK_RE.sub(
        lambda match: "<break size='" + (match.group(1) or "0.1") + "'/>",
        value,
    )
    # Markdown is presentation noise on the robot. Do this outside the tag
    # parser only approximately; the supported ESML tags contain no markdown.
    value = re.sub(r"^\s*#{1,6}\s+", "", value, flags=re.M)
    value = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", value)
    value = re.sub(r"__([^_\n]+)__", r"\1", value)
    value = re.sub(r"(?<![<\w])\*([^*\n]+)\*", r"\1", value)
    value = re.sub(r"^[ \t]*[-*]\s+", "", value, flags=re.M)
    value = re.sub(r"\n+", " ", value)
    value = _rewrite_dollar_amounts(value)
    value = _rewrite_plain_symbols(value)
    value = re.sub(r"!{2,}", "!", value)
    value = re.sub(r"\?{2,}", "?", value)
    value = _remove_unsupported_unicode(value)
    # Preserve existing entities only if any survived unescape; replace bare
    # ampersands so the ROM XML parser does not reject the command.
    value = re.sub(r"&(?![A-Za-z][A-Za-z0-9#]*;)", "and", value)
    return value.strip()


def format_jibo_speech(
    text: str,
    *,
    kind: str = "answer",
    style: str = "neutral",
) -> str:
    """Format intentional speech as conservative, ROM-safe Jibo ESML.

    ``kind`` controls a small embodied cue. It never exposes model reasoning;
    thinking/progress are represented by a short animation and the supplied
    user-facing sentence.
    """
    normalized_kind = str(kind or "answer").strip().lower()
    if normalized_kind not in {"thinking", "progress", "answer", "error"}:
        normalized_kind = "answer"
    normalized_style = str(style or "neutral").strip().lower()
    if normalized_style not in _SUPPORTED_STYLES:
        normalized_style = "neutral"

    value = sanitize_esml(text)
    if not value:
        return ""
    if normalized_style != "neutral":
        value = f"<style set='{normalized_style}'> {value} </style>"
    animation = _STATUS_ANIMATION.get(normalized_kind)
    if animation:
        return (
            f"<anim cat='{animation}' nonBlocking='true' endNeutral='true'/> "
            f"<break size='0.1'/> {value}"
        )
    return value


def _find_cut(value: str, limit: int) -> int:
    """Find a whitespace cut at or before limit, outside XML tags."""
    in_tag = False
    sentence = -1
    word = -1
    for index, char in enumerate(value[:limit]):
        if char == "<":
            in_tag = True
        elif char == ">":
            in_tag = False
        elif not in_tag and char.isspace():
            if index and value[index - 1] in ".!?":
                sentence = index
            word = index
    if sentence > 0:
        return sentence
    if word > 0:
        return word
    return -1


def chunk_esml(text: str, max_len: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split at whitespace outside tags and never cut an XML tag in half."""
    if max_len < 32:
        raise ValueError("max_len is too small for a safe ESML chunk")
    if not text:
        return []
    remaining = text.strip()
    chunks: list[str] = []
    plain_prefix = "<break size='0.1'/> "
    # Plain chunks receive this prefix below, so reserve its bytes/characters
    # while finding cuts. Tagged chunks may consequently be a little shorter,
    # which is safer than ever exceeding the ROM command limit.
    payload_limit = max_len - len(plain_prefix)
    while len(remaining) > payload_limit:
        cut = _find_cut(remaining, payload_limit)
        if cut < 0:
            # Avoid splitting inside a tag. If the first tag itself is too
            # large, retain it whole and let the caller's max be exceeded.
            tag_end = remaining.find(">")
            if 0 <= tag_end < max_len:
                cut = tag_end + 1
            else:
                cut = payload_limit
        chunk = remaining[:cut].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    # Plain text is accepted by ROM, but a tiny break tag makes the legacy
    # parser consistently treat every chunk as an ESML expression.
    return [chunk if re.search(r"<[A-Za-z][^>]*>", chunk) else "<break size='0.1'/> " + chunk for chunk in chunks]


def prepare_for_jibo(text: str) -> list[str]:
    """Apply all output limits and return ROM-ready chunks."""
    value = sanitize_esml(text)
    if len(value) > MAX_TOTAL_CHARS:
        value = value[:MAX_TOTAL_CHARS]
        value = re.sub(r"<[^>]*$", "", value).rsplit(" ", 1)[0].rstrip() + "..."
    return chunk_esml(value)
