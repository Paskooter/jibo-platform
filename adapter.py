"""Hermes gateway adapter for direct Jibo ROM + local ASR."""

from __future__ import annotations

import asyncio
import concurrent.futures
import http.server
import logging
import mimetypes
import os
import re
import secrets
import shutil
import socket
import tempfile
import threading
import time
import uuid
import weakref
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import build_session_key

from .asr import WakewordWatcher, listen_once
from .esml import MAX_TOTAL_CHARS, format_jibo_speech, prepare_for_jibo
from .rom import RomError, RomSession

logger = logging.getLogger(__name__)

DEFAULT_ROM_PORT = 8160
DEFAULT_ASR_PORT = 8088
DEFAULT_APP_ID = "HermesJiboRom"
_CANCEL_PHRASES = {
    "cancel",
    "cancel that",
    "never mind",
    "stop",
    "stop that",
    "stop what you're doing",
    "stop what you are doing",
    "tap anywhere to cancel",
}
_PAIRING_CODE_RE = re.compile(
    r"Here's your pairing code:\s*`([A-HJ-NP-Z2-9]{8})`",
    re.IGNORECASE,
)
_ACTIVE_ADAPTERS: weakref.WeakSet = weakref.WeakSet()
_ACTIVE_ADAPTERS_LOCK = threading.RLock()
_TOOL_SPOKEN_GRACE_SECONDS = 30.0
PROCESSING_SCREEN_NAME = "hermes-processing"
PROCESSING_INTERVAL_SECONDS = 18.0
PROCESSING_STATUS_PHRASES = (
    "Still working on that.",
    "I'm checking a few things.",
    "Almost there.",
    "Just a moment longer.",
)
_PROCESSING_SCREEN_TEXT = {
    "THINKING": "THINKING...\nI'm on it.\nTAP ANYWHERE TO STOP",
    "WORKING": "WORKING...\nHold on a moment.\nTAP ANYWHERE TO STOP",
    "CHECKING": "CHECKING...\nGetting there.\nTAP ANYWHERE TO STOP",
    "PROCESSING": "PROCESSING...\nStill working.\nTAP ANYWHERE TO STOP",
    "ANSWERING": "ANSWERING...\nI've got it.",
    "CANCELLED": "CANCELLED...\nStopped.",
    "ERROR": "SORRY...\nSomething went wrong.",
    "": "",
}


class _JiboMediaHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address):
        self.files: dict[str, str] = {}
        super().__init__(server_address, _JiboMediaRequestHandler)


class _JiboMediaRequestHandler(http.server.BaseHTTPRequestHandler):
    server: _JiboMediaHTTPServer

    def _serve(self, *, include_body: bool) -> None:
        token = self.path.split("?", 1)[0].removeprefix("/jibo-media/")
        file_path = self.server.files.get(token)
        if not file_path or not os.path.isfile(file_path):
            self.send_error(404)
            return
        try:
            file_size = os.path.getsize(file_path)
            content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(file_size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if include_body:
                with open(file_path, "rb") as source:
                    shutil.copyfileobj(source, self.wfile)
        except (OSError, BrokenPipeError):
            return

    def do_GET(self) -> None:  # noqa: N802
        self._serve(include_body=True)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(include_body=False)

    def log_message(self, _format: str, *_args: Any) -> None:
        return None



def processing_screen_text(stage: str) -> str:
    """Return a bounded, high-level status for Jibo's screen."""
    normalized = str(stage or "").strip().upper()
    return _PROCESSING_SCREEN_TEXT.get(normalized, _PROCESSING_SCREEN_TEXT["PROCESSING"])


def is_cancel_phrase(text: str) -> bool:
    """Return True only for short, explicit voice cancellation commands."""
    normalized = re.sub(r"[^a-z0-9']+", " ", str(text or "").lower().replace("’", "'")).strip()
    return normalized in _CANCEL_PHRASES or normalized in {"please stop", "please cancel"}


def _register_active_adapter(adapter: Any) -> None:
    with _ACTIVE_ADAPTERS_LOCK:
        _ACTIVE_ADAPTERS.add(adapter)


def _unregister_active_adapter(adapter: Any) -> None:
    with _ACTIVE_ADAPTERS_LOCK:
        _ACTIVE_ADAPTERS.discard(adapter)


def get_active_adapter(
    device_id: Optional[str] = None,
) -> Optional[Any]:
    """Return a connected Jibo adapter for the intentional speech/tool APIs.

    The native adapter owns several devices behind one platform instance.  A
    device id is therefore a selection hint rather than a second adapter;
    legacy ROM callers continue to get the single active ROM adapter.
    """
    with _ACTIVE_ADAPTERS_LOCK:
        for adapter in list(_ACTIVE_ADAPTERS):
            try:
                if device_id:
                    has_device = getattr(adapter, "has_device", None)
                    if not callable(has_device) or not has_device(device_id):
                        continue
                connected = getattr(adapter, "connected", None)
                if callable(connected):
                    connected = connected()
                if connected is None:
                    connected = bool(getattr(getattr(adapter, "_rom", None), "connected", False))
                if connected:
                    return adapter
            except Exception:
                continue
    return None


def _pairing_code_from_message(content: str) -> Optional[str]:
    match = _PAIRING_CODE_RE.search(content or "")
    return match.group(1).upper() if match else None


def check_requirements() -> bool:
    try:
        import httpx  # noqa: F401
        import websockets  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config: PlatformConfig) -> bool:
    extra = config.extra or {}
    return bool(str(extra.get("host") or os.getenv("JIBO_HOST") or "").strip())


