"""The integration spine: three producers, one conversation.

These exercise the host side end to end against a fake robot and a fake
session store — a Phoenix-handled turn, a Phoenix escalation, and a streamed
Hermes answer — without a robot, a gateway, or a network.
"""

import asyncio
import importlib.util
import pathlib
import sys
import types
import unittest
import os
import tempfile
from collections import deque

import yaml


_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "jibo_platform_spine_tests"
_PACKAGE_SPEC = importlib.util.spec_from_file_location(
    _PACKAGE_NAME,
    _PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(_PLUGIN_DIR)],
)
_PACKAGE = importlib.util.module_from_spec(_PACKAGE_SPEC)
sys.modules[_PACKAGE_NAME] = _PACKAGE
assert _PACKAGE_SPEC.loader is not None
_PACKAGE_SPEC.loader.exec_module(_PACKAGE)

from jibo_platform_spine_tests import model_settings  # noqa: E402
from jibo_platform_spine_tests.native_adapter import (  # noqa: E402
    CHAT_HISTORY_METHOD,
    CHAT_STATUS_METHOD,
    RESPONSE_DELTA_METHOD,
    TURN_APPEND_METHOD,
    NativeJiboAdapter,
)
from jibo_platform_spine_tests.native_protocol import (  # noqa: E402
    NativeDeviceConfig,
    NativeRemoteError,
)
from jibo_platform_spine_tests.turn_ingest import (  # noqa: E402
    IngestConfigError,
    TurnIngestConfig,
    TurnIngestError,
    TurnIngestHandlers,
)
from jibo_platform_spine_tests.turn_record import (  # noqa: E402
    ORIGIN_BRIDGE,
    ORIGIN_PHOENIX,
    ORIGIN_PHOENIX_ESCALATED,
    TurnRecord,
    new_turn_id,
)

TOKEN = "test-ingest-token"


class _FakeClient:
    """A Be peer that records what the host asked it to do."""

    def __init__(self, capabilities=()):
        self.connected = True
        self.session_id = "jibo:robot"
        self.capabilities = tuple(capabilities)
        self.requests = []
        self.notifications = []
        self.state = {"connected": True, "device_id": "robot"}

    async def request(self, method, params=None, timeout=None):
        self.requests.append((method, dict(params or {})))
        return {}

    async def notify(self, method, params=None, session_id=None):
        self.notifications.append((method, dict(params or {})))

    def methods(self, name):
        return [params for method, params in self.requests if method == name]

    def notified(self, name):
        return [params for method, params in self.notifications if method == name]


class _FakeOverrideStore:
    """Just the session-model override half of the gateway session store."""

    def __init__(self):
        self.overrides = {}
        self.writes = 0

    def get_model_override(self, session_key):
        return self.overrides.get(session_key)

    def set_model_override(self, session_key, override):
        self.writes += 1
        self.overrides[session_key] = override


class _FakeStore:
    """Just enough SessionStore for the canonical-history projection."""

    def __init__(self):
        self.rows = []
        self._ids = set()

    def get_or_create_session(self, source):
        return types.SimpleNamespace(session_id="sess-1")

    def has_platform_message_id(self, session_id, message_id):
        return message_id in self._ids

    def append_to_transcript(self, session_id, message):
        self._ids.add(message.get("platform_message_id"))
        self.rows.append(dict(message))


class _FakeHistoryDB:
    def __init__(self):
        self.sessions = [
            {
                "id": "saved-1",
                "source": "jibo",
                "title": "First saved conversation",
                "preview": "Tell me about whales",
                "started_at": 100,
                "last_active": 200,
                "last_activity_at": 200,
                "message_count": 11,
            }
        ]
        self.messages = [
            {"role": "system", "content": "never display this"},
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "tool", "content": "never display this either"},
        ]

    def list_sessions_rich(self, **_kwargs):
        return [dict(row) for row in self.sessions]

    def get_session(self, session_id):
        for row in self.sessions:
            if row["id"] == session_id:
                return dict(row)
        return None

    def get_messages_as_conversation(self, _session_id, **_kwargs):
        return [dict(row) for row in self.messages]


