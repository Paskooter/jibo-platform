"""One model setting per robot, shared by every surface that shows it."""

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest


_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "jibo_platform_model_tests"
_PACKAGE_SPEC = importlib.util.spec_from_file_location(
    _PACKAGE_NAME,
    _PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(_PLUGIN_DIR)],
)
_PACKAGE = importlib.util.module_from_spec(_PACKAGE_SPEC)
sys.modules[_PACKAGE_NAME] = _PACKAGE
assert _PACKAGE_SPEC.loader is not None
_PACKAGE_SPEC.loader.exec_module(_PACKAGE)

from jibo_platform_model_tests import model_settings  # noqa: E402


CONFIG = """
model:
  default: gpt-5.6-luna
  provider: openai-codex
gateway:
  platforms:
    jibo:
      extra:
        devices:
          - id: robot-one
            host: one.jibo
            token: secret-one
          - id: robot-two
            host: two.jibo
            token: secret-two
            model: grok-4.6
            model_provider: xai-oauth
"""


class _FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def list_sessions_rich(self, **_kwargs):
        return [dict(row) for row in self.rows]


def _row(model, provider=None, **extra):
    row = {"model": model, **extra}
    if provider is not None:
        row["model_config"] = json.dumps({"gateway_runtime": {"provider": provider}})
    return row


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        handle.write(CONFIG)
        handle.close()
        self.path = handle.name
        os.environ["HERMES_CONFIG_PATH"] = self.path
        self.addCleanup(os.environ.pop, "HERMES_CONFIG_PATH", None)
        self.addCleanup(os.unlink, self.path)

    def test_a_robot_with_no_setting_uses_the_hermes_default(self):
        document = model_settings.load_config()
        self.assertEqual(
            model_settings.device_model(document, "robot-one"),
            {"model": "", "provider": ""},
        )
        self.assertEqual(
            model_settings.default_model(document),
            {"model": "gpt-5.6-luna", "provider": "openai-codex"},
        )

    def test_setting_a_model_keeps_every_other_field_including_the_token(self):
        model_settings.set_device_model("robot-one", "claude-opus-5", "openrouter")
        document = model_settings.load_config()
        self.assertEqual(
            model_settings.device_model(document, "robot-one"),
            {"model": "claude-opus-5", "provider": "openrouter"},
        )
        devices = document["gateway"]["platforms"]["jibo"]["extra"]["devices"]
        self.assertEqual(devices[0]["token"], "secret-one")
        self.assertEqual(devices[0]["host"], "one.jibo")
        # The other robot is untouched.
        self.assertEqual(
            model_settings.device_model(document, "robot-two"),
            {"model": "grok-4.6", "provider": "xai-oauth"},
        )

    def test_clearing_the_model_restores_the_default(self):
        model_settings.set_device_model("robot-two", "", "")
        document = model_settings.load_config()
        self.assertEqual(
            model_settings.device_model(document, "robot-two"),
            {"model": "", "provider": ""},
        )
        devices = document["gateway"]["platforms"]["jibo"]["extra"]["devices"]
        self.assertNotIn("model", devices[1])
        self.assertNotIn("model_provider", devices[1])
        self.assertEqual(devices[1]["token"], "secret-two")

    def test_an_unknown_robot_is_refused(self):
        with self.assertRaises(model_settings.ModelSettingsError):
            model_settings.set_device_model("nobody", "grok-4.6", "xai-oauth")

    def test_recent_models_are_deduped_and_kept_in_order(self):
        db = _FakeDB([
            _row("grok-4.6", "xai-oauth"),
            _row("grok-4.6", "xai-oauth"),
            _row("deepseek/deepseek-v4-flash-0731", "openrouter"),
            _row(""),
            _row("gpt-5.6-luna", "openai-codex"),
        ])
        self.assertEqual(
            model_settings.recent_models(db),
            [
                {"model": "grok-4.6", "provider": "xai-oauth"},
                {"model": "deepseek/deepseek-v4-flash-0731", "provider": "openrouter"},
                {"model": "gpt-5.6-luna", "provider": "openai-codex"},
            ],
        )

    def test_recent_models_are_capped(self):
        db = _FakeDB([_row(f"model-{i}", "p") for i in range(20)])
        self.assertEqual(len(model_settings.recent_models(db)), model_settings.RECENT_MODEL_LIMIT)

    def test_a_model_is_listed_once_even_when_a_session_lost_its_provider(self):
        """Older sessions recorded no provider; the same model must not appear twice."""
        db = _FakeDB([
            _row("grok-4.6"),
            _row("grok-4.6", "xai-oauth"),
            _row("gpt-5.6-luna", "openai-codex"),
        ])
        self.assertEqual(
            model_settings.recent_models(db),
            [
                # The provider is filled in from the best record we have.
                {"model": "grok-4.6", "provider": "xai-oauth"},
                {"model": "gpt-5.6-luna", "provider": "openai-codex"},
            ],
        )

    def test_a_missing_history_database_is_not_an_error(self):
        self.assertEqual(model_settings.recent_models(None), [])


if __name__ == "__main__":
    unittest.main()
