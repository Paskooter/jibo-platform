"""Per-robot state has to cross the process boundary to the dashboard."""

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch


_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "jibo_platform_status_tests"
_SPEC = importlib.util.spec_from_file_location(
    _PACKAGE_NAME,
    _PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(_PLUGIN_DIR)],
)
_PACKAGE = importlib.util.module_from_spec(_SPEC)
sys.modules[_PACKAGE_NAME] = _PACKAGE
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_PACKAGE)

from jibo_platform_status_tests import device_status  # noqa: E402


class DeviceStatusTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = directory.name
        environment = patch.dict(os.environ, {"HERMES_HOME": self.home})
        environment.start()
        self.addCleanup(environment.stop)

    def test_a_published_snapshot_reads_back(self):
        device_status.publish({
            "robot-one": {"connected": True, "capabilities": ["chat.history"]},
            "robot-two": {"connected": False, "last_error": "refused"},
        })
        # Snapshot round-trip is independent of the test interpreter's argv.
        # The real writer-family gate is exercised separately below.
        with patch.object(device_status, "_writer_is_alive", return_value=True):
            state = device_status.read()
        self.assertTrue(state["robot-one"]["connected"])
        self.assertEqual(state["robot-one"]["capabilities"], ["chat.history"])
        self.assertFalse(state["robot-two"]["connected"])
        self.assertEqual(state["robot-two"]["last_error"], "refused")

    def test_writer_identity_guard_accepts_only_the_hermes_gateway_family(self):
        with patch.object(device_status.os, "kill", return_value=None):
            for command, expected in ((b'python\\0-m\\0unittest', False),
                                      (b'hermes\\0gateway\\0run', True)):
                with self.subTest(command=command), \
                     patch.object(pathlib.Path, "read_bytes", return_value=command):
                    self.assertEqual(device_status._writer_is_alive(1234), expected)

    def test_nothing_published_reads_as_empty(self):
        self.assertEqual(device_status.read(), {})

    def test_a_snapshot_from_a_dead_writer_is_not_trusted(self):
        """A gateway that has exited must not leave 'connected' behind."""
        device_status.publish({"robot-one": {"connected": True}})
        path = device_status.status_path()
        payload = json.loads(path.read_text())
        # A pid that cannot be running: writable, but nobody is there.
        payload["writer_pid"] = 2 ** 22
        path.write_text(json.dumps(payload))
        self.assertEqual(device_status.read(), {})

    def test_a_corrupt_file_is_not_an_error(self):
        device_status.status_path().write_text("{not json")
        self.assertEqual(device_status.read(), {})


if __name__ == "__main__":
    unittest.main()
