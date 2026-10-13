"""Hermes adapter for the native Jibo/Be bridge.

This is the first native slice of the platform.  It keeps the gateway-facing
shape of the ROM adapter, but delegates robot operations to a small Be-owned
WebSocket service.  The adapter owns device fan-out, reconnect state, Hermes
session routing, tool-friendly synchronous wrappers, and the bounded operator
log used by the dashboard.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextvars import ContextVar
import concurrent.futures
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import threading
import hashlib
import tempfile
import time
import urllib.request
import uuid
from typing import Any, Mapping, Optional
from urllib.parse import quote_plus, urlsplit

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    validate_media_delivery_path,
)
from gateway.session import build_session_key

from .adapter import (
    _JiboMediaHTTPServer,
    _register_active_adapter,
    _unregister_active_adapter,
    is_cancel_phrase,
    _pairing_code_from_message,
)
from .esml import MAX_TOTAL_CHARS, format_jibo_speech, prepare_for_jibo
from .media import JIBO_MAX_MEDIA_BYTES, letterbox_image
from .native_protocol import (
    NativeBridgeError,
    NativeDeviceConfig,
    NativeDisconnected,
    NativeJiboClient,
    NativeRemoteError,
    INPUT_ROUTE_HERMES_FALLBACK,
    INPUT_ROUTE_LEGACY_FALLBACK,
    INPUT_ROUTE_LOCAL,
    classify_input_route,
    configured_device_configs,
    parse_device_configs,
)
from . import device_status, model_settings
from .pronunciation import normalize_for_speech
from .turn_ingest import IngestConfigError, TurnIngestConfig, TurnIngestServer
from .turn_record import (
    BRIDGE_THUMBNAIL_MAX_HEIGHT,
    BRIDGE_THUMBNAIL_MAX_WIDTH,
    HERMES_ANSWERED,
    ORIGIN_BRIDGE,
    ORIGIN_HERMES,
    ORIGIN_PHOENIX_ESCALATED,
    SPEECH_HERMES,
    SPEECH_NONE,
    SPEECH_PHOENIX,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_OK,
    TurnRecord,
    exceeds_thumbnail,
    merge_records,
    new_turn_id,
)
from .turn_stream import ResponseStream

logger = logging.getLogger(__name__)
# Hermes copies context into agent workers and their delivery callbacks. Keep
# the originating turn there so delayed work cannot adopt a device's new turn.
_NATIVE_TURN: ContextVar[tuple[Any, str, str] | None] = ContextVar('jibo_native_turn', default=None)

NATIVE_MODE = "native"
LEGACY_MODE = "rom"
DEFAULT_LOG_LIMIT = 200
MAX_LOG_LIMIT = 500
_IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# Images are fetched on the host: the robot's 2019 certificate bundle cannot
# verify many current HTTPS endpoints. This is deliberately larger than the
# Be-side delivery cap because a large source is downsampled before delivery.
JIBO_IMAGE_USER_AGENT = "HermesJiboBridge/1.0 (+https://github.com/hermes; robot image display)"
JIBO_IMAGE_FETCH_TIMEOUT = 15.0
JIBO_IMAGE_FETCH_MAX_BYTES = 16 * 1024 * 1024

# Bridge methods that carry the shared conversation to the robot's chat UI.
TURN_APPEND_METHOD = "turn.append"
RESPONSE_DELTA_METHOD = "response.delta"
CHAT_STATUS_METHOD = "chat.status"
CHAT_HISTORY_METHOD = "chat.history"
CHAT_HISTORY_REQUEST_EVENT = "chat.history.request"
CHAT_MODELS_METHOD = "chat.models"
MODEL_REQUEST_EVENT = "model.request"
MODEL_SELECT_EVENT = "model.select"
HISTORY_SESSION_PAGE_SIZE = 7
HISTORY_MESSAGE_PAGE_SIZE = 6
HISTORY_MAX_OFFSET = 10_000
HISTORY_MAX_TEXT = 600

#: Turns kept per device for the dashboard and for folding a later report of
#: the same turn into the record already held.  History itself is Hermes'
#: (decision D7); this is a bounded projection, not the store.
DEFAULT_TURN_LOG_LIMIT = 100
MAX_TURN_LOG_LIMIT = 500


def native_mode(extra: Mapping[str, Any] | None) -> str:
    """Return the normalized transport mode, defaulting to native."""
    value = str((extra or {}).get("mode") or NATIVE_MODE).strip().lower()
    return LEGACY_MODE if value == LEGACY_MODE else NATIVE_MODE


def native_validate_config(config: PlatformConfig) -> bool:
    """Validate native configuration without making a network connection."""
    extra = config.extra or {}
    if native_mode(extra) == LEGACY_MODE:
        return bool(str(extra.get("host") or os.getenv("JIBO_HOST") or "").strip())
    return any(item.enabled and item.host for item in configured_device_configs(extra))


def native_is_connected(config: PlatformConfig) -> bool:
    """Report whether at least one enabled native device is configured."""
    return native_validate_config(config)


def _safe_view_name(value: Any, default: str) -> str:
    name = str(value or default).strip()
    return name if _IMAGE_NAME_RE.fullmatch(name) else default


def _raw_log_text(value: Any, limit: int = 2000) -> str:
    """Bound raw conversation text without applying the speech sanitizer."""
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _valid_native_image_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    url = value.strip()
    if len(url) > 2048:
        return None
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if any(ord(char) < 0x20 or char in "<>\"'" for char in url):
        return None
    return url


def _event_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "final"}


def _bounded_setting(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


# A gateway status callback is a progress notice, not an answer.  On a chat
# platform it is an extra bubble; on a robot it is spoken, and a provider
# failure would be narrated twice — once here and again as the turn's own
# final reply.  These shapes are collapsed to a short on-screen label instead.
STATUS_LABEL_MAX = 48
_STATUS_RATE_LIMIT_RE = re.compile(
    r"rate[- ]limit|usage limit|quota|too many requests", re.IGNORECASE
)
_STATUS_PROVIDER_ERROR_RE = re.compile(
    r"provider (?:authentication )?(?:failed|rejected)"
    r"|failed after retries"
    r"|not responding"
    r"|unreachable",
    re.IGNORECASE,
)


# Speech is the one bridge call whose failure says nothing about whether the
# room heard it: Be answers ``speech.speak`` when Jibo stops talking, so a long
# answer can outlast a request deadline having been delivered perfectly.
# Reporting that as retryable made the gateway re-send it, and Jibo said the
# same answer once per retry before the delivery-failure notice arrived.
def _speech_retryable(exc: Exception, *, default: bool = True) -> bool:
    """Whether a failed speak may be re-sent without risking a repeat."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return False
    if "TIMEOUT" in str(getattr(exc, "code", "") or "").upper():
        return False
    if "timed out" in str(exc).lower():
        return False
    return bool(getattr(exc, "retryable", default))


def _status_screen_label(value: Any) -> str:
    """Reduce a gateway status notice to a few words for the chat status line."""
    text = _raw_log_text(value, 200).strip()
    if not text:
        return ""
    if _STATUS_RATE_LIMIT_RE.search(text):
        return "provider rate limit"
    if _STATUS_PROVIDER_ERROR_RE.search(text):
        return "provider error"
    clause = re.split(r"[.;:\n]", re.sub(r"^[^\w]+", "", text), maxsplit=1)[0]
    return clause.strip()[:STATUS_LABEL_MAX].strip()


def _same_model_override(left: Any, right: Any) -> bool:
    """Whether two session model overrides mean the same thing."""
    def norm(value: Any) -> tuple:
        if not isinstance(value, Mapping):
            return ()
        return (
            str(value.get("model") or "").strip(),
            str(value.get("provider") or "").strip(),
        )
    return norm(left) == norm(right)


def _input_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


@dataclass(frozen=True)
class _DeliveredAnswer:
    turn_id: str
    text: str
    # Only an unsolicited, unowned delivery uses a time limit. An active
    # agent turn can take arbitrarily long to finish its post-tool bookkeeping.
    unowned_deadline: float


@dataclass
class _Escalation:
    """One Phoenix turn currently being answered by the Hermes agent.

    Phoenix asks over HTTP and blocks its own skill on the answer, so the
    adapter has to hand the completed text back to that waiting request
    rather than only speaking it.  ``speech_owner`` decides which: when
    Phoenix will speak the answer through its own skill action, Hermes must
    not also say it.
    """

    turn_id: str
    device_id: str
    text: str
    speech_owner: str
    intent: str
    skill: str
    correlation: dict[str, str]
    attribute_skill: bool
    future: "asyncio.Future[dict[str, Any]]"
    on_delta: Optional[Any] = None
    started: float = field(default_factory=time.time)
    spoken: bool = False

    def resolve(self, text: str, *, status: str = STATUS_OK) -> None:
        if not self.future.done():
            self.future.set_result(
                {
                    "turn_id": self.turn_id,
                    "text": text,
                    "spoken": self.spoken,
                    "status": status,
                }
            )


