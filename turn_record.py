"""The turn record — the shared unit of conversation history.

Three producers can start a turn on the same robot:

* **Phoenix** answers it with one of the robot's own stock skills (weather,
  news, chitchat, the clock).  Hermes is not involved at all.
* **Phoenix escalates** it: the answer-skill or the parser fallback asks
  Hermes for the answer and speaks the result itself.
* **Hermes** originates it — a bridge chat turn, the dashboard, another
  platform, a proactive nudge.

All three must land in one history, render on the robot's screen, and be
addressable from the dashboard.  This module is that contract: one validated,
bounded record shape plus the three projections it is used through —

``to_dict``            the wire/dashboard form,
``to_bridge_params``   the trimmed form the robot's chat UI renders,
``to_transcript_rows`` the two rows written into Hermes' canonical session
                       transcript, which is authoritative per decision D7.

Nothing here talks to a socket or a database; it is pure so that both the
ingest server and the adapter can be tested against a fake peer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import re
from typing import Any, Optional
from urllib.parse import urlsplit
import uuid

#: Bumped only for an incompatible change to the record shape.  The bridge
#: and the ingest endpoint both reject an unknown version rather than
#: guessing at a field they do not understand.
TURN_RECORD_VERSION = 1

# Where a turn came from.  ``origin`` is decided by whoever *started* the
# turn and never rewritten afterwards; an escalation is still a Phoenix turn.
ORIGIN_PHOENIX = "phoenix"
ORIGIN_PHOENIX_ESCALATED = "phoenix_escalated"
ORIGIN_HERMES = "hermes"
ORIGIN_BRIDGE = "bridge"
ORIGINS = frozenset({ORIGIN_PHOENIX, ORIGIN_PHOENIX_ESCALATED, ORIGIN_HERMES, ORIGIN_BRIDGE})

#: Origins whose answer was produced by the Hermes agent.  Used to decide
#: whether a record is a *report* of work already done or the completion of
#: work this process performed.
HERMES_ANSWERED = frozenset({ORIGIN_PHOENIX_ESCALATED, ORIGIN_HERMES, ORIGIN_BRIDGE})

STATUS_OK = "ok"
STATUS_NO_MATCH = "no_match"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUSES = frozenset({STATUS_OK, STATUS_NO_MATCH, STATUS_FAILED, STATUS_CANCELLED})

# Who put the answer through a speaker.  A turn record is a history entry,
# not a command, so this is a statement of fact rather than an instruction:
# it stops the robot speaking an answer Phoenix already spoke.  It is never
# inferred — a producer that knows it spoke the answer says so, and silence
# means "unstated" rather than "nobody".
SPEECH_PHOENIX = "phoenix"
SPEECH_HERMES = "hermes"
SPEECH_NONE = "none"
SPEECH_OWNERS = frozenset({SPEECH_PHOENIX, SPEECH_HERMES, SPEECH_NONE})

MEDIA_KINDS = frozenset({"image", "video", "audio", "card", "link"})

# How a robot can be handed a picture.  The archived asset loader accepts an
# absolute filesystem path, ``file://`` and ``core://`` — and nothing else.
# An ``http(s)`` URL fails outright in the loader, so the Be fork has to fetch
# it to a temporary file first, which is a network round trip inside the
# render path.  A relative path is worse than useless: it resolves against
# whichever skill happens to be open rather than against the caller.
#
# So a record may carry up to three handles on the same picture, and the
# bridge projection picks the cheapest one the robot can actually use.
MEDIA_SOURCE_KEYS = ("path", "data", "url")

# Storage bounds.  Generous enough for a real answer, small enough that a
# misbehaving producer cannot push an unbounded blob into the session DB.
MAX_TRANSCRIPT_CHARS = 2000
MAX_RESPONSE_CHARS = 8000
MAX_ESML_CHARS = 8000
MAX_MEDIA_ITEMS = 8
MAX_URL_CHARS = 2048
MAX_PATH_CHARS = 1024
MAX_DIMENSION = 8192

#: Hard ceiling on one inline ``data:`` URI, independent of the frame budget
#: computed per turn.  Roughly 12 KB of image once base64 is undone.
MAX_INLINE_DATA_CHARS = 16384
MAX_ID_CHARS = 128
MAX_LABEL_CHARS = 120
MAX_CORRELATION_KEYS = 12

# Bridge bounds.  ``HERMES_MAX_TEXT_LENGTH`` on the Be side is 4096, and Be
# rejects *any* inbound frame over 24 KB — ``turn.append`` included — before
# it is parsed.  That last one is the reason the media projection below is
# size-aware rather than merely capped: an oversized picture must never cost
# the turn its text.
BRIDGE_TEXT_CHARS = 3800
BRIDGE_FRAME_BYTES = 24576
#: Slack left for the envelope Hermes wraps these params in, and for any
#: re-encoding difference between this serializer and Be's parser.
BRIDGE_FRAME_HEADROOM = 2048

#: One image per turn is what the transcript actually renders: the bubble is
#: at most 760 px wide in a 1184x520 viewport with a 260 px image cap, so a
#: second is tolerable and a fourth cannot be seen at all.  Entries beyond
#: this are reported as ``media_omitted`` rather than sent to be discarded.
BRIDGE_MEDIA_ITEMS = 1

#: Anything larger is downscaled on arrival and only burns texture memory on
#: a 2016 GPU with an 8 MB per-screen budget.  Producers are expected to send
#: a thumbnail; this is the size to aim at.
BRIDGE_THUMBNAIL_MAX_WIDTH = 760
BRIDGE_THUMBNAIL_MAX_HEIGHT = 520

#: Largest accepted ingest body.  Anything bigger is a bug or an attack.
MAX_INGEST_BYTES = 64 * 1024

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_DATA_URI_RE = re.compile(r"data:[A-Za-z0-9.+/-]+;base64,[A-Za-z0-9+/=\s]+")
_TURN_ID_PREFIXES = {
    ORIGIN_PHOENIX: "ph",
    ORIGIN_PHOENIX_ESCALATED: "ph",
    ORIGIN_HERMES: "hm",
    ORIGIN_BRIDGE: "br",
}


class TurnRecordError(ValueError):
    """A record could not be built from the supplied data."""

    def __init__(self, message: str, *, field: str = ""):
        super().__init__(message)
        self.field = field


def new_turn_id(origin: str = ORIGIN_HERMES) -> str:
    """Mint a turn id.

    The id is minted once, by whoever starts the turn, and travels unchanged
    through escalation and reporting.  The prefix is a debugging affordance
    only — correlation is by exact string.
    """
    return f"{_TURN_ID_PREFIXES.get(origin, 'tr')}-{uuid.uuid4().hex}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, limit: int, *, field_name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TurnRecordError(f"{field_name} must be a string", field=field_name)
    cleaned = value.replace("\x00", "").strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned


def _identifier(value: Any, *, field_name: str, required: bool = True) -> str:
    raw = "" if value is None else str(value).strip()
    if not raw:
        if required:
            raise TurnRecordError(f"{field_name} is required", field=field_name)
        return ""
    if len(raw) > MAX_ID_CHARS or not _ID_RE.fullmatch(raw):
        raise TurnRecordError(
            f"{field_name} must be a short identifier of letters, numbers, '.', ':', '_' or '-'",
            field=field_name,
        )
    return raw


def _choice(value: Any, allowed: frozenset[str], default: str, *, field_name: str) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    if not raw:
        return default
    if raw not in allowed:
        raise TurnRecordError(
            f"{field_name} must be one of {', '.join(sorted(allowed))}", field=field_name
        )
    return raw


def _timestamp(value: Any) -> str:
    if value is None or value == "":
        return _now_iso()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    raw = str(value).strip()
    if len(raw) > 64:
        raise TurnRecordError("timestamps must be ISO-8601 strings", field="started_at")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TurnRecordError("timestamps must be ISO-8601 strings", field="started_at") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _http_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > MAX_URL_CHARS:
        raise TurnRecordError("media urls must be bounded HTTP(S) urls", field="media")
    if any(ord(char) < 0x20 or char in "<>\"'" for char in raw):
        raise TurnRecordError("media urls must not contain control characters", field="media")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise TurnRecordError("media urls must be bounded HTTP(S) urls", field="media")
    if parsed.username is not None or parsed.password is not None:
        # Credentials in the authority would be forwarded to the robot.
        raise TurnRecordError("media urls must not carry credentials", field="media")
    return raw


def _dimension(value: Any) -> Optional[int]:
    """Intrinsic pixel size, so the row reserves the right box up front.

    Without this the transcript draws a fixed placeholder and then jumps
    under the reader when the real texture lands.
    """
    if value is None or value == "":
        return None
    try:
        pixels = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    if pixels <= 0:
        return None
    return min(pixels, MAX_DIMENSION)


def _robot_path(value: Any) -> str:
    """A handle the robot's asset loader can open directly."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) > MAX_PATH_CHARS or any(ord(char) < 0x20 for char in raw):
        raise TurnRecordError("media paths must be short and printable", field="media.path")
    lowered = raw.lower()
    if raw.startswith("/") or lowered.startswith("file://") or lowered.startswith("core://"):
        return raw
    raise TurnRecordError(
        "media paths must be absolute, file:// or core://; a relative path "
        "resolves against whichever skill is open on the robot rather than "
        "against the caller",
        field="media.path",
    )


