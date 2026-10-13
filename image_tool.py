"""Jibo screen image and eye tools."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from .adapter import get_active_adapter


_IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

JIBO_DISPLAY_IMAGE_SCHEMA: dict[str, Any] = {
    "name": "jibo_display_image",
    "description": (
        "Display a direct image URL on Jibo's persistent chat while discussing it. "
        "Hermes downloads and scales the image before sending it over the local "
        "network, so pass a camera snapshot URL or direct image file URL, not a "
        "search-results page. Use this before jibo_speak when the user should see "
        "the image during the spoken answer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "format": "uri",
                "pattern": r"^https?://[^\s]+$",
                "minLength": 8,
                "maxLength": 2048,
                "description": "Direct public HTTP(S) image URL that Hermes can retrieve.",
            },
            "name": {
                "type": "string",
                "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
                "default": "hermes-image",
                "description": "Short replacement name for the displayed image view.",
            },
            "device_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "description": "Optional configured Jibo id when multiple robots are connected.",
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    },
}

JIBO_SHOW_EYE_SCHEMA: dict[str, Any] = {
    "name": "jibo_show_eye",
    "description": "Restore Jibo's normal eye view after showing an image.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
                "default": "default",
            },
            "device_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "description": "Optional configured Jibo id when multiple robots are connected.",
            },
        },
        "additionalProperties": False,
    },
}


def jibo_display_image_available() -> bool:
    """Advertise the tool before the asynchronous ROM connection completes."""
    return True


def jibo_show_eye_available() -> bool:
    """Advertise the eye-reset tool before the asynchronous ROM connection completes."""
    return True


def _error(message: str) -> str:
    return json.dumps({"success": False, "error": message})


def _valid_image_url(value: Any) -> str | None:
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


def _view_name(value: Any, default: str) -> str:
    name = str(value or default).strip()
    return name if _IMAGE_NAME_RE.fullmatch(name) else default


def jibo_display_image_tool(args: dict[str, Any], **kwargs: Any) -> str:
    """Show an explicit, user-requested image on Jibo's display."""
    if not isinstance(args, dict):
        return _error("arguments must be an object")
    url = _valid_image_url(args.get("url"))
    if url is None:
        return _error("url must be a direct HTTP(S) URL without credentials or unsafe characters")

    platform = kwargs.get("platform")
    platform_value = getattr(platform, "value", platform)
    if platform_value and str(platform_value).strip().lower() != "jibo":
        return _error("jibo_display_image is only available from a Jibo conversation")

    device_id = str(args.get("device_id") or "").strip() or None
    adapter = get_active_adapter(device_id=device_id) if device_id else get_active_adapter()
    if adapter is None:
        return _error("Jibo adapter is not connected")
    display_args = {"name": _view_name(args.get("name"), "hermes-image")}
    if device_id:
        display_args["device_id"] = device_id
    result = adapter.display_image(url, **display_args)
    if not getattr(result, "success", False):
        return _error(getattr(result, "error", None) or "Jibo image display failed")
    return json.dumps({
        "success": True,
        "displayed": True,
        "message_id": getattr(result, "message_id", None),
    })


def jibo_show_eye_tool(args: dict[str, Any], **kwargs: Any) -> str:
    """Restore Jibo's normal eye view."""
    if not isinstance(args, dict):
        return _error("arguments must be an object")
    platform = kwargs.get("platform")
    platform_value = getattr(platform, "value", platform)
    if platform_value and str(platform_value).strip().lower() != "jibo":
        return _error("jibo_show_eye is only available from a Jibo conversation")

    device_id = str(args.get("device_id") or "").strip() or None
    adapter = get_active_adapter(device_id=device_id) if device_id else get_active_adapter()
    if adapter is None:
        return _error("Jibo adapter is not connected")
    eye_args = {"name": _view_name(args.get("name"), "default")}
    if device_id:
        eye_args["device_id"] = device_id
    result = adapter.show_eye(**eye_args)
    if not getattr(result, "success", False):
        return _error(getattr(result, "error", None) or "Jibo eye restore failed")
    return json.dumps({
        "success": True,
        "displayed": "eye",
        "message_id": getattr(result, "message_id", None),
    })
