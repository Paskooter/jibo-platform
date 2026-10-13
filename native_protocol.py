"""Native Hermes-to-Be WebSocket transport for the Jibo platform.

The legacy plugin talks to the ROM command socket.  This module is the small,
independent transport used by the native POC: one reconnecting WebSocket per
configured robot, a versioned JSON envelope, and correlated requests.  It is
kept deliberately free of Hermes Relay so the same client can talk to a fake
peer in unit tests and to the custom Be service on a robot.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
import inspect
import json
import logging
import os
import re
import time
from typing import Any, Optional
import uuid

import websockets

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
DEFAULT_NATIVE_PORT = 8890
DEFAULT_NATIVE_PATH = "/hermes"
DEFAULT_REQUEST_TIMEOUT = 15.0
DEFAULT_CONNECT_TIMEOUT = 10.0
MAX_FRAME_BYTES = 4 * 1024 * 1024
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# Input routing is part of the native bridge policy, rather than a second
# Hermes/NLU protocol.  Be should run Global Listen, Jetstream/ASR, and its
# local NLU first.  Only an unhandled utterance is forwarded as a final
# ``input.transcript`` with ``route=hermes_fallback``.  The legacy value is
# retained for peers from before this signal existed and is deliberately
# visible in logs so it can be removed after all Be peers are upgraded.
INPUT_ROUTE_LOCAL = "local_handled"
INPUT_ROUTE_HERMES_FALLBACK = "hermes_fallback"
INPUT_ROUTE_LEGACY_FALLBACK = "legacy_implicit_fallback"
INPUT_ROUTE_UNKNOWN = "unknown"
LOCAL_INPUT_EVENT_METHODS = frozenset(
    {
        "input.local_handled",
        "input.intent_handled",
        "nlu.local_handled",
        "local.intent",
    }
)
_LOCAL_ROUTE_VALUES = frozenset(
    {"local", "local_handled", "handled_locally", "local_nlu", "local_intent", "jibo_local"}
)
_FALLBACK_ROUTE_VALUES = frozenset(
    {"fallback", "hermes", "hermes_fallback", "forward", "forward_to_hermes", "unhandled"}
)


class NativeBridgeError(Exception):
    """Base error for failures on the native bridge."""

    code = "NATIVE_BRIDGE_ERROR"
    retryable = True

    def __init__(self, message: str, *, code: str | None = None, retryable: bool | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable


class NativeDisconnected(NativeBridgeError):
    code = "DISCONNECTED"
    retryable = True


class NativeProtocolError(NativeBridgeError):
    code = "INVALID_MESSAGE"
    retryable = False


class NativeRemoteError(NativeBridgeError):
    """An error returned by the Be-side request handler."""

    def __init__(self, error: Mapping[str, Any]):
        code = str(error.get("code") or "REMOTE_ERROR")
        message = str(error.get("message") or code)
        retryable = bool(error.get("retryable", False))
        super().__init__(message, code=code, retryable=retryable)
        self.error = dict(error)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        return max(minimum, min(float(value), maximum))
    except (TypeError, ValueError):
        return default


def _slug(value: str, fallback: str = "jibo") -> str:
    candidate = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-._")
    candidate = candidate[:64] or fallback
    if not candidate[0].isalnum():
        candidate = f"jibo-{candidate}"[:64]
    return candidate


def _device_id(value: Any, fallback: str) -> str:
    candidate = _slug(str(value or ""), fallback)
    if not _DEVICE_ID_RE.fullmatch(candidate):
        raise ValueError(
            "Jibo device ids must start with a letter or number and contain "
            "only letters, numbers, '.', '_' or '-'."
        )
    return candidate


@dataclass(frozen=True)
class NativeDeviceConfig:
    """Validated host-side settings for one Jibo native bridge."""

    id: str
    name: str
    host: str
    port: int = DEFAULT_NATIVE_PORT
    token: str = field(default="", repr=False)
    enabled: bool = True
    path: str = DEFAULT_NATIVE_PATH
    tls: bool = False
    allow_empty_token: bool = False
    session_id: str = ""
    session_title: str = "Hermes"
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    reconnect_initial: float = 2.0
    reconnect_max: float = 30.0
    #: Which model answers on this robot. Empty means the Hermes default.
    #: It lives on the device so every surface -- the dashboard, the robot's
    #: own screen -- reads and writes the one value.
    model: str = ""
    model_provider: str = ""
    # Local id is a user alias; only a pair-returned identity is authoritative.
    # Empty retains legacy token/alias compatibility without silently pinning it.
    robot_device_id: str = ""

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        fallback_id: str = "jibo",
        default_port: int = DEFAULT_NATIVE_PORT,
        default_token: str = "",
        default_enabled: bool = True,
        default_allow_empty_token: bool = False,
    ) -> "NativeDeviceConfig":
        if not isinstance(value, Mapping):
            raise ValueError("each Jibo device must be an object")
        host = str(value.get("host") or "").strip()
        if not host:
            raise ValueError("each Jibo device needs a host or IP address")
        device_id = _device_id(value.get("id"), fallback_id)
        name = str(value.get("name") or device_id).strip()[:120] or device_id
        path = str(value.get("path") or DEFAULT_NATIVE_PATH).strip()
        if not path.startswith("/"):
            path = "/" + path
        path = "/" + "/".join(part for part in path.split("/") if part)
        if path == "/":
            path = DEFAULT_NATIVE_PATH
        session_id = str(value.get("session_id") or f"jibo:{device_id}").strip()
        session_title = str(value.get("session_title") or "Hermes").strip()[:120] or "Hermes"
        robot_device_id = value.get("robot_device_id", "")
        if not isinstance(robot_device_id, str) or (robot_device_id and not _DEVICE_ID_RE.fullmatch(robot_device_id)):
            raise ValueError("robot_device_id must be an exact valid robot identity")
        return cls(
            id=device_id,
            name=name,
            host=host,
            port=_bounded_int(value.get("port", default_port), default_port, 1, 65535),
            token=str(value.get("token") if value.get("token") is not None else default_token),
            enabled=_as_bool(value.get("enabled"), default_enabled),
            path=path,
            tls=_as_bool(value.get("tls"), False),
            allow_empty_token=_as_bool(
                value.get("allow_empty_token"), default_allow_empty_token
            ),
            session_id=session_id,
            session_title=session_title,
            request_timeout=_bounded_float(
                value.get("request_timeout"), DEFAULT_REQUEST_TIMEOUT, 1.0, 120.0
            ),
            connect_timeout=_bounded_float(
                value.get("connect_timeout"), DEFAULT_CONNECT_TIMEOUT, 1.0, 60.0
            ),
            reconnect_initial=_bounded_float(
                value.get("reconnect_initial"), 2.0, 0.1, 60.0
            ),
            reconnect_max=_bounded_float(
                value.get("reconnect_max"), 30.0, 1.0, 300.0
            ),
            robot_device_id=robot_device_id,
            model=str(value.get("model") or "").strip()[:120],
            model_provider=str(value.get("model_provider") or value.get("provider") or "").strip()[:60],
        )

    @property
    def uri(self) -> str:
        scheme = "wss" if self.tls else "ws"
        return f"{scheme}://{self.host}:{self.port}{self.path}"

    def to_dict(self, *, include_token: bool = False) -> dict[str, Any]:
        """Return dashboard/config data without exposing the token by default."""
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "enabled": self.enabled,
            "path": self.path,
            "tls": self.tls,
            "allow_empty_token": self.allow_empty_token,
            "session_id": self.session_id,
            "session_title": self.session_title,
            "request_timeout": self.request_timeout,
            "connect_timeout": self.connect_timeout,
            "reconnect_initial": self.reconnect_initial,
            "reconnect_max": self.reconnect_max,
        }
        if self.robot_device_id:
            data["robot_device_id"] = self.robot_device_id
        if self.model:
            data["model"] = self.model
        if self.model_provider:
            data["model_provider"] = self.model_provider
        if include_token:
            data["token"] = self.token
        elif self.token:
            data["token_configured"] = True
        return data


def parse_device_configs(
    extra: Mapping[str, Any] | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> list[NativeDeviceConfig]:
    """Parse the multi-device config, retaining the old single-host shape.

    Native mode accepts ``extra.devices`` as either a list of objects or a
    mapping keyed by id.  A top-level ``host`` is intentionally still
    accepted as a one-device convenience so an operator can migrate without
    changing every surrounding gateway setting.
    """
    values = dict(extra or {})
    env = os.environ if environ is None else environ
    default_port = _bounded_int(
        values.get("native_port", values.get("bridge_port", DEFAULT_NATIVE_PORT)),
        DEFAULT_NATIVE_PORT,
        1,
        65535,
    )
    default_token = str(
        values.get("token")
        if values.get("token") is not None
        else env.get("JIBO_NATIVE_TOKEN") or env.get("HERMES_JIBO_TOKEN") or ""
    )
    default_allow_empty = _as_bool(values.get("allow_empty_token"), False)
    raw_devices = values.get("devices")
    entries: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(raw_devices, Mapping):
        for key, item in raw_devices.items():
            if not isinstance(item, Mapping):
                raise ValueError(f"Jibo device {key!r} must be an object")
            merged = dict(item)
            merged.setdefault("id", key)
            entries.append((str(key), merged))
    elif isinstance(raw_devices, (list, tuple)):
        for index, item in enumerate(raw_devices):
            if not isinstance(item, Mapping):
                raise ValueError(f"Jibo devices[{index}] must be an object")
            entries.append((str(item.get("id") or f"jibo-{index + 1}"), item))
    else:
        host = str(values.get("host") or env.get("JIBO_HOST") or "").strip()
        if host:
            entries.append(
                (
                    str(values.get("device_id") or _slug(host)),
                    {
                        "id": values.get("device_id") or _slug(host),
                        "name": values.get("name") or host,
                        "host": host,
                        "port": values.get("port", default_port),
                        "token": default_token,
                        "enabled": values.get("enabled", True),
                        "path": values.get("path", DEFAULT_NATIVE_PATH),
                        "tls": values.get("tls", False),
                        "allow_empty_token": values.get(
                            "allow_empty_token", default_allow_empty
                        ),
                    },
                )
            )

    result: list[NativeDeviceConfig] = []
    seen: set[str] = set()
    for index, (fallback, item) in enumerate(entries):
        merged = dict(item)
        merged.setdefault("token", default_token)
        merged.setdefault("port", default_port)
        merged.setdefault("allow_empty_token", default_allow_empty)
        config = NativeDeviceConfig.from_mapping(
            merged,
            fallback_id=_slug(fallback, f"jibo-{index + 1}"),
            default_port=default_port,
            default_token=default_token,
            default_allow_empty_token=default_allow_empty,
        )
        if config.id in seen:
            raise ValueError(f"duplicate Jibo device id: {config.id}")
        seen.add(config.id)
        result.append(config)
    return result


def configured_device_configs(extra: Mapping[str, Any] | None) -> list[NativeDeviceConfig]:
    """Parse config without letting a malformed entry escape to the loader."""
    try:
        return parse_device_configs(extra)
    except ValueError:
        return []


def make_request(
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    request_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Create one contract-compliant request envelope."""
    envelope: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "kind": "request",
        "id": request_id or uuid.uuid4().hex,
        "method": str(method),
        "params": dict(params or {}),
    }
    if session_id:
        envelope["session_id"] = str(session_id)
    return envelope


