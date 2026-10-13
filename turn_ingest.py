"""The turn-ingest listener — Phoenix's way into the one conversation.

Phoenix restores the robot's stock voice stack and answers most turns by
itself.  Two things have to reach Hermes for the conversation to stay whole:

``POST /jibo/v1/turns``
    A completed turn, *including one Phoenix handled entirely on its own*.
    Without this the on-screen history has holes exactly where the robot
    worked best.  Idempotent on ``turn_id``; a later report of the same turn
    completes the earlier one rather than duplicating it.

``POST /jibo/v1/chat/completions``
    The escalation hook.  Phoenix's answer-skill and parser fallback already
    POST to an OpenAI-compatible ``{LLM_URL}/chat/completions``; pointing
    ``ETCO_answer_llmUrl`` at this listener instead of at a bare model
    endpoint means the request arrives carrying robot identity, so Hermes
    *correlates* the turn instead of guessing which robot asked.

This listener is deliberately owned by the plugin rather than by Hermes'
own OpenAI-compatible API server.  It needs no gateway-wide surface to be
switched on, it can bind loopback-only, and it can do the bridge-side work
(streaming deltas, turn rendering) that a generic completions endpoint
cannot.  Consolidating onto ``gateway/platforms/api_server.py`` later is a
configuration change, not a redesign.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import hmac
import json
import logging
import os
import time
from typing import Any, Callable, Optional
import uuid

from .turn_record import (
    MAX_INGEST_BYTES,
    ORIGIN_PHOENIX,
    ORIGIN_PHOENIX_ESCALATED,
    SPEECH_HERMES,
    SPEECH_NONE,
    SPEECH_PHOENIX,
    TurnRecord,
    TurnRecordError,
    new_turn_id,
)

logger = logging.getLogger(__name__)

DEFAULT_INGEST_HOST = "127.0.0.1"
DEFAULT_INGEST_PORT = 8891
DEFAULT_ESCALATION_TIMEOUT = 120.0
API_PREFIX = "/jibo/v1"
MAX_BATCH = 16
#: Advertised as the model name so an OpenAI-shaped client has something
#: stable to configure; the real model is whatever Hermes is running.
VIRTUAL_MODEL = "hermes-jibo"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class IngestConfigError(ValueError):
    """The listener was configured in a way that cannot be made safe."""


@dataclass(frozen=True)
class TurnIngestConfig:
    """Validated settings for the ingest listener."""

    enabled: bool = False
    host: str = DEFAULT_INGEST_HOST
    port: int = DEFAULT_INGEST_PORT
    token: str = ""
    default_device_id: str = ""
    escalation_timeout: float = DEFAULT_ESCALATION_TIMEOUT
    attribute_skill: bool = False
    allow_insecure_local: bool = False

    @classmethod
    def from_extra(
        cls,
        extra: Mapping[str, Any] | None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> "TurnIngestConfig":
        values = (extra or {}).get("turn_ingest")
        values = dict(values) if isinstance(values, Mapping) else {}
        env = os.environ if environ is None else environ
        token = str(values.get("token") or env.get("JIBO_TURN_INGEST_TOKEN") or "").strip()
        host = str(values.get("host") or DEFAULT_INGEST_HOST).strip() or DEFAULT_INGEST_HOST
        enabled = _as_bool(values.get("enabled"), False)
        allow_insecure_local = _as_bool(values.get("allow_insecure_local"), False)
        if enabled and not token:
            # Fail closed.  An unauthenticated writer into canonical history
            # could put words in the user's mouth, so the only way to run
            # without a token is an explicit loopback development opt-in.
            if not (allow_insecure_local and host in _LOOPBACK_HOSTS):
                raise IngestConfigError(
                    "turn_ingest needs a token (extra.turn_ingest.token or "
                    "JIBO_TURN_INGEST_TOKEN); set allow_insecure_local with a "
                    "loopback host only for local development"
                )
        try:
            port = int(values.get("port", DEFAULT_INGEST_PORT))
        except (TypeError, ValueError) as exc:
            raise IngestConfigError("turn_ingest.port must be a number") from exc
        if not 1 <= port <= 65535:
            raise IngestConfigError("turn_ingest.port must be between 1 and 65535")
        try:
            timeout = float(values.get("escalation_timeout", DEFAULT_ESCALATION_TIMEOUT))
        except (TypeError, ValueError):
            timeout = DEFAULT_ESCALATION_TIMEOUT
        return cls(
            enabled=enabled,
            host=host,
            port=port,
            token=token,
            default_device_id=str(values.get("default_device_id") or "").strip(),
            escalation_timeout=max(5.0, min(timeout, 600.0)),
            attribute_skill=_as_bool(values.get("attribute_skill"), False),
            allow_insecure_local=allow_insecure_local,
        )


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


class TurnIngestError(Exception):
    """A request could not be served; carries an HTTP status."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class TurnIngestHandlers:
    """The transport-free half of the listener.

    Every route is a plain coroutine over dicts, so the whole contract can be
    exercised without binding a socket or importing aiohttp.
    """

    def __init__(self, adapter: Any, config: TurnIngestConfig):
        self._adapter = adapter
        self._config = config

    # ── auth ──────────────────────────────────────────────────────────
    def authorize(self, authorization: str = "") -> None:
        expected = self._config.token
        if not expected:
            return
        header = str(authorization or "").strip()
        prefix = "bearer "
        presented = header[len(prefix):].strip() if header[: len(prefix)].lower() == prefix else ""
        if not presented or not hmac.compare_digest(presented, expected):
            raise TurnIngestError(401, "unauthorized", "a valid bearer token is required")

    # ── routes ────────────────────────────────────────────────────────
    async def health(self) -> dict[str, Any]:
        devices = list(getattr(self._adapter, "device_ids", ()) or ())
        return {
            "ok": True,
            "service": "jibo-turn-ingest",
            "turn_record_version": 1,
            "devices": devices,
            "connected": bool(getattr(self._adapter, "connected", False)),
        }

    async def ingest_turns(self, payload: Any) -> dict[str, Any]:
        """Record one or more completed turns reported by Phoenix."""
        entries = payload.get("turns") if isinstance(payload, Mapping) else None
        if entries is None:
            entries = [payload]
        if not isinstance(entries, list):
            raise TurnIngestError(422, "invalid_body", "turns must be a list of records")
        if not entries:
            raise TurnIngestError(422, "invalid_body", "no turn records supplied")
        if len(entries) > MAX_BATCH:
            raise TurnIngestError(422, "invalid_body", f"at most {MAX_BATCH} records per request")
        results: list[dict[str, Any]] = []
        for entry in entries:
            try:
                record = TurnRecord.from_mapping(
                    entry, default_device_id=self._config.default_device_id
                )
            except TurnRecordError as exc:
                raise TurnIngestError(422, "invalid_record", str(exc)) from exc
            if not self._adapter.has_device(record.device_id):
                raise TurnIngestError(
                    404, "unknown_device", f"no Jibo device named {record.device_id}"
                )
            outcome = await self._adapter.record_turn(
                record, attribute_skill=self._config.attribute_skill
            )
            results.append(outcome)
        return {"recorded": results}

    async def chat_completions(
        self,
        payload: Any,
        headers: Mapping[str, str] | None = None,
        *,
        on_delta: Optional[Callable[[str], Any]] = None,
    ) -> dict[str, Any]:
        """Answer an escalation, correlated to the robot that asked."""
        if not isinstance(payload, Mapping):
            raise TurnIngestError(422, "invalid_body", "the request body must be a JSON object")
        head = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
        jibo = payload.get("jibo") if isinstance(payload.get("jibo"), Mapping) else {}

        device_id = (
            head.get("x-jibo-device-id")
            or str(jibo.get("device_id") or "")
            or head.get("x-hermes-session-id", "").replace("jibo:", "")
            or self._config.default_device_id
        ).strip()
        if not device_id:
            raise TurnIngestError(
                400,
                "device_required",
                "the escalation must name a robot: send X-Jibo-Device-Id, or configure "
                "turn_ingest.default_device_id",
            )
        if not self._adapter.has_device(device_id):
            raise TurnIngestError(404, "unknown_device", f"no Jibo device named {device_id}")

        turn_id = (head.get("x-jibo-turn-id") or str(jibo.get("turn_id") or "")).strip()
        speech_owner = (
            head.get("x-jibo-speech-owner") or str(jibo.get("speech_owner") or "")
        ).strip().lower() or SPEECH_PHOENIX
        if speech_owner not in {SPEECH_PHOENIX, SPEECH_HERMES, SPEECH_NONE}:
            raise TurnIngestError(
                422, "invalid_speech_owner", "speech_owner must be phoenix, hermes, or none"
            )

        prompt = _last_user_message(payload.get("messages"))
        if not prompt:
            raise TurnIngestError(422, "empty_prompt", "no user message to answer")

        correlation = {
            "phoenix_session_id": str(jibo.get("session_id") or head.get("x-jibo-session-id") or ""),
            "trace_id": str(jibo.get("trace_id") or head.get("x-jibo-trace-id") or ""),
        }
        started = time.time()
        try:
            outcome = await self._adapter.escalate_turn(
                device_id=device_id,
                text=prompt,
                turn_id=turn_id or new_turn_id(ORIGIN_PHOENIX_ESCALATED),
                speech_owner=speech_owner,
                intent=str(jibo.get("intent") or ""),
                skill=str(jibo.get("skill") or "answer-skill"),
                correlation={key: value for key, value in correlation.items() if value},
                timeout=self._config.escalation_timeout,
                attribute_skill=self._config.attribute_skill,
                on_delta=on_delta,
            )
        except asyncio.TimeoutError as exc:
            raise TurnIngestError(
                504, "escalation_timeout", "Hermes did not answer in time"
            ) from exc
        answer = str(outcome.get("text") or "")
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(started),
            "model": str(payload.get("model") or VIRTUAL_MODEL),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": answer},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            # Extension block.  Phoenix reads ``spoken`` to decide whether to
            # speak the answer itself, and echoes ``turn_id`` back on the
            # turn report so the two halves join into one history entry.
            "jibo": {
                "turn_id": outcome.get("turn_id"),
                "device_id": device_id,
                "spoken": bool(outcome.get("spoken")),
                "status": outcome.get("status", "ok"),
                "elapsed_ms": int((time.time() - started) * 1000),
            },
        }