class NativeJiboAdapter(BasePlatformAdapter):
    """Multi-device Hermes adapter backed by native Be WebSockets."""

    # The native transcript owns one stream for the whole task. At a tool
    # boundary Hermes must keep sending drafts; send() completes the turn.
    draft_stream_is_message = True

    MAX_MESSAGE_LENGTH = MAX_TOTAL_CHARS
    splits_long_messages = False
    supports_code_blocks = False
    supports_async_delivery = False
    interactive_resume = False

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("jibo"))
        extra = config.extra or {}
        self._device_configs: list[NativeDeviceConfig] = parse_device_configs(extra)
        self._clients: dict[str, NativeJiboClient] = {}
        self._device_states: dict[str, dict[str, Any]] = {}
        self._logs: dict[str, deque[dict[str, Any]]] = {
            item.id: deque(maxlen=self._log_limit(extra)) for item in self._device_configs
        }
        self._session_devices: dict[str, str] = {}
        self._active_turns: dict[str, dict[str, Any]] = {}
        #: Count consecutive questions only for an explicitly configured cap.
        #: Each question opens one bounded capture, with no silent rearming.
        self._follow_up_depth: dict[str, int] = {}
        self._seen_input_ids: dict[str, deque[str]] = {
            item.id: deque(maxlen=256) for item in self._device_configs
        }
        self._recent_local_inputs: dict[str, deque[tuple[str, float, str]]] = {
            item.id: deque(maxlen=64) for item in self._device_configs
        }
        self._processing_stages: dict[str, str] = {}
        self._delivered_answers: dict[str, _DeliveredAnswer] = {}
        self._speak_locks: dict[str, asyncio.Lock] = {}
        self._last_device_id = ""
        self._closing = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._media_lock = threading.RLock()
        self._media_server: Optional[_JiboMediaHTTPServer] = None
        self._media_server_thread: Optional[threading.Thread] = None
        self._media_cache_dir: Optional[Path] = None
        self._active_source: Optional[Any] = None
        self._active_session_key = ""
        self._active_device_for_turn = ""
        self._audio_captures: dict[str, AudioCapture] = {}
        # ── the integration spine ────────────────────────────────────
        turn_limit = self._turn_log_limit(extra)
        self._turn_log: dict[str, deque[TurnRecord]] = {
            item.id: deque(maxlen=turn_limit) for item in self._device_configs
        }
        self._turn_index: dict[str, dict[str, TurnRecord]] = {
            item.id: {} for item in self._device_configs
        }
        self._pending_escalations: dict[str, _Escalation] = {}
        self._streams: dict[str, ResponseStream] = {}
        # Be rejects a repeated or lower ``seq`` for a turn, so the counter is
        # monotonic per device and survives a stream rebuilt mid-answer.
        self._delta_seq: dict[str, int] = {}
        self._turn_ids: dict[str, str] = {}
        try:
            self._ingest_config = TurnIngestConfig.from_extra(extra)
        except IngestConfigError as exc:
            logger.error("Jibo turn ingest is misconfigured and stays off: %s", exc)
            self._ingest_config = TurnIngestConfig()
        self._ingest_server: Optional[TurnIngestServer] = None
        _register_active_adapter(self)

    @staticmethod
    def _log_limit(extra: Mapping[str, Any]) -> int:
        try:
            return max(20, min(int(extra.get("log_limit", DEFAULT_LOG_LIMIT)), MAX_LOG_LIMIT))
        except (TypeError, ValueError):
            return DEFAULT_LOG_LIMIT

    @staticmethod
    def _turn_log_limit(extra: Mapping[str, Any]) -> int:
        try:
            return max(10, min(int(extra.get("turn_log_limit", DEFAULT_TURN_LOG_LIMIT)), MAX_TURN_LOG_LIMIT))
        except (TypeError, ValueError):
            return DEFAULT_TURN_LOG_LIMIT

    @property
    def connected(self) -> bool:
        return any(client.connected for client in self._clients.values())

    @property
    def device_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self._device_configs)

    def has_device(self, device_id: str) -> bool:
        return str(device_id or "").strip() in self._clients or any(
            item.id == str(device_id or "").strip() for item in self._device_configs
        )

    def device_config(self, device_id: str) -> Optional[NativeDeviceConfig]:
        """The configured settings for one robot, or None if it is unknown."""
        wanted = str(device_id or "").strip()
        for item in self._device_configs:
            if item.id == wanted:
                return item
        return None

    def device_model_override(self, device_id: str) -> Optional[dict[str, Any]]:
        """This robot's model as a session override, or None for the default.

        Read from the config rather than from the copy parsed at startup: the
        dashboard is a different process and may have changed it since.
        """
        chosen = model_settings.current_device_model(device_id)
        if not chosen.get("model"):
            return None
        override: dict[str, Any] = {"model": chosen["model"]}
        if chosen.get("provider"):
            override["provider"] = chosen["provider"]
        return override

    def _apply_device_model(self, device_id: str, session_key: str) -> None:
        """Point this robot's session at the model configured for the robot.

        The setting lives on the device rather than on the session so that the
        dashboard and the robot's own screen read and write one value.  What
        makes a turn actually use it is the session's persisted override, so
        that is written here; the in-memory copy is then dropped so the
        gateway re-reads the override -- and re-resolves its credentials --
        on the next turn instead of carrying an older choice.
        """
        if not session_key:
            return
        store = getattr(self, "_session_store", None)
        setter = getattr(store, "set_model_override", None)
        getter = getattr(store, "get_model_override", None)
        if not callable(setter) or not callable(getter):
            return
        desired = self.device_model_override(device_id)
        try:
            current = getter(session_key)
        except Exception:
            logger.debug("Could not read the Jibo session model override", exc_info=True)
            return
        if _same_model_override(current, desired):
            return
        try:
            setter(session_key, desired)
        except Exception:
            logger.warning("Could not set the Jibo session model", exc_info=True)
            return
        logger.info(
            "Jibo %s now answers on %s",
            device_id,
            (desired or {}).get("model") or "the Hermes default model",
        )
        runner = getattr(self, "gateway_runner", None)
        peek = getattr(runner, "_peek_session_state", None)
        if not callable(peek):
            return
        try:
            state = peek(session_key)
            conversation = getattr(state, "conversation", None)
            if conversation is not None:
                conversation.model_override = None
        except Exception:
            logger.debug("Could not refresh the live Jibo model override", exc_info=True)

    def resolve_device_id(
        self,
        device_id: str | None = None,
        *,
        chat_id: str | None = None,
        session_id: str | None = None,
        require_connected: bool = False,
    ) -> str:
        """Resolve an explicit id, a Jibo chat id, or a safe active default."""
        candidate = str(device_id or "").strip()
        if not candidate and chat_id:
            chat_value = str(chat_id).strip()
            if chat_value.startswith("jibo:"):
                candidate = chat_value.split(":", 1)[1].split(":", 1)[0]
        if not candidate and session_id:
            candidate = self._session_devices.get(str(session_id).strip(), "")
        if candidate and not self.has_device(candidate):
            raise NativeBridgeError(
                f"unknown Jibo device: {candidate}", code="DEVICE_NOT_FOUND", retryable=False
            )
        if candidate:
            if require_connected and not self._clients.get(candidate, None):
                raise NativeDisconnected(f"Jibo device {candidate} is not connected")
            return candidate

        connected = [
            item.id
            for item in self._device_configs
            if item.enabled and self._clients.get(item.id) is not None and self._clients[item.id].connected
        ]
        if self._last_device_id in connected:
            return self._last_device_id
        if connected:
            return connected[0]
        enabled = [item.id for item in self._device_configs if item.enabled]
        if enabled:
            return enabled[0]
        raise NativeDisconnected("no enabled Jibo native device is configured")

    def _config_for(self, device_id: str) -> NativeDeviceConfig:
        for item in self._device_configs:
            if item.id == device_id:
                return item
        raise NativeBridgeError(
            f"unknown Jibo device: {device_id}", code="DEVICE_NOT_FOUND", retryable=False
        )

    def _client_for(self, device_id: str, *, require_connected: bool = True) -> NativeJiboClient:
        client = self._clients.get(device_id)
        if client is None:
            raise NativeDisconnected(f"Jibo device {device_id} is not initialized")
        if require_connected and not client.connected:
            raise NativeDisconnected(f"Jibo device {device_id} is not connected")
        return client

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        del is_reconnect
        if self._closing:
            return False
        self._loop = asyncio.get_running_loop()
        for item in self._device_configs:
            if not item.enabled or item.id in self._clients:
                continue
            self._clients[item.id] = NativeJiboClient(
                item,
                on_event=lambda frame, device_id=item.id: self._on_native_event(device_id, frame),
                on_state=lambda state, device_id=item.id: self._on_client_state(device_id, state),
            )
            self._device_states[item.id] = self._clients[item.id].state
            self._publish_device_status()
            self._speak_locks[item.id] = asyncio.Lock()
        clients = [self._clients[item.id] for item in self._device_configs if item.enabled]
        if not clients:
            self._mark_disconnected()
            return False
        # Registration succeeds when reconnect loops are started, even when a
        # robot is asleep. Waiting for every peer here lets the gateway's
        # startup timeout remove the adapter and kill those reconnect loops.
        for client in clients:
            await client.start()
        if getattr(self, '_ingest_start_task', None) is None:
            self._ingest_start_task = asyncio.create_task(self._start_turn_ingest(), name='jibo-ingest-start')
            self._ingest_start_task.add_done_callback(self._log_background_future)
        if self.connected:
            self._mark_connected()
        else:
            self._mark_disconnected()
        logger.info('Native Jibo platform started %d device reconnect loop(s)', len(clients))
        return True

    async def _start_turn_ingest(self) -> bool:
        """Bring up the Phoenix-facing listener, if it is configured on."""
        if self._closing or self._ingest_server is not None or not self._ingest_config.enabled:
            return False
        server = TurnIngestServer(self, self._ingest_config)
        try:
            started = await server.start()
        except Exception:
            logger.warning("Jibo turn ingest failed to start", exc_info=True)
            return False
        if started:
            self._ingest_server = server
        return started

    async def _stop_turn_ingest(self) -> None:
        server = self._ingest_server
        self._ingest_server = None
        if server is not None:
            await server.stop()

    async def disconnect(self) -> None:
        self._closing = True
        # Publish the shutdown before tearing the clients down, so the panel
        # stops claiming a robot is reachable the moment this gateway stops.
        for device_id in list(self._device_states):
            self._device_states[device_id] = {"connected": False}
        self._publish_device_status()
        starting = getattr(self, '_ingest_start_task', None)
        self._ingest_start_task = None
        if starting is not None:
            if not starting.done():
                starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)
        await self._stop_turn_ingest()
        for escalation in list(self._pending_escalations.values()):
            if not escalation.future.done():
                escalation.future.set_exception(
                    NativeDisconnected("the Jibo platform is shutting down")
                )
        self._pending_escalations.clear()
        self._streams.clear()
        clients = list(self._clients.values())
        if clients:
            await asyncio.gather(*(client.stop() for client in clients), return_exceptions=True)
        self._clients.clear()
        self._speak_locks.clear()
        self._audio_captures.clear()
        await asyncio.to_thread(self._stop_media_server)
        self._loop = None
        self._active_source = None
        self._active_session_key = ""
        self._active_device_for_turn = ""
        _unregister_active_adapter(self)
        self._mark_disconnected()
        publisher = getattr(self, '_status_publisher', None)
        if publisher is not None:
            await publisher.flush()

    def _on_client_state(self, device_id: str, state: dict[str, Any]) -> None:
        self._device_states[device_id] = dict(state)
        self._append_log(
            device_id,
            direction="state",
            method="connection",
            payload={
                "connected": bool(state.get("connected")),
                "connecting": bool(state.get("connecting")),
                "error": state.get("last_error"),
            },
        )
        if state.get("connected"):
            self._mark_connected()
        else:
            # A half-streamed utterance or a half-delivered answer cannot be
            # completed after a drop.
            self._audio_captures.pop(device_id, None)
            self._streams.pop(device_id, None)
            if not self.connected:
                self._mark_disconnected()
        self._publish_device_status()

    def _publish_device_status(self) -> None:
        """Tell the other Hermes processes which robots are actually up.

        The dashboard runs in its own process and cannot see this adapter, so
        without this every robot reads as offline there while the bridge is
        working perfectly.
        """
        snapshot = {}
        for config in self._device_configs:
            client = self._clients.get(config.id)
            state = {'connected': False} if self._closing else client.state if client is not None else dict(
                self._device_states.get(config.id) or {}
            )
            snapshot[config.id] = state
        publisher = getattr(self, '_status_publisher', None)
        if publisher is None:
            publisher = self._status_publisher = device_status.StatusPublisher()
        publisher.update(snapshot)

    async def _on_native_event(self, device_id: str, frame: dict[str, Any]) -> None:
        method = str(frame.get("method") or "event").strip()
        params = frame.get("params") if isinstance(frame.get("params"), Mapping) else {}
        route = classify_input_route(frame) if method == "input.transcript" else None
        log_payload = {key: value for key, value in params.items() if key != "audio"}
        if route is not None:
            log_payload.setdefault("route", route)
        self._append_log(device_id, direction="event", method=method, payload=log_payload)
        if method in {"status.changed", "skill.changed", "session.updated"}:
            self._device_states.setdefault(device_id, {})[method.replace(".", "_")] = dict(params)
        if method == CHAT_HISTORY_REQUEST_EVENT:
            await self._deliver_history(device_id, params)
            return
        if method in {MODEL_REQUEST_EVENT, MODEL_SELECT_EVENT}:
            await self._handle_model_event(device_id, method, params)
            return
        if method == "web.panel.request":
            await self._handle_web_panel_request(device_id, params)
            return
        if method in {"input.tap", "screen.tap", "display.tap", "input.interrupt"}:
            # `input.interrupt` is the robot reporting a barge-in: "Jibo, stop"
            # or a touch on the head while he is answering. It carries a
            # `source`, but the response is the same for all of them -- the
            # robot has already stopped speaking, and what remains is to cancel
            # the agent request still running here.
            if method == "input.interrupt":
                logger.info(
                    "Jibo %s barge-in from %s; cancelling the turn",
                    device_id,
                    params.get("source") or "unknown",
                )
            await self._cancel_current_turn(
                device_id,
                expected_turn_id=str(params.get('turn_id') or ''),
                stopped_locally=params.get('stopped_locally') is True,
            )
            return
        if method != "input.transcript":
            return
        # The robot runs Global Listen and its local skill/NLU arbitration
        # first.  Only the explicit fallback signal is allowed to enter the
        # Hermes agent flow; local turns are logged for the operator but are
        # never double-dispatched.
        if route == INPUT_ROUTE_LOCAL:
            return
        if route not in {INPUT_ROUTE_HERMES_FALLBACK, INPUT_ROUTE_LEGACY_FALLBACK}:
            logger.debug("Ignoring input transcript with unknown route from %s", device_id)
            return
        text = str(params.get("text") or params.get("transcript") or "").strip()
        if not text:
            return
        final = bool(
            params.get("final", params.get("is_final", frame.get("final", False)))
        )
        if not final:
            return
        supplied_turn_id = str(params.get("turn_id") or "").strip()
        if len(supplied_turn_id) > 128:
            supplied_turn_id = ""
        await self._dispatch_utterance(device_id, text, frame, turn_id=supplied_turn_id)

    async def _handle_model_event(
        self, device_id: str, method: str, request: Mapping[str, Any]
    ) -> None:
        """Serve the robot's own model picker.

        The robot asks what it could run and tells us what it has been set to;
        the setting itself lives with the device in the Hermes config, so a
        change here is the same change the dashboard would make.
        """
        if method == MODEL_SELECT_EVENT:
            model = str(request.get("model") or "").strip()
            provider = str(request.get("provider") or "").strip()
            try:
                await asyncio.to_thread(
                    self.set_device_model_choice, device_id, model, provider
                )
            except Exception as exc:
                logger.warning("Could not set the Jibo model", exc_info=True)
                await self._send_models(device_id, {"error": str(exc)})
                return
        try:
            payload = await asyncio.to_thread(self.model_options, device_id)
        except Exception as exc:
            logger.warning("Could not read the Jibo model options", exc_info=True)
            payload = {"error": str(exc)}
        await self._send_models(device_id, payload)

    async def _handle_web_panel_request(
        self, device_id: str, request: Mapping[str, Any]
    ) -> None:
        """Open the configured default browser page for a Main Menu Web tap.

        The event contains no URL and cannot direct the host browser. Its only
        effect is to ask for the local configuration's default, which is then
        subjected to the same proxy allow-list used by the explicit agent tool.
        """
        if str(request.get("action") or "").strip() != "open_default":
            logger.warning("Ignoring unknown Web Panel request from %s", device_id)
            return
        # Imported lazily because native_tools imports the adapter for its
        # synchronous tool entry point. At event time both modules are loaded.
        from .native_tools import _WEB_PANEL_SKILL, _open_browser_page, _panel_target, _web_panel_settings

        settings, config_error = _web_panel_settings(self)
        if config_error or settings is None:
            logger.warning("Jibo Web Panel is not configured: %s", config_error)
            return
        target, target_error = _panel_target({"url": settings["default_url"]}, settings)
        if target_error or not target:
            logger.warning("Jibo Web Panel default page is invalid: %s", target_error)
            return
        opened, open_error = await asyncio.to_thread(_open_browser_page, settings, target)
        if not opened:
            logger.warning("Could not open Jibo Web Panel default page: %s", open_error)
            return
        stream_url = settings["public_url"] + "/video.ts?token=" + quote_plus(settings["token"])
        try:
            await self._request_native(
                device_id,
                "skill.launch",
                {"skill": _WEB_PANEL_SKILL, "options": {"stream_url": stream_url}},
                timeout=20.0,
                required_capability="skill.launch",
            )
        except Exception:
            logger.warning("Could not launch Jibo Web Panel for %s", device_id, exc_info=True)

    async def _send_models(self, device_id: str, payload: Mapping[str, Any]) -> None:
        try:
            client = self._client_for(device_id)
            await client.request(CHAT_MODELS_METHOD, dict(payload), timeout=8.0)
        except Exception:
            logger.debug("Could not deliver Jibo model options to %s", device_id, exc_info=True)

    def model_options(self, device_id: str) -> dict[str, Any]:
        """What this robot is set to, and the short list it can be set to."""
        document = model_settings.load_config()
        store = getattr(self, "_session_store", None)
        db = getattr(store, "_db", None) if store is not None else None
        current = model_settings.device_model(document, device_id)
        return {
            "device_id": str(device_id or "").strip(),
            "current": current,
            "default": model_settings.default_model(document),
            "recent": model_settings.recent_models(db),
        }

    def set_device_model_choice(
        self, device_id: str, model: str, provider: str = ""
    ) -> dict[str, str]:
        """Change which model answers on one robot, everywhere at once."""
        chosen = model_settings.set_device_model(device_id, model, provider)
        target = str(device_id or "").strip()
        for index, item in enumerate(self._device_configs):
            if item.id == target:
                self._device_configs[index] = replace(
                    item,
                    model=chosen["model"],
                    model_provider=chosen["provider"],
                )
                break
        for session_key, mapped in list(self._session_devices.items()):
            if mapped == target:
                self._apply_device_model(target, session_key)
        return chosen

    async def _deliver_history(self, device_id: str, request: Mapping[str, Any]) -> None:
        """Return one bounded, read-only page of canonical Jibo history.

        This intentionally has no session-switch side effect.  The face can
        inspect saved conversations, but its current live conversation and the
        model's active context stay exactly where they were.
        """
        mode = "transcript" if str(request.get("mode") or "").strip() == "transcript" else "sessions"
        offset = _bounded_setting(request.get("offset"), 0, 0, HISTORY_MAX_OFFSET)
        try:
            store = getattr(self, "_session_store", None)
            db = getattr(store, "_db", None) if store is not None else None
            if db is None:
                raise RuntimeError("Hermes session history is unavailable")
            if mode == "sessions":
                payload = await asyncio.to_thread(self._history_sessions_page, db, offset)
            else:
                session_id = str(request.get("session_id") or "").strip()
                payload = await asyncio.to_thread(
                    self._history_transcript_page, db, session_id, offset
                )
        except Exception:
            logger.warning("Could not load Jibo chat history", exc_info=True)
            payload = {
                "kind": "error",
                "message": "Saved conversations are unavailable right now.",
            }
        try:
            client = self._client_for(device_id)
            await client.request(CHAT_HISTORY_METHOD, payload, timeout=8.0)
        except Exception:
            logger.debug("Could not deliver Jibo chat history to %s", device_id, exc_info=True)

    @staticmethod
    def _history_sessions_page(db: Any, offset: int) -> dict[str, Any]:
        rows = db.list_sessions_rich(
            source="jibo",
            limit=HISTORY_SESSION_PAGE_SIZE + 1,
            offset=offset,
            order_by_last_active=True,
            compact_rows=True,
        )
        sessions: list[dict[str, Any]] = []
        for row in rows[:HISTORY_SESSION_PAGE_SIZE]:
            session_id = str(row.get("id") or "").strip()
            if not session_id:
                continue
            sessions.append(
                {
                    "id": session_id[:128],
                    "title": _raw_log_text(row.get("title"), 180),
                    "preview": _raw_log_text(row.get("preview"), 220),
                    "started_at": row.get("started_at") or 0,
                    "last_active": row.get("last_active") or 0,
                    "message_count": max(0, int(row.get("message_count") or 0)),
                }
            )
        return {
            "kind": "sessions",
            "offset": offset,
            "has_more": len(rows) > HISTORY_SESSION_PAGE_SIZE,
            "sessions": sessions,
        }

    @staticmethod
    def _history_transcript_page(
        db: Any, session_id: str, offset: int
    ) -> dict[str, Any]:
        if not session_id or len(session_id) > 128:
            return {"kind": "error", "message": "That saved conversation is unavailable."}
        session = db.get_session(session_id)
        if not isinstance(session, Mapping) or session.get("source") != "jibo":
            return {"kind": "error", "message": "That saved conversation is unavailable."}
        messages = db.get_messages_as_conversation(
            session_id, include_ancestors=True, repair_alternation=False
        )
        visible = []
        for message in messages:
            if not isinstance(message, Mapping) or message.get("role") not in {"user", "assistant"}:
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            text = _raw_log_text(content, HISTORY_MAX_TEXT).strip()
            if text:
                visible.append({"role": message["role"], "content": text})
        end = max(0, len(visible) - offset)
        start = max(0, end - HISTORY_MESSAGE_PAGE_SIZE)
        return {
            "kind": "transcript",
            "session_id": session_id,
            "offset": offset,
            "has_older": start > 0,
            "has_newer": end < len(visible),
            "session": {
                "id": session_id,
                "title": _raw_log_text(session.get("title"), 180),
                "started_at": session.get("started_at") or 0,
                "last_active": session.get("last_activity_at") or session.get("started_at") or 0,
            },
            "messages": visible[start:end],
        }

    async def _dispatch_utterance(
        self,
        device_id: str,
        text: str,
        frame: Mapping[str, Any],
        *,
        origin: str = ORIGIN_BRIDGE,
        turn_id: str = "",
    ) -> None:
        """Turn a finished utterance into a Hermes agent turn.

        Shared by the text ``input.transcript`` path, by audio captures
        transcribed on this host, and by a Phoenix escalation, so all three
        routes get identical session keying, cancel-phrase handling, and turn
        bookkeeping.  ``turn_id`` is minted once here when the caller did not
        bring one, and every artefact of the turn — transcript rows, bridge
        deltas, the rendered record — carries it.
        """
        if is_cancel_phrase(text):
            await self._cancel_current_turn(device_id)
            return
        async with self._turn_lock(device_id):
            if self._active_turns.get(device_id):
                await self._cancel_turn_locked(device_id, stopped_locally=False)
            # A delivered answer clears _active_turns before the base adapter
            # finishes typing/hooks/callback cleanup. Its live session guard
            # would queue this reply and send a generic busy acknowledgment
            # through send(), incorrectly finalizing the NEW native turn.
            # Retire that previous processing owner before binding the reply,
            # even when there is no remaining native turn to cancel.
            for session_key, session_device in list(self._session_devices.items()):
                if session_device == device_id and session_key in getattr(self, '_active_sessions', {}):
                    await self.cancel_session_processing(session_key)
            record = self._begin_utterance(device_id, text, origin, turn_id)
        source = record['source']
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            user_id='jibo-local-user',
            user_name='Jibo user',
            message_id='jibo-' + uuid.uuid4().hex,
            raw_message={'native': frame, 'device_id': device_id, 'native_turn_id': record['turn_id']},
            timestamp=datetime.now(timezone.utc),
        )
        logger.info('Native Jibo transcript received from %s: %s', device_id, text[:120])
        token = _NATIVE_TURN.set((self, device_id, record['turn_id']))
        try:
            await self.handle_message(event)
        finally:
            _NATIVE_TURN.reset(token)

    async def _process_message_background(self, event: MessageEvent, session_key: str) -> None:
        # A gateway queue/drain task may inherit the PREVIOUS task's context
        # (or startup's empty context). Rebind from the accepted event itself.
        raw = event.raw_message if isinstance(event.raw_message, Mapping) else {}
        device_id, turn_id = raw.get('device_id'), raw.get('native_turn_id')
        token = _NATIVE_TURN.set((self, device_id, turn_id)) if device_id and turn_id else None
        try:
            await super()._process_message_background(event, session_key)
        finally:
            if token is not None:
                _NATIVE_TURN.reset(token)

    def _begin_utterance(self, device_id: str, text: str, origin: str, turn_id: str) -> dict[str, Any]:
        self._delivered_answers.pop(device_id, None)
        self._turn_ids[device_id] = turn_id or new_turn_id(origin)
        config = self._config_for(device_id)
        chat_id = f"jibo:{device_id}"
        source = self.build_source(
            chat_id=chat_id,
            chat_name=config.name,
            chat_type="dm",
            user_id="jibo-local-user",
            user_name="Jibo user",
        )
        extra = self.config.extra or {}
        session_key = build_session_key(
            source,
            group_sessions_per_user=extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=extra.get("thread_sessions_per_user", False),
            profile=self._session_key_profile(source),
        )
        self._session_devices[session_key] = device_id
        self._apply_device_model(device_id, session_key)
        self._last_device_id = device_id
        self._active_source = source
        self._active_session_key = session_key
        self._active_device_for_turn = device_id
        self._active_turns[device_id] = {
            "session_key": session_key,
            "source": source,
            "text": text,
            "origin": origin,
            "turn_id": self._turn_ids.get(device_id, ""),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "dispatch_task": asyncio.current_task(),
        }
        return self._active_turns[device_id]

    def _turn_lock(self, device_id: str) -> asyncio.Lock:
        locks = getattr(self, '_turn_locks', None)
        if locks is None:
            locks = self._turn_locks = {}
        return locks.setdefault(device_id, asyncio.Lock())

    def _turn_id_for(self, device_id: str) -> str:
        context = _NATIVE_TURN.get()
        if context is not None and context[0] is self and context[1] == device_id:
            return context[2]
        return self._turn_ids.get(device_id, '')

    def _turn_is_current(self, device_id: str, turn_id: str) -> bool:
        return self._turn_ids.get(device_id, '') == turn_id

    def _assert_turn_current(self, device_id: str, turn_id: str) -> None:
        if not self._turn_is_current(device_id, turn_id):
            raise NativeBridgeError('This Jibo turn was cancelled or superseded', code='TURN_CANCELLED', retryable=False)

    async def _cancel_current_turn(
        self, device_id: str, *, expected_turn_id: str = '', stopped_locally: bool = False,
    ) -> None:
        async with self._turn_lock(device_id):
            if expected_turn_id and not self._turn_is_current(device_id, expected_turn_id):
                return
            await self._cancel_turn_locked(device_id, stopped_locally=stopped_locally)

    async def _cancel_turn_locked(self, device_id: str, *, stopped_locally: bool) -> None:
        record = self._active_turns.get(device_id) or {}
        turn_id = str(record.get('turn_id') or self._turn_ids.get(device_id) or '')
        delivered = self._delivered_answer(device_id, turn_id=turn_id)
        escalation = self._pending_escalations.get(device_id)
        if escalation is not None and escalation.turn_id != turn_id:
            escalation = None
        # Invalidate outputs before the first await. The per-device lock keeps
        # gateway cancellation from reaching a newly dispatched agent session.
        self._turn_ids.pop(device_id, None)
        self._active_turns.pop(device_id, None)
        self._streams.pop(device_id, None)
        self._processing_stages.pop(device_id, None)
        self._delivered_answers.pop(device_id, None)
        dispatch = record.get('dispatch_task')
        if dispatch is not None and dispatch is not asyncio.current_task() and not dispatch.done():
            dispatch.cancel()
        client = self._clients.get(device_id)
        if not stopped_locally and client is not None and client.connected:
            try:
                await client.request('speech.stop', {'reason': 'jibo_input_cancel', 'turn_id': turn_id}, timeout=5.0)
            except Exception:
                logger.debug("Native Jibo speech stop failed for %s", device_id, exc_info=True)
        session_key = str(record.get("session_key") or "")
        source = record.get("source")
        if session_key:
            runner = getattr(self, "gateway_runner", None)
            hard_cancel = getattr(runner, "_interrupt_and_clear_session", None)
            if callable(hard_cancel) and source is not None:
                try:
                    await hard_cancel(
                        session_key,
                        source,
                        interrupt_reason="jibo_native_cancel",
                        invalidation_reason="jibo_native_cancel",
                    )
                except Exception:
                    logger.debug("Native Jibo gateway cancellation failed", exc_info=True)
            else:
                try:
                    await self.interrupt_session_activity(session_key, f"jibo:{device_id}")
                except Exception:
                    logger.debug("Native Jibo session cancellation failed", exc_info=True)
            # Runner invalidation stops generation/tools but does not release
            # BasePlatformAdapter's processing task/guard. Without this the
            # new question queues behind the old task and can inherit its
            # cancelled worker context when that task finally drains.
            try:
                await self.cancel_session_processing(session_key)
            except Exception:
                logger.debug('Native Jibo processing task cancellation failed', exc_info=True)
        # Cancelling the model's remaining bookkeeping cannot take back an
        # answer already delivered. Preserve it when the next question starts.
        try:
            await self._complete_turn(
                device_id,
                delivered.text if delivered else '',
                escalation=escalation,
                spoken=bool(delivered),
                status=STATUS_OK if delivered else STATUS_CANCELLED,
                active=record,
                turn_id=turn_id,
            )
        except Exception:
            logger.debug("Jibo cancelled-turn record failed", exc_info=True)
        if not stopped_locally:
            try:
                await self._request_best_effort(device_id, 'display.clear', {'restore': 'eye', 'owner_turn_id': turn_id})
            except Exception:
                pass

    # ── the integration spine ────────────────────────────────────────
    #
    # Three producers, one conversation.  Everything below exists so that a
    # turn Phoenix answered alone, a turn Phoenix escalated, and a turn that
    # started in Hermes all end up in the same history, on the same screen,
    # and in the same dashboard list.

    def _turn_source(self, device_id: str) -> Any:
        config = self._config_for(device_id)
        return self.build_source(
            chat_id=f"jibo:{device_id}",
            chat_name=config.name,
            chat_type="dm",
            user_id="jibo-local-user",
            user_name="Jibo user",
        )

    def _write_transcript_rows(self, record: TurnRecord, attribute_skill: bool) -> bool:
        """Project one turn into Hermes' canonical session transcript.

        Hermes owns history (decision D7), so a turn Phoenix answered by
        itself has to be written here or the agent's next turn will not know
        it happened.  Both a user row and an assistant row go in: Hermes
        replays the transcript with alternation repair, and a user row with
        no answer is a wedge the repair has to heal on every later request.

        ``platform_message_id`` makes this idempotent, so Phoenix may retry a
        report without double-writing.
        """
        store = getattr(self, "_session_store", None)
        if store is None:
            return False
        try:
            entry = store.get_or_create_session(self._turn_source(record.device_id))
            session_id = str(getattr(entry, "session_id", "") or "")
        except Exception:
            logger.warning("Could not resolve a Hermes session for a Jibo turn", exc_info=True)
            return False
        if not session_id:
            return False
        written = False
        for row in record.to_transcript_rows(attribute_skill=attribute_skill):
            message_id = str(row.get("platform_message_id") or "")
            try:
                if message_id and store.has_platform_message_id(session_id, message_id):
                    continue
            except Exception:
                logger.debug("Jibo transcript dedupe lookup failed", exc_info=True)
            try:
                store.append_to_transcript(session_id, row)
                written = True
            except Exception:
                logger.warning("Could not record a Jibo turn in Hermes history", exc_info=True)
        return written

    async def record_turn(
        self,
        record: TurnRecord,
        *,
        attribute_skill: bool = False,
        final: bool = True,
        owner_turn_id: str = '',
    ) -> dict[str, Any]:
        """Record one completed turn, whoever answered it.

        A later report of the same ``turn_id`` completes the record already
        held rather than becoming a second turn, which is what makes the
        escalation path safe: Hermes records the answer as soon as it has it,
        and Phoenix's follow-up report adds the skill id and the timings.
        """
        device_id = self.resolve_device_id(record.device_id, require_connected=False)
        index = self._turn_index.setdefault(device_id, {})
        existing = index.get(record.turn_id)
        merged = merge_records(existing, record) if existing is not None else record
        index[record.turn_id] = merged
        log = self._turn_log.setdefault(device_id, deque(maxlen=DEFAULT_TURN_LOG_LIMIT))
        for position, item in enumerate(log):
            if item.turn_id == merged.turn_id:
                log[position] = merged
                break
        else:
            log.append(merged)
        if len(index) > 2 * (log.maxlen or DEFAULT_TURN_LOG_LIMIT):
            self._turn_index[device_id] = {item.turn_id: item for item in log}

        # A turn the agent answered is already in the transcript — the agent
        # persisted it.  Writing it again here would duplicate every reply.
        in_history = merged.origin in HERMES_ANSWERED
        if not in_history:
            in_history = await asyncio.to_thread(
                self._write_transcript_rows, merged, attribute_skill
            )
        rendered = await self._render_turn(device_id, merged, final=final, owner_turn_id=owner_turn_id)
        self._append_log(
            device_id,
            direction="turn",
            method=TURN_APPEND_METHOD,
            payload={
                "turn_id": merged.turn_id,
                "origin": merged.origin,
                "skill": merged.skill,
                "text": merged.transcript,
                "status": merged.status,
            },
        )
        return {
            "turn_id": merged.turn_id,
            "device_id": device_id,
            "origin": merged.origin,
            "status": merged.status,
            "history": bool(in_history),
            "rendered": bool(rendered),
        }

    async def _render_turn(
        self, device_id: str, record: TurnRecord, *, final: bool = True, owner_turn_id: str = ''
    ) -> bool:
        """Push one turn record to the robot's chat UI over the bridge."""
        client = self._clients.get(device_id)
        if client is None or not client.connected:
            return False
        if not self.has_capability(TURN_APPEND_METHOD, device_id=device_id):
            # A Be build without the chat UI simply does not render history;
            # that is a missing feature, not an error worth logging per turn.
            return False
        params = record.to_bridge_params(session_id=client.session_id)
        if owner_turn_id:
            self._assert_turn_current(device_id, owner_turn_id)
            params['owner_turn_id'] = owner_turn_id
        # An image tool may add an inline row before the agent has finished
        # its surrounding answer. That row must not clear the robot's active
        # thinking light; only the completed turn gets the final marker.
        if final:
            params["final"] = True
        for entry in record.media:
            if exceeds_thumbnail(entry):
                logger.info(
                    "Jibo %s was sent a %sx%s image for turn %s; anything over "
                    "%sx%s is downscaled on arrival and only costs texture memory",
                    device_id,
                    entry.get("width"),
                    entry.get("height"),
                    record.turn_id,
                    BRIDGE_THUMBNAIL_MAX_WIDTH,
                    BRIDGE_THUMBNAIL_MAX_HEIGHT,
                )
                break
        if params.get("media_omitted"):
            logger.info(
                "Jibo %s turn %s: %s media entries were not sent (the screen "
                "renders one, and the frame budget is 24 KB)",
                device_id,
                record.turn_id,
                params["media_omitted"],
            )
        try:
            result = await client.request(TURN_APPEND_METHOD, params, timeout=8.0)
            # Be resolves every attachment to a local file before publishing
            # the turn and reports how many landed.  A shortfall means a
            # fetch failed on the robot, which is invisible from here
            # otherwise.
            sent = len(params.get("media") or ())
            if sent and isinstance(result, Mapping) and "media_resolved" in result:
                resolved = result.get("media_resolved")
                if isinstance(resolved, int) and resolved < sent:
                    logger.info(
                        "Jibo %s resolved %s of %s attachments for turn %s",
                        device_id,
                        resolved,
                        sent,
                        record.turn_id,
                    )
                    return False
            return True
        except Exception:
            logger.debug("Jibo turn render failed for %s", device_id, exc_info=True)
            return False

    def get_turn_log(self, device_id: str | None = None, *, limit: int = 50) -> list[dict[str, Any]]:
        """Recent turns for the dashboard, newest last."""
        try:
            bounded = max(1, min(int(limit), MAX_TURN_LOG_LIMIT))
        except (TypeError, ValueError):
            bounded = 50
        records: list[TurnRecord] = []
        if device_id:
            records.extend(self._turn_log.get(self.resolve_device_id(device_id), ()))
        else:
            for device_records in self._turn_log.values():
                records.extend(device_records)
            records.sort(key=lambda item: item.started_at)
        return [item.to_dict() for item in records[-bounded:]]

    # ── escalation ───────────────────────────────────────────────────
    async def escalate_turn(
        self,
        *,
        device_id: str,
        text: str,
        turn_id: str = "",
        speech_owner: str = SPEECH_PHOENIX,
        intent: str = "",
        skill: str = "",
        correlation: Mapping[str, str] | None = None,
        timeout: float = 120.0,
        attribute_skill: bool = False,
        on_delta: Any = None,
    ) -> dict[str, Any]:
        """Answer a Phoenix escalation, correlated to the robot that asked.

        The request carries the robot's device id and the turn id Phoenix
        already minted, so Hermes joins this to the same conversation instead
        of guessing which robot is talking.  ``speech_owner`` decides who
        says the answer: Phoenix speaks it through its own skill action by
        default, in which case Hermes must stay silent and only render.
        """
        target = self.resolve_device_id(device_id, require_connected=False)
        loop = asyncio.get_running_loop()
        previous = self._pending_escalations.pop(target, None)
        if previous is not None and not previous.future.done():
            previous.future.set_exception(
                NativeBridgeError(
                    "superseded by a newer escalation on the same robot",
                    code="SUPERSEDED",
                    retryable=False,
                )
            )
        escalation = _Escalation(
            turn_id=turn_id or new_turn_id(ORIGIN_PHOENIX_ESCALATED),
            device_id=target,
            text=text,
            speech_owner=speech_owner,
            intent=intent,
            skill=skill,
            correlation=dict(correlation or {}),
            attribute_skill=attribute_skill,
            future=loop.create_future(),
            on_delta=on_delta,
        )
        self._pending_escalations[target] = escalation
        frame = {
            "method": "escalation.request",
            "params": {"turn_id": escalation.turn_id, "skill": skill, "intent": intent},
        }
        task = asyncio.ensure_future(
            self._dispatch_utterance(
                target,
                text,
                frame,
                origin=ORIGIN_PHOENIX_ESCALATED,
                turn_id=escalation.turn_id,
            )
        )

        def _dispatch_failed(finished: Any) -> None:
            if finished.cancelled():
                return
            error = finished.exception()
            if error is not None and not escalation.future.done():
                escalation.future.set_exception(error)

        task.add_done_callback(_dispatch_failed)
        try:
            return await asyncio.wait_for(asyncio.shield(escalation.future), timeout=timeout)
        except asyncio.TimeoutError:
            # Stop holding the answer back.  Phoenix has given up waiting, so
            # the better degradation is for Hermes to speak the reply itself
            # when it eventually arrives than for the user to hear nothing.
            if self._pending_escalations.get(target) is escalation:
                self._pending_escalations.pop(target, None)
            raise
        finally:
            if escalation.future.done() and self._pending_escalations.get(target) is escalation:
                self._pending_escalations.pop(target, None)

    async def _complete_turn(
        self,
        device_id: str,
        content: str,
        *,
        escalation: Optional[_Escalation],
        spoken: bool,
        status: str = STATUS_OK,
        active: Optional[Mapping[str, Any]] = None,
        turn_id: Optional[str] = None,
    ) -> None:
        """Close out one Hermes-answered turn: record it, then release Phoenix."""
        active = active if active is not None else (self._active_turns.get(device_id) or {})
        # No active turn means Hermes started this one itself — a dashboard
        # reply, another platform, a proactive nudge.
        origin = str(active.get("origin") or (ORIGIN_BRIDGE if active else ORIGIN_HERMES))
        turn_id = turn_id if turn_id is not None else str(active.get('turn_id') or self._turn_id_for(device_id))
        if escalation is not None:
            origin = ORIGIN_PHOENIX_ESCALATED
            turn_id = escalation.turn_id
        if not turn_id:
            turn_id = new_turn_id(origin)
        if escalation is not None:
            speech_owner = SPEECH_HERMES if spoken else escalation.speech_owner
        else:
            speech_owner = SPEECH_HERMES if spoken else SPEECH_NONE
        record = TurnRecord(
            turn_id=turn_id,
            device_id=device_id,
            origin=origin,
            transcript=str(active.get("text") or (escalation.text if escalation else "")),
            response=str(content or ""),
            intent=escalation.intent if escalation else "",
            skill=escalation.skill if escalation else "",
            status=status,
            speech_owner=speech_owner,
            correlation=dict(escalation.correlation) if escalation else {},
            started_at=str(active.get("started_at") or datetime.now(timezone.utc).isoformat()),
        )
        if status == STATUS_OK and spoken and self._turn_is_current(device_id, turn_id):
            # Speech has already finished. Open the reply window before any
            # storage/projection delay; atomic speech/listen calls deduplicate.
            await self._maybe_listen_again(device_id, str(content or ''), turn_id=turn_id)
        if record.transcript or record.response:
            try:
                await self.record_turn(
                    record,
                    attribute_skill=bool(escalation.attribute_skill) if escalation else False,
                )
            except Exception:
                logger.warning("Could not record a Jibo turn for %s", device_id, exc_info=True)
        if escalation is not None:
            escalation.spoken = spoken
            escalation.resolve(content, status=status)
            if self._pending_escalations.get(device_id) is escalation:
                self._pending_escalations.pop(device_id, None)
        is_current = self._turn_is_current(device_id, turn_id)
        if is_current and self._turn_is_current(device_id, turn_id):
            self._turn_ids.pop(device_id, None)
            self._delivered_answers.pop(device_id, None)
            self._processing_stages.pop(device_id, None)
            if self._active_turns.get(device_id) is active:
                self._active_turns.pop(device_id, None)

    # ── follow-up listening ──────────────────────────────────────────
    def _follow_up_settings(self) -> Mapping[str, Any]:
        values = (self.config.extra or {}).get("follow_up")
        return values if isinstance(values, Mapping) else {}

    def _follow_up_timeout(self, device_id: str, answer: str, turn_id: str) -> Optional[int]:
        """Plan at most one bounded reply window for a completed question."""
        settings = self._follow_up_settings()
        handled = getattr(self, '_follow_up_handled', {})
        if not self._turn_is_current(device_id, turn_id) or (turn_id and handled.get(device_id) == turn_id):
            return None
        if not _event_bool(settings.get("enabled"), True):
            return None
        # Reached through _complete_turn, which subclasses and tests construct
        # without going through __init__, so the state is resolved lazily
        # rather than assumed.
        depths = getattr(self, "_follow_up_depth", None)
        if depths is None:
            depths = {}
            self._follow_up_depth = depths
        if not answer.rstrip().rstrip('\"\'”’)]*').rstrip().endswith("?"):
            depths.pop(device_id, None)
            return None
        try:
            configured_limit = settings.get('max_consecutive')
            limit = max(0, int(configured_limit)) if configured_limit is not None else None
        except (TypeError, ValueError):
            limit = None
        depth = depths.get(device_id, 0)
        if limit is not None and depth >= limit:
            logger.info(
                "Jibo %s asked another question but the follow-up limit (%d) is reached",
                device_id, limit,
            )
            # This question has been considered even though no capture opens.
            # A second planner call at model completion must not bypass the cap.
            self._mark_follow_up(device_id, turn_id, count=False)
            depths.pop(device_id, None)
            return None
        client = (getattr(self, "_clients", None) or {}).get(device_id)
        if client is None or not client.connected:
            return None
        if not self.has_capability("input.listen", device_id=device_id):
            return None
        try:
            timeout_ms = int(settings.get("timeout_ms", 9000))
        except (TypeError, ValueError):
            timeout_ms = 9000
        return max(1000, min(timeout_ms, 30000))

    def _mark_follow_up(self, device_id: str, turn_id: str, *, count: bool = True) -> None:
        handled = getattr(self, '_follow_up_handled', None)
        if handled is None:
            handled = self._follow_up_handled = {}
        handled[device_id] = turn_id
        depths = getattr(self, '_follow_up_depth', None)
        if depths is None:
            depths = self._follow_up_depth = {}
        if count:
            depths[device_id] = depths.get(device_id, 0) + 1

    async def _maybe_listen_again(self, device_id: str, answer: str, *, turn_id: Optional[str] = None) -> None:
        """Fallback for already-streamed speech and older robot builds."""
        turn_id = self._turn_id_for(device_id) if turn_id is None else turn_id
        timeout_ms = self._follow_up_timeout(device_id, answer, turn_id)
        if timeout_ms is None:
            return
        client = self._clients[device_id]
        self._mark_follow_up(device_id, turn_id)
        try:
            await client.request(
                "input.listen",
                {'enabled': True, 'timeout_ms': timeout_ms, 'turn_id': turn_id or ''},
                timeout=15.0,
            )
        except Exception:
            logger.debug("Jibo follow-up listen failed for %s", device_id, exc_info=True)
            return
        logger.info("Jibo %s opened one reply window", device_id)

    # ── incremental delivery ─────────────────────────────────────────
    def _streaming_settings(self) -> Mapping[str, Any]:
        values = (self.config.extra or {}).get("streaming")
        return values if isinstance(values, Mapping) else {}

    def supports_draft_streaming(
        self,
        chat_type: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        chat_id: Optional[str] = None,
    ) -> bool:
        """Offer incremental delivery when the robot can render it.

        The gateway's stream consumer calls ``send_draft`` with the growing
        response whenever this returns True, which is exactly the signal the
        robot needs: text as it is produced, and sentence boundaries at which
        speech can start.  A Be build that does not advertise
        ``response.delta`` gets the old whole-answer path unchanged.
        """
        del chat_type, metadata
        if not _event_bool(self._streaming_settings().get("enabled"), True):
            return False
        try:
            target = self.resolve_device_id(chat_id=chat_id, require_connected=False)
        except NativeBridgeError:
            return False
        return self.has_capability(RESPONSE_DELTA_METHOD, device_id=target)

    async def send_draft(
        self,
        chat_id: str,
        draft_id: int,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Deliver one streaming frame as bridge deltas and speech."""
        del metadata
        try:
            target = self.resolve_device_id(chat_id=chat_id, require_connected=False)
        except NativeBridgeError as exc:
            return SendResult(success=False, error=str(exc), retryable=exc.retryable)
        try:
            self._assert_turn_current(target, self._turn_id_for(target))
            if self._tool_answer_pending(target):
                # final=true already published the answer. A later model
                # transcript must not reopen its bubble or restart speech.
                return SendResult(success=True, message_id='already-delivered')
            stream = self._stream_for(target, draft_id)
            await self._emit_step(target, stream, stream.push(content or ""), final=False)
        except (NativeRemoteError, NativeBridgeError) as exc:
            return SendResult(success=False, error=str(exc), retryable=exc.retryable)
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
        return SendResult(success=True, message_id=f"jibo-draft-{draft_id}")

    def _stream_for(self, device_id: str, draft_id: int) -> ResponseStream:
        turn_id = self._turn_id_for(device_id)
        self._assert_turn_current(device_id, turn_id)
        stream = self._streams.get(device_id)
        if stream is not None and stream.draft_id == int(draft_id) and stream.turn_id == turn_id:
            return stream
        if not turn_id:
            turn_id = new_turn_id(ORIGIN_BRIDGE)
            self._turn_ids[device_id] = turn_id
        settings = self._streaming_settings()
        stream = ResponseStream(
            turn_id=turn_id,
            draft_id=int(draft_id),
            start_seq=self._delta_seq.get(device_id, 0),
            min_delta_chars=_bounded_setting(settings.get("min_delta_chars"), 24, 0, 512),
            min_sentence_chars=_bounded_setting(settings.get("min_sentence_chars"), 24, 1, 512),
            max_sentence_chars=_bounded_setting(settings.get("max_sentence_chars"), 240, 32, 2000),
        )
        self._streams[device_id] = stream
        return stream

    async def _emit_step(self, device_id: str, stream: ResponseStream, step: Any, *, final: bool) -> None:
        self._assert_turn_current(device_id, stream.turn_id)
        if step.first:
            # The transcript owns the visual surface. This only changes the
            # live status under it from thinking to speaking.
            self.update_processing_stage("ANSWERING", device_id=device_id)
        if step.delta:
            await self._notify_native(
                device_id,
                RESPONSE_DELTA_METHOD,
                {
                    "turn_id": stream.turn_id,
                    "seq": self._next_delta_seq(device_id, stream),
                    "text": step.delta,
                    "replace": bool(step.replace),
                    "final": bool(final),
                },
            )
            self._assert_turn_current(device_id, stream.turn_id)
            self._tee_to_escalation(device_id, step.delta, turn_id=stream.turn_id)
        # An agent that called jibo_speak has already said this answer out
        # loud. The draft stream still renders it on screen, but speaking it
        # again is the same answer twice -- which from the room is
        # indistinguishable from a stutter.
        if self._tool_answer_pending(device_id):
            logger.debug("speak[stream] suppressed; jibo_speak already said it")
            return
        for sentence in step.sentences:
            logger.debug("speak[stream] %r", sentence[:60])
            await self._speak_segment(device_id, sentence, turn_id=stream.turn_id)

    def _next_delta_seq(self, device_id: str, stream: ResponseStream) -> int:
        seq = stream.next_seq()
        self._delta_seq[device_id] = seq
        return seq

    def _tee_to_escalation(self, device_id: str, delta: str, *, turn_id: Optional[str] = None) -> None:
        """Mirror a delta to a Phoenix escalation waiting on SSE.

        A Phoenix client that asked for ``stream: true`` gets the same
        incremental text the robot's screen is getting, so an escalated
        answer is no less responsive than a native one.
        """
        escalation = self._pending_escalations.get(device_id)
        turn_id = self._turn_id_for(device_id) if turn_id is None else turn_id
        if escalation is not None and escalation.turn_id != turn_id:
            return
        callback = getattr(escalation, "on_delta", None) if escalation else None
        if callback is None or not delta:
            return
        try:
            callback(delta)
        except Exception:
            logger.debug("Jibo escalation delta tee failed", exc_info=True)

    async def _finish_stream(
        self, device_id: str, stream: ResponseStream, content: str, *, speak: bool
    ) -> SendResult:
        """Close a streamed response against the authoritative final answer."""
        step = stream.finish(content or "")
        try:
            self._assert_turn_current(device_id, stream.turn_id)
            # Always sent, even with an empty delta: it is the signal that
            # closes the in-progress bubble on the robot's screen.
            await self._notify_native(
                device_id,
                RESPONSE_DELTA_METHOD,
                {
                    "turn_id": stream.turn_id,
                    "seq": self._next_delta_seq(device_id, stream),
                    "text": step.delta,
                    "replace": bool(step.replace),
                    "final": True,
                },
            )
            if not step.replace:
                # A replacement cannot be expressed as an OpenAI delta, so a
                # streaming Phoenix client keeps what it already received and
                # the authoritative answer still reaches it in the response.
                self._assert_turn_current(device_id, stream.turn_id)
                self._tee_to_escalation(device_id, step.delta, turn_id=stream.turn_id)
            if speak and not self._tool_answer_pending(device_id):
                for index, sentence in enumerate(step.sentences):
                    logger.debug("speak[finish] %r", sentence[:60])
                    await self._speak_segment(device_id, sentence,
                        final=index == len(step.sentences) - 1, turn_id=stream.turn_id)
        except (NativeRemoteError, NativeBridgeError) as exc:
            return SendResult(success=False, error=str(exc), retryable=exc.retryable)
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
        self._append_log(
            device_id,
            direction="outbound",
            method="chat.message",
            payload={"text": content, "final": True, "streamed": True},
        )
        return SendResult(success=True, message_id="jibo-streamed")

    async def _speak_segment(self, device_id: str, text: str, *, final: bool = False, turn_id: Optional[str] = None) -> None:
        """Speak one sentence of a streaming answer through native TTS.

        Deliberately leaner than :meth:`_speak_on_loop`: the chat UI is
        already rendering the text from ``response.delta``, so this does not
        also drive ``display.text``.  The ESML and pronunciation layers are
        the same ones the whole-answer path uses.
        """
        turn_id = self._turn_id_for(device_id) if turn_id is None else turn_id
        self._assert_turn_current(device_id, turn_id)
        client = self._clients.get(device_id)
        if client is None or not client.connected:
            return
        configured_phonemes = (self.config.extra or {}).get("pronunciation_phonemes")
        custom_phonemes = configured_phonemes if isinstance(configured_phonemes, Mapping) else None
        formatted = format_jibo_speech(text, kind="answer", style="neutral")
        pronunciation = normalize_for_speech(formatted, custom_phonemes=custom_phonemes)
        chunks = prepare_for_jibo(pronunciation.esml)
        if not chunks:
            return
        async with self._speak_locks.setdefault(device_id, asyncio.Lock()):
            for index, chunk in enumerate(chunks):
                self._assert_turn_current(device_id, turn_id)
                spoken = normalize_for_speech(chunk, custom_phonemes=custom_phonemes).spoken_text
                last = final and index == len(chunks) - 1
                reply_ms = self._follow_up_timeout(device_id, text, turn_id) if last else None
                paired = reply_ms is not None and self.has_capability('speech.speak_and_listen', device_id=device_id)
                if paired:
                    self._mark_follow_up(device_id, turn_id)
                result = await client.request(
                    'speech.speak_and_listen' if paired else 'speech.speak',
                    {
                        "text": spoken,
                        "spoken_text": spoken,
                        "esml": chunk,
                        "mode": "ssml",
                        "kind": "answer",
                        "style": "neutral",
                        "final": bool(last),
                        **({'listen_timeout_ms': reply_ms} if paired else {}),
                        "session_id": client.session_id,
                        'turn_id': turn_id,
                    },
                    timeout=90.0,
                )
                if isinstance(result, Mapping) and (result.get('stopped') or result.get('spoken') is False):
                    raise NativeBridgeError('This Jibo speech was cancelled', code='TURN_CANCELLED', retryable=False)

    async def _notify_native(
        self, device_id: str, method: str, params: Mapping[str, Any]
    ) -> None:
        """Send one uncorrelated bridge frame, tolerating a missing peer."""
        client = self._clients.get(device_id)
        if client is None or not client.connected:
            return
        if not self.has_capability(method, device_id=device_id):
            return
        try:
            await client.notify(method, params)
        except NativeDisconnected:
            logger.debug("Jibo %s dropped a %s frame while disconnected", device_id, method)

    def _append_log(self, device_id: str, *, direction: str, method: str, payload: Any) -> None:
        log = self._logs.setdefault(device_id, deque(maxlen=DEFAULT_LOG_LIMIT))
        entry: dict[str, Any] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "direction": str(direction),
            "method": str(method),
        }
        if isinstance(payload, Mapping):
            if "text" in payload or "transcript" in payload:
                entry["text"] = _raw_log_text(payload.get("text") or payload.get("transcript"))
            if "spoken_text" in payload:
                entry["spoken_text"] = _raw_log_text(payload.get("spoken_text"))
            if "final" in payload:
                entry["final"] = bool(payload.get("final"))
            if "route" in payload:
                entry["route"] = str(payload.get("route"))
            if direction in {"state", "error"}:
                for key in ("connected", "connecting", "error", "code"):
                    if key in payload and payload[key] is not None:
                        entry[key] = payload[key]
            if method not in {"input.transcript", "chat.message", "speech.speak", "display.text"}:
                compact = {key: value for key, value in payload.items() if key not in {"token", "auth"}}
                if compact:
                    try:
                        encoded = json.dumps(compact, ensure_ascii=False)
                        entry["detail"] = encoded[:1800]
                    except (TypeError, ValueError):
                        pass
        elif payload is not None:
            entry["detail"] = str(payload)[:1800]
        log.append(entry)

    def get_event_log(self, device_id: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
        target = self.resolve_device_id(device_id) if device_id else None
        logs: list[dict[str, Any]] = []
        if target:
            logs.extend(self._logs.get(target, ()))
        else:
            for device_logs in self._logs.values():
                logs.extend(device_logs)
            logs.sort(key=lambda item: str(item.get("at") or ""))
        try:
            bounded = max(1, min(int(limit), MAX_LOG_LIMIT))
        except (TypeError, ValueError):
            bounded = 100
        return [dict(item) for item in logs[-bounded:]]

    def dashboard_snapshot(self) -> dict[str, Any]:
        devices = []
        for config in self._device_configs:
            state = dict(self._device_states.get(config.id) or {})
            client = self._clients.get(config.id)
            if client is not None:
                state = client.state
            devices.append(
                {
                    **config.to_dict(),
                    "state": state,
                    "capabilities": list(client.capabilities) if client is not None else state.get("capabilities", []),
                    "chat_log": [dict(item) for item in self._logs.get(config.id, ())],
                    "turns": [item.to_dict() for item in self._turn_log.get(config.id, ())],
                }
            )
        return {
            "mode": NATIVE_MODE,
            "connected": self.connected,
            "devices": devices,
            "turn_ingest": {
                "enabled": self._ingest_config.enabled,
                "running": self._ingest_server is not None,
                "host": self._ingest_config.host,
                "port": self._ingest_config.port,
                "token_configured": bool(self._ingest_config.token),
            },
        }

    def has_capability(self, capability: str, *, device_id: str | None = None) -> bool:
        target = self.resolve_device_id(device_id)
        client = self._clients.get(target)
        if client is None:
            return False
        want = str(capability or "").strip().lower()
        capabilities = {item.lower() for item in client.capabilities}
        root = want.split(".", 1)[0]
        return want in capabilities or root in capabilities or any(
            item.startswith(want + ".") for item in capabilities
        )

    async def _request_best_effort(self, device_id: str, method: str, params: Mapping[str, Any] | None = None) -> Any:
        client = self._client_for(device_id)
        try:
            return await client.request(method, params)
        finally:
            self._append_log(device_id, direction="request", method=method, payload=dict(params or {}))

    async def _request_native(
        self,
        device_id: str,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
        required_capability: str | None = None,
    ) -> Any:
        if required_capability and not self.has_capability(required_capability, device_id=device_id):
            raise NativeBridgeError(
                f"Jibo device {device_id} does not advertise {required_capability}",
                code="CAPABILITY_UNAVAILABLE",
                retryable=False,
            )
        turn_id = self._turn_id_for(device_id)
        self._assert_turn_current(device_id, turn_id)
        owned_params = dict(params or {})
        if turn_id:
            owned_params.setdefault('owner_turn_id', turn_id)
            if method == 'speech.stop':
                # Be's stop-specific guard reads turn_id, including when the
                # transport queued this request before a replacement turn.
                owned_params.setdefault('turn_id', turn_id)
        client = self._client_for(device_id)
        result = await client.request(method, owned_params, timeout=timeout)
        self._append_log(device_id, direction="request", method=method, payload=dict(params or {}))
        return result

    def native_call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        device_id: str | None = None,
        timeout: float = 15.0,
        required_capability: str | None = None,
    ) -> SendResult:
        """Run one native operation from a synchronous Hermes tool worker."""
        try:
            target = self.resolve_device_id(device_id, require_connected=True)
        except NativeBridgeError as exc:
            return SendResult(success=False, error=str(exc), retryable=exc.retryable)
        return self._run_native_operation(
            lambda: self._request_native(
                target,
                method,
                params,
                timeout=timeout,
                required_capability=required_capability,
            ),
            timeout=timeout + 2.0,
        )

    def observe_environment(self, params: Mapping[str, Any], *, device_id: str | None = None,
                            capture: bool = True, show_image: bool = True) -> SendResult:
        """One worker call: native gaze/capture, host artifact, optional inline preview."""
        from .observation import camera_url, save_camera_image
        try:
            target = self.resolve_device_id(device_id, require_connected=True)
            turn_id = self._turn_id_for(target)
            self._assert_turn_current(target, turn_id)
            method = "environment.observe" if capture else "environment.get"
            response = self.native_call(method, params, device_id=target,
                                        required_capability=method, timeout=14)
            if not response.success or not capture:
                return response
            self._assert_turn_current(target, turn_id)
            payload = dict(response.raw_response or {})
            image = dict(payload.get("image") or {})
            config = next(item for item in self._device_configs if item.id == target)
            url = camera_url(config.host, image)
            saved = save_camera_image(url, self._media_cache_directory() / "observations")
            self._assert_turn_current(target, turn_id)
            payload["image"] = {**image, **saved}
            payload["image"]["shown"] = False
            if show_image:
                shown = self.display_local_image(saved["local_path"], device_id=target, name="hermes-observation")
                payload["image"]["shown"] = bool(shown.success)
                if not shown.success:
                    payload["image"]["display_error"] = shown.error or "Image display failed"
            self._assert_turn_current(target, turn_id)
            payload["next_step"] = "Inspect image.local_path with vision_analyze before describing what is visible."
            return SendResult(success=True, raw_response=payload)
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=False,
                              raw_response={"code": getattr(exc, "code", "OBSERVATION_FAILED")})

    def _run_native_operation(
        self, coroutine_factory, *, timeout: float = 17.0, timeout_retryable: bool = True,
    ) -> SendResult:
        loop = self._loop
        if loop is None or not loop.is_running():
            return SendResult(success=False, error="Jibo native event loop is not available", retryable=True)
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            return SendResult(success=False, error="Jibo native operation called on its event loop")
        coroutine = coroutine_factory()
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
            result = future.result(timeout=timeout)
            if isinstance(result, SendResult):
                return result
            return SendResult(success=True, message_id="jibo-" + uuid.uuid4().hex, raw_response=result)
        except (TimeoutError, concurrent.futures.TimeoutError):
            try:
                future.cancel()
            except Exception:
                pass
            return SendResult(success=False, error="Jibo native operation timed out", retryable=timeout_retryable)
        except (NativeRemoteError, NativeBridgeError) as exc:
            return SendResult(success=False, error=str(exc), retryable=exc.retryable, raw_response=getattr(exc, "error", None))
        except Exception as exc:
            logger.debug("Native Jibo operation failed", exc_info=True)
            return SendResult(success=False, error=str(exc), retryable=True)

    async def _speak_on_loop(
        self,
        device_id: str,
        text: str,
        *,
        kind: str = "answer",
        style: str = "neutral",
        final: bool = False,
        turn_id: Optional[str] = None,
    ) -> SendResult:
        turn_id = self._turn_id_for(device_id) if turn_id is None else turn_id
        client = self._client_for(device_id)
        configured_phonemes = (self.config.extra or {}).get("pronunciation_phonemes")
        custom_phonemes = configured_phonemes if isinstance(configured_phonemes, Mapping) else None
        formatted = format_jibo_speech(text, kind=kind, style=style)
        pronunciation = normalize_for_speech(
            formatted,
            custom_phonemes=custom_phonemes,
        )
        chunks = prepare_for_jibo(pronunciation.esml)
        if not chunks:
            return SendResult(success=True, message_id="empty")
        # The completed exchange is delivered as ``turn.append`` below and
        # opens Be's transcript screen.  Sending a second, legacy TextView
        # here would cover that screen with the old single-message face and
        # make the richer UI look unused.  Speech remains native and
        # immediate; the transcript becomes the visual answer.
        last_id = None
        async with self._speak_locks.setdefault(device_id, asyncio.Lock()):
            for index, chunk in enumerate(chunks):
                spoken = normalize_for_speech(
                    chunk,
                    custom_phonemes=custom_phonemes,
                ).spoken_text
                try:
                    self._assert_turn_current(device_id, turn_id)
                    if self._tool_answer_pending(device_id, turn_id=turn_id):
                        raise NativeBridgeError('The final answer for this turn was already delivered', code='TURN_COMPLETED', retryable=False)
                    last = final and index == len(chunks) - 1
                    reply_ms = self._follow_up_timeout(device_id, text, turn_id) if last else None
                    paired = reply_ms is not None and self.has_capability('speech.speak_and_listen', device_id=device_id)
                    if paired:
                        self._mark_follow_up(device_id, turn_id)
                    result = await client.request(
                        'speech.speak_and_listen' if paired else 'speech.speak',
                        {
                            "text": spoken,
                            "spoken_text": spoken,
                            "esml": chunk,
                            "mode": "ssml",
                            "kind": kind,
                            "style": style,
                            "final": bool(last),
                            **({'listen_timeout_ms': reply_ms} if paired else {}),
                            "session_id": client.session_id,
                            'turn_id': turn_id,
                        },
                        timeout=90.0,
                    )
                    if isinstance(result, Mapping) and (result.get('stopped') or result.get('spoken') is False):
                        raise NativeBridgeError('This Jibo speech was cancelled', code='TURN_CANCELLED', retryable=False)
                    last_id = (result or {}).get("message_id") if isinstance(result, Mapping) else None
                except (NativeRemoteError, NativeBridgeError) as exc:
                    return SendResult(
                        success=False,
                        message_id=last_id,
                        error=str(exc),
                        retryable=_speech_retryable(exc, default=exc.retryable),
                    )
                except Exception as exc:
                    return SendResult(
                        success=False,
                        message_id=last_id,
                        error=str(exc),
                        retryable=_speech_retryable(exc),
                    )
                if index + 1 < len(chunks):
                    await asyncio.sleep(0.1)
        self._append_log(
            device_id,
            direction="outbound",
            method="chat.message",
            payload={"text": text, "final": final, "spoken_text": pronunciation.spoken_text},
        )
        return SendResult(success=True, message_id=last_id or "jibo-speech")

    def _delivered_answer(
        self, device_id: str, *, turn_id: Optional[str] = None,
    ) -> Optional[_DeliveredAnswer]:
        turn_id = self._turn_id_for(device_id) if turn_id is None else turn_id
        answer = self._delivered_answers.get(device_id)
        if answer is None or answer.turn_id != turn_id:
            return None
        if not turn_id and time.monotonic() > answer.unowned_deadline:
            return None
        return answer

    def _tool_answer_pending(self, device_id: str, *, turn_id: Optional[str] = None) -> bool:
        return self._delivered_answer(device_id, turn_id=turn_id) is not None

    async def _speak_explicit_on_loop(
        self, device_id: str, text: str, *, kind: str, style: str, final: bool, turn_id: str,
    ) -> SendResult:
        self._assert_turn_current(device_id, turn_id)
        if self._tool_answer_pending(device_id, turn_id=turn_id):
            return SendResult(success=False, error='The final answer for this turn was already delivered', retryable=False)
        result = await self._speak_on_loop(device_id, text, kind=kind, style=style, final=final, turn_id=turn_id)
        if not final or not result.success or result.message_id == 'empty':
            return result
        self._assert_turn_current(device_id, turn_id)
        plain = normalize_for_speech(text).spoken_text
        self._delivered_answers[device_id] = _DeliveredAnswer(turn_id, plain, time.monotonic() + 30.0)
        await self._maybe_listen_again(device_id, plain, turn_id=turn_id)
        self._assert_turn_current(device_id, turn_id)
        self._processing_stages.pop(device_id, None)
        active = self._active_turns.get(device_id) or {}
        # Publish speech and text together, without waiting for another model
        # request. Keep host ownership until send/cancel closes the agent task.
        try:
            await self.record_turn(TurnRecord(
                turn_id=turn_id or new_turn_id(ORIGIN_HERMES), device_id=device_id,
                origin=str(active.get('origin') or ORIGIN_HERMES),
                transcript=str(active.get('text') or ''), response=plain,
                speech_owner=SPEECH_HERMES,
                started_at=str(active.get('started_at') or datetime.now(timezone.utc).isoformat()),
            ), owner_turn_id=turn_id)
        except Exception:
            # A projection/storage failure cannot make already audible speech
            # safe to repeat. Normal turn completion can retry the projection.
            logger.warning('Could not render explicitly delivered answer for %s', device_id, exc_info=True)
        return result

    def speak_explicit(
        self,
        text: str,
        *,
        kind: str = "answer",
        style: str = "neutral",
        final: bool = False,
        device_id: str | None = None,
    ) -> SendResult:
        target = self.resolve_device_id(device_id, require_connected=False)
        turn_id = self._turn_id_for(target)
        if final:
            self.finish_processing(success=True, device_id=target)
        return self._run_native_operation(
            lambda: self._speak_explicit_on_loop(target, text, kind=kind, style=style, final=final, turn_id=turn_id),
            timeout=95.0,
            timeout_retryable=False,
        )

    async def send_or_update_status(
        self,
        chat_id: str,
        status_key: str,
        content: str,
        *,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Put a gateway status notice on the screen, never into speech.

        The gateway routes agent status callbacks here when an adapter offers
        this hook (#30045).  Jibo has one text surface and speaks everything it
        shows, so delivering a status through :meth:`send` made the robot say
        the same thing twice on a failed turn: the retry notice first, then the
        turn's own final explanation.  The explanation is the one worth
        hearing.  What is worth showing here is the short live label beside the
        state dot, which already carries the working beat.

        Deliberately silent while no turn is in flight: a late notice must not
        light the ring or the status line after the screen has gone back to
        Ready.
        """
        del metadata
        try:
            target = self.resolve_device_id(chat_id=chat_id, require_connected=False)
        except NativeBridgeError as exc:
            return SendResult(success=False, error=str(exc))
        label = _status_screen_label(content)
        logger.debug("Jibo status notice (%s) shown as %r", status_key, label)
        turn_id = self._turn_id_for(target)
        if label and turn_id and self._turn_is_current(target, turn_id) and not self._tool_answer_pending(target, turn_id=turn_id):
            client = self._clients.get(target)
            if client is not None and client.connected:
                try:
                    await client.request(
                        CHAT_STATUS_METHOD,
                        {'state': 'thinking', 'detail': label, 'turn_id': turn_id},
                        timeout=8.0,
                    )
                except Exception:
                    logger.debug(
                        "Could not show a Jibo status notice on %s", target, exc_info=True
                    )
        return SendResult(success=True, message_id="jibo-status")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        del reply_to
        if isinstance(metadata, dict) and metadata.get("_interim_send"):
            return SendResult(success=True, message_id="suppressed-interim")
        target = self.resolve_device_id(chat_id=chat_id, require_connected=False)
        context = _NATIVE_TURN.get()
        if context is not None and context[0] is self and context[1] == target:
            turn_id = context[2]
        else:
            # Startup recovery and other out-of-band sends have no native
            # input owner. They must never adopt and finalize a new question.
            # Keep a busy recovery retryable in the gateway's delivery ledger.
            async with self._turn_lock(target):
                if self._turn_ids.get(target) or self._active_turns.get(target):
                    return SendResult(success=False, error='Jibo is handling an active conversation', retryable=True)
                turn_id = new_turn_id(ORIGIN_HERMES)
                standalone = self._begin_utterance(target, '', ORIGIN_HERMES, turn_id)
                # A boot sender may also deliver to other platforms. A newer
                # native question cancels this output, not the whole sender.
                standalone['dispatch_task'] = None
        if not self._turn_is_current(target, turn_id):
            return SendResult(success=False, error='This Jibo turn was cancelled or superseded', retryable=False)
        pairing_code = _pairing_code_from_message(content)
        approval_command = re.search(
            r"`(hermes (?:-p [A-Za-z0-9_.-]+ )?pairing approve jibo ([A-HJ-NP-Z2-9]{8}))`",
            content or "",
        )
        if (pairing_code and approval_command and approval_command.group(2) == pairing_code
                and self.has_capability("display.text", device_id=target)):
            # This is Hermes's separate first-speaker authorization, not the
            # robot's five-character transport exchange. Preserve the exact
            # profile-qualified command supplied by the gateway; do not invent
            # a default-profile command or approve anyone from the adapter.
            try:
                await self._clients[target].request("display.text", {
                    "text": "HERMES AUTHORIZATION\n" + pairing_code + "\nOn your computer:\n"
                        + approval_command.group(1)
                        + "\nSeparate from Jibo’s five-character transport code.",
                    "name": "hermes-authorization", "turn_id": turn_id,
                })
                self._assert_turn_current(target, turn_id)
            except NativeBridgeError as exc:
                return SendResult(success=False, error=str(exc), retryable=exc.retryable)
        active = self._active_turns.get(target) or {}
        if isinstance(metadata, dict) and metadata.get("_discard_failed_turn"):
            # A provider usage-limit reply is a one-off notice, not an answer
            # that belongs in the conversation. Speaking it through the normal
            # final-speech path gives the user one clear explanation and closes
            # the on-screen status, but deliberately skips _complete_turn():
            # otherwise the failed prompt gets written back into Hermes and is
            # unexpectedly answered after the provider recovers.
            self.finish_processing(success=False, device_id=target)
            self._streams.pop(target, None)
            logger.info("Discarding failed model turn for Jibo %s", target)
            result = await self._speak_on_loop(target, content, final=True, turn_id=turn_id)
            if self._turn_is_current(target, turn_id):
                self._active_turns.pop(target, None)
                self._turn_ids.pop(target, None)
                self._processing_stages.pop(target, None)
            return result
        escalation = self._pending_escalations.get(target)
        delivered = self._delivered_answer(target, turn_id=turn_id)
        tool_spoke = delivered is not None
        stream = self._streams.pop(target, None)
        if delivered is not None:
            # final=true is the user-facing answer. Later assistant text is
            # bookkeeping, and an empty one must not erase the visible reply.
            content = delivered.text
            stream = None

        # Whoever already said it, says it.  A jibo_speak tool call has
        # spoken the answer, and an escalation Phoenix will speak through its
        # own skill action must not be said twice.
        speak = not tool_spoke
        if escalation is not None and escalation.speech_owner != SPEECH_HERMES:
            speak = False

        if stream is not None:
            result = await self._finish_stream(target, stream, content, speak=speak)
        elif speak:
            logger.debug("speak[whole] %r", str(content)[:60])
            result = await self._speak_on_loop(target, content, final=True, turn_id=turn_id)
        elif tool_spoke:
            result = SendResult(success=True, message_id="spoken-by-jibo-tool")
        else:
            result = SendResult(success=True, message_id="jibo-recorded")

        if not self._turn_is_current(target, turn_id):
            return SendResult(success=False, error='This Jibo turn was cancelled or superseded', retryable=False)
        await self._complete_turn(
            target,
            content,
            escalation=escalation,
            spoken=bool(speak or tool_spoke),
            status=STATUS_OK if result.success else STATUS_FAILED,
            active=active,
            turn_id=turn_id,
        )
        if self._delivered_answers.get(target) is delivered:
            self._delivered_answers.pop(target, None)
        return result

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Normalize a remote image and append it to Jibo's chat history."""
        del reply_to, metadata
        url = _valid_native_image_url(image_url)
        if url is None:
            return SendResult(
                success=False,
                error="Jibo requires a direct HTTP(S) image URL",
                retryable=False,
            )
        try:
            target = self.resolve_device_id(chat_id=chat_id, require_connected=False)
            await self._present_remote_image_on_loop(
                target, url, name="hermes-image", caption=caption
            )
            return SendResult(success=True, message_id="jibo-image")
        except (NativeRemoteError, NativeBridgeError) as exc:
            return SendResult(
                success=False,
                error=str(exc),
                retryable=exc.retryable,
            )
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        """Normalize a local attachment and append it to Jibo's chat history."""
        del reply_to, metadata, kwargs
        safe_path = validate_media_delivery_path(str(image_path or ""))
        if not safe_path or not Path(safe_path).is_file():
            return SendResult(success=False, error="local image path is not available")
        try:
            target = self.resolve_device_id(chat_id=chat_id, require_connected=False)
            await self._present_local_image_on_loop(
                target, safe_path, name="hermes-image", caption=caption
            )
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

        return SendResult(success=True, message_id="jibo-image")

    def display_image(self, url: str, *, name: str = "hermes-image", device_id: str | None = None) -> SendResult:
        """Fetch an image on the host and keep it visible in Jibo's chat."""
        source = _valid_native_image_url(url)
        if source is None:
            return SendResult(
                success=False,
                error="Jibo requires a direct HTTP(S) image URL",
                retryable=False,
            )
        try:
            target = self.resolve_device_id(device_id, require_connected=True)
        except NativeBridgeError as exc:
            return SendResult(success=False, error=str(exc), retryable=exc.retryable)
        return self._run_native_operation(
            lambda: self._present_remote_image_on_loop(
                target,
                source,
                name=_safe_view_name(name, "hermes-image"),
                caption=None,
            ),
            timeout=JIBO_IMAGE_FETCH_TIMEOUT + 17.0,
        )

    def show_eye(self, *, name: str = "default", device_id: str | None = None) -> SendResult:
        target = self.resolve_device_id(device_id, require_connected=False)
        return self.native_call(
            "display.clear",
            {"name": _safe_view_name(name, "default"), "restore": "eye"},
            device_id=target,
            timeout=8.0,
        )

    def play_expression(self, name: str, *, text: str = "", device_id: str | None = None) -> SendResult:
        target = self.resolve_device_id(device_id, require_connected=False)
        return self.native_call(
            "expression.play",
            {"name": str(name).strip(), "text": str(text or "").strip()},
            device_id=target,
            timeout=30.0,
        )

    def display_local_image(
        self,
        image_path: str,
        *,
        name: str = "hermes-image",
        device_id: str | None = None,
    ) -> SendResult:
        safe_path = validate_media_delivery_path(str(image_path or ""))
        if not safe_path or not Path(safe_path).is_file():
            return SendResult(success=False, error="local image path is not available")
        try:
            target = self.resolve_device_id(device_id, require_connected=True)
        except NativeBridgeError as exc:
            return SendResult(success=False, error=str(exc), retryable=exc.retryable)
        try:
            return self._run_native_operation(
                lambda: self._present_local_image_on_loop(
                    target,
                    safe_path,
                    name=_safe_view_name(name, "hermes-image"),
                    caption=None,
                ),
                timeout=20.0,
            )
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

    async def _present_remote_image_on_loop(
        self,
        device_id: str,
        source: str,
        *,
        name: str,
        caption: Optional[str],
        owner_turn_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Fetch a remote image off-robot, then render its local LAN copy."""
        owner_turn_id = self._turn_id_for(device_id) if owner_turn_id is None else owner_turn_id
        self._assert_turn_current(device_id, owner_turn_id)
        if self._is_own_media_url(source):
            return await self._append_image_turn(device_id, source, caption=caption, owner_turn_id=owner_turn_id)
        local_path = await asyncio.to_thread(self._fetch_remote_image, source)
        self._assert_turn_current(device_id, owner_turn_id)
        return await self._present_local_image_on_loop(
            device_id, local_path, name=name, caption=caption, owner_turn_id=owner_turn_id
        )

    async def _present_local_image_on_loop(
        self,
        device_id: str,
        image_path: str,
        *,
        name: str,
        caption: Optional[str],
        owner_turn_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Re-encode one host image under Be's attachment limit and append it."""
        del name
        owner_turn_id = self._turn_id_for(device_id) if owner_turn_id is None else owner_turn_id
        self._assert_turn_current(device_id, owner_turn_id)
        normalized_path = await asyncio.to_thread(self._normalize_image, image_path)
        self._assert_turn_current(device_id, owner_turn_id)
        media_url = await asyncio.to_thread(self._local_media_url, normalized_path, device_id)
        self._assert_turn_current(device_id, owner_turn_id)
        return await self._append_image_turn(device_id, media_url, caption=caption, owner_turn_id=owner_turn_id)

    async def _append_image_turn(
        self, device_id: str, media_url: str, *, caption: Optional[str], owner_turn_id: str = ''
    ) -> dict[str, Any]:
        """Render a served image in the persistent transcript, not a transient view."""
        self._assert_turn_current(device_id, owner_turn_id)
        label = str(caption or "Image").strip()[:120] or "Image"
        record = TurnRecord(
            turn_id=new_turn_id(ORIGIN_HERMES),
            device_id=device_id,
            origin=ORIGIN_HERMES,
            response=str(caption or "").strip()[:2000],
            media=({"kind": "image", "url": media_url, "alt": label},),
        )
        outcome = await self.record_turn(record, final=False, owner_turn_id=owner_turn_id)
        if not outcome.get("rendered"):
            raise NativeBridgeError("Jibo could not render the image attachment", retryable=True)
        return {"displayed": True, "turn_id": record.turn_id, "url": media_url}

    def _normalize_image(self, image_path: str) -> str:
        source = Path(str(image_path or "")).expanduser().resolve()
        if not source.is_file():
            raise ValueError("local image path is not available")
        return str(
            letterbox_image(
                str(source),
                self._media_cache_directory(),
                max_bytes=JIBO_MAX_MEDIA_BYTES,
            )
        )

    def _is_own_media_url(self, url: str) -> bool:
        """True for a URL already served by this adapter's media server."""
        with self._media_lock:
            server = self._media_server
        if server is None:
            return False
        parsed = urlsplit(url)
        return (
            parsed.path.startswith("/jibo-media/")
            and str(server.server_port) in (parsed.netloc or "")
        )

    def _fetch_remote_image(self, url: str) -> str:
        """Download one image on the host and normalize it for Jibo's screen."""
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("only http(s) image URLs can be displayed")
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": JIBO_IMAGE_USER_AGENT,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(request, timeout=JIBO_IMAGE_FETCH_TIMEOUT) as response:
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type and not content_type.startswith("image/"):
                raise ValueError(f"the URL returned {content_type}, not an image")
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    announced_bytes = int(content_length)
                except ValueError:
                    announced_bytes = 0
                if announced_bytes > JIBO_IMAGE_FETCH_MAX_BYTES:
                    raise ValueError("the source image is larger than 16MB")
            payload = response.read(JIBO_IMAGE_FETCH_MAX_BYTES + 1)
        if not payload:
            raise ValueError("the URL returned an empty body")
        if len(payload) > JIBO_IMAGE_FETCH_MAX_BYTES:
            raise ValueError("the source image is larger than 16MB")
        cache_dir = self._media_cache_directory()
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        raw_path = cache_dir / f"remote-{digest}.img"
        raw_path.write_bytes(payload)
        try:
            return self._normalize_image(str(raw_path))
        finally:
            try:
                raw_path.unlink()
            except OSError:
                pass

    def _media_host_address(self, device_id: str) -> str:
        extra = self.config.extra or {}
        configured = str(extra.get("media_host") or "").strip()
        if configured:
            return configured
        config = self._config_for(device_id)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((config.host, config.port))
            address = probe.getsockname()[0]
        if not address or address.startswith("127."):
            raise RuntimeError("could not determine a LAN address reachable by Jibo")
        return address

    def _media_cache_directory(self) -> Path:
        with self._media_lock:
            if self._media_cache_dir is None:
                hermes_home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()
                root = hermes_home / "cache" / "jibo-images"
                root.mkdir(parents=True, exist_ok=True)
                self._media_cache_dir = Path(tempfile.mkdtemp(prefix=f"{os.getpid()}-", dir=str(root)))
            return self._media_cache_dir

    def _serve_media_file(self, file_path: str) -> str:
        with self._media_lock:
            if self._media_server is None:
                self._media_server = _JiboMediaHTTPServer(("0.0.0.0", 0))
                self._media_server_thread = threading.Thread(
                    target=self._media_server.serve_forever,
                    name="jibo-native-media",
                    daemon=True,
                )
                self._media_server_thread.start()
            token = secrets.token_urlsafe(18)
            self._media_server.files[token] = file_path
            return token

    def _local_media_url(self, image_path: str, device_id: str) -> str:
        token = self._serve_media_file(image_path)
        with self._media_lock:
            server = self._media_server
            if server is None:
                raise RuntimeError("native media server is unavailable")
            return f"http://{self._media_host_address(device_id)}:{server.server_port}/jibo-media/{token}"

    def _stop_media_server(self) -> None:
        with self._media_lock:
            server = self._media_server
            thread = self._media_server_thread
            self._media_server = None
            self._media_server_thread = None
            self._media_cache_dir = None
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _device_for_session(self, session_id: str | None = None, device_id: str | None = None) -> str:
        return self.resolve_device_id(device_id, session_id=session_id, require_connected=False)

    def begin_processing(
        self,
        *,
        turn_id: str,
        user_message: str = "",
        session_id: str | None = None,
        device_id: str | None = None,
    ) -> bool:
        del turn_id, user_message
        try:
            target = self._device_for_session(session_id, device_id)
        except NativeBridgeError:
            return False
        self._processing_stages[target] = "THINKING"
        return self._submit_background(self._set_processing_stage_async(target, "THINKING"))

    def update_processing_stage(
        self,
        stage: str,
        *,
        session_id: str | None = None,
        device_id: str | None = None,
    ) -> bool:
        try:
            target = self._device_for_session(session_id, device_id)
        except NativeBridgeError:
            return False
        normalized = str(stage or "PROCESSING").upper()
        turn_id = self._turn_id_for(target)
        if not turn_id or not self._turn_is_current(target, turn_id) or self._tool_answer_pending(target, turn_id=turn_id):
            return False
        self._processing_stages[target] = normalized
        return self._submit_background(self._set_processing_stage_async(target, normalized, turn_id=turn_id))

    def finish_processing(
        self,
        *,
        success: bool,
        session_id: str | None = None,
        device_id: str | None = None,
    ) -> bool:
        try:
            target = self._device_for_session(session_id, device_id)
        except NativeBridgeError:
            return False
        turn_id = self._turn_id_for(target)
        if not turn_id or not self._turn_is_current(target, turn_id) or self._tool_answer_pending(target, turn_id=turn_id):
            return False
        self._processing_stages.pop(target, None)
        return self._submit_background(self._set_processing_stage_async(target, 'ANSWERING' if success else 'ERROR', turn_id=turn_id))

    async def _set_processing_stage_async(self, device_id: str, stage: str, *, turn_id: Optional[str] = None) -> None:
        turn_id = self._turn_id_for(device_id) if turn_id is None else turn_id
        if not turn_id or not self._turn_is_current(device_id, turn_id) or self._tool_answer_pending(device_id, turn_id=turn_id):
            return
        client = self._clients.get(device_id)
        if client is None or not client.connected:
            return
        try:
            # The full Hermes chat is the sole text surface for a turn.  Old
            # builds drew phase labels with display.text, repeatedly replacing
            # that chat.  Keep phase information as the chat's small live
            # state instead.
            state = "speaking" if stage == "ANSWERING" else ("idle" if stage == "ERROR" else "thinking")
            await client.request(
                CHAT_STATUS_METHOD,
                {'state': state, 'turn_id': turn_id},
                timeout=8.0,
            )
        except Exception:
            logger.debug("Native Jibo processing status failed for %s", device_id, exc_info=True)

    def _submit_background(self, coroutine) -> bool:
        loop = self._loop
        if loop is None or not loop.is_running():
            coroutine.close()
            return False
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            task = asyncio.create_task(coroutine, name="jibo-native-background")
            task.add_done_callback(self._log_background_future)
            return True
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except Exception:
            coroutine.close()
            return False
        future.add_done_callback(self._log_background_future)
        return True

    @staticmethod
    def _log_background_future(future) -> None:
        try:
            future.result()
        except (asyncio.CancelledError, concurrent.futures.CancelledError):
            return
        except Exception:
            logger.debug("Native Jibo background operation failed", exc_info=True)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        del chat_id, metadata
        return None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        target = self.resolve_device_id(chat_id=chat_id, require_connected=False)
        config = self._config_for(target)
        return {"name": config.name, "type": "dm", "robot": target, "transport": NATIVE_MODE}