class _SpineAdapter(NativeJiboAdapter):
    """One configured robot, no gateway, no agent."""

    def __init__(self, *, capabilities=(), extra=None):
        extra = dict(extra or {})
        self.config = types.SimpleNamespace(extra=extra)
        self._device_configs = [
            NativeDeviceConfig.from_mapping(
                {"id": "robot", "name": "Robot", "host": "10.0.0.5", "token": "t"}
            )
        ]
        self.client = _FakeClient(capabilities)
        self._clients = {"robot": self.client}
        self._device_states = {}
        self._logs = {"robot": deque(maxlen=50)}
        self._turn_log = {"robot": deque(maxlen=50)}
        self._turn_index = {"robot": {}}
        self._pending_escalations = {}
        self._streams = {}
        self._delta_seq = {}
        self._turn_ids = {}
        self._active_turns = {}
        self._session_devices = {}
        self._processing_stages = {}
        self._delivered_answers = {}
        self._speak_locks = {}
        self._last_device_id = "robot"
        self._loop = None
        self._closing = False
        self.store = _FakeStore()
        self._session_store = self.store
        self._ingest_config = TurnIngestConfig()
        self._ingest_server = None
        self.stages = []
        self.dispatched = []

    # The Hermes session and the agent both sit outside these tests.
    def _turn_source(self, device_id):
        return types.SimpleNamespace(chat_id=f"jibo:{device_id}")

    def update_processing_stage(self, stage, *, session_id=None, device_id=None):
        self.stages.append(stage)
        return True

    def finish_processing(self, *, success, session_id=None, device_id=None):
        return True

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        # Standalone unit fixtures invoke model-final delivery directly. Give
        # those calls the same origin context installed by gateway ingress.
        # Tests of out-of-band sends call NativeJiboAdapter.send explicitly.
        module = sys.modules[NativeJiboAdapter.__module__]
        token = None
        if module._NATIVE_TURN.get() is None:
            device = self.resolve_device_id(chat_id=chat_id,require_connected=False)
            token = module._NATIVE_TURN.set((self,device,self._turn_ids.get(device,'')))
        try:
            return await super().send(chat_id,content,reply_to=reply_to,metadata=metadata)
        finally:
            if token is not None:
                module._NATIVE_TURN.reset(token)

    async def _dispatch_utterance(
        self, device_id, text, frame, *, origin=ORIGIN_BRIDGE, turn_id=""
    ):
        self._turn_ids[device_id] = turn_id or new_turn_id(origin)
        self._active_turns[device_id] = {
            "session_key": "agent:main:jibo",
            "source": None,
            "text": text,
            "origin": origin,
            "turn_id": self._turn_ids[device_id],
            "started_at": "2026-08-29T00:00:00+00:00",
        }
        self.dispatched.append((device_id, text, origin, self._turn_ids[device_id]))


def _handlers(adapter, **config):
    settings = {"enabled": True, "token": TOKEN}
    settings.update(config)
    return TurnIngestHandlers(adapter, TurnIngestConfig.from_extra({"turn_ingest": settings}))


def _phoenix_report(**overrides):
    payload = {
        "turn_id": "ph-1",
        "device_id": "robot",
        "origin": ORIGIN_PHOENIX,
        "transcript": "what time is it",
        "response": "It's twenty past four.",
        "intent": "clock.time",
        "skill": "clock-skill",
        "speech_owner": "phoenix",
        "timing": {"asr_ms": 300, "total_ms": 520},
    }
    payload.update(overrides)
    return payload


async def _until(predicate, *, tries=100):
    for _ in range(tries):
        if predicate():
            return True
        await asyncio.sleep(0)
    return False


