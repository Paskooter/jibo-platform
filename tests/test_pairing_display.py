import importlib.util
import pathlib
import sys
import threading
import unittest


_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "jibo_platform"
_PACKAGE_SPEC = importlib.util.spec_from_file_location(
    _PACKAGE_NAME,
    _PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(_PLUGIN_DIR)],
)
_PACKAGE = importlib.util.module_from_spec(_PACKAGE_SPEC)
sys.modules[_PACKAGE_NAME] = _PACKAGE
assert _PACKAGE_SPEC.loader is not None
_PACKAGE_SPEC.loader.exec_module(_PACKAGE)

from jibo_platform.adapter import JiboRomAdapter  # noqa: E402
from jibo_platform.rom import RomSession  # noqa: E402


PAIRING_MESSAGE = (
    "Hi~ I don't recognize you yet!\n\n"
    "Here's your pairing code: `ABCD2345`\n\n"
    "Ask the bot owner to run:\n"
    "`hermes pairing approve jibo ABCD2345`"
)


class PairingDisplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_rom_display_text_uses_text_view_command(self):
        class ProbeRom(RomSession):
            def __init__(self):
                self.commands = []

            async def command(self, command, timeout=5.0):
                self.commands.append((command, timeout))
                return {"ok": True}

        rom = ProbeRom()
        result = await rom.display_text("PAIRING CODE\nABCD2345", name="hermes-pairing")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            rom.commands,
            [
                (
                    {
                        "Type": "Display",
                        "View": {
                            "Type": "Text",
                            "Name": "hermes-pairing",
                            "Text": "PAIRING CODE\nABCD2345",
                        },
                    },
                    5.0,
                )
            ],
        )

    async def test_pairing_message_is_displayed_on_jibo_screen(self):
        class FakeRom:
            connected = True

            def __init__(self):
                self.displayed = []
                self.spoken = []

            async def display_eye(self, name="default"):
                self.displayed.append(("EYE", name))
                return {"ok": True}

            async def display_text(self, text, name="view"):
                self.displayed.append((text, name))
                return {"ok": True}

            async def say(self, text, timeout=60.0):
                self.spoken.append(text)
                return "tx", {"ok": True}

        adapter = object.__new__(JiboRomAdapter)
        adapter._rom = FakeRom()
        adapter._speak_lock = __import__("asyncio").Lock()
        adapter._speech_state_lock = threading.Lock()
        adapter._tool_spoken_deadline = 0.0
        adapter._processing_state_lock = threading.RLock()
        adapter._user_display_mode = "processing"

        result = await adapter.send("jibo:robot", PAIRING_MESSAGE)

        self.assertTrue(result.success)
        self.assertEqual(
            adapter._rom.displayed,
            [
                ("PAIRING CODE\nABCD2345", "hermes-pairing"),
                ("EYE", "default"),
            ],
        )
        self.assertTrue(adapter._rom.spoken)


class NativePairingDisplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_authorization_code_and_profile_command_are_legible(self):
        from test_turn_spine import _SpineAdapter
        adapter = _SpineAdapter(capabilities=("display.text", "speech.speak", "turn.append"))
        await adapter._dispatch_utterance("robot", "hello", {}, turn_id="synthetic-approval-turn")
        message = PAIRING_MESSAGE.replace("hermes pairing", "hermes -p robot-test pairing")
        result = await adapter.send("jibo:robot", message)
        self.assertTrue(result.success)
        displays = adapter.client.methods("display.text")
        self.assertTrue(displays, "Native approval must have a legible screen projection, not only ROM speech")
        self.assertIn("HERMES AUTHORIZATION", displays[0]["text"])
        self.assertIn("ABCD2345", displays[0]["text"])
        self.assertIn("hermes -p robot-test pairing approve jibo ABCD2345", displays[0]["text"])
        self.assertIn("five-character", displays[0]["text"])
        self.assertTrue(adapter.client.methods("speech.speak"))


if __name__ == "__main__":
    unittest.main()
