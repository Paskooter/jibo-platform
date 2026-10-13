import asyncio
import importlib.util
import json
import pathlib
import sys
import unittest


PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
PACKAGE_NAME = "jibo_platform_native_poc_tests"
PACKAGE_SPEC = importlib.util.spec_from_file_location(
    PACKAGE_NAME,
    PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(PLUGIN_DIR)],
)
PACKAGE = importlib.util.module_from_spec(PACKAGE_SPEC)
sys.modules[PACKAGE_NAME] = PACKAGE
assert PACKAGE_SPEC.loader is not None
PACKAGE_SPEC.loader.exec_module(PACKAGE)

from jibo_platform_native_poc_tests.native_protocol import (  # noqa: E402
    INPUT_ROUTE_HERMES_FALLBACK,
    INPUT_ROUTE_LEGACY_FALLBACK,
    INPUT_ROUTE_LOCAL,
    NativeDeviceConfig,
    classify_input_route,
    decode_frame,
    exchange_pairing_code,
    make_request,
    parse_device_configs,
    NativeJiboClient,
)
from jibo_platform_native_poc_tests.pronunciation import (  # noqa: E402
    normalize_for_speech,
)


class NativePocTests(unittest.TestCase):
    def test_multi_device_config_and_secret_redaction(self):
        configs = parse_device_configs(
            {
                "devices": [
                    {"id": "kitchen", "host": "192.168.1.20", "token": "secret"},
                    {"id": "office", "host": "jibo-office.local", "enabled": False},
                ]
            }
        )
        self.assertEqual([item.id for item in configs], ["kitchen", "office"])
        self.assertEqual(configs[0].uri, "ws://192.168.1.20:8890/hermes")
        self.assertNotIn("secret", str(configs[0].to_dict()))
        self.assertEqual(configs[0].to_dict(include_token=True)["token"], "secret")

    def test_input_routing_keeps_local_turns_out_of_hermes(self):
        self.assertEqual(
            classify_input_route(
                {"method": "input.transcript", "params": {"route": "local"}}
            ),
            INPUT_ROUTE_LOCAL,
        )
        self.assertEqual(
            classify_input_route(
                {"method": "input.transcript", "params": {"route": "hermes_fallback"}}
            ),
            INPUT_ROUTE_HERMES_FALLBACK,
        )
        self.assertEqual(
            classify_input_route({"method": "input.transcript", "params": {}}),
            INPUT_ROUTE_LEGACY_FALLBACK,
        )

    def test_protocol_frame_and_pronunciation_views(self):
        request = make_request("speech.speak", {"text": "The AI API uses a URL."})
        self.assertEqual(decode_frame(request)["method"], "speech.speak")
        pronunciation = normalize_for_speech("The AI API uses a URL.")
        self.assertEqual(pronunciation.raw_text, "The AI API uses a URL.")
        self.assertIn("<phoneme", pronunciation.esml)
        self.assertIn("A I", pronunciation.spoken_text)
        self.assertIn("U R L", pronunciation.spoken_text)

    def test_device_mapping_is_validated(self):
        config = NativeDeviceConfig.from_mapping(
            {"id": "living-room", "host": "jibo.local", "port": 8891}
        )
        self.assertEqual(config.path, "/hermes")
        with self.assertRaises(ValueError):
            NativeDeviceConfig.from_mapping({"id": "missing-host"})


class FakeNativeSocket:
    def __init__(self):
        self.sent = []
        self.incoming = asyncio.Queue()
        self.open_started = asyncio.Event()
        self.allow_open = asyncio.Event()
        self.allow_open.set()

    async def send(self, raw):
        request = json.loads(raw)
        self.sent.append(request)
        method = request["method"]
        if method == "auth":
            result = {
                "device_id": "kitchen",
                "name": "Kitchen",
                "capabilities": ["status.get", "speech.speak"],
            }
        elif method == "pair.exchange":
            result = {
                "token": "secure-token",
                "device_id": "kitchen",
                "name": "Kitchen",
            }
        elif method == "session.open":
            self.open_started.set()
            await self.allow_open.wait()
            result = {"session_id": request["params"]["session_id"], "status": "active"}
        else:
            result = {"echo": method}
        await self.incoming.put(
            json.dumps(
                {
                    "v": 1,
                    "kind": "response",
                    "id": request["id"],
                    "method": method,
                    "result": result,
                }
            )
        )

    async def close(self):
        await self.incoming.put(None)

    async def recv(self):
        value = await self.incoming.get()
        if value is None:
            raise RuntimeError("fake socket closed")
        return value

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.incoming.get()
        if value is None:
            raise StopAsyncIteration
        return value


class NativeClientHandshakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_pairing_code_exchange_returns_secure_token(self):
        socket = FakeNativeSocket()

        def factory(_uri, **_options):
            return socket

        config = NativeDeviceConfig.from_mapping(
            {"id": "kitchen", "host": "127.0.0.1"}
        )
        result = await exchange_pairing_code(
            config,
            "ab2k7",
            websocket_factory=factory,
        )
        self.assertEqual(result["token"], "secure-token")
        self.assertEqual(socket.sent[0]["method"], "pair.exchange")
        self.assertEqual(socket.sent[0]["params"]["code"], "AB2K7")

    async def test_auth_session_and_correlated_request(self):
        socket = FakeNativeSocket()

        def factory(_uri, **_options):
            return socket

        config = NativeDeviceConfig.from_mapping(
            {"id": "kitchen", "host": "127.0.0.1", "token": "test-token"}
        )
        client = NativeJiboClient(config, websocket_factory=factory)
        await client.start()
        try:
            self.assertTrue(await client.wait_until_connected(timeout=1))
            await asyncio.sleep(0)
            response = await client.request("status.get")
            self.assertEqual(response, {"echo": "status.get"})
            self.assertEqual(
                [item["method"] for item in socket.sent],
                ["auth", "session.open", "status.get"],
            )
            self.assertEqual(client.capabilities, ("speech.speak", "status.get"))
        finally:
            await client.stop()

    async def test_connection_is_not_ready_until_session_open_is_confirmed(self):
        socket = FakeNativeSocket()
        socket.allow_open.clear()
        config = NativeDeviceConfig.from_mapping(
            {'id': 'kitchen', 'host': '127.0.0.1', 'token': 'test-token'})
        client = NativeJiboClient(config, websocket_factory=lambda *_args, **_kwargs: socket)
        await client.start()
        try:
            await asyncio.wait_for(socket.open_started.wait(), timeout=1)
            self.assertFalse(client.connected)
            self.assertFalse(client.state['connected'])
            socket.allow_open.set()
            self.assertTrue(await client.wait_until_connected(timeout=1))
        finally:
            socket.allow_open.set()
            await client.stop()


if __name__ == "__main__":
    unittest.main()