class IngestAuthTests(unittest.TestCase):
    def test_an_unauthenticated_writer_cannot_touch_history(self):
        handlers = _handlers(_SpineAdapter())
        with self.assertRaises(TurnIngestError) as caught:
            handlers.authorize("")
        self.assertEqual(caught.exception.status, 401)
        with self.assertRaises(TurnIngestError):
            handlers.authorize("Bearer wrong-token")
        handlers.authorize(f"Bearer {TOKEN}")

    def test_the_listener_refuses_to_run_unauthenticated_off_loopback(self):
        with self.assertRaises(IngestConfigError):
            TurnIngestConfig.from_extra(
                {"turn_ingest": {"enabled": True, "host": "0.0.0.0"}}, environ={}
            )
        # A loopback development peer still has to say so explicitly.
        with self.assertRaises(IngestConfigError):
            TurnIngestConfig.from_extra(
                {"turn_ingest": {"enabled": True, "host": "127.0.0.1"}}, environ={}
            )
        config = TurnIngestConfig.from_extra(
            {
                "turn_ingest": {
                    "enabled": True,
                    "host": "127.0.0.1",
                    "allow_insecure_local": True,
                }
            },
            environ={},
        )
        self.assertTrue(config.enabled)


class PhoenixTurnReportTests(unittest.TestCase):
    def test_an_inline_tool_row_does_not_mark_the_turn_finished(self):
        adapter = _SpineAdapter(capabilities=(TURN_APPEND_METHOD,))
        record = TurnRecord(
            turn_id="image-row",
            device_id="robot",
            origin="hermes",
            response="Image",
        )

        async def scenario():
            await adapter._render_turn("robot", record, final=False)
            await adapter._render_turn("robot", record, final=True)

        asyncio.run(scenario())
        rows = adapter.client.methods(TURN_APPEND_METHOD)
        self.assertNotIn("final", rows[0])
        self.assertTrue(rows[1]["final"])

    def test_rate_limit_notice_is_spoken_once_without_recording_the_failed_turn(self):
        adapter = _SpineAdapter()
        adapter._turn_ids["robot"] = "failed-turn"
        adapter._active_turns["robot"] = {
            "text": "an old question",
            "turn_id": "failed-turn",
            "origin": ORIGIN_BRIDGE,
        }

        result = asyncio.run(adapter.send(
            "jibo:robot",
            "The model usage limit is reached. Please try again later.",
            metadata={"_discard_failed_turn": True},
        ))

        self.assertTrue(result.success)
        self.assertEqual(len(adapter.client.methods("speech.speak")), 1)
        self.assertEqual(adapter.store.rows, [])
        self.assertNotIn("robot", adapter._active_turns)
        self.assertNotIn("robot", adapter._turn_ids)
        self.assertEqual(adapter.get_turn_log("robot"), [])

    def test_a_speech_timeout_is_never_offered_for_retry(self):
        """A timed-out speak may already have been heard; re-sending repeats it."""
        adapter = _SpineAdapter()

        class _SlowClient(_FakeClient):
            async def request(self, method, params=None, timeout=None):
                if method == "speech.speak":
                    raise NativeRemoteError(
                        {
                            "code": "REQUEST_TIMEOUT",
                            "message": "Native bridge request timed out",
                            "retryable": True,
                        }
                    )
                return await super().request(method, params, timeout=timeout)

        adapter.client = _SlowClient()
        adapter._clients = {"robot": adapter.client}

        result = asyncio.run(adapter.send("jibo:robot", "a long spoken answer"))

        self.assertFalse(result.success)
        self.assertFalse(result.retryable)

    def _config_with_model(self, model="", provider=""):
        """A Hermes config whose robot is this test's device, on `model`."""
        entry = {"id": "robot", "host": "10.0.0.5", "token": "t"}
        if model:
            entry["model"] = model
        if provider:
            entry["model_provider"] = provider
        document = {
            "model": {"default": "gpt-5.6-luna", "provider": "openai-codex"},
            "gateway": {"platforms": {"jibo": {"extra": {"devices": [entry]}}}},
        }
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        yaml.safe_dump(document, handle)
        handle.close()
        os.environ["HERMES_CONFIG_PATH"] = handle.name
        self.addCleanup(os.environ.pop, "HERMES_CONFIG_PATH", None)
        self.addCleanup(os.unlink, handle.name)
        model_settings.invalidate_cache()
        self.addCleanup(model_settings.invalidate_cache)

    def test_a_turn_uses_the_model_configured_for_that_robot(self):
        adapter = _SpineAdapter()
        self._config_with_model("grok-4.6", "xai-oauth")
        store = _FakeOverrideStore()
        adapter._session_store = store
        state = types.SimpleNamespace(
            conversation=types.SimpleNamespace(model_override={"model": "stale"})
        )
        adapter.gateway_runner = types.SimpleNamespace(
            _peek_session_state=lambda key: state
        )

        adapter._apply_device_model("robot", "agent:main:jibo")

        self.assertEqual(
            store.overrides["agent:main:jibo"],
            {"model": "grok-4.6", "provider": "xai-oauth"},
        )
        # The live copy is dropped so the gateway re-reads the override, and
        # re-resolves its credentials, on the next turn.
        self.assertIsNone(state.conversation.model_override)

    def test_a_dashboard_change_reaches_the_gateway_without_a_restart(self):
        """The setting is re-read from disk, not from the copy parsed at boot."""
        adapter = _SpineAdapter()
        self._config_with_model()
        store = _FakeOverrideStore()
        adapter._session_store = store

        adapter._apply_device_model("robot", "agent:main:jibo")
        # Nothing set and nothing stored: there is nothing to write.
        self.assertIsNone(store.overrides.get("agent:main:jibo"))

        # Another process rewrites the config; this one was never restarted.
        model_settings.set_device_model("robot", "claude-opus-5", "openrouter")
        adapter._apply_device_model("robot", "agent:main:jibo")

        self.assertEqual(
            store.overrides["agent:main:jibo"],
            {"model": "claude-opus-5", "provider": "openrouter"},
        )

    def test_a_robot_with_no_model_set_clears_the_override(self):
        adapter = _SpineAdapter()
        self._config_with_model()
        store = _FakeOverrideStore()
        store.overrides["agent:main:jibo"] = {"model": "left-over"}
        adapter._session_store = store

        adapter._apply_device_model("robot", "agent:main:jibo")

        self.assertIsNone(store.overrides["agent:main:jibo"])

    def test_an_unchanged_model_is_not_rewritten(self):
        adapter = _SpineAdapter()
        self._config_with_model("grok-4.6", "xai-oauth")
        store = _FakeOverrideStore()
        store.overrides["agent:main:jibo"] = {"model": "grok-4.6", "provider": "xai-oauth"}
        adapter._session_store = store

        adapter._apply_device_model("robot", "agent:main:jibo")

        self.assertEqual(store.writes, 0)

    def test_a_provider_status_notice_is_shown_but_never_spoken(self):
        adapter = _SpineAdapter()
        adapter._turn_ids["robot"] = "live-turn"

        result = asyncio.run(adapter.send_or_update_status(
            "jibo:robot",
            "provider",
            "\u23f1\ufe0f The model provider is rate-limiting requests. "
            "Please wait a moment and try again.",
        ))

        self.assertTrue(result.success)
        self.assertEqual(adapter.client.methods("speech.speak"), [])
        self.assertEqual(adapter.client.methods(TURN_APPEND_METHOD), [])
        self.assertEqual(adapter.store.rows, [])
        self.assertEqual(
            adapter.client.methods(CHAT_STATUS_METHOD),
            [{'state': 'thinking', 'detail': 'provider rate limit', 'turn_id': 'live-turn'}],
        )

    def test_a_status_notice_after_the_turn_ends_changes_nothing(self):
        adapter = _SpineAdapter()

        result = asyncio.run(adapter.send_or_update_status(
            "jibo:robot", "provider", "The model provider is rate-limiting requests.",
        ))

        self.assertTrue(result.success)
        self.assertEqual(adapter.client.requests, [])

    def test_a_status_notice_keeps_its_own_words_when_it_is_not_an_error(self):
        adapter = _SpineAdapter()
        adapter._turn_ids["robot"] = "live-turn"

        asyncio.run(adapter.send_or_update_status(
            "jibo:robot", "lifecycle", "Compressing the conversation; this takes a moment.",
        ))

        self.assertEqual(
            adapter.client.methods(CHAT_STATUS_METHOD),
            [{'state': 'thinking', 'detail': 'Compressing the conversation', 'turn_id': 'live-turn'}],
        )

    def test_a_turn_phoenix_answered_alone_lands_in_history_and_on_screen(self):
        adapter = _SpineAdapter(capabilities=(TURN_APPEND_METHOD,))
        handlers = _handlers(adapter)
        result = asyncio.run(handlers.ingest_turns(_phoenix_report()))

        self.assertEqual(result["recorded"][0]["turn_id"], "ph-1")
        self.assertTrue(result["recorded"][0]["history"])
        self.assertTrue(result["recorded"][0]["rendered"])

        # Canonical history gets both halves, so the agent's next turn knows
        # the exchange happened.
        self.assertEqual([row["role"] for row in adapter.store.rows], ["user", "assistant"])
        self.assertEqual(adapter.store.rows[1]["content"], "It's twenty past four.")

        # And the robot's chat UI gets the record to render.
        rendered = adapter.client.methods(TURN_APPEND_METHOD)
        self.assertEqual(len(rendered), 1)
        self.assertEqual(rendered[0]["origin"], ORIGIN_PHOENIX)
        self.assertEqual(rendered[0]["skill"], "clock-skill")
        self.assertTrue(rendered[0]["final"])

    def test_a_retried_report_does_not_duplicate_the_turn(self):
        adapter = _SpineAdapter(capabilities=(TURN_APPEND_METHOD,))
        handlers = _handlers(adapter)

        async def scenario():
            await handlers.ingest_turns(_phoenix_report())
            await handlers.ingest_turns(_phoenix_report())

        asyncio.run(scenario())
        self.assertEqual(len(adapter.store.rows), 2)
        self.assertEqual(len(adapter.get_turn_log("robot")), 1)

    def test_a_report_for_an_unknown_robot_is_refused(self):
        handlers = _handlers(_SpineAdapter())
        with self.assertRaises(TurnIngestError) as caught:
            asyncio.run(handlers.ingest_turns(_phoenix_report(device_id="somebody-elses")))
        self.assertEqual(caught.exception.status, 404)

    def test_a_malformed_report_is_refused_rather_than_half_recorded(self):
        adapter = _SpineAdapter()
        handlers = _handlers(adapter)
        with self.assertRaises(TurnIngestError) as caught:
            asyncio.run(handlers.ingest_turns(_phoenix_report(origin="whoever")))
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(adapter.store.rows, [])

    def test_a_batch_is_bounded(self):
        handlers = _handlers(_SpineAdapter())
        with self.assertRaises(TurnIngestError):
            asyncio.run(handlers.ingest_turns({"turns": [_phoenix_report()] * 40}))

    def test_one_image_is_rendered_and_the_rest_are_reported(self):
        adapter = _SpineAdapter(capabilities=(TURN_APPEND_METHOD,))
        handlers = _handlers(adapter)
        report = _phoenix_report(
            media=[
                {"kind": "image", "path": "/opt/jibo/media/radar.png", "width": 640, "height": 480},
                {"kind": "image", "url": "https://h/second.png"},
                {"kind": "image", "url": "https://h/third.png"},
            ]
        )
        asyncio.run(handlers.ingest_turns(report))
        rendered = adapter.client.methods(TURN_APPEND_METHOD)[0]
        self.assertEqual(len(rendered["media"]), 1)
        # The handle the robot's loader can open directly, with the size the
        # row needs to reserve.
        self.assertEqual(rendered["media"][0]["path"], "/opt/jibo/media/radar.png")
        self.assertEqual(rendered["media"][0]["width"], 640)
        self.assertEqual(rendered["media_omitted"], 2)

    def test_the_dashboard_can_see_every_turn(self):
        adapter = _SpineAdapter()
        handlers = _handlers(adapter)
        asyncio.run(handlers.ingest_turns(_phoenix_report()))
        snapshot = adapter.dashboard_snapshot()
        self.assertEqual(snapshot["devices"][0]["turns"][0]["turn_id"], "ph-1")
        self.assertIn("turn_ingest", snapshot)


