import asyncio
import importlib.util
import json
import pathlib
import sys
import unittest


_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "jibo_platform_speech_tests"
_PACKAGE_SPEC = importlib.util.spec_from_file_location(
    _PACKAGE_NAME,
    _PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(_PLUGIN_DIR)],
)
_PACKAGE = importlib.util.module_from_spec(_PACKAGE_SPEC)
sys.modules[_PACKAGE_NAME] = _PACKAGE
assert _PACKAGE_SPEC.loader is not None
_PACKAGE_SPEC.loader.exec_module(_PACKAGE)

from jibo_platform_speech_tests.esml import format_jibo_speech  # noqa: E402
from jibo_platform_speech_tests.adapter import processing_screen_text  # noqa: E402
from jibo_platform_speech_tests.tool import (  # noqa: E402
    JIBO_SPEAK_SCHEMA,
    jibo_speak_tool,
)
from jibo_platform_speech_tests.status import (  # noqa: E402
    on_interim_message,
    on_pre_llm_call,
    on_pre_tool_call,
    on_post_llm_call,
    on_post_tool_call,
    on_session_end,
    reset_status_state_for_tests,
)


class SpeechPipelineTests(unittest.IsolatedAsyncioTestCase):
    def test_format_removes_non_speech_noise(self):
        value = format_jibo_speech(
            "I found **three** results. See https://example.com/a?b=1.\n\n"
            "💾 Self-improvement review: Skill 'demo' patched"
        )

        self.assertIn("three", value)
        self.assertNotIn("**", value)
        self.assertNotIn("https://", value)
        self.assertNotIn("Self-improvement", value)
        self.assertNotIn("💾", value)

    def test_format_says_dollar_amounts_number_then_currency(self):
        value = format_jibo_speech("The ticket costs $100, and the total is $1,234.56.")

        self.assertIn("100 dollars", value)
        self.assertIn("1,234.56 dollars", value)
        self.assertNotIn("dollars 100", value)
        self.assertNotIn("dollars 1,234.56", value)

    def test_format_does_not_speak_spoiler_wrapped_image_alt_text(self):
        value = format_jibo_speech(
            "Here is the picture:\n\n"
            "||![Sensitive synthetic snapshot](/tmp/synthetic-image.jpg)||"
        )

        self.assertIn("Here is the picture", value)
        self.assertNotIn("Sensitive", value)
        self.assertNotIn(" or  or ", value)

    def test_thinking_status_uses_a_safe_embodied_animation(self):
        value = format_jibo_speech(
            "Hmm, let me think about that.",
            kind="thinking",
        )

        self.assertIn(
            "<anim cat='confused' nonBlocking='true' endNeutral='true'/>",
            value,
        )
        self.assertIn("Hmm, let me think about that.", value)

    def test_processing_screen_shows_high_level_phase_only(self):
        self.assertEqual(processing_screen_text("working"), "WORKING...\nHold on a moment.\nTAP ANYWHERE TO STOP")
        self.assertEqual(processing_screen_text("unknown"), "PROCESSING...\nStill working.\nTAP ANYWHERE TO STOP")
        self.assertNotIn("reasoning", processing_screen_text("working").lower())

    def test_tool_schema_requires_plain_intentional_text(self):
        self.assertEqual(JIBO_SPEAK_SCHEMA["name"], "jibo_speak")
        properties = JIBO_SPEAK_SCHEMA["parameters"]["properties"]
        self.assertIn("text", properties)
        self.assertEqual(properties["kind"]["enum"], ["thinking", "progress", "answer", "error"])
        self.assertEqual(JIBO_SPEAK_SCHEMA["parameters"]["required"], ["text"])

    def test_jibo_speak_is_advertised_before_rom_connects(self):
        import jibo_platform_speech_tests.tool as tool_module

        original = tool_module.get_active_adapter
        tool_module.get_active_adapter = lambda: None
        try:
            self.assertTrue(tool_module.jibo_speak_available())
        finally:
            tool_module.get_active_adapter = original

    async def test_tool_speaks_through_adapter_and_returns_json(self):
        class FakeAdapter:
            def __init__(self):
                self.calls = []

            def speak_explicit(self, text, *, kind, style, final):
                self.calls.append((text, kind, style, final))
                return type("Result", (), {"success": True, "message_id": "jibo-tx", "error": None})()

        fake = FakeAdapter()
        import jibo_platform_speech_tests.tool as tool_module

        original = tool_module.get_active_adapter
        tool_module.get_active_adapter = lambda: fake
        try:
            result = jibo_speak_tool(
                {
                    "text": "I am checking the files.",
                    "kind": "progress",
                    "style": "neutral",
                    "final": False,
                },
                session_id="session-1",
            )
        finally:
            tool_module.get_active_adapter = original

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["message_id"], "jibo-tx")
        self.assertEqual(
            fake.calls,
            [("I am checking the files.", "progress", "neutral", False)],
        )

    async def test_processing_hooks_use_screen_stages_without_raw_interim_text(self):
        class FakeAdapter:
            def __init__(self):
                self.calls = []

            def begin_processing(self, *, turn_id, user_message):
                self.calls.append(("begin", turn_id, user_message))

            def update_processing_stage(self, stage):
                self.calls.append(("stage", stage))

            def finish_processing(self, *, success):
                self.calls.append(("finish", success))

        fake = FakeAdapter()
        import jibo_platform_speech_tests.status as status_module

        reset_status_state_for_tests()
        original = status_module.get_active_adapter
        status_module.get_active_adapter = lambda: fake
        try:
            on_pre_llm_call(
                platform="jibo",
                session_id="session-1",
                turn_id="turn-1",
                user_message="Find my files",
                conversation_history=[],
            )
            on_pre_tool_call(
                session_id="session-1",
                turn_id="turn-1",
                tool_name="terminal",
                args={"command": "secret command"},
            )
            on_interim_message(
                session_id="session-1",
                turn_id="turn-1",
                surface="jibo",
                text="Here is private scratch reasoning that must not be spoken.",
            )
            on_post_tool_call(
                session_id="session-1",
                turn_id="turn-1",
                tool_name="terminal",
                args={"command": "secret command"},
                result="private tool result",
            )
            on_post_llm_call(
                platform="jibo",
                session_id="session-1",
                turn_id="turn-1",
                platform_name="jibo",
                assistant_response="Done.",
            )
            on_session_end(
                platform="jibo",
                session_id="session-1",
                turn_id="turn-1",
                completed=True,
                failed=False,
                interrupted=False,
            )
        finally:
            status_module.get_active_adapter = original
            reset_status_state_for_tests()

        self.assertEqual(
            fake.calls,
            [
                ("begin", "turn-1", "Find my files"),
                ("stage", "WORKING"),
                ("stage", "PROCESSING"),
                ("stage", "CHECKING"),
                ("finish", True),
            ],
        )


if __name__ == "__main__":
    unittest.main()
