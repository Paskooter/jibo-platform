"""A spoken final answer stays delivered while the agent finishes bookkeeping."""

import asyncio
import importlib
import types
import unittest
from unittest.mock import AsyncMock, patch

from test_turn_cancellation import activate
from test_turn_spine import _SpineAdapter, NativeJiboAdapter, CHAT_STATUS_METHOD

native = importlib.import_module(NativeJiboAdapter.__module__)


class FinalDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self):
        adapter = _SpineAdapter(capabilities=['turn.append', 'response.delta'])
        adapter._loop = asyncio.get_running_loop()
        activate(adapter, 'answer')
        return adapter

    async def speak(self, adapter, text='The gateway is ready.', final=True):
        return await asyncio.to_thread(adapter.speak_explicit, text, final=final, device_id='robot')

    async def test_final_speech_renders_immediately_and_late_hooks_stay_quiet(self):
        adapter = self.adapter()
        queued = []
        adapter._submit_background = lambda coroutine: queued.append(coroutine) or True
        NativeJiboAdapter.update_processing_stage(adapter, 'CHECKING', device_id='robot')
        self.assertTrue((await self.speak(adapter)).success)
        rows = adapter.client.methods('turn.append')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['response'], 'The gateway is ready.')
        self.assertTrue(rows[0]['final'])
        # A status scheduled before the speech finished is stale too.
        await queued.pop()
        await adapter._set_processing_stage_async('robot', 'PROCESSING', turn_id='answer')
        await adapter.send_or_update_status('jibo:robot', 'work', 'Still working')
        NativeJiboAdapter.finish_processing(adapter, success=True, device_id='robot')
        for coroutine in queued:
            await coroutine
        self.assertEqual(adapter.client.methods(CHAT_STATUS_METHOD), [])

    async def test_slow_agent_tail_cannot_respeak_or_reopen_a_delivered_answer(self):
        adapter = self.adapter()
        self.assertTrue((await self.speak(adapter)).success)
        later = native.time.monotonic() + 120
        with patch.object(native, 'time', types.SimpleNamespace(monotonic=lambda: later)):
            await adapter.send_draft('jibo:robot', 9, 'The gateway is ready.')
            result = await adapter.send('jibo:robot', 'The gateway is ready.')
        self.assertTrue(result.success)
        self.assertEqual(len(adapter.client.methods('speech.speak')), 1)
        self.assertEqual(adapter.client.notified('response.delta'), [])
        self.assertEqual(adapter._turn_log['robot'][-1].response, 'The gateway is ready.')
        activate(adapter, 'new')
        await adapter.send('jibo:robot', 'A new answer.')
        self.assertEqual(len(adapter.client.methods('speech.speak')), 2)

    async def test_failed_native_speech_is_not_reported_or_marked_as_delivered(self):
        adapter = self.adapter()
        request = adapter.client.request

        async def fail(method, params=None, timeout=None):
            if method == 'speech.speak':
                return {'spoken': False, 'stopped': True}
            return await request(method, params, timeout)

        adapter.client.request = fail
        result = await self.speak(adapter)
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)
        self.assertFalse(adapter._tool_answer_pending('robot'))
        self.assertEqual(adapter.client.methods('turn.append'), [])

    async def test_new_question_preserves_the_answer_already_heard(self):
        adapter = self.adapter()
        self.assertTrue((await self.speak(adapter)).success)
        adapter.gateway_runner = types.SimpleNamespace(_interrupt_and_clear_session=AsyncMock())
        await adapter._cancel_current_turn('robot', stopped_locally=True)
        old = adapter._turn_log['robot'][-1]
        self.assertEqual(old.status, 'ok')
        self.assertEqual(old.response, 'The gateway is ready.')
        self.assertEqual(old.speech_owner, 'hermes')
        adapter.gateway_runner._interrupt_and_clear_session.assert_awaited_once()
        activate(adapter, 'new')
        await adapter._set_processing_stage_async('robot', 'THINKING', turn_id='new')
        self.assertEqual(adapter.client.methods(CHAT_STATUS_METHOD)[-1]['state'], 'thinking')

    async def test_progress_speech_does_not_suppress_the_final_answer(self):
        adapter = self.adapter()
        await self.speak(adapter, 'Checking the connection.', final=False)
        await adapter._set_processing_stage_async('robot', 'CHECKING', turn_id='answer')
        result = await adapter.send('jibo:robot', 'The gateway is ready.')
        self.assertTrue(result.success)
        self.assertEqual(len(adapter.client.methods('speech.speak')), 2)
        self.assertEqual(adapter.client.methods(CHAT_STATUS_METHOD)[-1]['state'], 'thinking')

    async def test_two_queued_final_speech_tools_cannot_both_deliver(self):
        adapter = self.adapter()
        entered, release = asyncio.Event(), asyncio.Event()
        request = adapter.client.request

        async def transport(method, params=None, timeout=None):
            if method == 'speech.speak':
                entered.set()
                await release.wait()
            return await request(method, params, timeout)

        adapter.client.request = transport
        first = asyncio.create_task(self.speak(adapter))
        await entered.wait()
        second = asyncio.create_task(self.speak(adapter))
        await asyncio.sleep(0.01)
        release.set()
        results = await asyncio.gather(first, second)
        self.assertEqual([r.success for r in results], [True, False])
        self.assertFalse(results[1].retryable)
        self.assertEqual(len(adapter.client.methods('speech.speak')), 1)

    async def test_display_failure_does_not_invite_repeating_audible_speech(self):
        adapter = self.adapter()
        adapter.record_turn = AsyncMock(side_effect=OSError('storage unavailable'))
        with self.assertLogs(native.logger, level='WARNING'):
            result = await self.speak(adapter)
        self.assertTrue(result.success)
        self.assertTrue(adapter._tool_answer_pending('robot'))

    async def test_a_queued_stop_remains_scoped_after_the_next_question_starts(self):
        adapter = self.adapter()
        entered, release = asyncio.Event(), asyncio.Event()
        captured = []

        async def transport(method, params=None, timeout=None):
            entered.set()
            await release.wait()
            captured.append((method, params))
            # Be's stop handler compares turn_id with its current speech.
            return {'stopped': params.get('turn_id') in (None, '', 'new')}

        adapter.client.request = transport
        task = asyncio.create_task(adapter._request_native('robot', 'speech.stop'))
        await entered.wait()
        activate(adapter, 'new')
        release.set()
        result = await task
        self.assertFalse(result['stopped'])
        self.assertEqual(captured[0][1]['owner_turn_id'], 'answer')
