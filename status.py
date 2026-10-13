"""Private-to-Jibo processing indicators for the Hermes LLM lifecycle."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
import re
import threading
from typing import Any

from .adapter import get_active_adapter

logger = logging.getLogger(__name__)

_STATUS_LOCK = threading.Lock()
_STARTED_TURNS: set[tuple[str, str]] = set()
_MAX_STATUS_KEYS = 256
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_FILE_URI_RE = re.compile(r"file://(?P<path>/[^\s`<>\"']+)", re.IGNORECASE)


def reset_status_state_for_tests() -> None:
    """Clear the bounded turn state used by unit tests."""
    with _STATUS_LOCK:
        _STARTED_TURNS.clear()


def _is_jibo(platform: Any) -> bool:
    value = getattr(platform, "value", platform)
    return str(value or "").strip().lower() == "jibo"


def _turn_key(kwargs: dict[str, Any]) -> tuple[str, str]:
    session_id = str(kwargs.get("session_id") or "").strip()
    turn_id = str(kwargs.get("turn_id") or "").strip()
    if not turn_id:
        message = str(kwargs.get("user_message") or "").strip()
        digest = hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]
        turn_id = f"message:{digest}"
    return session_id, turn_id


def _is_tracked_jibo_turn(kwargs: dict[str, Any]) -> bool:
    key = _turn_key(kwargs)
    with _STATUS_LOCK:
        return key in _STARTED_TURNS


def _adapter_for_jibo_hook(kwargs: dict[str, Any]):
    """Resolve Jibo from explicit platform/surface or tracked turn state."""
    if _is_jibo(kwargs.get("platform")) or _is_jibo(kwargs.get("surface")):
        return get_active_adapter()
    if _is_tracked_jibo_turn(kwargs):
        return get_active_adapter()
    return None


def _call_with_session_context(method, session_id: str, **call_kwargs: Any) -> Any:
    """Pass the native device-routing hint while keeping ROM test doubles compatible."""
    if not session_id:
        return method(**call_kwargs)
    try:
        return method(session_id=session_id, **call_kwargs)
    except TypeError as exc:
        # The legacy ROM adapter and older test doubles do not accept the
        # optional routing keyword.  Only fall back for that signature shape;
        # do not hide a TypeError raised by the operation itself.
        if "session_id" not in str(exc) and "unexpected keyword" not in str(exc):
            raise
        return method(**call_kwargs)


def extract_local_image_paths(text: Any) -> list[str]:
    """Return safe local image paths already recognized by Hermes delivery."""
    value = str(text or "")
    candidates: list[str] = []
    try:
        from gateway.platforms.base import BasePlatformAdapter

        paths, _cleaned = BasePlatformAdapter.extract_local_files(value)
        candidates.extend(paths)
    except Exception:
        logger.debug("Jibo local image extraction failed", exc_info=True)
    candidates.extend(match.group("path") for match in _FILE_URI_RE.finditer(value))

    try:
        from gateway.platforms.base import BasePlatformAdapter

        candidates = BasePlatformAdapter.filter_local_delivery_paths(candidates)
    except Exception:
        candidates = [str(Path(path).expanduser()) for path in candidates]
    seen: set[str] = set()
    images: list[str] = []
    for path in candidates:
        if Path(path).suffix.lower() in _IMAGE_EXTENSIONS and path not in seen:
            seen.add(path)
            images.append(path)
    return images


def on_pre_llm_call(**kwargs: Any) -> None:
    """Start one public processing indicator for a Jibo turn."""
    if not _is_jibo(kwargs.get("platform")):
        return None
    key = _turn_key(kwargs)
    with _STATUS_LOCK:
        if key in _STARTED_TURNS:
            return None
        _STARTED_TURNS.add(key)
        if len(_STARTED_TURNS) > _MAX_STATUS_KEYS:
            _STARTED_TURNS.pop()
    adapter = get_active_adapter()
    if adapter is not None:
        try:
            _call_with_session_context(
                adapter.begin_processing,
                str(kwargs.get("session_id") or ""),
                turn_id=key[1],
                user_message=str(kwargs.get("user_message") or ""),
            )
        except Exception:
            logger.debug("Jibo processing start failed", exc_info=True)
    return None


def on_pre_tool_call(**kwargs: Any) -> None:
    """Show a safe high-level working label before a tool runs."""
    adapter = _adapter_for_jibo_hook(kwargs)
    if adapter is not None:
        try:
            _call_with_session_context(
                adapter.update_processing_stage,
                str(kwargs.get("session_id") or ""),
                stage="WORKING",
            )
        except Exception:
            logger.debug("Jibo working status failed", exc_info=True)
    return None


def on_post_tool_call(**kwargs: Any) -> None:
    """Show a safe high-level checking label after a tool returns."""
    adapter = _adapter_for_jibo_hook(kwargs)
    if adapter is not None:
        try:
            _call_with_session_context(
                adapter.update_processing_stage,
                str(kwargs.get("session_id") or ""),
                stage="CHECKING",
            )
        except Exception:
            logger.debug("Jibo checking status failed", exc_info=True)
    return None


def on_interim_message(**kwargs: Any) -> None:
    """Indicate processing without ever relaying interim model text."""
    adapter = _adapter_for_jibo_hook(kwargs)
    if adapter is not None:
        try:
            _call_with_session_context(
                adapter.update_processing_stage,
                str(kwargs.get("session_id") or ""),
                stage="PROCESSING",
            )
        except Exception:
            logger.debug("Jibo processing status failed", exc_info=True)
    return None


def on_post_llm_call(**kwargs: Any) -> None:
    """Stop periodic status before normal final-answer delivery."""
    if not _is_jibo(kwargs.get("platform")):
        return None
    key = _turn_key(kwargs)
    with _STATUS_LOCK:
        _STARTED_TURNS.discard(key)
    adapter = get_active_adapter()
    if adapter is not None:
        try:
            for image_path in extract_local_image_paths(kwargs.get("assistant_response"))[:1]:
                try:
                    adapter.display_local_image(image_path)
                except Exception:
                    logger.debug("Jibo local image pre-display failed", exc_info=True)
            _call_with_session_context(
                adapter.finish_processing,
                str(kwargs.get("session_id") or ""),
                success=bool(kwargs.get("assistant_response")),
            )
        except Exception:
            logger.debug("Jibo processing finish failed", exc_info=True)
    return None


def on_session_end(**kwargs: Any) -> None:
    """Stop/error-mark an unfinished Jibo turn on interruption or failure."""
    if not _is_jibo(kwargs.get("platform")):
        return None
    key = _turn_key(kwargs)
    with _STATUS_LOCK:
        started = key in _STARTED_TURNS
        _STARTED_TURNS.discard(key)
    failed = bool(
        kwargs.get("failed")
        or kwargs.get("interrupted")
        or kwargs.get("completed") is False
    )
    if started and failed:
        adapter = get_active_adapter()
        if adapter is not None:
            try:
                _call_with_session_context(
                    adapter.finish_processing,
                    str(kwargs.get("session_id") or ""),
                    success=False,
                )
            except Exception:
                logger.debug("Jibo failed-turn status cleanup failed", exc_info=True)
    return None
