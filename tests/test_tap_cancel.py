import asyncio
import importlib.util
import pathlib
import sys
import threading
import unittest


_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "jibo_platform_cancel_tests"
_PACKAGE_SPEC = importlib.util.spec_from_file_location(
    _PACKAGE_NAME,
    _PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(_PLUGIN_DIR)],
)
_PACKAGE = importlib.util.module_from_spec(_PACKAGE_SPEC)
sys.modules[_PACKAGE_NAME] = _PACKAGE
assert _PACKAGE_SPEC.loader is not None
_PACKAGE_SPEC.loader.exec_module(_PACKAGE)

from jibo_platform_cancel_tests.adapter import JiboRomAdapter, is_cancel_phrase  # noqa: E402
from jibo_platform_cancel_tests.rom import RomSession  # noqa: E402


class TapCancelTests(unittest.IsolatedAsyncioTestCase):
    async def test_rom_tracks_and_cancels_active_say(self):
        rom = RomSession("jibo", 8160)
        say_future = None
        commands = []

        async def fake_send_with_future(command, event_names=None):
            nonlocal say_future
            del event_names
            commands.append(command)
            future = asyncio.get_running_loop().create_future()
            if command.get("Type") == "Say":
                say_future = future
                return "say-tx", future
            if command.get("Type") == "Cancel":
                say_future.set_result({"EventBody": {"Event": "onStop"}})
            future.set_result({})
            return "control-tx", future

        rom._send_with_future = fake_send_with_future
        rom._connected.set()
        rom._ws = object()
        say_task = asyncio.create_task(rom.say("hello", timeout=2.0))
        await asyncio.sleep(0)

        self.assertEqual(rom._active_say_tx, "say-tx")
        await rom.cancel_active_say()
        await say_task

        self.assertEqual(commands[1], {"Type": "Cancel", "ID": "say-tx"})
        self.assertEqual(rom._active_say_tx, "")

    def test_cancel_phrase_is_explicit(self):
        self.assertTrue(is_cancel_phrase("Tap anywhere to cancel!"))
        self.assertTrue(is_cancel_phrase("never mind"))
        self.assertFalse(is_cancel_phrase("tell me why you should stop"))

    async def test_tap_hard_stops_hermes_turn_and_restores_eye(self):
        adapter = object.__new__(JiboRomAdapter)
        adapter._closing = False
        adapter._active_session_key = "jibo-session"
        adapter._active_source = object()
        adapter._active_speech_tx = ""
        adapter._active_sessions = {"jibo-session": object()}
        adapter._chat_id = "jibo:robot"
        adapter._processing_task = None
        adapter._processing_turn_id = "turn-1"
        adapter._processing_stage = "WORKING"
        adapter._processing_generation = 1
        adapter._user_display_mode = "processing"
        adapter._processing_state_lock = threading.RLock()
        calls = []

        class FakeRom:
            async def cancel_active_say(self):
                calls.append("speech-cancel")

            async def display_eye(self, *, name):
                calls.append(("eye", name))

        class FakeRunner:
            async def _interrupt_and_clear_session(self, session_key, source, **kwargs):
                calls.append(("hard-cancel", session_key, source, kwargs))

        adapter._rom = FakeRom()
        adapter.gateway_runner = FakeRunner()

        await adapter._cancel_current_turn_from_tap()

        self.assertEqual(calls[0], "speech-cancel")
        self.assertEqual(calls[1][0], "hard-cancel")
        self.assertEqual(calls[2], ("eye", "default"))
        self.assertIsNone(adapter._processing_turn_id)

    async def test_full_screen_tap_schedules_current_turn_cancel(self):
        adapter = object.__new__(JiboRomAdapter)
        adapter._closing = False
        adapter._tap_cancel_lock = asyncio.Lock()
        calls = []

        async def cancel_from_tap():
            calls.append("cancelled")

        adapter._cancel_current_turn_from_tap = cancel_from_tap

        await adapter._on_rom_event({"EventBody": {"Event": "onTap"}})
        await asyncio.sleep(0)

        self.assertEqual(calls, ["cancelled"])

    async def test_rom_connects_full_screen_tap_subscription(self):
        rom = RomSession("jibo", 8160)
        rom._post_aco = lambda: asyncio.sleep(0)
        commands = []
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

        async def fake_connect(_uri, **_kwargs):
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
        import jibo_platform_cancel_tests.rom as rom_module
        original = rom_module.websockets.connect
        rom_module.websockets.connect = fake_connect
        try:
            await rom.connect()
        finally:
            rom_module.websockets.connect = original
            await rom.close()

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


if __name__ == "__main__":
    unittest.main()
