"""Incremental response delivery for the native bridge.

The agent is far slower than a 2018 skill.  Without incremental delivery the
robot sits on a THINKING screen for the whole generation and then says
everything at once, which reads as a hang rather than as thought.

This module is the pure half of the fix: it turns the gateway's *cumulative*
streaming frames into

* a monotonic character delta, which the adapter pushes to the robot as
  ``response.delta`` so the chat UI renders text as it is produced, and
* completed sentences, which the adapter hands to native TTS one at a time so
  speech starts at the first sentence boundary instead of at the end.

It holds no sockets and no robot state, so the segmentation rules can be
tested directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
import re
from typing import Optional

#: Never emit a "sentence" shorter than this; it is almost always an
#: abbreviation ("Dr.", "e.g.") or a decimal point rather than a boundary,
#: and a one-word utterance sounds like a stutter through native TTS.
MIN_SENTENCE_CHARS = 24

#: A run-on paragraph with no terminal punctuation still has to start
#: speaking.  Past this length the segmenter breaks at the last clause
#: boundary (comma, semicolon, colon, dash) instead of waiting.
MAX_SENTENCE_CHARS = 240

#: Below this the delta is held back rather than sent, so a token-by-token
#: stream does not become one bridge request per token.  A pending delta is
#: always flushed at a sentence boundary and at the end of the turn.
MIN_DELTA_CHARS = 24

logger = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"[.!?…]+[\"'”’)\]]*(?=\s|$)")
_CLAUSE_END = re.compile(r"[,;:—–-](?=\s)")
_ABBREVIATIONS = frozenset(
    {
        "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt", "vs", "etc",
        "e.g", "i.e", "approx", "no", "fig", "inc", "ltd", "co", "dept",
        "a.m", "p.m", "u.s", "u.k",
    }
)


def _ends_in_abbreviation(text: str) -> bool:
    """Whether a candidate boundary is really an abbreviation or a decimal."""
    stripped = text.rstrip()
    if not stripped.endswith("."):
        return False
    tail = stripped[:-1]
    if tail and tail[-1].isdigit():
        # "3." in "3.5" — a decimal point, not a full stop.
        return True
    word = re.split(r"[\s(]", tail)[-1].lower().strip("\"'“‘([")
    return word in _ABBREVIATIONS


class SentenceSegmenter:
    """Accumulate streamed text and yield speakable sentences."""

    def __init__(
        self,
        *,
        min_chars: int = MIN_SENTENCE_CHARS,
        max_chars: int = MAX_SENTENCE_CHARS,
    ):
        self._buffer = ""
        self._min_chars = max(1, int(min_chars))
        self._max_chars = max(self._min_chars, int(max_chars))

    @property
    def pending(self) -> str:
        """Text held back because no boundary has been reached yet."""
        return self._buffer

    def feed(self, text: str) -> list[str]:
        """Add text; return every sentence that is now complete."""
        if not text:
            return []
        self._buffer += text
        sentences: list[str] = []
        while True:
            cut = self._next_cut()
            if cut is None:
                break
            sentence = self._buffer[:cut].strip()
            self._buffer = self._buffer[cut:].lstrip()
            if sentence:
                sentences.append(sentence)
        return sentences

    def flush(self) -> str:
        """Return and clear whatever is left, boundary or not."""
        remainder = self._buffer.strip()
        self._buffer = ""
        return remainder

    def _next_cut(self) -> Optional[int]:
        for match in _SENTENCE_END.finditer(self._buffer):
            end = match.end()
            if end < self._min_chars:
                continue
            if _ends_in_abbreviation(self._buffer[:end]):
                continue
            return end
        if len(self._buffer) < self._max_chars:
            return None
        # Forced break: prefer the last clause boundary inside the window so
        # the robot does not stop mid-noun-phrase.
        window = self._buffer[: self._max_chars]
        clause = None
        for match in _CLAUSE_END.finditer(window):
            if match.end() >= self._min_chars:
                clause = match.end()
        if clause is not None:
            return clause
        space = window.rfind(" ", self._min_chars)
        return space if space > 0 else self._max_chars


@dataclass
class StreamStep:
    """What one streaming frame produced."""

    #: New characters since the last frame, ready for ``response.delta``.
    #: Empty when the delta was held back as too small to be worth a frame.
    delta: str = ""
    #: Completed sentences, ready for one ``speech.speak`` each.
    sentences: list[str] = field(default_factory=list)
    #: True on the first frame that carried any text, so the caller can move
    #: the robot from THINKING to ANSWERING exactly once.
    first: bool = False
    #: True when ``delta`` supersedes everything rendered so far rather than
    #: appending to it.  Only ``finish`` can set this, and only when the
    #: authoritative answer diverged from what was streamed.
    replace: bool = False


class ResponseStream:
    """Turn cumulative streaming frames into deltas and speakable sentences.

    The gateway's stream consumer hands an adapter the *accumulated* text of
    the current segment, and starts a new segment when the model interleaves
    tool calls with prose.  This class reassembles those frames into one
    monotonically growing response, so a delta is always an append and the
    robot's transcript never has to be rewritten.
    """

    def __init__(
        self,
        *,
        turn_id: str = "",
        draft_id: int = 0,
        start_seq: int = 0,
        min_delta_chars: int = MIN_DELTA_CHARS,
        min_sentence_chars: int = MIN_SENTENCE_CHARS,
        max_sentence_chars: int = MAX_SENTENCE_CHARS,
    ):
        self.turn_id = turn_id
        self.draft_id = int(draft_id)
        self._prefix = ""
        self._segment = ""
        self._emitted = 0
        self._held = ""
        self._spoken = ""
        self._min_delta_chars = max(0, int(min_delta_chars))
        self._segmenter = SentenceSegmenter(
            min_chars=min_sentence_chars, max_chars=max_sentence_chars
        )
        self._seq = max(0, int(start_seq))
        self._started = False

    @property
    def text(self) -> str:
        """Everything received so far, as one response."""
        return self._prefix + self._segment

    @property
    def spoken_text(self) -> str:
        """Everything already handed to native TTS this turn."""
        return self._spoken

    @property
    def seq(self) -> int:
        """Monotonic delta counter, so the robot can detect a gap."""
        return self._seq

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def push(self, cumulative: str) -> StreamStep:
        """Absorb one streaming frame carrying the accumulated segment text."""
        content = cumulative or ""
        if content.startswith(self._segment):
            self._segment = content
        elif self._segment and not content:
            # A cleared frame is a segment break with nothing in it yet.
            self._prefix = self._join(self._prefix, self._segment)
            self._segment = ""
        else:
            # A fresh segment after a tool call: keep what came before.
            self._prefix = self._join(self._prefix, self._segment)
            self._segment = content
        return self._advance(force=False)

    def finish(self, final_text: str = "") -> StreamStep:
        """Close the stream, reconciling against the authoritative answer.

        The gateway's final send carries the completed response, which may
        differ from the streamed text (post-stream augmentation, filtering).
        When it extends what was already spoken, only the tail is spoken.
        When it diverges, the streamed remainder is dropped and the caller is
        handed the whole authoritative answer to speak instead — better a
        short repeat than a wrong or truncated one.
        """
        final = (final_text or "").strip()
        if not final:
            return self._advance(force=True)

        # Anything still buffered is superseded by the authoritative answer.
        self._segmenter.flush()
        self._held = ""
        streamed = self.text
        spoken = _normalize(self._spoken)

        if spoken and not _normalize(final).startswith(spoken):
            # Divergence.  A short repeat is better than a wrong or truncated
            # answer, so the whole thing is re-rendered and re-spoken.
            #
            # This is the one path that makes the robot say an answer twice, so
            # it says so: a divergence that happens on *every* turn is not the
            # rare reconciliation this was designed for, it is a formatting
            # mismatch between the streamed sentences and the final text.
            logger.info(
                "stream diverged; re-speaking the whole answer "
                "(spoken=%r final=%r)", spoken[:80], _normalize(final)[:80],
            )
            self._reset_to(final)
            self._spoken = final
            self._started = True
            return StreamStep(delta=final, sentences=[final], replace=True)

        tail = _tail_after_spoken(final, self._spoken) if spoken else final
        consumed = _appended_prefix_length(final, streamed)
        appends = consumed is not None
        delta = final[consumed:] if appends else final
        self._reset_to(final)
        if tail:
            self._spoken = final
        step = StreamStep(
            delta=delta,
            sentences=[tail] if tail else [],
            replace=not appends,
            first=not self._started and bool(delta or tail),
        )
        self._started = self._started or bool(step.delta or step.sentences)
        return step

    def _reset_to(self, text: str) -> None:
        self._prefix = text
        self._segment = ""
        self._emitted = len(text)

    def _advance(self, *, force: bool) -> StreamStep:
        text = self.text
        fresh = text[self._emitted:]
        self._emitted = len(text)
        sentences = self._segmenter.feed(fresh)
        if force:
            remainder = self._segmenter.flush()
            if remainder:
                sentences.append(remainder)
        self._held += fresh
        step = StreamStep()
        if self._held and (force or sentences or len(self._held) >= self._min_delta_chars):
            step.delta = self._held
            self._held = ""
        if sentences:
            step.sentences = sentences
            self._spoken = self._join_spoken(sentences)
        if (step.delta or step.sentences) and not self._started:
            self._started = True
            step.first = True
        return step

    def _join_spoken(self, sentences: list[str]) -> str:
        spoken = self._spoken
        for sentence in sentences:
            spoken = f"{spoken} {sentence}".strip() if spoken else sentence
        return spoken

    @staticmethod
    def _join(prefix: str, segment: str) -> str:
        if not prefix:
            return segment
        if not segment:
            return prefix
        return f"{prefix.rstrip()}\n\n{segment.lstrip()}"


_WHITESPACE = re.compile(r"\s+")


def _normalize(value: str) -> str:
    return _WHITESPACE.sub(" ", (value or "").strip()).casefold()


def _appended_prefix_length(final: str, streamed: str) -> Optional[int]:
    """How much of *final* the robot has already rendered, or None.

    The authoritative answer arrives stripped while the streamed text keeps
    the trailing whitespace the frames carried — delta whitespace is
    load-bearing on the robot, which concatenates the frames verbatim. A
    plain ``startswith`` would call that identical answer a divergence and
    trigger a needless full replace, so trailing whitespace on the streamed
    side is tolerated. Anything else really is a divergence.
    """
    if not streamed:
        return 0
    if final.startswith(streamed):
        return len(streamed)
    trimmed = streamed.rstrip()
    if trimmed and final.startswith(trimmed):
        return len(trimmed)
    return None


def _tail_after_spoken(final: str, spoken: str) -> str:
    """Return the part of *final* that has not been spoken yet.

    Compared on normalized whitespace, because the spoken text is the
    concatenation of trimmed sentences and rarely matches the final answer's
    spacing byte for byte.
    """
    target = _normalize(spoken)
    if not target:
        return final.strip()
    consumed = 0
    seen = ""
    for index, char in enumerate(final):
        seen = _normalize(final[: index + 1])
        if seen == target:
            consumed = index + 1
            break
    if not consumed:
        return ""
    return final[consumed:].strip()


__all__ = [
    "MAX_SENTENCE_CHARS",
    "MIN_DELTA_CHARS",
    "MIN_SENTENCE_CHARS",
    "ResponseStream",
    "SentenceSegmenter",
    "StreamStep",
]
