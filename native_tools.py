"""Bounded Hermes tools for the native Jibo capability surface.

Install/update the reviewed source with Hermes's standard plugin installer.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlsplit
from urllib.request import Request, urlopen

from .adapter import get_active_adapter


def _schema(name: str, description: str, properties: Mapping[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": dict(properties),
            "required": list(required or []),
            "additionalProperties": False,
        },
    }


_DEVICE = {
    "type": "string",
    "minLength": 1,
    "maxLength": 64,
    "description": "Optional configured Jibo id; omit to use the current/only robot.",
}

JIBO_STATUS_SCHEMA = _schema(
    "jibo_status",
    "Read native status from a connected Jibo, including advertised capabilities.",
    {"device_id": _DEVICE},
)
JIBO_CAPABILITIES_SCHEMA = _schema(
    "jibo_list_capabilities",
    "List the fine-grained native capabilities advertised by a connected Jibo.",
    {"device_id": _DEVICE},
)
JIBO_OBSERVE_SCHEMA = _schema(
    "jibo_observe",
    "Observe the robot's environment. For live visual questions use this to hold gaze through camera capture, "
    "save a real JPEG on the Hermes host and show it in the chat. The photo is attached directly for native vision "
    "when supported; otherwise follow next_step with vision_analyze on image.local_path before describing the view. "
    "Returns timestamped presence, touch, power, hardware temperatures and body pose. "
    "Omit direction to capture the current view; call separately for each view of a room. "
    "Set capture=false for fast sensor readings without a photo or movement. Unknown sensors stay unknown.",
    {
        "direction": {"type": "string", "enum": ["center", "left", "right", "far_left", "far_right", "up", "down"]},
        "position": {"type": "object", "properties": {
            axis: {"type": "number", "minimum": -5, "maximum": 5} for axis in ("x", "y", "z")
        }, "required": ["x", "y", "z"], "additionalProperties": False,
            "description": "Alternative to direction, in robot-relative metres: x forward, y left, z up."},
        "camera": {"type": "integer", "enum": [0, 1], "default": 1,
                   "description": "0 is the left camera; 1 is the right camera."},
        "capture": {"type": "boolean", "default": True},
        "show_image": {"type": "boolean", "default": True},
        "device_id": _DEVICE,
    },
)
JIBO_CHAT_LOG_SCHEMA = _schema(
    "jibo_chat_log",
    "Read the bounded recent native Jibo chat/event log for operator context.",
    {
        "device_id": _DEVICE,
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
)
JIBO_STOP_SCHEMA = _schema(
    "jibo_stop",
    "Stop native Jibo speech and restore the normal eye/display surface.",
    {"device_id": _DEVICE},
)
JIBO_LISTEN_SCHEMA = _schema(
    "jibo_listen",
    "Ask Jibo to listen for one deliberate follow-up reply and continue the current chat session.",
    {
        "hint_phrases": {
            "type": "array",
            "maxItems": 32,
            "items": {"type": "string", "minLength": 1, "maxLength": 160},
        },
        "device_id": _DEVICE,
    },
)
JIBO_DISPLAY_TEXT_SCHEMA = _schema(
    "jibo_display_text",
    "Display a short, user-facing text view directly through the native Jibo bridge.",
    {
        "text": {"type": "string", "minLength": 1, "maxLength": 1400},
        "name": {
            "type": "string",
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
            "default": "hermes-chat",
        },
        "device_id": _DEVICE,
    },
    ["text"],
)
JIBO_CLEAR_DISPLAY_SCHEMA = _schema(
    "jibo_clear_display",
    "Clear a native Jibo display view and optionally restore its eye.",
    {
        "name": {
            "type": "string",
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
            "default": "hermes-chat",
        },
        "restore": {"type": "string", "enum": ["eye", "blank"], "default": "eye"},
        "device_id": _DEVICE,
    },
)
JIBO_COLOR_BAR_SCHEMA = _schema(
    "jibo_color_bar",
    "Control Jibo's native color bar when the robot advertises that surface.",
    {
        "color": {
            "type": "string",
            "pattern": r"^#[0-9A-Fa-f]{6}$",
            "description": "RGB color such as #2F80ED.",
        },
        "brightness": {"type": "number", "minimum": 0, "maximum": 1},
        "duration_ms": {"type": "integer", "minimum": 0, "maximum": 60000},
        "segments": {
            "type": "array",
            "maxItems": 12,
            "items": {"type": "object"},
        },
        "device_id": _DEVICE,
    },
)
JIBO_SKILLS_SCHEMA = _schema(
    "jibo_list_skills",
    "List native Be skills and their current state on Jibo.",
    {"device_id": _DEVICE},
)
JIBO_LAUNCH_SKILL_SCHEMA = _schema(
    "jibo_launch_skill",
    "Launch a named native Be skill after capability discovery.",
    {
        "name": {"type": "string", "minLength": 1, "maxLength": 120},
        "arguments": {"type": "object", "maxProperties": 32},
        "device_id": _DEVICE,
    },
    ["name"],
)
JIBO_STOP_SKILL_SCHEMA = _schema(
    "jibo_stop_skill",
    "Stop a named native Be skill.",
    {
        "name": {"type": "string", "minLength": 1, "maxLength": 120},
        "device_id": _DEVICE,
    },
    ["name"],
)
JIBO_CAMERA_SCHEMA = _schema(
    "jibo_camera_capture",
    "Capture the current view and save a JPEG on the Hermes host for vision_analyze. Use jibo_observe to aim first or read sensors.",
    {
        "camera": {"type": "integer", "enum": [0, 1], "default": 1},
        "device_id": _DEVICE,
    },
)
JIBO_OPEN_WEB_PANEL_SCHEMA = _schema(
    "jibo_open_web_panel",
    (
        "Open a modern web page on Jibo's full-face native video panel. Use this when the user explicitly "
        "asks to see or browse a permitted web site, or asks for a web search to be shown. Pass exactly one "
        "of url or query. This is voice-first: it does not use a keyboard."
    ),
    {
        "url": {
            "type": "string",
            "minLength": 8,
            "maxLength": 2048,
            "format": "uri",
            "description": "A permitted http(s) page URL to show.",
        },
        "query": {
            "type": "string",
            "minLength": 1,
            "maxLength": 400,
            "description": "Words to search with the configured, permitted search provider.",
        },
        "device_id": _DEVICE,
    },
)
JIBO_CLOSE_WEB_PANEL_SCHEMA = _schema(
    "jibo_close_web_panel",
    "Close Jibo's Web Panel only when that panel is currently active, stopping its native video stream.",
    {"device_id": _DEVICE},
)

_WEB_PANEL_SKILL = "@hermes/jibo-web-panel"


def _error(message: str, *, code: str | None = None) -> str:
    payload: dict[str, Any] = {"success": False, "error": message}
    if code:
        payload["code"] = code
    return json.dumps(payload)


def _platform_ok(kwargs: dict[str, Any]) -> bool:
    platform = kwargs.get("platform")
    value = getattr(platform, "value", platform)
    return not value or str(value).strip().lower() == "jibo"


def _native_adapter(args: Any, kwargs: dict[str, Any]):
    if not _platform_ok(kwargs):
        return None, _error("native Jibo tools are only available from a Jibo conversation")
    if not isinstance(args, dict):
        return None, _error("arguments must be an object")
    device_id = str(args.get("device_id") or "").strip() or None
    adapter = get_active_adapter(device_id=device_id)
    if adapter is None or not callable(getattr(adapter, "native_call", None)):
        return None, _error("native Jibo bridge is not connected", code="NOT_CONNECTED")
    return adapter, None


def _call(args: Any, kwargs: dict[str, Any], method: str, params: Mapping[str, Any] | None = None, *, capability: str | None = None, timeout: float = 15.0) -> str:
    adapter, error = _native_adapter(args, kwargs)
    if error:
        return error
    assert isinstance(args, dict)
    device_id = str(args.get("device_id") or "").strip() or None
    result = adapter.native_call(
        method,
        params or {},
        device_id=device_id,
        timeout=timeout,
        required_capability=capability,
    )
    if not getattr(result, "success", False):
        return _error(
            getattr(result, "error", None) or f"native Jibo {method} failed",
            code=(getattr(result, "raw_response", None) or {}).get("code")
            if isinstance(getattr(result, "raw_response", None), dict)
            else None,
        )
    payload: dict[str, Any] = {
        "success": True,
        "method": method,
        "device_id": device_id or adapter.resolve_device_id(),
        "result": getattr(result, "raw_response", None),
    }
    return json.dumps(payload, default=str)


def jibo_status_available() -> bool:
    return True


def jibo_status_tool(args: dict[str, Any], **kwargs: Any) -> str:
    return _call(args, kwargs, "status.get", timeout=10.0)


def jibo_capabilities_available() -> bool:
    return True


def jibo_capabilities_tool(args: dict[str, Any], **kwargs: Any) -> str:
    return _call(args, kwargs, "capabilities.list", timeout=10.0)


def jibo_observe_available() -> bool:
    return True


def jibo_observe_tool(args: dict[str, Any], **kwargs: Any) -> str | dict:
    adapter, error = _native_adapter(args, kwargs)
    if error:
        return error
    for key in ("capture", "show_image"):
        if key in args and not isinstance(args[key], bool):
            return _error(key + " must be a boolean")
    if args.get("direction") and args.get("position"):
        return _error("choose direction or position, not both")
    if args.get("capture") is False and any(key in args for key in ("direction", "position", "camera")):
        return _error("direction, position and camera require capture=true")
    operation = getattr(adapter, "observe_environment", None)
    if not callable(operation):
        return _error("connected adapter does not support observations", code="CAPABILITY_UNAVAILABLE")
    try:
        from .observation import observation_tool_result

        target = adapter.resolve_device_id(str(args.get("device_id") or "").strip() or None,
                                           require_connected=True)
        turn_id = adapter._turn_id_for(target)
        adapter._assert_turn_current(target, turn_id)
        result = operation({key: args[key] for key in ("direction", "position", "camera") if key in args},
                           device_id=target, capture=args.get("capture", True),
                           show_image=args.get("show_image", True))
        if not result.success:
            return _error(result.error or "Observation failed",
                          code=(result.raw_response or {}).get("code"))
        output = observation_tool_result(result.raw_response or {})
        # Encoding runs in the tool worker. STOP or a replacement turn may have
        # arrived meanwhile; never publish its old photo into the new task.
        adapter._assert_turn_current(target, turn_id)
        return output
    except Exception as exc:
        return _error(str(exc), code=getattr(exc, "code", "OBSERVATION_FAILED"))


def jibo_chat_log_available() -> bool:
    return True


def jibo_chat_log_tool(args: dict[str, Any], **kwargs: Any) -> str:
    adapter, error = _native_adapter(args, kwargs)
    if error:
        return error
    assert isinstance(args, dict)
    try:
        limit = max(1, min(int(args.get("limit", 50)), 100))
    except (TypeError, ValueError):
        limit = 50
    device_id = str(args.get("device_id") or "").strip() or None
    return json.dumps(
        {
            "success": True,
            "device_id": device_id or adapter.resolve_device_id(),
            "events": adapter.get_event_log(device_id, limit=limit),
        },
        default=str,
    )


def jibo_stop_available() -> bool:
    return True


def jibo_stop_tool(args: dict[str, Any], **kwargs: Any) -> str:
    return _call(args, kwargs, "speech.stop", {"reason": "hermes_tool"}, timeout=8.0)


def jibo_listen_available() -> bool:
    return True


def jibo_listen_tool(args: dict[str, Any], **kwargs: Any) -> str:
    if not isinstance(args, dict):
        return _error("arguments must be an object")
    params: dict[str, Any] = {"enabled": True}
    phrases = args.get("hint_phrases")
    if isinstance(phrases, list):
        params["hint_phrases"] = [
            str(item).strip()[:160] for item in phrases[:32]
            if isinstance(item, str) and str(item).strip()
        ]
    return _call(args, kwargs, "input.listen", params, capability="input.listen", timeout=15.0)


def jibo_display_text_available() -> bool:
    return True


def jibo_display_text_tool(args: dict[str, Any], **kwargs: Any) -> str:
    if not isinstance(args, dict):
        return _error("arguments must be an object")
    return _call(
        args,
        kwargs,
        "display.text",
        {
            "text": str(args.get("text") or "").strip(),
            "name": str(args.get("name") or "hermes-chat").strip(),
        },
        timeout=10.0,
    )


def jibo_clear_display_available() -> bool:
    return True


def jibo_clear_display_tool(args: dict[str, Any], **kwargs: Any) -> str:
    if not isinstance(args, dict):
        return _error("arguments must be an object")
    return _call(
        args,
        kwargs,
        "display.clear",
        {
            "name": str(args.get("name") or "hermes-chat").strip(),
            "restore": str(args.get("restore") or "eye").strip(),
        },
        timeout=10.0,
    )


def jibo_color_bar_available() -> bool:
    return True


def jibo_color_bar_tool(args: dict[str, Any], **kwargs: Any) -> str:
    if not isinstance(args, dict):
        return _error("arguments must be an object")
    params = {key: args[key] for key in ("color", "brightness", "duration_ms", "segments") if key in args}
    return _call(args, kwargs, "display.color_bar", params, capability="display.color_bar", timeout=10.0)


_VOCABULARY_PATH = Path(__file__).with_name("expression_vocabulary.json")


def _expression_keys() -> list[str]:
    """The curated vocabulary keys, generated alongside the robot's own copy.

    Read once per process. An empty list is not fatal: the handler still sends
    the request, and the robot answers with the authoritative key list in its
    error, so a stale or missing file degrades to a slightly worse tool schema
    rather than a broken tool.
    """
    global _EXPRESSION_KEYS_CACHE
    if _EXPRESSION_KEYS_CACHE is None:
        try:
            with _VOCABULARY_PATH.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            keys = [str(k) for k in payload.get("keys") or [] if str(k).strip()]
        except (OSError, ValueError):
            keys = []
        _EXPRESSION_KEYS_CACHE = keys
    return _EXPRESSION_KEYS_CACHE


_EXPRESSION_KEYS_CACHE: list[str] | None = None


def _express_schema() -> dict[str, Any]:
    expression: dict[str, Any] = {
        "type": "string",
        "minLength": 1,
        "maxLength": 40,
        "description": "The expression to perform.",
    }
    keys = _expression_keys()
    if keys:
        expression["enum"] = keys
    return _schema(
        "jibo_express",
        (
            "Move Jibo's body. Use this often and naturally -- it is the main thing "
            "that makes him feel present rather than a speaker with a screen. Play a "
            "curated expression such as thinking, yes, no, shrug, confused, happy, "
            "surprised, greeting, sorry or glance_left. It returns immediately so the "
            "movement overlaps whatever you say next, which is what makes it read as "
            "body language rather than a pause. Good moments: acknowledging a request "
            "before answering, reacting to what someone said, showing uncertainty, or "
            "greeting someone. For a specific named animation such as a particular "
            "dance or jiboji, raw jibo_find_expression/jibo_play_expression require an "
            "independently authorized local catalog; that data is not bundled. "
            "Use only the connected robot's supported curated keys."
        ),
        {
            "expression": expression,
            "wait": {
                "type": "boolean",
                "description": (
                    "Finish the movement before returning. Default false, which lets it "
                    "overlap speech. Use true only for a deliberate beat."
                ),
            },
            "max_duration_ms": {
                "type": "integer",
                "minimum": 200,
                "maximum": 20000,
                "description": "Prefer an animation no longer than this.",
            },
            "device_id": _DEVICE,
        },
        ["expression"],
    )


JIBO_EXPRESS_SCHEMA = _express_schema()


def jibo_express_available() -> bool:
    return True


def jibo_express_tool(args: dict[str, Any], **kwargs: Any) -> str:
    if not isinstance(args, dict):
        return _error("arguments must be an object")
    key = str(args.get("expression") or "").strip().lower()
    if not key:
        return _error("expression must be non-empty")
    params: dict[str, Any] = {"key": key}
    if args.get("wait") is True:
        params["wait"] = True
    if isinstance(args.get("max_duration_ms"), int):
        params["max_duration_ms"] = args["max_duration_ms"]
    return _call(
        args,
        kwargs,
        "expression.perform",
        params,
        capability="expression.perform",
        # Not waited by default, so the robot answers as soon as the movement
        # is dispatched; the ceiling only covers a `wait: true` set piece.
        timeout=30.0,
    )


JIBO_LOOK_SCHEMA = _schema(
    "jibo_look",
    (
        "Turn Jibo's head and body to face a direction. Use it to glance at "
        "something being talked about, to look back at the person after looking "
        "away, or as a beat of attention. Jibo holds the gaze briefly and then "
        "the attention system takes over again, so this steers a moment rather "
        "than pinning him permanently."
    ),
    {
        "direction": {
            "type": "string",
            "enum": ["center", "forward", "left", "right",
                     "far_left", "far_right", "up", "down"],
            "description": "Where to look. 'center' returns to facing forward.",
        },
        "hold_ms": {
            "type": "integer",
            "minimum": 0,
            "maximum": 15000,
            "description": "How long to hold the gaze before releasing. Default 1500.",
        },
        "device_id": _DEVICE,
    },
    ["direction"],
)


def jibo_look_available() -> bool:
    return True


def jibo_look_tool(args: dict[str, Any], **kwargs: Any) -> str:
    if not isinstance(args, dict):
        return _error("arguments must be an object")
    direction = str(args.get("direction") or "").strip().lower()
    if not direction:
        return _error("direction must be non-empty")
    params: dict[str, Any] = {"direction": direction}
    if isinstance(args.get("hold_ms"), int):
        params["hold_ms"] = args["hold_ms"]
    return _call(
        args,
        kwargs,
        "expression.look",
        params,
        capability="expression.look",
        # The robot waits for the movement and the hold before answering.
        timeout=40.0,
    )


def jibo_skills_available() -> bool:
    return True


def jibo_skills_tool(args: dict[str, Any], **kwargs: Any) -> str:
    return _call(args, kwargs, "skill.list", timeout=10.0)


def jibo_launch_skill_available() -> bool:
    return True


def jibo_launch_skill_tool(args: dict[str, Any], **kwargs: Any) -> str:
    if not isinstance(args, dict):
        return _error("arguments must be an object")
    return _call(
        args,
        kwargs,
        "skill.launch",
        {"name": str(args.get("name") or "").strip(), "options": args.get("arguments") or {}},
        capability="skill.launch",
        timeout=20.0,
    )


def jibo_stop_skill_available() -> bool:
    return True


def jibo_stop_skill_tool(args: dict[str, Any], **kwargs: Any) -> str:
    if not isinstance(args, dict):
        return _error("arguments must be an object")
    return _call(
        args,
        kwargs,
        "skill.stop",
        {"name": str(args.get("name") or "").strip()},
        capability="skill.stop",
        timeout=15.0,
    )


def _web_panel_settings(adapter: Any) -> tuple[dict[str, str] | None, str | None]:
    """Read panel settings from the Jibo platform config, with env fallbacks.

    The browser service lives with Phoenix, while this plugin runs in the
    Hermes gateway process. Keeping the LAN-facing address in the Jibo
    platform's ``extra.browser_panel`` block avoids guessing which host address
    a robot can reach, and keeps the browser token out of tool arguments and
    model-visible output.
    """
    config = getattr(adapter, "config", None)
    extra = getattr(config, "extra", {}) or {}
    raw = extra.get("browser_panel") if isinstance(extra, Mapping) else None
    raw = raw if isinstance(raw, Mapping) else {}

    values = {
        "control_url": str(raw.get("control_url") or os.getenv("JIBO_BROWSER_PROXY_CONTROL_URL") or "http://127.0.0.1:9013").strip(),
        "public_url": str(raw.get("public_url") or os.getenv("JIBO_BROWSER_PROXY_PUBLIC_URL") or "").strip(),
        "token": str(raw.get("token") or os.getenv("JIBO_BROWSER_PROXY_TOKEN") or "").strip(),
        "search_url": str(
            raw.get("search_url")
            or os.getenv("JIBO_BROWSER_PROXY_SEARCH_URL")
            or "https://duckduckgo.com/?q={query}"
        ).strip(),
        "default_url": str(
            raw.get("default_url")
            or os.getenv("JIBO_BROWSER_PROXY_DEFAULT_URL")
            or "https://duckduckgo.com/"
        ).strip(),
    }
    if not values["public_url"] or not values["token"]:
        return None, (
            "Jibo Web Panel is not configured; set gateway.platforms.jibo.extra.browser_panel "
            "with public_url and token"
        )
    for key in ("control_url", "public_url"):
        try:
            parsed = urlsplit(values[key])
        except ValueError:
            return None, f"Jibo Web Panel {key} is invalid"
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return None, f"Jibo Web Panel {key} must be a plain http(s) base URL"
        values[key] = values[key].rstrip("/")
    if len(values["token"]) < 16 or any(char in values["token"] for char in "\r\n"):
        return None, "Jibo Web Panel token must be a non-empty secret of at least 16 characters"
    if "{query}" not in values["search_url"]:
        return None, "Jibo Web Panel search_url must contain the {query} placeholder"
    default_target, default_error = _panel_target({"url": values["default_url"]}, values)
    if default_error or not default_target:
        return None, "Jibo Web Panel default_url must be a credential-free http(s) URL"
    return values, None


def _panel_target(args: Mapping[str, Any], settings: Mapping[str, str]) -> tuple[str | None, str | None]:
    raw_url = args.get("url")
    raw_query = args.get("query")
    has_url = bool(isinstance(raw_url, str) and raw_url.strip())
    has_query = bool(isinstance(raw_query, str) and raw_query.strip())
    if has_url == has_query:
        return None, "pass exactly one of url or query"
    if has_query:
        query = str(raw_query).strip()
        if len(query) > 400:
            return None, "query exceeds 400 characters"
        target = settings["search_url"].replace("{query}", quote_plus(query))
    else:
        target = str(raw_url).strip()
    if len(target) > 2048 or any(char in target for char in "\r\n"):
        return None, "web URL is invalid or too long"
    try:
        parsed = urlsplit(target)
    except ValueError:
        return None, "web URL is invalid"
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return None, "web URL must be a credential-free http(s) URL"
    return target, None


def _open_browser_page(settings: Mapping[str, str], target: str) -> tuple[bool, str | None]:
    """Ask the proxy to navigate, returning a concise operator-safe error."""
    payload = json.dumps({"url": target}).encode("utf-8")
    request = Request(
        settings["control_url"] + "/api/open",
        data=payload,
        method="POST",
        headers={
            "Authorization": "Bearer " + settings["token"],
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            if response.status < 200 or response.status >= 300:
                return False, "browser proxy returned HTTP " + str(response.status)
            return True, None
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("error")
        except (ValueError, OSError):
            detail = None
        return False, str(detail or f"browser proxy returned HTTP {exc.code}")
    except (URLError, OSError) as exc:
        return False, "browser proxy is unreachable: " + str(getattr(exc, "reason", exc))


def jibo_open_web_panel_available() -> bool:
    return True


def jibo_open_web_panel_tool(args: dict[str, Any], **kwargs: Any) -> str:
    adapter, error = _native_adapter(args, kwargs)
    if error:
        return error
    assert isinstance(args, dict)
    settings, settings_error = _web_panel_settings(adapter)
    if settings_error:
        return _error(settings_error, code="NOT_CONFIGURED")
    assert settings is not None
    target, target_error = _panel_target(args, settings)
    if target_error:
        return _error(target_error, code="INVALID_PARAMS")
    assert target is not None
    opened, open_error = _open_browser_page(settings, target)
    if not opened:
        return _error(open_error or "browser proxy could not open the page", code="BROWSER_PROXY_ERROR")

    device_id = str(args.get("device_id") or "").strip() or None
    stream_url = settings["public_url"] + "/video.ts?token=" + quote_plus(settings["token"])
    result = adapter.native_call(
        "skill.launch",
        {"skill": _WEB_PANEL_SKILL, "options": {"stream_url": stream_url}},
        device_id=device_id,
        timeout=20.0,
        required_capability="skill.launch",
    )
    if not getattr(result, "success", False):
        return _error(
            getattr(result, "error", None) or "could not launch Jibo Web Panel",
            code=(getattr(result, "raw_response", None) or {}).get("code")
            if isinstance(getattr(result, "raw_response", None), dict)
            else None,
        )
    return json.dumps(
        {
            "success": True,
            "device_id": device_id or adapter.resolve_device_id(),
            "page_url": target,
            "panel": "requested",
        },
        default=str,
    )


def jibo_close_web_panel_available() -> bool:
    return True


def jibo_close_web_panel_tool(args: dict[str, Any], **kwargs: Any) -> str:
    adapter, error = _native_adapter(args, kwargs)
    if error:
        return error
    assert isinstance(args, dict)
    device_id = str(args.get("device_id") or "").strip() or None
    status = adapter.native_call("status.get", {}, device_id=device_id, timeout=10.0)
    if not getattr(status, "success", False):
        return _error(getattr(status, "error", None) or "could not read Jibo status")
    raw_status = getattr(status, "raw_response", None) or {}
    current = raw_status.get("skill") if isinstance(raw_status, Mapping) else None
    if not isinstance(current, Mapping) or current.get("name") != _WEB_PANEL_SKILL:
        return _error("Jibo Web Panel is not the active skill", code="PANEL_NOT_ACTIVE")
    result = adapter.native_call(
        "skill.stop",
        {},
        device_id=device_id,
        timeout=15.0,
        required_capability="skill.stop",
    )
    if not getattr(result, "success", False):
        return _error(getattr(result, "error", None) or "could not stop Jibo Web Panel")
    return json.dumps({"success": True, "device_id": device_id or adapter.resolve_device_id(), "panel": "stopping"})


def jibo_camera_available() -> bool:
    return True


def jibo_camera_tool(args: dict[str, Any], **kwargs: Any) -> str:
    if not isinstance(args, dict):
        return _error("arguments must be an object")
    observed = json.loads(jibo_observe_tool({**args, "capture": True, "show_image": False}, **kwargs))
    if observed.get("success"):
        observed["result"] = observed["result"]["image"]
    return json.dumps(observed, default=str)