def is_connected(config: PlatformConfig) -> bool:
    extra = config.extra or {}
    return bool(str(extra.get("host") or os.getenv("JIBO_HOST") or "").strip())


class JiboRomAdapter(BasePlatformAdapter):
    """Ambient single-user Jibo adapter.

    Wake-word and local speech recognition are input-only transport details;
    the normal Hermes gateway remains responsible for sessions, model calls,
    tools, and response delivery.
    """

    MAX_MESSAGE_LENGTH = MAX_TOTAL_CHARS
    splits_long_messages = False
    supports_code_blocks = False
    supports_async_delivery = False
    interactive_resume = False

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("jibo"))
        extra = config.extra or {}
        self._host = str(extra.get("host") or os.getenv("JIBO_HOST") or "").strip()
        if not self._host:
            raise ValueError("Jibo ROM mode requires a configured host target")
        self._rom_port = int(extra.get("rom_port", extra.get("port", DEFAULT_ROM_PORT)))
        self._asr_port = int(extra.get("asr_port", DEFAULT_ASR_PORT))
        self._app_id = str(extra.get("app_id") or DEFAULT_APP_ID)
        self._media_host = str(extra.get("media_host") or "").strip()
        self._max_speech_ms = max(1000, int(extra.get("max_speech_ms", 15000)))
        self._max_silence_ms = max(500, int(extra.get("max_silence_ms", 5000)))
        self._volume = extra.get("volume")
        try:
            self._volume = float(self._volume) if self._volume is not None else None
        except (TypeError, ValueError):
            self._volume = None
        self._chat_id = "jibo:" + self._host
        self._rom = RomSession(self._host, self._rom_port, app_id=self._app_id)
        self._rom.set_disconnect_handler(self._on_rom_disconnect)
        self._rom.add_handler(self._on_rom_event)
        self._wake = WakewordWatcher(self._host, self._asr_port)
        self._wake_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._turn_lock = asyncio.Lock()
        self._speak_lock = asyncio.Lock()
        self._closing = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._speech_state_lock = threading.Lock()
        self._tool_spoken_deadline = 0.0
        self._processing_state_lock = threading.RLock()
        self._processing_turn_id: Optional[str] = None
        self._processing_stage = ""
        self._processing_task: Optional[asyncio.Task] = None
        self._processing_generation = 0
        self._user_display_mode = "eye"
        self._media_lock = threading.RLock()
        self._media_server: Optional[_JiboMediaHTTPServer] = None
        self._media_server_thread: Optional[threading.Thread] = None
        self._media_cache_dir: Optional[Path] = None
        self._active_source: Optional[Any] = None
        self._active_session_key = ""
        self._active_speech_tx = ""
        self._tap_cancel_lock = asyncio.Lock()
        _register_active_adapter(self)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if self._closing:
            return False
        if self._rom.connected:
            self._mark_connected()
            return True
        try:
            self._loop = asyncio.get_running_loop()
            await self._rom.connect()
            self._mark_connected()
            if self._volume is not None:
                try:
                    await self._rom.command({"Type": "SetConfig", "Options": {"Mixer": max(0.0, min(1.0, self._volume))}})
                except Exception:
                    logger.debug("Unable to set Jibo volume", exc_info=True)
            if self._wake_task is None or self._wake_task.done():
                self._wake_task = asyncio.create_task(self._wake.run(self._on_hotword), name="jibo-wakeword")
            logger.info("Jibo platform connected to %s:%s", self._host, self._rom_port)
            return True
        except Exception as exc:
            logger.error("Jibo platform connection failed: %s", exc)
            self._mark_disconnected()
            return False

    async def _on_rom_disconnect(self) -> None:
        if self._closing:
            return
        self._mark_disconnected()
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect_loop(), name="jibo-rom-reconnect")

    async def _reconnect_loop(self) -> None:
        delay = 2.0
        while not self._closing and not self._rom.connected:
            try:
                await asyncio.sleep(delay)
                if self._closing:
                    return
                if await self.connect(is_reconnect=True):
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Jibo reconnect attempt failed", exc_info=True)
            delay = min(delay * 2.0, 30.0)

    async def disconnect(self) -> None:
        self._closing = True
        processing_task = self._processing_task
        self._processing_task = None
        if processing_task is not None:
            processing_task.cancel()
            try:
                await processing_task
            except asyncio.CancelledError:
                pass
        with self._processing_state_lock:
            self._processing_turn_id = None
            self._processing_stage = ""
            self._processing_generation += 1
        await self._wake.stop()
        if self._wake_task is not None:
            self._wake_task.cancel()
            try:
                await self._wake_task
            except asyncio.CancelledError:
                pass
            self._wake_task = None
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            self._reconnect_task = None
        await self._rom.close()
        await asyncio.to_thread(self._stop_media_server)
        self._loop = None
        _unregister_active_adapter(self)
        self._mark_disconnected()
        logger.info("Jibo platform disconnected")

    async def _on_rom_event(self, message: dict[str, Any]) -> None:
        """Handle ROM-side input that is not part of a command response."""
        event_body = message.get("EventBody") or {}
        event_name = str(event_body.get("Event") or "").strip().lower()
        if event_name not in {"ontap", "onscreentap", "ontapdetected"}:
            return
        if self._closing or self._tap_cancel_lock.locked():
            return
        logger.info("Jibo screen tap received; cancelling active turn")
        async with self._tap_cancel_lock:
            await self._cancel_current_turn_from_tap()

    async def _cancel_current_turn_from_tap(self) -> None:
        """Hard-cancel the active Hermes turn initiated through Jibo."""
        session_key = str(getattr(self, "_active_session_key", "") or "")
        source = getattr(self, "_active_source", None)
        speech_tx = str(getattr(self, "_active_speech_tx", "") or "")
        rom_speech_tx = str(getattr(self._rom, "_active_say_tx", "") or "")
        active_sessions = getattr(self, "_active_sessions", {})
        has_processing = bool(getattr(self, "_processing_turn_id", None))
        if not session_key and not speech_tx and not rom_speech_tx and not has_processing:
            return

        if speech_tx:
            await self._rom.cancel(speech_tx)
        else:
            cancel_active_say = getattr(self._rom, "cancel_active_say", None)
            if callable(cancel_active_say):
                try:
                    await cancel_active_say()
                except Exception:
                    logger.debug("Unable to cancel active Jibo speech", exc_info=True)

        if session_key and (session_key in active_sessions or has_processing):
            runner = getattr(self, "gateway_runner", None)
            hard_cancel = getattr(runner, "_interrupt_and_clear_session", None)
            if callable(hard_cancel) and source is not None:
                try:
                    await hard_cancel(
                        session_key,
                        source,
                        interrupt_reason="jibo_screen_tap",
                        invalidation_reason="jibo_screen_tap",
                    )
                except Exception:
                    logger.warning("Jibo screen-tap hard cancel failed", exc_info=True)
                    await self.interrupt_session_activity(session_key, self._chat_id)
                    await self.cancel_session_processing(session_key)
            else:
                await self.interrupt_session_activity(session_key, self._chat_id)
                await self.cancel_session_processing(session_key)

        processing_task = self._processing_task
        with self._processing_state_lock:
            self._processing_task = None
            self._processing_turn_id = None
            self._processing_stage = "CANCELLED"
            self._processing_generation += 1
            self._user_display_mode = "eye"
        if processing_task is not None and processing_task is not asyncio.current_task():
            processing_task.cancel()
            try:
                await processing_task
            except asyncio.CancelledError:
                pass
        try:
            await self._rom.display_eye(name="default")
        except Exception:
            logger.debug("Unable to restore Jibo eye after screen-tap cancel", exc_info=True)
        self._active_speech_tx = ""
        logger.info("Jibo screen-tap cancellation complete for %s", session_key or "speech")

    async def _on_hotword(self, data: dict[str, Any]) -> None:
        if self._closing or self._turn_lock.locked():
            logger.debug("Ignoring overlapping Jibo wake-word event")
            return
        asyncio.create_task(self._capture_and_dispatch(data), name="jibo-capture-turn")

    async def _capture_and_dispatch(self, hotword: dict[str, Any]) -> None:
        async with self._turn_lock:
            rom_listen_tx = ""
            try:
                if self._rom.connected:
                    # ROM Listen supplies the robot-side visual/attention cue;
                    # transcript capture remains on the local ASR service.
                    try:
                        rom_listen_tx = await self._rom.listen(self._max_speech_ms, self._max_silence_ms)
                    except Exception:
                        logger.debug("ROM Listen cue failed; continuing with local ASR", exc_info=True)
                text = await listen_once(
                    self._host,
                    self._asr_port,
                    max_speech_ms=self._max_speech_ms,
                    max_silence_ms=self._max_silence_ms,
                )
                if not text:
                    logger.info("Jibo wake-word produced no transcript")
                    return
                source = self.build_source(
                    chat_id=self._chat_id,
                    chat_name=self._host,
                    chat_type="dm",
                    user_id="jibo-local-user",
                    user_name="Jibo user",
                )
                extra = self.config.extra or {}
                self._active_source = source
                self._active_session_key = build_session_key(
                    source,
                    group_sessions_per_user=extra.get("group_sessions_per_user", True),
                    thread_sessions_per_user=extra.get("thread_sessions_per_user", False),
                    profile=self._session_key_profile(source),
                )
                if is_cancel_phrase(text) and (
                    self._active_session_key in getattr(self, "_active_sessions", {})
                    or bool(getattr(self, "_processing_turn_id", None))
                    or bool(getattr(self._rom, "_active_say_tx", ""))
                ):
                    logger.info("Jibo voice cancellation phrase received")
                    async with self._tap_cancel_lock:
                        await self._cancel_current_turn_from_tap()
                    return
                event = MessageEvent(
                    text=text,
                    message_type=MessageType.TEXT,
                    source=source,
                    message_id="jibo-" + uuid.uuid4().hex,
                    raw_message={"hotword": hotword, "robot_host": self._host},
                    timestamp=datetime.now(timezone.utc),
                )
                logger.info("Jibo transcript received: %s", text[:120])
                await self.handle_message(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Jibo turn failed")
            finally:
                if rom_listen_tx:
                    await self._rom.cancel(rom_listen_tx)

    @staticmethod
    def _log_background_future(future) -> None:
        try:
            future.result()
        except (asyncio.CancelledError, concurrent.futures.CancelledError):
            return
        except Exception:
            logger.debug("Jibo background status task failed", exc_info=True)

    def _submit_background(self, coroutine) -> bool:
        """Schedule a coroutine on the ROM loop without blocking hook callers."""
        loop = self._loop
        if loop is None or not loop.is_running():
            coroutine.close()
            return False
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            task = asyncio.create_task(coroutine, name="jibo-background-status")
            task.add_done_callback(self._log_background_future)
            return True
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except Exception:
            coroutine.close()
            logger.debug("Unable to schedule Jibo background status", exc_info=True)
            return False
        future.add_done_callback(self._log_background_future)
        return True

    def begin_processing(self, *, turn_id: str, user_message: str = "") -> bool:
        """Start a bounded Jibo processing indicator for one Hermes turn."""
        del user_message  # Never put the user's request on the public screen.
        with self._processing_state_lock:
            self._processing_generation += 1
            generation = self._processing_generation
            self._processing_turn_id = str(turn_id or generation)
            self._processing_stage = "THINKING"
            self._user_display_mode = "processing"
        return self._submit_background(self._begin_processing_async(generation))

    async def _begin_processing_async(self, generation: int) -> None:
        if not self._processing_is_current(generation):
            return
        old_task = self._processing_task
        if old_task is not None and old_task is not asyncio.current_task():
            old_task.cancel()
            self._processing_task = None
        await self._set_processing_screen("THINKING")
        if not self._processing_is_current(generation):
            return
        task = asyncio.create_task(
            self._processing_status_loop(generation),
            name="jibo-processing-status",
        )
        self._processing_task = task

    def _processing_is_current(self, generation: int) -> bool:
        with self._processing_state_lock:
            return (
                self._processing_turn_id is not None
                and self._processing_generation == generation
                and not self._closing
            )

    async def _set_processing_screen(self, stage: str) -> None:
        with self._processing_state_lock:
            if self._user_display_mode == "image":
                return
        if not self._rom.connected:
            return
        try:
            await self._rom.display_text(
                processing_screen_text(stage),
                name=PROCESSING_SCREEN_NAME,
            )
        except Exception:
            logger.debug("Jibo processing screen update failed", exc_info=True)

    async def _clear_processing_screen(self) -> None:
        if not self._rom.connected:
            return
        try:
            await self._rom.display_eye(name="default")
            with self._processing_state_lock:
                self._user_display_mode = "eye"
        except Exception:
            logger.debug("Jibo eye-view restore failed", exc_info=True)

    async def _processing_status_loop(self, generation: int) -> None:
        try:
            await self._speak_esml(
                format_jibo_speech("Okay, I'm on it.", kind="thinking"),
                display_pairing=False,
                clear_screen_after=False,
            )
            index = 0
            while self._processing_is_current(generation):
                await asyncio.sleep(PROCESSING_INTERVAL_SECONDS)
                if not self._processing_is_current(generation):
                    return
                with self._processing_state_lock:
                    if self._user_display_mode == "image":
                        continue
                phrase = PROCESSING_STATUS_PHRASES[index % len(PROCESSING_STATUS_PHRASES)]
                stage = "CHECKING" if index % 2 else "WORKING"
                index += 1
                with self._processing_state_lock:
                    self._processing_stage = stage
                await self._set_processing_screen(stage)
                await self._speak_esml(
                    format_jibo_speech(phrase, kind="progress"),
                    display_pairing=False,
                    clear_screen_after=False,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Jibo periodic processing status failed", exc_info=True)

    def update_processing_stage(self, stage: str) -> bool:
        """Update only the public high-level processing label."""
        return self._submit_background(self._update_processing_stage_async(stage))

    async def _update_processing_stage_async(self, stage: str) -> None:
        with self._processing_state_lock:
            if (
                self._processing_turn_id is None
                or self._closing
                or self._user_display_mode == "image"
            ):
                return
            self._processing_stage = str(stage or "PROCESSING").upper()
            current_stage = self._processing_stage
        await self._set_processing_screen(current_stage)

    def finish_processing(self, *, success: bool) -> bool:
        """Stop periodic status and show a short final/error screen state."""
        return self._submit_background(self._finish_processing_async(success))

    async def _finish_processing_async(self, success: bool) -> None:
        with self._processing_state_lock:
            if self._processing_turn_id is None and self._processing_task is None:
                return
            task = self._processing_task
            self._processing_task = None
            self._processing_turn_id = None
            self._processing_generation += 1
            generation = self._processing_generation
            stage = "ANSWERING" if success else "ERROR"
            preserve_image = self._user_display_mode == "image"
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if not preserve_image:
            await self._set_processing_screen(stage)
        if not success:
            asyncio.create_task(
                self._clear_processing_screen_after(8.0, generation),
                name="jibo-error-screen-clear",
            )

    async def _clear_processing_screen_after(self, delay: float, generation: int) -> None:
        await asyncio.sleep(delay)
        with self._processing_state_lock:
            if self._processing_turn_id is not None or self._processing_generation != generation:
                return
        await self._clear_processing_screen()

    async def _speak_esml(
        self,
        content: str,
        *,
        display_pairing: bool = False,
        clear_screen_after: bool = False,
    ) -> SendResult:
        """Send ROM-ready speech, optionally displaying a pairing code."""
        if not self._rom.connected:
            return SendResult(success=False, error="Jibo ROM is not connected", retryable=True)
        chunks = prepare_for_jibo(content)
        if not chunks:
            return SendResult(success=True, message_id="empty")
        last_tx = None
        async with self._speak_lock:
            if display_pairing:
                pairing_code = _pairing_code_from_message(content)
                if pairing_code:
                    try:
                        await self._rom.display_text(
                            f"PAIRING CODE\n{pairing_code}",
                            name="hermes-pairing",
                        )
                    except Exception as exc:
                        # Speech remains the fallback if the optional screen
                        # view is unavailable on a particular ROM build.
                        logger.warning("Unable to display Jibo pairing code: %s", exc)
            for chunk in chunks:
                try:
                    last_tx, _response = await self._rom.say(chunk, timeout=60.0)
                except (RomError, asyncio.TimeoutError) as exc:
                    logger.error("Jibo ROM Say failed: %s", exc)
                    return SendResult(
                        success=False,
                        message_id=last_tx,
                        error=str(exc),
                        retryable=True,
                    )
                except Exception as exc:
                    logger.exception("Jibo speech delivery failed")
                    return SendResult(success=False, message_id=last_tx, error=str(exc), retryable=True)
                await asyncio.sleep(0.15)
        if clear_screen_after:
            await self._clear_processing_screen()
        return SendResult(success=True, message_id=last_tx)

    def _mark_tool_answer_spoken(self) -> None:
        with self._speech_state_lock:
            self._tool_spoken_deadline = time.monotonic() + _TOOL_SPOKEN_GRACE_SECONDS

    def _consume_tool_answer_spoken(self) -> bool:
        with self._speech_state_lock:
            deadline = self._tool_spoken_deadline
            if not deadline:
                return False
            self._tool_spoken_deadline = 0.0
            return time.monotonic() <= deadline

    def _run_rom_operation(self, coroutine_factory, *, timeout: float = 15.0) -> SendResult:
        """Run one ROM operation from a synchronous plugin-tool worker."""
        if not self._rom.connected:
            return SendResult(success=False, error="Jibo ROM is not connected", retryable=True)
        loop = self._loop
        if loop is None or not loop.is_running():
            return SendResult(success=False, error="Jibo ROM event loop is not available", retryable=True)
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            return SendResult(success=False, error="Jibo operation called on the ROM event loop")
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine_factory(), loop)
            return future.result(timeout=timeout)
        except (TimeoutError, concurrent.futures.TimeoutError):
            try:
                future.cancel()
            except Exception:
                pass
            return SendResult(success=False, error="Jibo display operation timed out", retryable=True)
        except Exception as exc:
            logger.debug("Jibo display operation failed", exc_info=True)
            return SendResult(success=False, error=str(exc), retryable=True)

    def display_image(self, url: str, *, name: str = "hermes-image") -> SendResult:
        """Display one user-requested remotely reachable image on Jibo."""
        return self._run_rom_operation(
            lambda: self._display_image_on_loop(url, name=name),
            timeout=15.0,
        )

    def _media_host_address(self) -> str:
        if self._media_host:
            return self._media_host
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((self._host, self._rom_port))
            address = probe.getsockname()[0]
        if not address or address.startswith("127."):
            raise RuntimeError("could not determine a LAN address reachable by Jibo")
        return address

    def _media_cache_directory(self) -> Path:
        with self._media_lock:
            if self._media_cache_dir is None:
                hermes_home = Path(
                    os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")
                ).expanduser()
                root = hermes_home / "cache" / "jibo-images"
                root.mkdir(parents=True, exist_ok=True)
                self._media_cache_dir = Path(
                    tempfile.mkdtemp(prefix=f"{os.getpid()}-", dir=str(root))
                )
            return self._media_cache_dir

    def _serve_media_file(self, file_path: str) -> str:
        """Register one prepared file behind a short-lived random URL."""
        if not os.path.isfile(file_path):
            raise ValueError("prepared image file does not exist")
        with self._media_lock:
            server = self._media_server
            if server is None:
                server = _JiboMediaHTTPServer(("0.0.0.0", 0))
                thread = threading.Thread(
                    target=server.serve_forever,
                    name="jibo-media-server",
                    daemon=True,
                )
                thread.start()
                self._media_server = server
                self._media_server_thread = thread
            token = secrets.token_urlsafe(24)
            server.files[token] = file_path
            while len(server.files) > 32:
                server.files.pop(next(iter(server.files)), None)
            port = int(server.server_address[1])
        return f"http://{self._media_host_address()}:{port}/jibo-media/{token}"

    def _stop_media_server(self) -> None:
        with self._media_lock:
            server = self._media_server
            cache_dir = self._media_cache_dir
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
        if cache_dir is not None:
            shutil.rmtree(cache_dir, ignore_errors=True)

    def _local_media_url(self, image_path: str) -> str:
        safe_path = self.validate_media_delivery_path(image_path)
        if not safe_path or not os.path.isfile(safe_path):
            raise ValueError("local image path is not a readable file")
        if os.path.splitext(safe_path)[1].lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
            raise ValueError("local path is not a supported image")
        from .media import letterbox_image

        normalized_path = letterbox_image(safe_path, self._media_cache_directory())
        return self._serve_media_file(str(normalized_path))

    async def _materialize_remote_image_on_loop(self, image_url: str) -> str:
        parsed = urlsplit(image_url)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Jibo requires a credential-free HTTP(S) image URL")
        import httpx
        from .media import letterbox_image

        max_bytes = 16 * 1024 * 1024
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"Accept": "image/*"},
        ) as client:
            async with client.stream("GET", image_url) as response:
                response.raise_for_status()
                final_url = urlsplit(str(response.url))
                if (
                    final_url.scheme.lower() not in {"http", "https"}
                    or final_url.username is not None
                    or final_url.password is not None
                ):
                    raise ValueError("image URL redirected to an unsafe destination")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                suffix = mimetypes.guess_extension(content_type) or Path(final_url.path).suffix.lower()
                if not suffix or suffix not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
                    raise ValueError("URL did not return a supported image")
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > max_bytes:
                    raise ValueError("remote image is too large")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("remote image is too large")
                    chunks.append(chunk)
        raw_path = self._media_cache_directory() / f"remote-{secrets.token_hex(8)}{suffix}"
        raw_path.write_bytes(b"".join(chunks))
        try:
            normalized_path = letterbox_image(str(raw_path), self._media_cache_directory())
        finally:
            try:
                raw_path.unlink()
            except OSError:
                pass
        return str(normalized_path)

    def display_local_image(self, image_path: str, *, name: str = "hermes-image") -> SendResult:
        """Serve and display a validated host-local image from a tool worker."""
        return self._run_rom_operation(
            lambda: self._display_local_image_on_loop(image_path, name=name),
            timeout=15.0,
        )

    async def _display_local_image_on_loop(self, image_path: str, *, name: str) -> SendResult:
        try:
            url = self._local_media_url(image_path)
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=False)
        return await self._display_image_view_on_loop(url, name=name)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        """Display a local attachment on Jibo instead of sending file://."""
        del chat_id, caption, reply_to, metadata, kwargs
        return await self._display_local_image_on_loop(image_path, name="hermes-image")

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Display a remote image URL on Jibo's screen."""
        del chat_id, caption, reply_to, metadata
        if not isinstance(image_url, str) or not image_url.strip().lower().startswith(("http://", "https://")):
            return SendResult(success=False, error="Jibo requires an HTTP(S) image URL", retryable=False)
        return await self._display_image_on_loop(image_url.strip(), name="hermes-image")

    async def _display_image_on_loop(self, url: str, *, name: str) -> SendResult:
        try:
            normalized_path = await self._materialize_remote_image_on_loop(url)
            prepared_url = self._serve_media_file(normalized_path)
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=False)
        return await self._display_image_view_on_loop(prepared_url, name=name)

    async def _display_image_view_on_loop(self, url: str, *, name: str) -> SendResult:
        try:
            await self._rom.display_image(url, name=name)
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
        with self._processing_state_lock:
            self._user_display_mode = "image"
        return SendResult(success=True, message_id="display-image")

    def show_eye(self, *, name: str = "default") -> SendResult:
        """Restore Jibo's normal eye view from a synchronous tool worker."""
        return self._run_rom_operation(
            lambda: self._show_eye_on_loop(name=name),
            timeout=10.0,
        )

    async def _show_eye_on_loop(self, *, name: str = "default") -> SendResult:
        try:
            await self._rom.display_eye(name=name)
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
        with self._processing_state_lock:
            self._user_display_mode = "eye"
        return SendResult(success=True, message_id="display-eye")

    def play_expression(self, name: str, *, text: str = "") -> SendResult:
        """Run one catalog-confirmed animation through ROM Say."""
        return self._run_rom_operation(
            lambda: self._play_expression_on_loop(name, text=text),
            timeout=180.0,
        )

    async def _play_expression_on_loop(self, name: str, *, text: str = "") -> SendResult:
        from .catalog import expression_to_esml

        esml = expression_to_esml(name, text=text)
        if not esml:
            return SendResult(success=False, error="expression name is empty", retryable=False)
        return await self._speak_esml(
            esml,
            display_pairing=False,
            clear_screen_after=False,
        )

    def speak_explicit(
        self,
        text: str,
        *,
        kind: str = "answer",
        style: str = "neutral",
        final: bool = False,
    ) -> SendResult:
        """Speak intentional Jibo text from a synchronous tool worker."""
        if not self._rom.connected:
            return SendResult(success=False, error="Jibo ROM is not connected", retryable=True)
        loop = self._loop
        if loop is None or not loop.is_running():
            return SendResult(success=False, error="Jibo ROM event loop is not available", retryable=True)
        formatted = format_jibo_speech(text, kind=kind, style=style)
        if not formatted:
            return SendResult(success=True, message_id="empty")
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            return SendResult(success=False, error="Jibo speech tool called on the ROM event loop")

        if final:
            # Stop the periodic status loop before the final intentional
            # utterance is queued, so a progress beat cannot interrupt it.
            self.finish_processing(success=True)
        future = asyncio.run_coroutine_threadsafe(
            self._speak_esml(formatted, clear_screen_after=final),
            loop,
        )
        try:
            result = future.result(timeout=90.0)
        except TimeoutError:
            future.cancel()
            return SendResult(success=False, error="Jibo speech timed out", retryable=True)
        except Exception as exc:
            future.cancel()
            logger.exception("Explicit Jibo speech failed")
            return SendResult(success=False, error=str(exc), retryable=True)
        if final and result.success:
            self._mark_tool_answer_spoken()
        return result

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        del chat_id, reply_to
        # Background-review notices and other gateway-only progress sends carry
        # this marker. They are not conversational speech for Jibo.
        if isinstance(metadata, dict) and metadata.get("_interim_send"):
            return SendResult(success=True, message_id="suppressed-interim")
        # If the model already used jibo_speak(final=true), don't repeat the
        # same answer when the normal gateway final-delivery rail runs.
        if self._consume_tool_answer_spoken():
            return SendResult(success=True, message_id="spoken-by-jibo-tool")
        return await self._speak_esml(
            content,
            display_pairing=True,
            clear_screen_after=True,
        )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        del chat_id, metadata
        # Jibo has no useful separate typing indicator; intentional speech cues
        # and the final answer are the user-visible activity signals.
        return None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": chat_id, "type": "dm", "robot": self._host}