def _inline_data(value: Any) -> str:
    """A base64 ``data:`` URI, which spares the robot a fetch mid-render."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) > MAX_INLINE_DATA_CHARS:
        raise TurnRecordError(
            f"inline media must be at most {MAX_INLINE_DATA_CHARS} characters; "
            "the robot rejects any bridge frame over 24 KB",
            field="media.data",
        )
    if not _DATA_URI_RE.fullmatch(raw):
        raise TurnRecordError(
            "inline media must be a base64 data: URI", field="media.data"
        )
    return raw


def _media(value: Any) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping) or not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TurnRecordError("media must be a list", field="media")
    items: list[dict[str, Any]] = []
    for entry in value[:MAX_MEDIA_ITEMS]:
        if not isinstance(entry, Mapping):
            raise TurnRecordError("each media entry must be an object", field="media")
        item: dict[str, Any] = {
            "kind": _choice(entry.get("kind"), MEDIA_KINDS, "image", field_name="media.kind"),
            "alt": _text(entry.get("alt"), MAX_LABEL_CHARS, field_name="media.alt"),
        }
        if entry.get("url"):
            item["url"] = _http_url(entry.get("url"))
        path = _robot_path(entry.get("path"))
        if path:
            item["path"] = path
        data = _inline_data(entry.get("data"))
        if data:
            item["data"] = data
        if not any(item.get(key) for key in MEDIA_SOURCE_KEYS):
            raise TurnRecordError(
                "each media entry needs a source: an absolute path, a base64 "
                "data: URI, or an HTTP(S) url",
                field="media",
            )
        width = _dimension(entry.get("width"))
        height = _dimension(entry.get("height"))
        if width is not None:
            item["width"] = width
        if height is not None:
            item["height"] = height
        items.append(item)
    return tuple(items)


def _encoded_size(value: Any) -> int:
    """Bytes this object costs inside a bridge frame."""
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def exceeds_thumbnail(entry: Mapping[str, Any]) -> bool:
    """Whether a media entry is larger than the screen can use.

    Reported rather than enforced: the record is a contract, not an image
    pipeline, and a producer that cannot resize should still be able to send
    what it has.
    """
    width = entry.get("width") or 0
    height = entry.get("height") or 0
    return width > BRIDGE_THUMBNAIL_MAX_WIDTH or height > BRIDGE_THUMBNAIL_MAX_HEIGHT


def _timing(value: Any) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TurnRecordError("timing must be an object of millisecond integers", field="timing")
    result: dict[str, int] = {}
    for key, raw in list(value.items())[:MAX_CORRELATION_KEYS]:
        name = str(key).strip()[:32]
        if not name:
            continue
        try:
            millis = int(round(float(raw)))
        except (TypeError, ValueError):
            continue
        result[name] = max(0, min(millis, 24 * 60 * 60 * 1000))
    return result


def _correlation(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TurnRecordError("correlation must be an object of strings", field="correlation")
    result: dict[str, str] = {}
    for key, raw in list(value.items())[:MAX_CORRELATION_KEYS]:
        name = str(key).strip()[:48]
        if not name:
            continue
        result[name] = str(raw or "").strip()[:MAX_ID_CHARS]
    return result


def _best_media_source(
    entry: Mapping[str, Any], *, budget: int
) -> Optional[tuple[str, str]]:
    """Pick the cheapest handle on a picture that this robot can open.

    Ordered by what it costs the robot, not by what is convenient here:

    ``path``  an absolute path the loader opens directly — no fetch, no bytes
              on the wire;
    ``data``  inline base64 — no fetch, but it has to fit the frame;
    ``url``   accepted, but it is a network round trip *inside* the render
              path, with a 4 MB cap and an 8 s timeout on the robot.

    Returns ``None`` when nothing usable fits, so the caller can report the
    entry as omitted instead of sending a source the robot cannot resolve.
    """
    path = entry.get("path")
    if path:
        return ("path", str(path))
    data = entry.get("data")
    if data and _encoded_size(str(data)) <= max(0, budget):
        return ("data", str(data))
    url = entry.get("url")
    if url:
        return ("url", str(url))
    return None


@dataclass(frozen=True)
class TurnRecord:
    """One completed exchange on one robot, whoever answered it."""

    turn_id: str
    device_id: str
    origin: str = ORIGIN_HERMES
    transcript: str = ""
    response: str = ""
    intent: str = ""
    skill: str = ""
    status: str = STATUS_OK
    speech_owner: str = SPEECH_NONE
    response_esml: str = ""
    confidence: Optional[float] = None
    media: tuple[dict[str, Any], ...] = ()
    timing: dict[str, int] = field(default_factory=dict)
    correlation: dict[str, str] = field(default_factory=dict)
    started_at: str = field(default_factory=_now_iso)
    recorded_at: str = field(default_factory=_now_iso)

    # ── construction ──────────────────────────────────────────────────
    @classmethod
    def from_mapping(cls, value: Any, *, default_device_id: str = "") -> "TurnRecord":
        """Validate one inbound record.  Raises :class:`TurnRecordError`."""
        if not isinstance(value, Mapping):
            raise TurnRecordError("a turn record must be a JSON object")
        version = value.get("v", TURN_RECORD_VERSION)
        try:
            version = int(version)
        except (TypeError, ValueError) as exc:
            raise TurnRecordError("v must be an integer", field="v") from exc
        if version != TURN_RECORD_VERSION:
            raise TurnRecordError(
                f"unsupported turn record version {version}; this host speaks v{TURN_RECORD_VERSION}",
                field="v",
            )
        origin = _choice(value.get("origin"), ORIGINS, ORIGIN_PHOENIX, field_name="origin")
        device_id = _identifier(
            value.get("device_id") or value.get("robot_id") or default_device_id,
            field_name="device_id",
        )
        turn_id = _identifier(value.get("turn_id") or new_turn_id(origin), field_name="turn_id")
        confidence: Optional[float] = None
        raw_confidence = value.get("confidence", value.get("transcript_confidence"))
        if raw_confidence is not None:
            try:
                confidence = max(0.0, min(1.0, float(raw_confidence)))
            except (TypeError, ValueError):
                confidence = None
        record = cls(
            turn_id=turn_id,
            device_id=device_id,
            origin=origin,
            transcript=_text(value.get("transcript"), MAX_TRANSCRIPT_CHARS, field_name="transcript"),
            response=_text(value.get("response"), MAX_RESPONSE_CHARS, field_name="response"),
            intent=_text(value.get("intent"), MAX_LABEL_CHARS, field_name="intent"),
            skill=_text(value.get("skill"), MAX_LABEL_CHARS, field_name="skill"),
            status=_choice(value.get("status"), STATUSES, STATUS_OK, field_name="status"),
            # Never inferred from the origin.  An unstated owner stays
            # unstated, so a later report of the same turn cannot silently
            # overwrite what the actual answerer recorded.
            speech_owner=_choice(
                value.get("speech_owner"), SPEECH_OWNERS, SPEECH_NONE, field_name="speech_owner"
            ),
            response_esml=_text(value.get("response_esml"), MAX_ESML_CHARS, field_name="response_esml"),
            confidence=confidence,
            media=_media(value.get("media")),
            timing=_timing(value.get("timing")),
            correlation=_correlation(value.get("correlation")),
            started_at=_timestamp(value.get("started_at")),
            recorded_at=_now_iso(),
        )
        if not record.transcript and not record.response:
            raise TurnRecordError("a turn record needs a transcript, a response, or both")
        return record

    # ── projections ───────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        """The canonical wire/dashboard form."""
        data: dict[str, Any] = {
            "v": TURN_RECORD_VERSION,
            "turn_id": self.turn_id,
            "device_id": self.device_id,
            "origin": self.origin,
            "transcript": self.transcript,
            "response": self.response,
            "intent": self.intent,
            "skill": self.skill,
            "status": self.status,
            "speech_owner": self.speech_owner,
            "started_at": self.started_at,
            "recorded_at": self.recorded_at,
        }
        if self.response_esml:
            data["response_esml"] = self.response_esml
        if self.confidence is not None:
            data["confidence"] = self.confidence
        if self.media:
            data["media"] = [dict(item) for item in self.media]
        if self.timing:
            data["timing"] = dict(self.timing)
        if self.correlation:
            data["correlation"] = dict(self.correlation)
        return data

    def to_bridge_params(
        self,
        *,
        session_id: str = "",
        max_media: int = BRIDGE_MEDIA_ITEMS,
        frame_budget: int = BRIDGE_FRAME_BYTES,
    ) -> dict[str, Any]:
        """The trimmed form the robot's chat UI renders.

        Be caps a single text field at 4096 characters and rejects any
        inbound frame over 24 KB before parsing it, so this drops the ESML,
        the timing breakdown and the correlation block — none of which the
        screen shows — and trims the rest well inside both limits.

        Media is fitted to whatever budget the text leaves, because the
        alternative is worse than a missing picture: one oversized
        attachment would push the frame past Be's limit and lose the whole
        turn, text and all.  Anything that does not fit is reported in
        ``media_omitted`` rather than silently dropped.
        """
        params: dict[str, Any] = {
            "v": TURN_RECORD_VERSION,
            "turn_id": self.turn_id,
            "device_id": self.device_id,
            "origin": self.origin,
            "status": self.status,
            "transcript": self.transcript[:BRIDGE_TEXT_CHARS],
            "response": self.response[:BRIDGE_TEXT_CHARS],
            "started_at": self.started_at,
        }
        if self.skill:
            params["skill"] = self.skill
        if self.intent:
            params["intent"] = self.intent
        if session_id:
            params["session_id"] = session_id
        media, omitted = self._bridge_media(
            params, max_media=max_media, frame_budget=frame_budget
        )
        if media:
            params["media"] = media
        if omitted:
            params["media_omitted"] = omitted
        return params

    def _bridge_media(
        self, params: Mapping[str, Any], *, max_media: int, frame_budget: int
    ) -> tuple[list[dict[str, Any]], int]:
        if not self.media:
            return [], 0
        allowed = max(0, int(max_media))
        chosen = self.media[:allowed]
        omitted = len(self.media) - len(chosen)
        # Reserve room for the params already built, the envelope Hermes
        # wraps them in, and the ``media``/``media_omitted`` keys themselves.
        budget = frame_budget - BRIDGE_FRAME_HEADROOM - _encoded_size(params)
        rendered: list[dict[str, Any]] = []
        for entry in chosen:
            item: dict[str, Any] = {"kind": entry["kind"]}
            for key in ("alt", "width", "height"):
                if entry.get(key):
                    item[key] = entry[key]
            source = _best_media_source(entry, budget=budget - _encoded_size(item))
            if source is None:
                omitted += 1
                continue
            item[source[0]] = source[1]
            cost = _encoded_size(item)
            if cost > budget:
                omitted += 1
                continue
            budget -= cost
            rendered.append(item)
        return rendered, omitted

    def to_transcript_rows(self, *, attribute_skill: bool = False) -> list[dict[str, Any]]:
        """The rows written into Hermes' canonical session transcript.

        Both a user row and an assistant row are written, even for a turn
        Hermes never saw.  Two reasons:

        * the on-screen and dashboard history would otherwise have holes
          exactly where the robot worked best, and
        * Hermes replays the transcript with ``repair_alternation=True``; a
          user row with no answer is a wedge that the repair has to heal on
          every subsequent request.

        ``platform_message_id`` makes both rows idempotent, so a retried
        report cannot double-write.  ``observed`` marks them as context the
        agent did not itself produce.
        """
        rows: list[dict[str, Any]] = []
        metadata = self.to_dict()
        if self.transcript:
            rows.append(
                {
                    "role": "user",
                    "content": self.transcript,
                    "timestamp": self.started_at,
                    "observed": True,
                    "platform_message_id": f"jibo-turn:{self.turn_id}:user",
                    "display_kind": "jibo_turn",
                    "display_metadata": metadata,
                }
            )
        if self.response:
            content = self.response
            if attribute_skill and self.origin == ORIGIN_PHOENIX and self.skill:
                content = f"{content}\n\n[answered on-robot by {self.skill}]"
            rows.append(
                {
                    "role": "assistant",
                    "content": content,
                    "timestamp": self.recorded_at,
                    "observed": self.origin not in HERMES_ANSWERED,
                    "platform_message_id": f"jibo-turn:{self.turn_id}:assistant",
                    "display_kind": "jibo_turn",
                    "display_metadata": metadata,
                }
            )
        return rows

    # ── mutation ──────────────────────────────────────────────────────
    def completed_with(self, **changes: Any) -> "TurnRecord":
        """Return a copy carrying a later stage of the same turn.

        Escalation produces the record twice: once when Hermes answers, and
        again when Phoenix reports the finished turn with its skill id and
        timings.  The second report completes the first rather than becoming
        a second turn, because ``turn_id`` is stable across both.
        """
        allowed = {key: value for key, value in changes.items() if key in self.__dataclass_fields__}
        allowed.setdefault("recorded_at", _now_iso())
        return replace(self, **allowed)


def merge_records(existing: TurnRecord, incoming: TurnRecord) -> TurnRecord:
    """Fold a later report of the same turn into the record already held.

    Non-empty incoming fields win; empty ones never erase what is known.  The
    origin is upgraded from ``phoenix`` to ``phoenix_escalated`` when the
    later report says Hermes answered, and never downgraded.
    """
    if existing.turn_id != incoming.turn_id:
        raise TurnRecordError("cannot merge records with different turn ids", field="turn_id")
    origin = incoming.origin
    if existing.origin == ORIGIN_PHOENIX_ESCALATED and incoming.origin == ORIGIN_PHOENIX:
        origin = ORIGIN_PHOENIX_ESCALATED
    timing = dict(existing.timing)
    timing.update(incoming.timing)
    correlation = dict(existing.correlation)
    correlation.update({key: value for key, value in incoming.correlation.items() if value})
    return existing.completed_with(
        origin=origin,
        transcript=incoming.transcript or existing.transcript,
        response=incoming.response or existing.response,
        intent=incoming.intent or existing.intent,
        skill=incoming.skill or existing.skill,
        status=incoming.status if incoming.status != STATUS_OK else existing.status,
        speech_owner=(
            incoming.speech_owner if incoming.speech_owner != SPEECH_NONE else existing.speech_owner
        ),
        response_esml=incoming.response_esml or existing.response_esml,
        confidence=existing.confidence if incoming.confidence is None else incoming.confidence,
        media=incoming.media or existing.media,
        timing=timing,
        correlation=correlation,
    )


__all__ = [
    "TURN_RECORD_VERSION",
    "BRIDGE_FRAME_BYTES",
    "BRIDGE_MEDIA_ITEMS",
    "BRIDGE_THUMBNAIL_MAX_HEIGHT",
    "BRIDGE_THUMBNAIL_MAX_WIDTH",
    "MAX_INGEST_BYTES",
    "MAX_INLINE_DATA_CHARS",
    "MEDIA_SOURCE_KEYS",
    "exceeds_thumbnail",
    "ORIGIN_BRIDGE",
    "ORIGIN_HERMES",
    "ORIGIN_PHOENIX",
    "ORIGIN_PHOENIX_ESCALATED",
    "ORIGINS",
    "HERMES_ANSWERED",
    "SPEECH_HERMES",
    "SPEECH_NONE",
    "SPEECH_PHOENIX",
    "STATUS_CANCELLED",
    "STATUS_FAILED",
    "STATUS_NO_MATCH",
    "STATUS_OK",
    "TurnRecord",
    "TurnRecordError",
    "merge_records",
    "new_turn_id",
]