def _last_user_message(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for entry in reversed(messages):
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("role") or "").lower() != "user":
            continue
        content = entry.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [
                str(part.get("text") or "")
                for part in content
                if isinstance(part, Mapping) and part.get("type") in {None, "text"}
            ]
            joined = " ".join(part for part in parts if part).strip()
            if joined:
                return joined
    return ""


class TurnIngestServer:
    """Binds :class:`TurnIngestHandlers` to an aiohttp listener."""

    def __init__(self, adapter: Any, config: TurnIngestConfig):
        self.config = config
        self.handlers = TurnIngestHandlers(adapter, config)
        self._runner: Any = None
        self._site: Any = None

    @property
    def running(self) -> bool:
        return self._runner is not None

    async def start(self) -> bool:
        if not self.config.enabled or self._runner is not None:
            return False
        try:
            from aiohttp import web
        except ImportError:
            logger.warning("Jibo turn ingest needs aiohttp; the listener stays off")
            return False

        app = web.Application(client_max_size=MAX_INGEST_BYTES)
        app.router.add_get(f"{API_PREFIX}/health", self._wrap(self._health))
        app.router.add_post(f"{API_PREFIX}/turns", self._wrap(self._turns))
        app.router.add_post(f"{API_PREFIX}/chat/completions", self._wrap(self._completions))
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.config.host, self.config.port)
        try:
            await site.start()
        except OSError as exc:
            await runner.cleanup()
            logger.warning(
                "Jibo turn ingest could not bind %s:%s: %s",
                self.config.host,
                self.config.port,
                exc,
            )
            return False
        self._runner = runner
        self._site = site
        logger.info(
            "Jibo turn ingest listening on http://%s:%s%s",
            self.config.host,
            self.config.port,
            API_PREFIX,
        )
        return True

    async def stop(self) -> None:
        runner = self._runner
        self._runner = None
        self._site = None
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception:
                logger.debug("Jibo turn ingest shutdown failed", exc_info=True)

    # ── aiohttp glue ──────────────────────────────────────────────────
    def _wrap(self, handler):
        from aiohttp import web

        async def route(request):
            try:
                return await handler(request)
            except TurnIngestError as exc:
                return web.json_response(
                    {"error": {"code": exc.code, "message": exc.message}}, status=exc.status
                )
            except Exception:
                logger.warning("Jibo turn ingest request failed", exc_info=True)
                return web.json_response(
                    {"error": {"code": "internal_error", "message": "request failed"}},
                    status=500,
                )

        return route

    async def _health(self, request):
        from aiohttp import web

        return web.json_response(await self.handlers.health())

    async def _read_json(self, request) -> Any:
        self.handlers.authorize(request.headers.get("Authorization", ""))
        try:
            body = await request.read()
        except Exception as exc:
            raise TurnIngestError(400, "unreadable_body", "could not read the request body") from exc
        if len(body) > MAX_INGEST_BYTES:
            raise TurnIngestError(413, "body_too_large", "the request body is too large")
        try:
            return json.loads(body or b"{}")
        except (TypeError, ValueError) as exc:
            raise TurnIngestError(400, "invalid_json", "the request body is not valid JSON") from exc

    async def _turns(self, request):
        from aiohttp import web

        payload = await self._read_json(request)
        return web.json_response(await self.handlers.ingest_turns(payload), status=202)

    async def _completions(self, request):
        from aiohttp import web

        payload = await self._read_json(request)
        if not _as_bool(payload.get("stream") if isinstance(payload, Mapping) else False):
            return web.json_response(
                await self.handlers.chat_completions(payload, request.headers)
            )
        return await self._streaming_completions(request, payload)

    async def _streaming_completions(self, request, payload):
        """Serve ``stream: true`` as SSE, fed by the same delta pipeline.

        When Hermes streaming is off for this platform the queue simply
        receives one chunk at the end; the client sees a valid, if abrupt,
        stream rather than an error.
        """
        from aiohttp import web

        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        loop = asyncio.get_running_loop()

        def on_delta(text: str) -> None:
            if not text:
                return
            try:
                loop.call_soon_threadsafe(queue.put_nowait, text)
            except (RuntimeError, asyncio.QueueFull):
                logger.debug("Jibo escalation delta dropped", exc_info=True)

        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )
        await response.prepare(request)
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        model = str(payload.get("model") or VIRTUAL_MODEL)
        task = asyncio.ensure_future(
            self.handlers.chat_completions(payload, request.headers, on_delta=on_delta)
        )
        sent_any = False
        try:
            while True:
                getter = asyncio.ensure_future(queue.get())
                done, _pending = await asyncio.wait(
                    {getter, task}, return_when=asyncio.FIRST_COMPLETED
                )
                if getter in done:
                    chunk = getter.result()
                    sent_any = True
                    await _write_sse(response, completion_id, model, {"content": chunk})
                    continue
                getter.cancel()
                break
            result = await task
            while not queue.empty():
                await _write_sse(
                    response, completion_id, model, {"content": queue.get_nowait()}
                )
                sent_any = True
            if not sent_any:
                answer = result["choices"][0]["message"]["content"]
                await _write_sse(response, completion_id, model, {"content": answer})
            await _write_sse(response, completion_id, model, {}, finish_reason="stop")
            await response.write(b"data: [DONE]\n\n")
        except TurnIngestError as exc:
            await response.write(
                f"data: {json.dumps({'error': {'code': exc.code, 'message': exc.message}})}\n\n".encode()
            )
        finally:
            if not task.done():
                task.cancel()
            await response.write_eof()
        return response


async def _write_sse(
    response: Any,
    completion_id: str,
    model: str,
    delta: dict[str, Any],
    *,
    finish_reason: Optional[str] = None,
) -> None:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    await response.write(f"data: {json.dumps(payload)}\n\n".encode())


__all__ = [
    "API_PREFIX",
    "DEFAULT_INGEST_PORT",
    "IngestConfigError",
    "TurnIngestConfig",
    "TurnIngestError",
    "TurnIngestHandlers",
    "TurnIngestServer",
    "VIRTUAL_MODEL",
]
