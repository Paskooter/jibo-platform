"""Local Jibo ASR and wake-word WebSocket helpers.

PVindex documents the resident local ASR service on port 8088, with result
streams at ``/simple_port`` and start/stop requests at
``/asr_simple_interface``. This module keeps the wake-word socket separate
from each post-wake transcription socket, matching the archived client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

import httpx
import websockets

logger = logging.getLogger(__name__)


Callback = Callable[[dict[str, Any]], Awaitable[None]]


def _event_type(event: dict[str, Any]) -> str:
    value = (
        event.get("event_type")
        or event.get("eventType")
        or event.get("event")
        or event.get("type")
        or ""
    )
    return str(value).lower().replace("-", "_")


def _task_matches(event: dict[str, Any], task_id: str, request_id: str) -> bool:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    event_task = event.get("task_id") or event.get("taskId") or payload.get("task_id")
    event_request = event.get("request_id") or event.get("requestId") or payload.get("request_id")
    if not event_task and not event_request:
        return True
    return event_task == task_id or event_request == request_id


def _utterance_text(event: dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    values: Any = (
        event.get("utterances")
        or event.get("Utterances")
        or payload.get("utterances")
        or event.get("transcript")
        or event.get("text")
        or event.get("speech_to_text")
        or ""
    )
    if isinstance(values, list):
        values = values[0] if values else ""
    if isinstance(values, dict):
        values = values.get("utterance") or values.get("Utterance") or values.get("text") or ""
    return str(values).strip()


class WakewordWatcher:
    """Reconnectable listener for the robot's resident Hey Jibo detector."""

    def __init__(self, host: str, port: int = 8088, reconnect_seconds: float = 3.0):
        self.host = host
        self.port = port
        self.reconnect_seconds = reconnect_seconds
        self._stop = asyncio.Event()
        self._ws = None

    async def run(self, callback: Callback) -> None:
        backoff = self.reconnect_seconds
        while not self._stop.is_set():
            try:
                uri = f"ws://{self.host}:{self.port}/simple_port"
                # Mark-I's legacy WebSocket server emits malformed control
                # frames when the modern client sends WebSocket pings. The
                # archived Jibo client leaves protocol-level pings disabled;
                # the short-lived ASR capture does not need them.
                async with websockets.connect(uri, ping_interval=None, ping_timeout=None, open_timeout=10) as ws:
                    self._ws = ws
                    backoff = self.reconnect_seconds
                    logger.info("Jibo local ASR wake-word stream connected")
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            event = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        kind = _event_type(event)
                        if kind not in {"hotphrase", "hotword", "hot_word", "hot_phrase"}:
                            continue
                        utterance = _utterance_text(event) or "hey jibo"
                        await callback({
                            "utterance": utterance,
                            "score": event.get("score") or (event.get("utterances") or [{}])[0].get("score", 0)
                            if isinstance(event.get("utterances"), list) else 0,
                            "raw": event,
                        })
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._stop.is_set():
                    logger.warning("Jibo wake-word stream failed: %s; retrying", exc)
            finally:
                self._ws = None
            if not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    backoff = min(backoff * 2, 30.0)

    async def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass


async def listen_once(
    host: str,
    port: int = 8088,
    *,
    max_speech_ms: int = 15000,
    max_silence_ms: int = 5000,
) -> Optional[str]:
    """Capture one local-ASR utterance and return final text, if any."""
    task_id = "hermes-" + uuid.uuid4().hex
    request_id = "start-" + uuid.uuid4().hex
    uri = f"ws://{host}:{port}/simple_port"
    start_payload = {
        "command": "start",
        "task_id": task_id,
        "request_id": request_id,
        "audio_source_id": "alsa1",
        "hotphrase": "none",
        "speech_to_text": True,
    }
    stop_payload = {
        "command": "stop",
        "task_id": task_id,
        "request_id": "stop-" + uuid.uuid4().hex,
    }
    deadline = time.monotonic() + max(1000, max_speech_ms, max_silence_ms) / 1000.0 + 3.0
    try:
        async with websockets.connect(uri, ping_interval=None, ping_timeout=None, open_timeout=10) as ws:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.post(
                    f"http://{host}:{port}/asr_simple_interface",
                    json=start_payload,
                )
                response.raise_for_status()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    try:
                        event = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if not _task_matches(event, task_id, request_id):
                        continue
                    kind = _event_type(event)
                    final = (
                        kind in {"speech_to_text_final", "stt_final", "transcription_final", "final"}
                        or event.get("final_simple") is True
                        or event.get("final_composite") is True
                        or event.get("final") is True
                    )
                    if final:
                        text = _utterance_text(event)
                        if text:
                            return text
                        return None
    except asyncio.TimeoutError:
        return None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Jibo local ASR listen failed: %s", exc)
        return None
    finally:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"http://{host}:{port}/asr_simple_interface",
                    json=stop_payload,
                )
        except Exception:
            pass
