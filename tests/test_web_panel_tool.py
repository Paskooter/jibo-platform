import importlib.util
import json
import pathlib
import sys
import unittest


_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "jibo_platform_web_panel_tests"
_PACKAGE_SPEC = importlib.util.spec_from_file_location(
    _PACKAGE_NAME,
    _PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(_PLUGIN_DIR)],
)
_PACKAGE = importlib.util.module_from_spec(_PACKAGE_SPEC)
sys.modules[_PACKAGE_NAME] = _PACKAGE
assert _PACKAGE_SPEC.loader is not None
_PACKAGE_SPEC.loader.exec_module(_PACKAGE)

from jibo_platform_web_panel_tests.native_tools import (  # noqa: E402
    JIBO_OPEN_WEB_PANEL_SCHEMA,
    _panel_target,
    jibo_close_web_panel_tool,
    jibo_open_web_panel_tool,
)


class _Result:
    def __init__(self, success=True, raw_response=None, error=None):
        self.success = success
        self.raw_response = raw_response
        self.error = error


class _FakeAdapter:
    def __init__(self):
        self.config = type(
            "Config",
            (),
            {
                "extra": {
                    "browser_panel": {
                        "control_url": "http://127.0.0.1:9013",
                        "public_url": "http://192.0.2.10:9013",
                        "token": "this-is-a-test-only-panel-token",
                        "search_url": "https://search.example.test/?q={query}",
                    }
                }
            },
        )()
        self.calls = []

    def native_call(self, method, params, **kwargs):
        self.calls.append((method, params, kwargs))
        if method == "status.get":
            return _Result(raw_response={"skill": {"name": "@hermes/jibo-web-panel"}})
        return _Result(raw_response={"requested": True})

    def resolve_device_id(self):
        return "robot-one"


class WebPanelToolTests(unittest.TestCase):
    def test_schema_has_voice_search_and_url_inputs(self):
        self.assertEqual(JIBO_OPEN_WEB_PANEL_SCHEMA["name"], "jibo_open_web_panel")
        self.assertIn("query", JIBO_OPEN_WEB_PANEL_SCHEMA["parameters"]["properties"])
        self.assertIn("url", JIBO_OPEN_WEB_PANEL_SCHEMA["parameters"]["properties"])

    def test_search_target_is_encoded_and_only_one_input_is_allowed(self):
        settings = {"search_url": "https://search.example.test/?q={query}"}
        target, error = _panel_target({"query": "red & blue"}, settings)
        self.assertIsNone(error)
        self.assertEqual(target, "https://search.example.test/?q=red+%26+blue")
        target, error = _panel_target({"url": "https://one.test", "query": "two"}, settings)
        self.assertIsNone(target)
        self.assertEqual(error, "pass exactly one of url or query")

    def test_open_navigates_then_launches_skill_without_returning_the_token(self):
        import jibo_platform_web_panel_tests.native_tools as tool_module

        adapter = _FakeAdapter()
        original_adapter = tool_module.get_active_adapter
        original_open = tool_module._open_browser_page
        tool_module.get_active_adapter = lambda device_id=None: adapter
        tool_module._open_browser_page = lambda settings, target: (True, None)
        try:
            result = json.loads(jibo_open_web_panel_tool({"query": "Jibo history"}, platform="jibo"))
        finally:
            tool_module.get_active_adapter = original_adapter
            tool_module._open_browser_page = original_open

        self.assertTrue(result["success"])
        self.assertNotIn("token", json.dumps(result))
        self.assertEqual(adapter.calls[0][0], "skill.launch")
        params = adapter.calls[0][1]
        self.assertEqual(params["skill"], "@hermes/jibo-web-panel")
        self.assertIn("video.ts?token=", params["options"]["stream_url"])

    def test_close_refuses_to_stop_some_other_skill(self):
        import jibo_platform_web_panel_tests.native_tools as tool_module

        adapter = _FakeAdapter()
        adapter.native_call = lambda method, params, **kwargs: _Result(
            raw_response={"skill": {"name": "@be/radio"}}
        )
        original_adapter = tool_module.get_active_adapter
        tool_module.get_active_adapter = lambda device_id=None: adapter
        try:
            result = json.loads(jibo_close_web_panel_tool({}, platform="jibo"))
        finally:
            tool_module.get_active_adapter = original_adapter

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "PANEL_NOT_ACTIVE")


if __name__ == "__main__":
    unittest.main()
