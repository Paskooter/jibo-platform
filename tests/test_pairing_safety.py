"""Offline pairing/handshake controls; all credentials and peers are synthetic."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

from test_native_poc import FakeNativeSocket, NativeDeviceConfig, NativeJiboClient

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("host_pairing_safety_api", ROOT / "dashboard/plugin_api.py")
api = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = api
spec.loader.exec_module(api)
REAL_CONFIGURED_SNAPSHOT = api._configured_snapshot


class HandshakeSocket(FakeNativeSocket):
    def __init__(self, identity="kitchen", session=None):
        super().__init__()
        self.identity = identity
        self.session = session

    async def send(self, raw):
        request = json.loads(raw)
        if request["method"] not in {"auth", "session.open"}:
            return await super().send(raw)
        self.sent.append(request)
        if request["method"] == "auth":
            result = {"device_id": self.identity, "capabilities": []}
        else:
            result = self.session
        await self.incoming.put(json.dumps({
            "v": 1, "kind": "response", "id": request["id"],
            "method": request["method"], "result": result,
        }))


def run_handshake(mapping, socket):
    async def run():
        config = NativeDeviceConfig.from_mapping(mapping)
        client = NativeJiboClient(config, websocket_factory=lambda *_a, **_k: socket)
        try:
            await client._connect_once()
            return client.connected, client.state
        finally:
            await client.stop()
    return asyncio.run(run())


def test_paired_robot_identity_mismatch_refuses_session_open():
    socket = HandshakeSocket(identity="different-robot", session={"status": "active", "session_id": "jibo:alias"})
    with pytest.raises(Exception, match="identity"):
        run_handshake({"id": "alias", "host": "127.0.0.1", "token": "synthetic-token", "robot_device_id": "expected-robot"}, socket)
    assert [frame["method"] for frame in socket.sent] == ["auth"]


@pytest.mark.parametrize("session", [None, "active", {}, {"status": "inactive"}, {"status": True}, {"status": "active"}, {"status": "active", "session_id": "wrong-session"}])
def test_connected_requires_active_matching_session(session):
    socket = HandshakeSocket(identity="expected-robot", session=session)
    with pytest.raises(Exception, match="session"):
        run_handshake({"id": "alias", "host": "127.0.0.1", "token": "synthetic-token", "robot_device_id": "expected-robot"}, socket)


def test_matching_robot_identity_and_active_session_connect():
    socket = HandshakeSocket(identity="expected-robot", session={"status": "active", "session_id": "jibo:alias"})
    connected, state = run_handshake({"id": "alias", "host": "127.0.0.1", "token": "synthetic-token", "robot_device_id": "expected-robot"}, socket)
    assert connected and state["connected"]


def test_legacy_alias_is_not_silently_used_as_robot_identity():
    socket = HandshakeSocket(identity="legacy-robot", session={"status": "active", "session_id": "jibo:alias"})
    connected, _state = run_handshake({"id": "alias", "host": "127.0.0.1", "token": "synthetic-token"}, socket)
    assert connected


@pytest.fixture
def synthetic_config(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    pending = getattr(api, "_PENDING_PAIRINGS", None)
    if pending is not None:
        pending.clear()
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path.write_text("model:\n  default: synthetic-model\n  provider: synthetic-provider\ngateway:\n  platforms:\n    jibo:\n      extra:\n        devices: []\n", encoding="utf-8")
    monkeypatch.setattr(api, "_configured_snapshot", lambda doc: {"connected": False, "devices": []})
    return path


PAIR_PAYLOAD = {"id": "alias", "name": "Synthetic robot", "host": "127.0.0.1", "code": "AB2K7"}


def test_config_writer_failure_is_checked_before_consuming_pairing(synthetic_config, monkeypatch):
    native = api._plugin_module("native_protocol")
    consumed = []
    async def exchange(*args, **kwargs):
        consumed.append(True)
        return {"token": "synthetic-pair-token", "device_id": "synthetic-robot"}
    def denied(*args, **kwargs):
        raise api.HTTPException(status_code=500, detail="synthetic writer denied")
    monkeypatch.setattr(native, "exchange_pairing_code", exchange)
    monkeypatch.setattr(api, "_write_config", denied)
    with pytest.raises(api.HTTPException):
        asyncio.run(api.pair_device(PAIR_PAYLOAD))
    assert consumed == []


@pytest.mark.parametrize("code", [" AB2K7", "AB2K7 ", "AB-2K7", "AB2K7extra", "AB0K7", "AB1K7", "ABIK7"])
def test_api_rejects_malformed_code_before_exchange(synthetic_config, monkeypatch, code):
    native = api._plugin_module("native_protocol")
    consumed = []
    async def exchange(*args, **kwargs):
        consumed.append(True)
        return {"token": "synthetic-pair-token", "device_id": "synthetic-robot"}
    monkeypatch.setattr(native, "exchange_pairing_code", exchange)
    with pytest.raises(api.HTTPException) as error:
        asyncio.run(api.pair_device(dict(PAIR_PAYLOAD, code=code)))
    assert error.value.status_code == 422
    assert consumed == []


@pytest.mark.parametrize("code", [" AB2K7", "AB2K7 ", "AB-2K7", "AB2K7extra", "AB0K7"])
def test_protocol_rejects_malformed_code_without_opening_socket(code):
    native = api._plugin_module("native_protocol")
    opened = []
    def factory(*args, **kwargs):
        opened.append(True)
        return FakeNativeSocket()
    config = native.NativeDeviceConfig.from_mapping({"id": "alias", "host": "127.0.0.1"})
    with pytest.raises(native.NativeBridgeError, match="five"):
        asyncio.run(native.exchange_pairing_code(config, code, websocket_factory=factory))
    assert opened == []


def test_atomic_save_is_verified_by_reading_exact_target(synthetic_config, monkeypatch):
    document = api._load_config()
    document["new_setting"] = "synthetic"
    monkeypatch.setattr(os, "replace", lambda *args: None)
    with pytest.raises(api.HTTPException):
        api._write_config(document)


def test_pairing_persists_robot_identity_separate_from_alias(synthetic_config, monkeypatch):
    native = api._plugin_module("native_protocol")
    async def exchange(*args, **kwargs):
        return {"token": "synthetic-pair-token", "device_id": "synthetic-robot"}
    monkeypatch.setattr(native, "exchange_pairing_code", exchange)
    result = asyncio.run(api.pair_device(PAIR_PAYLOAD))
    saved = api._load_config()["gateway"]["platforms"]["jibo"]["extra"]["devices"][0]
    assert saved["id"] == "alias"
    assert saved.get("robot_device_id") == "synthetic-robot"
    assert "synthetic-pair-token" not in json.dumps(result)
    assert synthetic_config.stat().st_mode & 0o777 == 0o600


def test_device_save_preserves_optional_model_identity_and_secrets(synthetic_config):
    document = api._load_config()
    original = {"id": "alias", "host": "127.0.0.1", "token": "synthetic-token", "model": "model-override", "model_provider": "provider-override", "robot_device_id": "synthetic-robot", "custom_options": {"enabled": False}}
    document["gateway"]["platforms"]["jibo"]["extra"]["devices"] = [original]
    api._write_config(document)
    asyncio.run(api.put_devices({"devices": [{"id": "alias", "host": "127.0.0.2", "name": "Renamed alias"}]}))
    saved = api._load_config()["gateway"]["platforms"]["jibo"]["extra"]["devices"][0]
    for field in ("model", "model_provider", "robot_device_id", "custom_options", "token"):
        assert saved.get(field) == original[field]


def test_returned_token_is_retained_privately_for_save_retry(synthetic_config, monkeypatch):
    native = api._plugin_module("native_protocol")
    consumed = []
    async def exchange(*args, **kwargs):
        consumed.append(True)
        return {"token": "synthetic-pair-token", "device_id": "synthetic-robot"}
    monkeypatch.setattr(native, "exchange_pairing_code", exchange)
    writer = api._write_config
    writes = []
    def fail_after_exchange(document):
        writes.append(True)
        if len(writes) == 2:
            raise api.HTTPException(status_code=500, detail="synthetic failure after exchange")
        return writer(document)
    monkeypatch.setattr(api, "_write_config", fail_after_exchange)
    with pytest.raises(api.HTTPException) as error:
        asyncio.run(api.pair_device(PAIR_PAYLOAD))
    assert "received" in error.value.detail.lower()
    assert "synthetic-pair-token" not in error.value.detail
    result = asyncio.run(api.pair_device(PAIR_PAYLOAD))
    assert len(consumed) == 1
    assert result["paired"] and result["saved"]
    assert "synthetic-pair-token" not in json.dumps(result)
    assert api._load_config()["gateway"]["platforms"]["jibo"]["extra"]["devices"][0]["token"] == "synthetic-pair-token"


def test_pairing_error_never_echoes_exception_secret(synthetic_config, monkeypatch):
    native = api._plugin_module("native_protocol")
    async def exchange(*args, **kwargs):
        raise RuntimeError("synthetic-pair-token echoed by synthetic peer")
    monkeypatch.setattr(native, "exchange_pairing_code", exchange)
    with pytest.raises(api.HTTPException) as error:
        asyncio.run(api.pair_device(PAIR_PAYLOAD))
    assert "synthetic-pair-token" not in error.value.detail


def test_config_parse_error_never_echoes_secret_line(synthetic_config):
    synthetic_config.write_text("token: [synthetic-config-token\n", encoding="utf-8")
    with pytest.raises(api.HTTPException) as error:
        api._load_config()
    assert "synthetic-config-token" not in error.value.detail
    assert "unterminated" not in error.value.detail


def test_device_config_repr_never_contains_native_token():
    config = NativeDeviceConfig.from_mapping({"id": "alias", "host": "127.0.0.1", "token": "synthetic-native-token"})
    assert "synthetic-native-token" not in repr(config)


def test_native_connection_errors_never_publish_or_log_configured_token(caplog):
    native = api._plugin_module("native_protocol")
    async def run():
        config = native.NativeDeviceConfig.from_mapping({"id": "alias", "host": "127.0.0.1", "token": "synthetic-native-token"})
        def factory(*args, **kwargs):
            raise native.NativeBridgeError("synthetic-native-token echoed by connector", code="AUTH_FAILED", retryable=False)
        client = native.NativeJiboClient(config, websocket_factory=factory)
        await client.start()
        await client._run_task
        state = client.state
        await client.stop()
        return state
    state = asyncio.run(run())
    assert "synthetic-native-token" not in json.dumps(state)
    assert "synthetic-native-token" not in caplog.text


@pytest.mark.parametrize("port", [0, 65536, "invalid", 1.5, True])
def test_pair_api_rejects_invalid_port_before_consuming_code(synthetic_config, monkeypatch, port):
    native = api._plugin_module("native_protocol")
    consumed = []
    async def exchange(*args, **kwargs):
        consumed.append(True)
        return {"token": "synthetic-pair-token", "device_id": "synthetic-robot"}
    monkeypatch.setattr(native, "exchange_pairing_code", exchange)
    with pytest.raises(api.HTTPException) as error:
        asyncio.run(api.pair_device(dict(PAIR_PAYLOAD, port=port)))
    assert error.value.status_code == 422
    assert consumed == []


def test_repair_preserves_existing_session_and_timeout_settings(synthetic_config, monkeypatch):
    native = api._plugin_module("native_protocol")
    document = api._load_config()
    original = dict(PAIR_PAYLOAD, token="synthetic-old-token", session_id="custom-session", request_timeout=27, reconnect_initial=7, session_title="Custom title")
    original.pop("code")
    document["gateway"]["platforms"]["jibo"]["extra"]["devices"] = [original]
    api._write_config(document)
    async def exchange(*args, **kwargs):
        return {"token": "synthetic-pair-token", "device_id": "synthetic-robot"}
    monkeypatch.setattr(native, "exchange_pairing_code", exchange)
    asyncio.run(api.pair_device(PAIR_PAYLOAD))
    saved = api._load_config()["gateway"]["platforms"]["jibo"]["extra"]["devices"][0]
    for key in ("session_id", "session_title", "request_timeout", "reconnect_initial"):
        assert saved[key] == original[key]


def test_stale_live_registry_cannot_claim_connected_after_new_token_save(synthetic_config, monkeypatch):
    native = api._plugin_module("native_protocol")
    document = api._load_config()
    current = {"id": "alias", "host": "127.0.0.1", "token": "synthetic-new-token", "robot_device_id": "synthetic-robot"}
    document["gateway"]["platforms"]["jibo"]["extra"]["devices"] = [current]
    api._write_config(document)
    class StaleAdapter:
        _device_configs = [native.NativeDeviceConfig.from_mapping(dict(current, token="synthetic-old-token"))]
        def dashboard_snapshot(self):
            return {"mode": "native", "connected": True, "devices": [{"id": "alias", "state": {"connected": True}}]}
    monkeypatch.setattr(api, "_live_adapter", lambda: StaleAdapter())
    overview = asyncio.run(api.get_overview())
    assert not overview["connected"]
    assert not overview["devices"][0]["state"]["connected"]
    assert overview["devices"][0]["restart_required"]
    assert "synthetic-new-token" not in json.dumps(overview)


def test_save_failure_during_config_reload_is_honest_about_received_pairing(synthetic_config, monkeypatch):
    native = api._plugin_module("native_protocol")
    async def exchange(*args, **kwargs):
        synthetic_config.write_text("bad: [malformed\n", encoding="utf-8")
        return {"token": "synthetic-pair-token", "device_id": "synthetic-robot"}
    monkeypatch.setattr(native, "exchange_pairing_code", exchange)
    with pytest.raises(api.HTTPException) as error:
        asyncio.run(api.pair_device(PAIR_PAYLOAD))
    assert error.value.status_code == 503
    assert "received" in error.value.detail.lower()
    assert "synthetic-pair-token" not in error.value.detail


def test_config_write_uses_context_local_hermes_profile(tmp_path, monkeypatch):
    import hermes_constants
    settings = api._plugin_module("model_settings")
    default_home = tmp_path / "default"
    selected_home = tmp_path / "selected-profile"
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    token = hermes_constants.set_hermes_home_override(selected_home)
    try:
        api._write_config({"model": {"default": "synthetic-model"}})
        assert (selected_home / "config.yaml").exists()
        assert not (default_home / "config.yaml").exists()
        assert settings.state_db_path() == selected_home / "state.db"
    finally:
        hermes_constants.reset_hermes_home_override(token)


def test_cross_process_old_connection_is_not_new_pairing_success(synthetic_config, monkeypatch):
    document = api._load_config()
    document["gateway"]["platforms"]["jibo"]["extra"]["devices"] = [{"id": "alias", "host": "127.0.0.1", "token": "synthetic-token", "robot_device_id": "synthetic-robot", "pairing_saved_at": 100}]
    status = api._plugin_module("device_status")
    monkeypatch.setattr(status, "read", lambda: {"alias": {"connected": True, "last_connected_at": 99}})
    before = REAL_CONFIGURED_SNAPSHOT(document)
    assert not before["connected"]
    assert before["devices"][0]["awaiting_new_session"]
    monkeypatch.setattr(status, "read", lambda: {"alias": {"connected": True, "last_connected_at": 101}})
    after = REAL_CONFIGURED_SNAPSHOT(document)
    assert after["connected"]
    assert not after["devices"][0]["awaiting_new_session"]