class EscalationTests(unittest.TestCase):
    def _escalate(self, adapter, headers, answer, *, speech_owner="phoenix"):
        handlers = _handlers(adapter)
        payload = {
            "model": "hermes-jibo",
            "messages": [{"role": "user", "content": "why is the sky blue"}],
            "jibo": {"skill": "answer-skill", "intent": "answer"},
        }

        async def scenario():
            task = asyncio.ensure_future(handlers.chat_completions(payload, headers))
            started = await _until(lambda: adapter._pending_escalations)
            assert started, "the escalation was never registered"
            await adapter.send("jibo:robot", answer)
            return await task

        return asyncio.run(scenario())

    def test_an_escalation_is_correlated_by_the_identity_it_carries(self):
        adapter = _SpineAdapter(capabilities=(TURN_APPEND_METHOD,))
        result = self._escalate(
            adapter,
            {"X-Jibo-Device-Id": "robot", "X-Jibo-Turn-Id": "ph-42"},
            "Because of Rayleigh scattering.",
        )
        self.assertEqual(result["choices"][0]["message"]["content"], "Because of Rayleigh scattering.")
        self.assertEqual(result["jibo"]["turn_id"], "ph-42")
        self.assertEqual(result["jibo"]["device_id"], "robot")

        # The turn Hermes answered went to the robot that asked, under the
        # turn id Phoenix minted — correlated, not guessed.
        dispatched = adapter.dispatched[0]
        self.assertEqual((dispatched[0], dispatched[2], dispatched[3]), ("robot", ORIGIN_PHOENIX_ESCALATED, "ph-42"))
        self.assertEqual(adapter.get_turn_log("robot")[0]["turn_id"], "ph-42")

    def test_hermes_stays_silent_when_phoenix_will_speak_the_answer(self):
        adapter = _SpineAdapter(capabilities=(TURN_APPEND_METHOD,))
        result = self._escalate(
            adapter, {"X-Jibo-Device-Id": "robot", "X-Jibo-Speech-Owner": "phoenix"}, "Blue light scatters."
        )
        self.assertFalse(result["jibo"]["spoken"])
        self.assertEqual(adapter.client.methods("speech.speak"), [])
        # It is still rendered, so the screen shows the exchange.
        self.assertEqual(len(adapter.client.methods(TURN_APPEND_METHOD)), 1)

    def test_hermes_speaks_when_the_escalation_asks_it_to(self):
        adapter = _SpineAdapter(capabilities=(TURN_APPEND_METHOD,))
        result = self._escalate(
            adapter, {"X-Jibo-Device-Id": "robot", "X-Jibo-Speech-Owner": "hermes"}, "Blue light scatters."
        )
        self.assertTrue(result["jibo"]["spoken"])
        self.assertTrue(adapter.client.methods("speech.speak"))

    def test_an_escalated_turn_is_not_written_to_history_twice(self):
        # The agent already persisted this exchange; the spine must not
        # duplicate every escalated reply in the transcript.
        adapter = _SpineAdapter(capabilities=(TURN_APPEND_METHOD,))
        self._escalate(adapter, {"X-Jibo-Device-Id": "robot"}, "Blue light scatters.")
        self.assertEqual(adapter.store.rows, [])

    def test_an_escalation_with_no_robot_is_refused_rather_than_guessed(self):
        handlers = _handlers(_SpineAdapter())
        with self.assertRaises(TurnIngestError) as caught:
            asyncio.run(
                handlers.chat_completions({"messages": [{"role": "user", "content": "hi"}]}, {})
            )
        self.assertEqual(caught.exception.status, 400)

    def test_an_escalation_for_an_unknown_robot_is_refused(self):
        handlers = _handlers(_SpineAdapter())
        with self.assertRaises(TurnIngestError) as caught:
            asyncio.run(
                handlers.chat_completions(
                    {"messages": [{"role": "user", "content": "hi"}]},
                    {"X-Jibo-Device-Id": "somebody-elses"},
                )
            )
        self.assertEqual(caught.exception.status, 404)

    def test_an_escalation_with_nothing_to_answer_is_refused(self):
        handlers = _handlers(_SpineAdapter())
        with self.assertRaises(TurnIngestError):
            asyncio.run(
                handlers.chat_completions(
                    {"messages": [{"role": "assistant", "content": "hi"}]},
                    {"X-Jibo-Device-Id": "robot"},
                )
            )

    def test_a_single_configured_robot_may_be_the_default(self):
        adapter = _SpineAdapter()
        handlers = _handlers(adapter, default_device_id="robot")
        payload = {"messages": [{"role": "user", "content": "hi"}]}

        async def scenario():
            task = asyncio.ensure_future(handlers.chat_completions(payload, {}))
            assert await _until(lambda: adapter._pending_escalations)
            await adapter.send("jibo:robot", "Hello there.")
            return await task

        self.assertEqual(asyncio.run(scenario())["jibo"]["device_id"], "robot")