async def exchange_pairing_code(
    config: NativeDeviceConfig,
    code: str,
    *,
    client_version: str = "0.2.0",
    websocket_factory: WebSocketFactory | None = None,
) -> dict[str, Any]:
    """Exchange the code shown by a Jibo for its stored native token.

    Pairing deliberately uses a short-lived, unauthenticated connection.  It
    never starts a Hermes session and it closes immediately after receiving
    the one-time result.  The long token is returned only to the caller so the
    dashboard can write it into Hermes configuration without displaying it.
    """
    if not isinstance(code, str) or not re.fullmatch(r"[A-HJ-NP-Z2-9]{5}", code, re.IGNORECASE | re.ASCII):
        raise NativeBridgeError(
            "enter exactly five characters from Agent Mode → Pairing (no spaces or punctuation)",
            code="PAIRING_CODE_REQUIRED",
            retryable=False,
        )

    options = {
        "ping_interval": None,
        "ping_timeout": None,
        "open_timeout": config.connect_timeout,
        "close_timeout": 3,
        "max_size": MAX_FRAME_BYTES,
    }
    factory = websocket_factory or websockets.connect
    ws: Any = None
    try:
        connection = factory(config.uri, **options)
        ws = await connection if inspect.isawaitable(connection) else connection
        request = make_request(
            "pair.exchange",
            {
                "code": code.upper(),
                "client": "hermes-jibo",
                "client_version": client_version,
            },
        )
        await ws.send(json.dumps(request, separators=(",", ":")))
        raw = await asyncio.wait_for(ws.recv(), timeout=config.connect_timeout)
        frame = decode_frame(raw)
        if frame.get("kind") != "response" or frame.get("method") != "pair.exchange":
            raise NativeProtocolError("pairing response has an unexpected shape")
        error = frame.get("error")
        if isinstance(error, Mapping):
            raise NativeRemoteError(error)
        result = frame.get("result")
        if not isinstance(result, Mapping) or not str(result.get("token") or "").strip():
            raise NativeProtocolError("pairing response did not contain a secure token")
        return dict(result)
    finally:
        if ws is not None:
            close = getattr(ws, "close", None)
            if callable(close):
                try:
                    returned = close()
                    if inspect.isawaitable(returned):
                        await returned
                except Exception:
                    logger.debug("native pairing socket close failed")


