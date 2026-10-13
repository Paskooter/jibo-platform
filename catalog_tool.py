"""Hermes tool for looking up exact Jibo AnimDB expressions."""

from __future__ import annotations

import json
from typing import Any

from .adapter import get_active_adapter
from .catalog import expression_to_esml, load_catalog, search_catalog


JIBO_FIND_EXPRESSION_SCHEMA: dict[str, Any] = {
    "name": "jibo_find_expression",
    "description": (
        "Look up exact names and metadata in the PVindex-derived Jibo AnimDB "
        "catalog before using an expressive animation. Search by a feeling, "
        "dance, emoji/jiboji, sound, music, or metadata term. Results include "
        "a safe suggested <anim> snippet for jibo_speak. Do not invent names; "
        "legacy ROM compatibility removes SSA-only output."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
                "description": "A feeling, dance, emoji, sound, music, or metadata term.",
            },
            "category": {
                "type": "string",
                "maxLength": 60,
                "description": "Optional exact AnimDB category filter.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": 8,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


JIBO_PLAY_EXPRESSION_SCHEMA: dict[str, Any] = {
    "name": "jibo_play_expression",
    "description": (
        "Execute one exact, catalog-confirmed Jibo animation such as a dance, "
        "emoji/jiboji, greeting, emotional gesture, or music-backed expression. "
        "Call jibo_find_expression first when you do not already have the exact "
        "AnimDB name. This tool actually makes Jibo perform it; do not merely "
        "say that you will dance. Optionally include a short sentence to speak "
        "inside the animation. SSA-only expressions are rejected on the legacy ROM path."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 120,
                "description": "Exact official AnimDB name returned by jibo_find_expression.",
            },
            "text": {
                "type": "string",
                "maxLength": 350,
                "description": "Optional short sentence spoken while the animation runs.",
            },
            "device_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "description": "Optional configured Jibo id when multiple robots are connected.",
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    },
}


def jibo_find_expression_available() -> bool:
    """Raw lookup is optional and unavailable without owner-supplied data."""
    return bool(load_catalog())


def jibo_find_expression_tool(args: dict[str, Any], **kwargs: Any) -> str:
    """Return bounded exact AnimDB matches as model context."""
    if not isinstance(args, dict):
        return json.dumps({"success": False, "error": "arguments must be an object"})
    platform = kwargs.get("platform")
    platform_value = getattr(platform, "value", platform)
    if platform_value and str(platform_value).strip().lower() != "jibo":
        return json.dumps({
            "success": False,
            "error": "jibo_find_expression is only available from a Jibo conversation",
        })
    query = str(args.get("query") or "").strip()
    if not query:
        return json.dumps({"success": False, "error": "query must be non-empty"})
    try:
        limit = max(1, min(int(args.get("limit", 8)), 20))
    except (TypeError, ValueError):
        limit = 8
    catalog = load_catalog()
    if not catalog:
        return json.dumps({
            "success": False,
            "error": "PVindex-derived AnimDB catalog is not installed on this host",
        })
    results = search_catalog(
        catalog,
        query=query,
        category=str(args.get("category") or "").strip() or None,
        limit=limit,
    )
    return json.dumps({
        "success": True,
        "count": len(results),
        "results": results,
        "notes": [
            "Use the exact name from a result; do not invent animation names.",
            "Suggested snippets are for catalog-confirmed <anim> use in jibo_speak.",
            "The legacy ROM route does not send SSA-only tags.",
        ],
    })


def jibo_play_expression_available() -> bool:
    """Raw playback requires the optional local catalog; handler checks connection."""
    return bool(load_catalog())


def jibo_play_expression_tool(args: dict[str, Any], **kwargs: Any) -> str:
    """Execute one exact catalog-confirmed animation through Jibo ROM."""
    if not isinstance(args, dict):
        return json.dumps({"success": False, "error": "arguments must be an object"})
    platform = kwargs.get("platform")
    platform_value = getattr(platform, "value", platform)
    if platform_value and str(platform_value).strip().lower() != "jibo":
        return json.dumps({
            "success": False,
            "error": "jibo_play_expression is only available from a Jibo conversation",
        })
    name = str(args.get("name") or "").strip()
    if not name:
        return json.dumps({"success": False, "error": "name must be non-empty"})
    catalog = load_catalog()
    entry = next(
        (item for item in catalog if str(item.get("name") or "").casefold() == name.casefold()),
        None,
    )
    if entry is None:
        return json.dumps({
            "success": False,
            "error": f"expression '{name}' not found in the AnimDB catalog",
        })
    meta_terms = {str(term).casefold() for term in entry.get("metaTerms") or []}
    if "ssa-only" in meta_terms:
        return json.dumps({
            "success": False,
            "error": "SSA-only expressions are disabled on the legacy ROM route",
        })
    text = str(args.get("text") or "").strip()
    if len(text) > 350:
        return json.dumps({"success": False, "error": "text is limited to 350 characters"})
    device_id = str(args.get("device_id") or "").strip() or None
    adapter = get_active_adapter(device_id=device_id) if device_id else get_active_adapter()
    if adapter is None:
        return json.dumps({"success": False, "error": "Jibo adapter is not connected"})
    play_args = {"text": text}
    if device_id:
        play_args["device_id"] = device_id
    result = adapter.play_expression(entry["name"], **play_args)
    if not getattr(result, "success", False):
        return json.dumps({
            "success": False,
            "error": getattr(result, "error", None) or "Jibo expression failed",
        })
    return json.dumps({
        "success": True,
        "performed": True,
        "name": entry["name"],
        "message_id": getattr(result, "message_id", None),
        "spoken_with_expression": bool(text),
    })
