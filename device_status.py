"""Per-robot connection state, published for Hermes' other processes.

The dashboard is its own process (``hermes dashboard``), so it cannot see the
gateway's live adapter: an in-process lookup there finds nothing and every
robot reads as offline no matter how well the bridge is working.  The
platform-level ``gateway_state.json`` does not help either -- it says "jibo:
connected", which is the wrong grain once there is more than one robot.

So the gateway publishes what only it knows, and the dashboard reads it.  The
file is written on every connection change, never on the turn path.

Freshness is decided by the writer, not by a clock: a robot can sit connected
and unchanged for days, so an old timestamp means nothing, while a file whose
writing process is gone means everything in it is unknown.
"""

from __future__ import annotations

import json
import asyncio
import logging
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

STATUS_FILENAME = "jibo-devices.json"


class StatusPublisher:
    """One off-loop writer and one replaceable pending snapshot.

    Publishing connection state must not delay WebSocket handshakes. Preserve
    write order so a slow older snapshot cannot overwrite a newer disconnect.
    """

    def __init__(self) -> None:
        self._pending = None
        self._task = None

    def update(self, devices: Mapping[str, Mapping[str, Any]]) -> None:
        snapshot = {key: dict(value) for key, value in devices.items()}
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            publish(snapshot)
            return
        self._pending = snapshot
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._write(), name='jibo-device-status')

    async def _write(self) -> None:
        while self._pending is not None:
            snapshot, self._pending = self._pending, None
            await asyncio.to_thread(publish, snapshot)

    async def flush(self) -> None:
        if self._task is not None:
            await asyncio.shield(self._task)


def status_path() -> Path:
    home = os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")
    return Path(home) / STATUS_FILENAME


def publish(devices: Mapping[str, Mapping[str, Any]]) -> None:
    """Write the current per-robot state. Best effort: never raises."""
    payload = {
        "version": 1,
        "updated_at": time.time(),
        "writer_pid": os.getpid(),
        "devices": {
            str(device_id): {
                "connected": bool(state.get("connected")),
                "last_connected_at": state.get("last_connected_at"),
                "last_error": state.get("last_error"),
                "capabilities": list(state.get("capabilities") or []),
            }
            for device_id, state in devices.items()
        },
    }
    path = status_path()
    temporary: Optional[Path] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle)
        os.replace(temporary, path)
    except OSError:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        logger.debug("Could not publish Jibo device status", exc_info=True)


def _writer_is_alive(pid: Any) -> bool:
    """Whether the process that wrote the file is still running."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive, owned by somebody else -- not our writer.
        return False
    except OSError:
        return False
    # Guard against a reused pid. Deliberately a family check rather than an
    # exact argv match: the writer is whichever Hermes process runs the
    # platform, and pinning its command line here would turn a rename into a
    # panel that says every robot is offline.
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return True  # No /proc to check; the signal test stands.
    lowered = cmdline.lower()
    return "hermes" in lowered or "gateway" in lowered


def read() -> dict[str, dict[str, Any]]:
    """Per-robot state as last published, or empty when it cannot be trusted."""
    try:
        raw = status_path().read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(payload, Mapping):
        return {}
    if not _writer_is_alive(payload.get("writer_pid")):
        return {}
    devices = payload.get("devices")
    if not isinstance(devices, Mapping):
        return {}
    return {
        str(device_id): dict(state)
        for device_id, state in devices.items()
        if isinstance(state, Mapping)
    }
