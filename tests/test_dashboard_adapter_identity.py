"""The dashboard must read the gateway's adapter, not a second copy of it.

The web server loads ``dashboard/plugin_api.py`` by file path under a module
name of its own choosing, so the module cannot rely on package-relative
imports.  When it built its own copy of the plugin package instead, it got a
private ``adapter`` module whose registry of live adapters was always empty,
and the Jibo panel reported every robot offline while the gateway was
connected to them.
"""

import importlib.util
import pathlib
import sys
import types
import unittest


_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_GATEWAY_PKG = "hermes_plugins_test_gateway_layout"


def _import_as_gateway(name: str):
    """Import a plugin module the way the running gateway does."""
    package = sys.modules.get(_GATEWAY_PKG)
    if package is None:
        package = types.ModuleType(_GATEWAY_PKG)
        package.__path__ = [str(_PLUGIN_DIR)]
        package.__package__ = _GATEWAY_PKG
        sys.modules[_GATEWAY_PKG] = package
    return importlib.import_module(f"{_GATEWAY_PKG}.{name}")


def _load_dashboard_api():
    """Load the dashboard API the way hermes_cli.web_server does."""
    module_name = "hermes_dashboard_plugin_jibo-platform-test"
    path = _PLUGIN_DIR / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


class _FakeAdapter:
    """Enough of the native adapter for the registry and the panel."""

    def connected(self) -> bool:
        return True

    def has_device(self, device_id: str) -> bool:
        return True

    def dashboard_snapshot(self) -> dict:
        return {"mode": "native", "connected": True, "devices": [{"id": "robot"}]}


class DashboardAdapterIdentityTests(unittest.TestCase):
    def test_the_panel_sees_an_adapter_the_gateway_registered(self):
        gateway_adapter = _import_as_gateway("adapter")
        api = _load_dashboard_api()

        live = _FakeAdapter()
        gateway_adapter._register_active_adapter(live)
        self.addCleanup(gateway_adapter._unregister_active_adapter, live)

        # Not "which module object" -- several copies legitimately exist in a
        # test run. What matters is that the panel finds the adapter that the
        # gateway registered, in whichever copy holds it.
        self.assertIs(api._live_adapter(), live)

    def test_the_panel_reports_the_live_snapshot_not_the_configured_one(self):
        gateway_adapter = _import_as_gateway("adapter")
        api = _load_dashboard_api()

        live = _FakeAdapter()
        gateway_adapter._register_active_adapter(live)
        self.addCleanup(gateway_adapter._unregister_active_adapter, live)

        import asyncio

        snapshot = asyncio.run(api.get_overview())
        self.assertTrue(snapshot["connected"])
        self.assertEqual(snapshot["devices"], [{"id": "robot"}])


if __name__ == "__main__":
    unittest.main()
