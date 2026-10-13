import asyncio
import importlib.util
import pathlib
import sys
import unittest
from unittest.mock import patch


_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "jibo_platform_transport_tests"
_PACKAGE_SPEC = importlib.util.spec_from_file_location(
    _PACKAGE_NAME,
    _PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(_PLUGIN_DIR)],
)
_PACKAGE = importlib.util.module_from_spec(_PACKAGE_SPEC)
sys.modules[_PACKAGE_NAME] = _PACKAGE
assert _PACKAGE_SPEC.loader is not None
_PACKAGE_SPEC.loader.exec_module(_PACKAGE)

from jibo_platform_transport_tests import asr as asr_module  # noqa: E402
from jibo_platform_transport_tests import rom as rom_module  # noqa: E402
from jibo_platform_transport_tests.asr import WakewordWatcher  # noqa: E402
from jibo_platform_transport_tests.rom import RomSession  # noqa: E402


class TransportCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_wakeword_socket_disables_websocket_ping(self):
        calls = []

        class FakeContext:
            async def __aenter__(self):
                raise asyncio.CancelledError

            async def __aexit__(self, exc_type, exc, tb):
                return False

        def fake_connect(uri, **kwargs):
            calls.append((uri, kwargs))
            return FakeContext()

        watcher = WakewordWatcher("jibo", 8088)
        with patch.object(asr_module.websockets, "connect", fake_connect):
            with self.assertRaises(asyncio.CancelledError):
                await watcher.run(lambda _event: asyncio.sleep(0))

        self.assertEqual(calls[0][1]["ping_interval"], None)
        self.assertEqual(calls[0][1]["ping_timeout"], None)

    async def test_rom_socket_disables_websocket_ping(self):
        calls = []
        commands = []
        rom = RomSession("jibo", 8160)
        rom._post_aco = lambda: asyncio.sleep(0)
        closed = asyncio.Event()

        class FakeWebSocket:
            async def send(self, _message):
                return None

            async def close(self):
                closed.set()

            def __aiter__(self):
                return self

            async def __anext__(self):
                await closed.wait()
                raise StopAsyncIteration

        async def fake_connect(uri, **kwargs):
            calls.append((uri, kwargs))
            return FakeWebSocket()

        async def fake_send_with_future(command, event_names=None):
            del event_names
            commands.append(command)
            future = asyncio.get_running_loop().create_future()
            if command.get("Type") == "StartSession":
                future.set_result({
                    "Response": {
                        "ResponseBody": {"SessionID": "session-1", "Version": "1.0"}
                    }
                })
            else:
                future.set_result({})
            return "tx", future

        rom._send_with_future = fake_send_with_future
        with patch.object(rom_module.websockets, "connect", fake_connect):
            await rom.connect()
        try:
            self.assertEqual(calls[0][1]["ping_interval"], None)
            self.assertEqual(calls[0][1]["ping_timeout"], None)
            self.assertIn(
                {
                    "Type": "Subscribe",
                    "StreamType": "ScreenGesture",
                    "StreamFilter": {
                        "Type": "Tap",
                        "Area": {"x": 0, "y": 0, "width": 1, "height": 1},
                    },
                },
                commands,
            )
        finally:
            await rom.close()


if __name__ == "__main__":
    unittest.main()
