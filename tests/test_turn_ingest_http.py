"""The ingest listener as Phoenix actually reaches it: over HTTP."""

import asyncio
import importlib.util
import json
import pathlib
import socket
import sys
import unittest


_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "jibo_platform_ingest_http_tests"
_PACKAGE_SPEC = importlib.util.spec_from_file_location(
    _PACKAGE_NAME,
    _PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(_PLUGIN_DIR)],
)
_PACKAGE = importlib.util.module_from_spec(_PACKAGE_SPEC)
sys.modules[_PACKAGE_NAME] = _PACKAGE
assert _PACKAGE_SPEC.loader is not None
_PACKAGE_SPEC.loader.exec_module(_PACKAGE)

from jibo_platform_ingest_http_tests.turn_ingest import (  # noqa: E402
    API_PREFIX,
    TurnIngestConfig,
    TurnIngestServer,
)

try:
    import aiohttp
except ImportError:  # pragma: no cover - the gateway ships aiohttp
    aiohttp = None

TOKEN = "http-test-token"


class _RecordingAdapter:
    """Only the surface the listener calls."""

    def __init__(self):
        self.device_ids = ("robot",)
        self.connected = True
        self.recorded = []
        self.escalations = []

    def has_device(self, device_id):
        return device_id == "robot"

    async def record_turn(self, record, *, attribute_skill=False):
        self.recorded.append(record)
        return {"turn_id": record.turn_id, "device_id": record.device_id, "history": True}

    async def escalate_turn(self, *, device_id, text, turn_id, on_delta=None, **kwargs):
        self.escalations.append({"device_id": device_id, "text": text, "turn_id": turn_id, **kwargs})
        if on_delta is not None:
            on_delta("Because of ")
            on_delta("Rayleigh scattering.")
        return {"turn_id": turn_id, "text": "Because of Rayleigh scattering.", "spoken": False}


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@unittest.skipIf(aiohttp is None, "aiohttp is required for the ingest listener")
class TurnIngestHTTPTests(unittest.TestCase):
    def _run(self, scenario):
        async def wrapper():
            adapter = _RecordingAdapter()
            config = TurnIngestConfig.from_extra(
                {
                    "turn_ingest": {
                        "enabled": True,
                        "host": "127.0.0.1",
                        "port": _free_port(),
                        "token": TOKEN,
                    }
                },
                environ={},
            )
            server = TurnIngestServer(adapter, config)
            self.assertTrue(await server.start())
            base = f"http://127.0.0.1:{config.port}{API_PREFIX}"
            try:
                async with aiohttp.ClientSession() as session:
                    return await scenario(session, base, adapter)
            finally:
                await server.stop()

        return asyncio.run(wrapper())

    def test_health_needs_no_token_and_names_the_robots(self):
        async def scenario(session, base, adapter):
            async with session.get(f"{base}/health") as response:
                self.assertEqual(response.status, 200)
                return await response.json()

        body = self._run(scenario)
        self.assertEqual(body["devices"], ["robot"])
        self.assertEqual(body["turn_record_version"], 1)

    def test_a_turn_report_needs_the_shared_token(self):
        async def scenario(session, base, adapter):
            payload = {"device_id": "robot", "transcript": "hi", "response": "hello"}
            async with session.post(f"{base}/turns", json=payload) as response:
                unauthorized = response.status
            async with session.post(
                f"{base}/turns", json=payload, headers={"Authorization": f"Bearer {TOKEN}"}
            ) as response:
                accepted = (response.status, await response.json())
            return unauthorized, accepted, adapter.recorded

        unauthorized, (status, body), recorded = self._run(scenario)
        self.assertEqual(unauthorized, 401)
        self.assertEqual(status, 202)
        self.assertEqual(len(recorded), 1)
        self.assertEqual(body["recorded"][0]["device_id"], "robot")

    def test_a_malformed_body_is_a_clean_error_not_a_crash(self):
        async def scenario(session, base, adapter):
            async with session.post(
                f"{base}/turns",
                data=b"{not json",
                headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
            ) as response:
                return response.status, await response.json()

        status, body = self._run(scenario)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_an_escalation_arrives_carrying_the_robot_that_asked(self):
        async def scenario(session, base, adapter):
            async with session.post(
                f"{base}/chat/completions",
                json={"messages": [{"role": "user", "content": "why is the sky blue"}]},
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "X-Jibo-Device-Id": "robot",
                    "X-Jibo-Turn-Id": "ph-99",
                },
            ) as response:
                return response.status, await response.json(), adapter.escalations

        status, body, escalations = self._run(scenario)
        self.assertEqual(status, 200)
        self.assertEqual(body["choices"][0]["message"]["content"], "Because of Rayleigh scattering.")
        self.assertEqual(body["jibo"]["turn_id"], "ph-99")
        self.assertEqual(escalations[0]["device_id"], "robot")
        self.assertEqual(escalations[0]["text"], "why is the sky blue")

    def test_a_streaming_escalation_is_served_as_server_sent_events(self):
        async def scenario(session, base, adapter):
            async with session.post(
                f"{base}/chat/completions",
                json={
                    "stream": True,
                    "messages": [{"role": "user", "content": "why is the sky blue"}],
                },
                headers={"Authorization": f"Bearer {TOKEN}", "X-Jibo-Device-Id": "robot"},
            ) as response:
                self.assertEqual(response.status, 200)
                return (await response.read()).decode()

        body = self._run(scenario)
        chunks = [
            json.loads(line[len("data: "):])
            for line in body.splitlines()
            if line.startswith("data: ") and not line.endswith("[DONE]")
        ]
        text = "".join(
            chunk["choices"][0]["delta"].get("content", "") for chunk in chunks
        )
        self.assertEqual(text, "Because of Rayleigh scattering.")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
        self.assertIn("data: [DONE]", body)


if __name__ == "__main__":
    unittest.main()
