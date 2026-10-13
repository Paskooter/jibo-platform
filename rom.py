"""Direct Jibo ROM WebSocket client based on PVindex protocol records."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import httpx
import websockets

logger = logging.getLogger(__name__)


class RomError(RuntimeError):
    """ROM transport or protocol failure."""


class RomDisconnected(RomError):
    """ROM WebSocket went away while a command was pending."""


EventHandler = Callable[[dict[str, Any]], Awaitable[None]]
DisconnectHandler = Callable[[], Awaitable[None]]


class RomSession:
    """One reconnectable ROM session on port 8160.

    The wire sequence is the one exercised against the restored unit:

    1. POST ``/request`` with an ACO command/stream set.
    2. Open ``ws://<robot>:8160``.
    3. Send ``StartSession`` with a ``ClientHeader``.
    4. Reuse the returned ``SessionID`` for commands and subscriptions.
    """

    COMMAND_SET = [
        "StartSession", "GetConfig", "Cancel", "Say", "Listen",
        "Subscribe", "SetConfig", "SetAttention", "LookAt",
        "TakePhoto", "Display", "FetchAsset", "UnloadAsset",
    ]
    STREAM_SET = ["Entity", "Motion", "HeadTouch", "ScreenGesture", "HotWord"]

    def __init__(
        self,
        host: str,
        port: int = 8160,
        *,
        app_id: str = "HermesJiboRom",
        heartbeat_seconds: float = 9.0,
    ) -> None:
        self.host = host
        self.port = port
        self.app_id = app_id
        self.heartbeat_seconds = heartbeat_seconds
        self.session_id = ""
        self.version = "1.0"
        self._ws = None
        self._reader_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._send_lock = asyncio.Lock()
        self._waiters: dict[str, list[tuple[set[str], asyncio.Future]]] = {}
        self._handlers: list[EventHandler] = []
        self._on_disconnect: Optional[DisconnectHandler] = None
        self._connected = asyncio.Event()
        self._closing = False
        self._active_say_tx = ""

    @property
    def connected(self) -> bool:
        return self._connected.is_set() and self._ws is not None

    def add_handler(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    def set_disconnect_handler(self, handler: DisconnectHandler) -> None:
        self._on_disconnect = handler

    async def _post_aco(self) -> None:
        body = {
            "aco": {
                "version": "1.0",
                "sourceId": self.app_id,
                "commandSet": self.COMMAND_SET,
                "streamSet": self.STREAM_SET,
                "keepAliveTimeout": 10000,
                "recoveryTimeout": 20000,
                "remoteConfig": {
                    "hideVisualCue": False,
                    "inactivityTimeout": 3600000,
                },
            }
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"http://{self.host}:{self.port}/request",
                json=body,
            )
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise RomError(f"ROM /request returned non-JSON: {response.text[:200]}") from exc
            if payload.get("accept") is False:
                raise RomError(f"ROM /request rejected ACO: {payload}")

    @staticmethod
    def _tx_id() -> str:
        return uuid.uuid4().hex

    async def _send_with_future(
        self,
        command: dict[str, Any],
        event_names: Optional[set[str]] = None,
    ) -> tuple[str, asyncio.Future]:
        ws = self._ws
        # StartSession is the one command sent after the TCP/WebSocket is up
        # but before the session ID exists.  Do not require the post-handshake
        # ``connected`` flag for that first command.
        if ws is None:
            raise RomDisconnected("ROM WebSocket is not connected")
        tx_id = self._tx_id()
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._waiters.setdefault(tx_id, []).append((event_names or set(), future))
        message = {
            "ClientHeader": {
                "TransactionID": tx_id,
                "SessionID": self.session_id,
                "AppID": self.app_id,
                "Credentials": "",
                "Version": self.version,
            },
            "Command": command,
        }
        try:
            async with self._send_lock:
                await ws.send(json.dumps(message, separators=(",", ":")))
        except Exception:
            self._remove_waiter(tx_id, future)
            raise
        return tx_id, future

    def _remove_waiter(self, tx_id: str, future: asyncio.Future) -> None:
        entries = self._waiters.get(tx_id, [])
        self._waiters[tx_id] = [(names, item) for names, item in entries if item is not future]
        if not self._waiters[tx_id]:
            self._waiters.pop(tx_id, None)

    async def _await_future(self, tx_id: str, future: asyncio.Future, timeout: float) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        finally:
            self._remove_waiter(tx_id, future)

    async def connect(self) -> None:
        if self.connected:
            return
        self._closing = False
        await self._post_aco()
        ws = await websockets.connect(
            f"ws://{self.host}:{self.port}",
            # Mark-I's legacy ROM server is incompatible with modern
            # protocol-level WebSocket pings. Keep the application-level
            # GetConfig heartbeat below instead.
            ping_interval=None,
            ping_timeout=None,
            open_timeout=10,
            close_timeout=3,
            max_size=4 * 1024 * 1024,
        )
        self._ws = ws
        self.session_id = ""
        self._reader_task = asyncio.create_task(self._reader(ws), name="jibo-rom-reader")
        tx_id, future = await self._send_with_future({"Type": "StartSession"})
        try:
            response = await self._await_future(tx_id, future, 10.0)
        except Exception:
            await self.close()
            raise
        body = response.get("Response", {}).get("ResponseBody", {})
        session_id = body.get("SessionID") or response.get("ResponseHeader", {}).get("SessionID")
        if not session_id:
            await self.close()
            raise RomError(f"ROM StartSession returned no SessionID: {response}")
        self.session_id = str(session_id)
        self.version = str(body.get("Version") or "1.0")
        self._connected.set()
        self._heartbeat_task = asyncio.create_task(self._heartbeat(), name="jibo-rom-heartbeat")
        # These are best-effort subscriptions. The adapter's primary input is
        # local ASR, while these streams are useful for later ROM expansion.
        for command in (
            {"Type": "Subscribe", "StreamType": "Entity"},
            {"Type": "Subscribe", "StreamType": "Motion"},
            {"Type": "Subscribe", "StreamType": "HeadTouch", "StreamFilter": {}},
            {
                "Type": "Subscribe",
                "StreamType": "ScreenGesture",
                "StreamFilter": {
                    "Type": "Tap",
                    "Area": {"x": 0, "y": 0, "width": 1, "height": 1},
                },
            },
        ):
            try:
                tx, waiter = await self._send_with_future(command)
                await self._await_future(tx, waiter, 5.0)
            except Exception as exc:
                logger.debug("ROM optional subscription failed: %s", exc)
        logger.info("Connected to Jibo ROM %s:%s, session=%s", self.host, self.port, self.session_id)

    async def _reader(self, ws) -> None:
        try:
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    logger.debug("Ignoring non-JSON ROM message")
                    continue
                self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closing:
                logger.warning("Jibo ROM reader stopped: %s", exc)
        finally:
            if self._ws is ws:
                self._ws = None
            self._connected.clear()
            self.session_id = ""
            self._fail_waiters(RomDisconnected("ROM WebSocket closed"))
            if not self._closing and self._on_disconnect is not None:
                try:
                    await self._on_disconnect()
                except Exception:
                    logger.debug("ROM disconnect callback failed", exc_info=True)

    def _dispatch(self, message: dict[str, Any]) -> None:
        header = message.get("EventHeader") or message.get("ResponseHeader") or {}
        tx_id = header.get("TransactionID")
        event_body = message.get("EventBody") or {}
        event_name = event_body.get("Event")
        if tx_id and tx_id in self._waiters:
            remaining = []
            for names, future in self._waiters[tx_id]:
                if future.done():
                    continue
                if not names or (event_name and event_name in names):
                    future.set_result(message)
                else:
                    remaining.append((names, future))
            if remaining:
                self._waiters[tx_id] = remaining
            else:
                self._waiters.pop(tx_id, None)
        for handler in list(self._handlers):
            try:
                task = asyncio.create_task(handler(message), name="jibo-rom-event")
                task.add_done_callback(self._log_handler_exception)
            except Exception:
                logger.debug("ROM event handler scheduling failed", exc_info=True)

    @staticmethod
    def _log_handler_exception(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.debug("ROM event handler failed", exc_info=task.exception())

    def _fail_waiters(self, error: Exception) -> None:
        waiters = self._waiters
        self._waiters = {}
        for entries in waiters.values():
            for _names, future in entries:
                if not future.done():
                    future.set_exception(error)

    async def _heartbeat(self) -> None:
        try:
            while self.connected and not self._closing:
                await asyncio.sleep(self.heartbeat_seconds)
                if not self.connected:
                    return
                try:
                    tx, future = await self._send_with_future({"Type": "GetConfig"})
                    await self._await_future(tx, future, 5.0)
                except Exception:
                    logger.debug("ROM heartbeat failed", exc_info=True)
        except asyncio.CancelledError:
            raise

    async def command(self, command: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
        tx_id, future = await self._send_with_future(command)
        return await self._await_future(tx_id, future, timeout)

    async def say(self, esml: str, timeout: float = 60.0) -> tuple[str, dict[str, Any]]:
        tx_id, future = await self._send_with_future({"Type": "Say", "ESML": esml}, {"onStop", "onError"})
        self._active_say_tx = tx_id
        try:
            result = await self._await_future(tx_id, future, timeout)
        finally:
            if self._active_say_tx == tx_id:
                self._active_say_tx = ""
        event = result.get("EventBody", {})
        if event.get("Event") == "onError":
            raise RomError(str(event.get("EventError") or event))
        return tx_id, result

    async def cancel_active_say(self) -> None:
        """Cancel the currently spoken ROM command, if any."""
        tx_id = self._active_say_tx
        if tx_id:
            await self.cancel(tx_id)

    async def display_eye(
        self,
        *,
        name: str = "default",
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Restore Jibo's normal eye view."""
        return await self.command(
            {
                "Type": "Display",
                "View": {
                    "Type": "Eye",
                    "Name": name,
                },
            },
            timeout=timeout,
        )

    async def display_text(
        self,
        text: str,
        *,
        name: str = "view",
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Display a text view on Jibo's screen."""
        return await self.command(
            {
                "Type": "Display",
                "View": {
                    "Type": "Text",
                    "Name": name,
                    "Text": text,
                },
            },
            timeout=timeout,
        )

    async def display_image(
        self,
        src: str,
        *,
        name: str = "hermes-image",
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Display a remotely reachable image URL on Jibo's screen."""
        return await self.command(
            {
                "Type": "Display",
                "View": {
                    "Type": "Image",
                    "Name": name,
                    "Image": {
                        "src": src,
                        "name": name,
                        "set": "",
                    },
                },
            },
            timeout=timeout,
        )

    async def listen(self, max_speech_ms: int, max_silence_ms: int) -> str:
        result = await self.command({
            "Type": "Listen",
            "MaxSpeechTimeout": max_speech_ms,
            "MaxNoSpeechTimeout": max_silence_ms,
            "LanguageCode": "en-US",
        })
        header = result.get("Response", {})
        return str(header.get("TransactionID") or result.get("ResponseHeader", {}).get("TransactionID") or "")

    async def cancel(self, tx_id: str) -> None:
        if tx_id and self.connected:
            try:
                await self.command({"Type": "Cancel", "ID": tx_id}, timeout=5.0)
            except Exception:
                logger.debug("ROM cancel failed for %s", tx_id, exc_info=True)

    async def close(self) -> None:
        self._closing = True
        self._connected.clear()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
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
                await asyncio.wait_for(reader, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                reader.cancel()
            except Exception:
                pass
        self.session_id = ""
        self._fail_waiters(RomDisconnected("ROM session closed"))