class ChatHistoryTests(unittest.TestCase):
    def test_history_is_bounded_read_only_and_keeps_tool_rows_off_screen(self):
        adapter = _SpineAdapter()
        adapter.store._db = _FakeHistoryDB()

        asyncio.run(
            adapter._on_native_event(
                "robot", {"method": "chat.history.request", "params": {"mode": "sessions"}}
            )
        )
        sessions = adapter.client.methods(CHAT_HISTORY_METHOD)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["kind"], "sessions")
        self.assertEqual(sessions[0]["sessions"][0]["id"], "saved-1")

        asyncio.run(
            adapter._on_native_event(
                "robot",
                {
                    "method": "chat.history.request",
                    "params": {"mode": "transcript", "session_id": "saved-1"},
                },
            )
        )
        transcript = adapter.client.methods(CHAT_HISTORY_METHOD)[-1]
        self.assertEqual(transcript["kind"], "transcript")
        self.assertEqual([item["role"] for item in transcript["messages"]], ["user", "assistant"])
        self.assertNotIn("never display", str(transcript["messages"]))
        # The history reader never calls a session switch or dispatches a turn.
        self.assertEqual(adapter.dispatched, [])

    def test_history_rejects_non_jibo_session_ids(self):
        db = _FakeHistoryDB()
        db.sessions[0]["source"] = "discord"
        payload = NativeJiboAdapter._history_transcript_page(db, "saved-1", 0)
        self.assertEqual(payload["kind"], "error")


