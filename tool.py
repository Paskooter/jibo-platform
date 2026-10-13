"""Agent-visible intentional speech tool for the Jibo platform."""

from __future__ import annotations

import json
from typing import Any

from .adapter import get_active_adapter


JIBO_SPEAK_SCHEMA: dict[str, Any] = {
    "name": "jibo_speak",
    "description": (
        "Speak one intentional, user-facing sentence through the connected Jibo. "
        "Ordinary assistant replies are automatically spoken and displayed; answer "
        "directly for those. Use this tool for an intentional spoken progress update "
        "or explicit style/ESML. Never speak raw tool output, URLs, JSON, or private "
        "chain-of-thought. Use kind=thinking "
        "or progress for a short status beat, kind=answer for the final answer, "
        "and kind=error when explaining a failure. Set final=true only when the "
        "spoken answer is complete, then finish with the same short plain-text answer "
        "for saved history; the adapter prevents duplicate speech. "
        "For an expressive animation, first use jibo_find_expression and then "
        "include only its exact catalog-confirmed <anim> name."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "minLength": 1,
                "maxLength": 2000,
                "description": "Plain spoken text, optionally containing a catalog-confirmed safe Jibo tag.",
            },
            "kind": {
                "type": "string",
                "enum": ["thinking", "progress", "answer", "error"],
                "default": "answer",
                "description": "The conversational purpose; adds a safe embodied cue.",
            },
            "style": {
                "type": "string",
                "enum": ["neutral", "enthusiastic", "sheepish", "confused", "confident"],
                "default": "neutral",
                "description": "Optional documented Jibo speaking style.",
            },
            "final": {
                "type": "boolean",
                "default": False,
                "description": "True when this tool call is the complete answer for the user.",
            },
            "device_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "description": "Optional configured Jibo id when multiple robots are connected.",
            },
        },
        "required": ["text"],
        "additionalProperties": False,
    },
}


def jibo_speak_available() -> bool:
    """Return whether the Jibo speech tool should be advertised.

    Connection readiness is checked inside the handler. Advertising the tool
    before the gateway's asynchronous platform connect completes prevents a
    newly-created Jibo agent from receiving a stale tool snapshot.
    """
    return True


def jibo_speak_tool(args: dict[str, Any], **kwargs: Any) -> str:
    """Format and speak one intentional Jibo utterance."""
    if not isinstance(args, dict):
        return json.dumps({"success": False, "error": "arguments must be an object"})

    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        return json.dumps({"success": False, "error": "text must be non-empty plain text"})

    kind = str(args.get("kind") or "answer").strip().lower()
    style = str(args.get("style") or "neutral").strip().lower()
    final = bool(args.get("final", False))
    device_id = str(args.get("device_id") or "").strip() or None

    try:
        from gateway.session_context import get_session_env
        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    except Exception:
        platform = ""
    if platform and str(platform).strip().lower() != "jibo":
        return json.dumps({
            "success": False,
            "error": "jibo_speak is only available from a Jibo conversation",
        })

    adapter = get_active_adapter(device_id=device_id) if device_id else get_active_adapter()
    if adapter is None:
        return json.dumps({"success": False, "error": "Jibo adapter is not connected"})

    speak_args = {
        "kind": kind,
        "style": style,
        "final": final,
    }
    if device_id:
        speak_args["device_id"] = device_id
    result = adapter.speak_explicit(text.strip(), **speak_args)
    if not getattr(result, "success", False):
        return json.dumps({
            "success": False,
            "error": getattr(result, "error", None) or "Jibo speech failed",
        })
    return json.dumps({
        "success": True,
        "spoken": True,
        "final": final,
        "message_id": getattr(result, "message_id", None),
    })