def decode_frame(raw: Any) -> dict[str, Any]:
    """Decode and minimally validate one incoming JSON frame."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_FRAME_BYTES:
            raise NativeProtocolError("native frame is too large")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise NativeProtocolError("native frame is not UTF-8") from exc
    if isinstance(raw, str) and len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
        raise NativeProtocolError("native frame is too large")
    if isinstance(raw, Mapping):
        value = dict(raw)
    else:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise NativeProtocolError("native frame is not valid JSON") from exc
    if not isinstance(value, dict):
        raise NativeProtocolError("native frame must be a JSON object")
    if value.get("v") != PROTOCOL_VERSION:
        raise NativeProtocolError("unsupported native protocol version")
    if value.get("kind") not in {"request", "response", "event"}:
        raise NativeProtocolError("native frame has an invalid kind")
    if not isinstance(value.get("id"), str) or not value["id"].strip():
        raise NativeProtocolError("native frame needs a non-empty id")
    if value["kind"] in {"request", "response"} and not str(value.get("method") or "").strip():
        raise NativeProtocolError("native request/response needs a method")
    return value


def classify_input_route(frame: Mapping[str, Any]) -> str:
    """Classify a Be input event without changing its wire envelope.

    The recommended signal is ``params.route`` on ``input.transcript``.  A
    peer may also use the top-level route or the explicit boolean aliases;
    accepting those forms makes the bridge tolerant while the custom Be
    service is being staged.  Missing route data is a compatibility fallback,
    not an assertion that the utterance was unhandled.
    """
    method = str(frame.get("method") or "").strip().lower()
    params = frame.get("params")
    values: Mapping[str, Any] = params if isinstance(params, Mapping) else {}
    if method in LOCAL_INPUT_EVENT_METHODS:
        return INPUT_ROUTE_LOCAL

    handled_locally = values.get("handled_locally", values.get("local_handled"))
    if handled_locally is True:
        return INPUT_ROUTE_LOCAL
    if values.get("forward_to_hermes") is False:
        return INPUT_ROUTE_LOCAL
    if values.get("forward_to_hermes") is True:
        return INPUT_ROUTE_HERMES_FALLBACK

    route: Any = values.get("route")
    if route is None:
        route = values.get("dispatch", values.get("policy", frame.get("route")))
    if isinstance(route, Mapping):
        route = route.get("route", route.get("target", route.get("decision")))
    normalized = str(route or "").strip().lower().replace("-", "_")
    if normalized in _LOCAL_ROUTE_VALUES:
        return INPUT_ROUTE_LOCAL
    if normalized in _FALLBACK_ROUTE_VALUES:
        return INPUT_ROUTE_HERMES_FALLBACK
    if not normalized:
        return INPUT_ROUTE_LEGACY_FALLBACK
    return INPUT_ROUTE_UNKNOWN


EventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]
StateCallback = Callable[[dict[str, Any]], None]
WebSocketFactory = Callable[..., Any]


class NativeJiboClient:
    """One reconnecting, correlated WebSocket client for one Jibo."""

    def __init__(
        self,
        config: NativeDeviceConfig,
        *,
        on_event: EventCallback | None = None,
        on_state: StateCallback | None = None,
        websocket_factory: WebSocketFactory | None = None,
        client_version: str = "0.1.0",
    ):
        self.config = config
        self._on_event = on_event
        self._on_state = on_state
        self._websocket_factory = websocket_factory or websockets.connect
        self._client_version = client_version
        self._ws: Any = None
        self._run_task: asyncio.Task | None = None
        self._reader_task: asyncio.Task | None = None
        self._event_tasks: set[asyncio.Task] = set()
        self._stop_event = asyncio.Event()
        self._transport_ready = asyncio.Event()
        self._connected = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._session_id = config.session_id or f"jibo:{config.id}"
        self._session_state: dict[str, Any] = {}
        self._device_info: dict[str, Any] = {}
        self._capabilities: tuple[str, ...] = ()
        self._seq = 0
        self._state: dict[str, Any] = {
            "device_id": config.id,
            "name": config.name,
            "host": config.host,
            "port": config.port,
            "uri": config.uri,
            "enabled": config.enabled,
            "connected": False,
            "connecting": False,
            "last_error": None,
            "last_error_code": None,
            "last_connected_at": None,
            "reconnect_count": 0,
            "capabilities": [],
            "session_id": self._session_id,
        }

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def capabilities(self) -> tuple[str, ...]:
        return self._capabilities

    @property
    def device_info(self) -> dict[str, Any]:
        return dict(self._device_info)

    @property
    def session_state(self) -> dict[str, Any]:
        return dict(self._session_state)

    @property
    def state(self) -> dict[str, Any]:
        result = dict(self._state)
        result["capabilities"] = list(self._capabilities)
        result["session_id"] = self._session_id
        return result

    def _set_state(self, **changes: Any) -> None:
        self._state.update(changes)
        if self._on_state is not None:
            try:
                self._on_state(self.state)
            except Exception:
                logger.debug("native Jibo state callback failed", exc_info=True)

    async def start(self) -> None:
        """Start the reconnect loop; safe to call more than once."""
        if self._run_task is not None and not self._run_task.done():
            return
        self._stop_event.clear()
        self._run_task = asyncio.create_task(
            self._run(), name=f"jibo-native-{self.config.id}"
        )

    async def wait_until_connected(self, timeout: float | None = None) -> bool:
        """Wait for auth + session.open, returning False on timeout."""
        if self.connected:
            return True
        wait_for = self.config.connect_timeout if timeout is None else max(0.0, timeout)
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=wait_for)
        except asyncio.TimeoutError:
            return False
        return self.connected

    def _safe_error_text(self, error: Any) -> str:
        text = str(error)
        return text.replace(self.config.token, "[redacted]") if self.config.token else text

    async def _run(self) -> None:
        backoff = self.config.reconnect_initial
        while not self._stop_event.is_set():
            try:
                await self._connect_once()
                backoff = self.config.reconnect_initial
                reader = self._reader_task
                if reader is not None:
                    await reader
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._stop_event.is_set():
                    self._set_state(
                        connected=False,
                        connecting=False,
                        last_error=self._safe_error_text(exc),
                        last_error_code=self._safe_error_text(getattr(exc, "code", "CONNECT_FAILED")),
                    )
                    logger.warning(
                        "Native Jibo connection failed for %s: %s; retrying in %.1fs",
                        self.config.id,
                        self._safe_error_text(exc),
                        backoff,
                    )
                    if isinstance(exc, NativeBridgeError) and not exc.retryable:
                        # Configuration/authentication failures cannot be
                        # repaired by a hot reconnect loop.  Keep the client
                        # stopped until the gateway is restarted or its
                        # configuration is reloaded.
                        break
            finally:
                await self._cleanup_connection()
            if self._stop_event.is_set():
                break
            self._set_state(
                connected=False,
                connecting=False,
                reconnect_count=int(self._state.get("reconnect_count") or 0) + 1,
            )
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                backoff = min(backoff * 2.0, self.config.reconnect_max)

    async def _connect_once(self) -> None:
        if not self.config.allow_empty_token and not self.config.token:
            raise NativeBridgeError(
                "native Jibo token is not configured; set token or explicitly "
                "enable allow_empty_token for a development peer",
                code="TOKEN_REQUIRED",
                retryable=False,
            )
        self._set_state(connecting=True, last_error=None, last_error_code=None)
        options = {
            "ping_interval": None,
            "ping_timeout": None,
            "open_timeout": self.config.connect_timeout,
            "close_timeout": 3,
            "max_size": MAX_FRAME_BYTES,
        }
        connection = self._websocket_factory(self.config.uri, **options)
        ws = await connection if inspect.isawaitable(connection) else connection
        self._ws = ws
        self._transport_ready.set()
        self._reader_task = asyncio.create_task(
            self._reader(ws), name=f"jibo-native-reader-{self.config.id}"
        )
        auth_result = await self._request(
            "auth",
            {
                "token": self.config.token,
                "client": "hermes-jibo",
                "client_version": self._client_version,
            },
            session_id="",
            timeout=self.config.connect_timeout,
        )
        if not isinstance(auth_result, Mapping):
            raise NativeProtocolError("native auth response result must be an object")
        advertised_id = auth_result.get("device_id")
        if self.config.robot_device_id and advertised_id != self.config.robot_device_id:
            raise NativeBridgeError(
                "authenticated robot identity does not match the saved pairing identity",
                code="DEVICE_ID_MISMATCH", retryable=False,
            )
        self._device_info = dict(auth_result)
        advertised_id = str(advertised_id or self.config.id).strip()
        if advertised_id and advertised_id != self.config.id:
            logger.info(
                "Native Jibo %s authenticated as advertised device %s",
                self.config.id,
                advertised_id,
            )
        capabilities = auth_result.get("capabilities") or []
        if isinstance(capabilities, Mapping):
            capabilities = list(capabilities)
        if not isinstance(capabilities, (list, tuple, set)):
            capabilities = []
        self._capabilities = tuple(
            sorted({str(item).strip() for item in capabilities if str(item).strip()})
        )
        session_result = await self._request(
            "session.open",
            {"session_id": self._session_id, "title": self.config.session_title},
            timeout=self.config.request_timeout,
        )
        if (not isinstance(session_result, Mapping)
                or session_result.get("status") != "active"
                or session_result.get("session_id") != self._session_id):
            raise NativeProtocolError("native session.open did not confirm the requested active session")
        self._session_state = dict(session_result)
        self._connected.set()
        self._set_state(
            connected=True,
            connecting=False,
            last_connected_at=time.time(),
            capabilities=list(self._capabilities),
            device_info=dict(self._device_info),
        )

    async def _reader(self, ws: Any) -> None:
        try:
            async for raw in ws:
                try:
                    frame = decode_frame(raw)
                except NativeProtocolError as exc:
                    logger.debug("Ignoring invalid native frame from %s: %s", self.config.id, exc)
                    continue
                kind = frame["kind"]
                if kind == "response":
                    request_id = frame["id"]
                    future = self._pending.get(request_id)
                    if future is None or future.done():
                        continue
                    error = frame.get("error")
                    if isinstance(error, Mapping):
                        future.set_exception(NativeRemoteError(error))
                    else:
                        future.set_result(frame.get("result"))
                    continue
                if kind == "event":
                    self._seq = max(self._seq, int(frame.get("seq") or 0))
                    task = asyncio.create_task(
                        self._dispatch_event(frame),
                        name=f"jibo-native-event-{self.config.id}",
                    )
                    self._event_tasks.add(task)
                    task.add_done_callback(self._event_tasks.discard)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._stop_event.is_set():
                logger.info("Native Jibo reader stopped for %s: %s", self.config.id, self._safe_error_text(exc))
        finally:
            self._connected.clear()
            self._transport_ready.clear()
            self._fail_pending(NativeDisconnected("native Jibo WebSocket closed"))

    async def _dispatch_event(self, frame: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            result = self._on_event(frame)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Native Jibo event callback failed", exc_info=True)

    async def _request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        if self._ws is None or not self._transport_ready.is_set():
            raise NativeDisconnected(f"native Jibo {self.config.id} is not connected")
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        envelope = make_request(
            method,
            params,
            request_id=request_id,
            session_id=self._session_id if session_id is None else session_id,
        )
        try:
            async with self._send_lock:
                await self._ws.send(json.dumps(envelope, separators=(",", ":")))
            result = await asyncio.wait_for(
                asyncio.shield(future),
                timeout=max(0.1, timeout if timeout is not None else self.config.request_timeout),
            )
            return result
        finally:
            self._pending.pop(request_id, None)

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
        session_id: str | None = None,
    ) -> Any:
        """Send one correlated native request after the handshake."""
        if not self.connected:
            raise NativeDisconnected(f"native Jibo {self.config.id} is not connected")
        return await self._request(method, params, timeout=timeout, session_id=session_id)

    async def notify(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> None:
        """Send a request frame without waiting for its response.

        Be accepts only ``kind: "request"`` inbound and answers every one of
        them, so a high-rate stream such as ``response.delta`` still travels
        as a request — it simply is not correlated.  Be's reply carries an id
        this client is not waiting on, and the reader already drops those, so
        no Be-side change is needed to make a frame fire-and-forget.

        Ordering is preserved because every send goes through the same lock,
        which is what a delta stream actually needs; what it does not need is
        a round trip per token.
        """
        if not self.connected:
            raise NativeDisconnected(f"native Jibo {self.config.id} is not connected")
        if self._ws is None or not self._transport_ready.is_set():
            raise NativeDisconnected(f"native Jibo {self.config.id} is not connected")
        envelope = make_request(
            method,
            params,
            session_id=self._session_id if session_id is None else session_id,
        )
        async with self._send_lock:
            await self._ws.send(json.dumps(envelope, separators=(",", ":")))

    def _fail_pending(self, error: Exception) -> None:
        pending = self._pending
        self._pending = {}
        for future in pending.values():
            if not future.done():
                future.set_exception(error)

    async def _cleanup_connection(self) -> None:
        self._connected.clear()
        self._transport_ready.clear()
        self._fail_pending(NativeDisconnected("native Jibo connection closed"))
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        reader = self._reader_task
        self._reader_task = None
        if reader is not None and reader is not asyncio.current_task():
            try:
                await asyncio.wait_for(reader, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                reader.cancel()
            except Exception:
                pass
        for task in list(self._event_tasks):
            task.cancel()
        if self._event_tasks:
            await asyncio.gather(*self._event_tasks, return_exceptions=True)
        self._event_tasks.clear()
        self._session_state = {}

    async def stop(self) -> None:
        """Stop reconnecting and close the current peer."""
        self._stop_event.set()
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        task = self._run_task
        if task is not None and task is not asyncio.current_task():
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except asyncio.CancelledError:
                pass
        self._run_task = None
        await self._cleanup_connection()
        self._set_state(connected=False, connecting=False)