class StreamingTests(unittest.TestCase):
    def test_streaming_is_offered_only_when_the_robot_can_render_it(self):
        self.assertFalse(_SpineAdapter().supports_draft_streaming(chat_id="jibo:robot"))
        self.assertTrue(
            _SpineAdapter(capabilities=(RESPONSE_DELTA_METHOD,)).supports_draft_streaming(
                chat_id="jibo:robot"
            )
        )
        # And it can be switched off for a robot that would rather not.
        self.assertFalse(
            _SpineAdapter(
                capabilities=(RESPONSE_DELTA_METHOD,), extra={"streaming": {"enabled": False}}
            ).supports_draft_streaming(chat_id="jibo:robot")
        )

    def test_text_renders_as_produced_and_speech_starts_at_a_sentence(self):
        adapter = _SpineAdapter(
            capabilities=(RESPONSE_DELTA_METHOD, TURN_APPEND_METHOD),
            extra={"streaming": {"min_delta_chars": 0}},
        )

        async def scenario():
            await adapter.send_draft("jibo:robot", 7, "The forecast is clear all day.")
            await adapter.send_draft(
                "jibo:robot", 7, "The forecast is clear all day. Bring a jacket after dark."
            )
            return await adapter.send(
                "jibo:robot", "The forecast is clear all day. Bring a jacket after dark."
            )

        result = asyncio.run(scenario())
        self.assertTrue(result.success)

        deltas = adapter.client.notified(RESPONSE_DELTA_METHOD)
        self.assertGreaterEqual(len(deltas), 2)
        self.assertEqual([item["seq"] for item in deltas], sorted(item["seq"] for item in deltas))
        self.assertEqual("".join(item["text"] for item in deltas).strip(),
                         "The forecast is clear all day. Bring a jacket after dark.")
        self.assertTrue(deltas[-1]["final"])

        # Speech begins at the first sentence, not at the end of the answer.
        spoken = [item["text"] for item in adapter.client.methods("speech.speak")]
        self.assertTrue(any("forecast" in text for text in spoken))
        self.assertTrue(any("jacket" in text for text in spoken))
        # Nothing is said twice.
        self.assertEqual(sum(text.count("forecast") for text in spoken), 1)

        # The face moves to ANSWERING once, using the existing indicators.
        self.assertEqual(adapter.stages.count("ANSWERING"), 1)

        # And the completed turn is still rendered as one record.
        self.assertEqual(len(adapter.client.methods(TURN_APPEND_METHOD)), 1)

    def test_a_final_answer_that_diverges_replaces_what_was_streamed(self):
        adapter = _SpineAdapter(
            capabilities=(RESPONSE_DELTA_METHOD,),
            extra={"streaming": {"min_delta_chars": 0}},
        )

        async def scenario():
            await adapter.send_draft("jibo:robot", 3, "Here is a draft that gets thrown away.")
            await adapter.send("jibo:robot", "Actually the office is closed today.")

        asyncio.run(scenario())
        deltas = adapter.client.notified(RESPONSE_DELTA_METHOD)
        self.assertTrue(deltas[-1]["replace"])
        self.assertEqual(deltas[-1]["text"], "Actually the office is closed today.")

    def test_a_streaming_escalation_sees_the_same_text_the_screen_does(self):
        adapter = _SpineAdapter(
            capabilities=(RESPONSE_DELTA_METHOD,),
            extra={"streaming": {"min_delta_chars": 0}},
        )
        received = []

        async def scenario():
            task = asyncio.ensure_future(
                adapter.escalate_turn(
                    device_id="robot",
                    text="why is the sky blue",
                    turn_id="ph-77",
                    on_delta=received.append,
                )
            )
            assert await _until(lambda: adapter._turn_ids.get('robot') == 'ph-77')
            await adapter.send_draft("jibo:robot", 5, "Blue light scatters more.")
            await adapter.send("jibo:robot", "Blue light scatters more. That is Rayleigh scattering.")
            return await task

        result = asyncio.run(scenario())
        self.assertEqual(result["turn_id"], "ph-77")
        self.assertEqual(
            "".join(received), "Blue light scatters more. That is Rayleigh scattering."
        )

    def test_sequence_numbers_survive_a_stream_rebuilt_mid_turn(self):
        # The gateway can start a second draft within one answer; a restarted
        # counter would be rejected by the robot as out_of_order and the text
        # would be dropped on the floor.
        adapter = _SpineAdapter(
            capabilities=(RESPONSE_DELTA_METHOD,),
            extra={"streaming": {"min_delta_chars": 0}},
        )

        async def scenario():
            await adapter.send_draft("jibo:robot", 1, "First part of the answer.")
            await adapter.send_draft("jibo:robot", 2, "Second part of the answer.")
            await adapter.send("jibo:robot", "First part of the answer. Second part of the answer.")

        asyncio.run(scenario())
        seqs = [item["seq"] for item in adapter.client.notified(RESPONSE_DELTA_METHOD)]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))

    def test_a_robot_without_delta_support_still_gets_the_whole_answer(self):
        adapter = _SpineAdapter()
        result = asyncio.run(adapter.send("jibo:robot", "Short answer."))
        self.assertTrue(result.success)
        self.assertEqual(adapter.client.notified(RESPONSE_DELTA_METHOD), [])
        self.assertTrue(adapter.client.methods("speech.speak"))
        # The completed turn is the visual answer now. A legacy TextView here
        # would cover the native chat transcript as soon as it opened.
        self.assertEqual(adapter.client.methods("display.text"), [])


if __name__ == "__main__":
    unittest.main()
