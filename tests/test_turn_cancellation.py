"""Turn ownership must survive waits, worker queues, and a newer question."""

import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch

from test_turn_spine import _SpineAdapter, NativeJiboAdapter, CHAT_STATUS_METHOD


def activate(adapter, turn_id):
    adapter._turn_ids['robot'] = turn_id
    record = {
        'turn_id': turn_id, 'text': 'Question ' + turn_id, 'origin': 'bridge',
        'session_key': 'session-robot', 'source': types.SimpleNamespace(chat_id='jibo:robot'),
    }
    adapter._active_turns['robot'] = record
    return record


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_robot_stop_is_not_echoed_back_over_new_audio(self):
        adapter = _SpineAdapter()
        activate(adapter, 'old')
        adapter.gateway_runner = types.SimpleNamespace(_interrupt_and_clear_session=AsyncMock())
        await adapter._on_native_event('robot', {
            'method': 'input.interrupt',
            'params': {'turn_id': 'old', 'stopped_locally': True},
        })
        self.assertEqual(adapter.client.methods('speech.stop'), [])
        self.assertEqual(adapter.client.methods('display.clear'), [])
        adapter.gateway_runner._interrupt_and_clear_session.assert_awaited_once()
        self.assertEqual(adapter._turn_log['robot'][-1].turn_id, 'old')
        self.assertEqual(adapter._turn_log['robot'][-1].status, 'cancelled')

    async def test_old_interrupt_cannot_cancel_a_newer_question(self):
        adapter = _SpineAdapter()
        new = activate(adapter, 'new')
        adapter.gateway_runner = types.SimpleNamespace(_interrupt_and_clear_session=AsyncMock())
        await adapter._on_native_event('robot', {
            'method': 'input.interrupt', 'params': {'turn_id': 'old', 'stopped_locally': True},
        })
        self.assertIs(adapter._active_turns['robot'], new)
        self.assertEqual(adapter._turn_ids['robot'], 'new')
        adapter.gateway_runner._interrupt_and_clear_session.assert_not_awaited()

    async def test_completion_waiting_on_storage_cannot_clear_or_listen_over_new_turn(self):
        adapter = _SpineAdapter()
        activate(adapter, 'old')
        started, finish = asyncio.Event(), asyncio.Event()

        async def record(*args, **kwargs):
            started.set()
            await finish.wait()

        adapter.record_turn = record
        adapter._maybe_listen_again = AsyncMock()
        task = asyncio.create_task(adapter._complete_turn('robot', 'Another question?', escalation=None, spoken=True))
        await started.wait()
        adapter._maybe_listen_again.assert_awaited_once_with('robot', 'Another question?', turn_id='old')
        activate(adapter, 'new')
        finish.set()
        await task
        self.assertEqual(adapter._turn_ids['robot'], 'new')
        adapter._maybe_listen_again.assert_awaited_once_with('robot', 'Another question?', turn_id='old')

    async def test_queued_speech_keeps_its_original_turn(self):
        adapter = _SpineAdapter()
        activate(adapter, 'old')
        lock = adapter._speak_locks.setdefault('robot', asyncio.Lock())
        await lock.acquire()
        task = asyncio.create_task(adapter._speak_on_loop('robot', 'Old answer.', final=True))
        await asyncio.sleep(0)
        activate(adapter, 'new')
        lock.release()
        result = await task
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)
        self.assertEqual(adapter.client.methods('speech.speak'), [])

    async def test_image_fetch_cannot_attach_its_result_to_a_newer_question(self):
        adapter = _SpineAdapter(capabilities=['turn.append'])
        activate(adapter, 'old')
        started, finish = asyncio.Event(), asyncio.Event()
        adapter._is_own_media_url = lambda _url: False

        async def worker(fn, *args):
            started.set()
            await finish.wait()
            return '/tmp/old-image.jpg'

        adapter._present_local_image_on_loop = AsyncMock()
        module = __import__(NativeJiboAdapter.__module__, fromlist=['asyncio'])
        with patch.object(module.asyncio, 'to_thread', worker):
            task = asyncio.create_task(adapter._present_remote_image_on_loop(
                'robot', 'https://example.com/image.jpg', name='image', caption=None))
            await started.wait()
            activate(adapter, 'new')
            finish.set()
            with self.assertRaisesRegex(Exception, 'cancelled|superseded'):
                await task
        adapter._present_local_image_on_loop.assert_not_awaited()

    async def test_processing_status_captures_the_turn_when_scheduled(self):
        adapter = _SpineAdapter()
        activate(adapter, 'old')
        queued = []
        adapter._submit_background = lambda coroutine: queued.append(coroutine) or True
        NativeJiboAdapter.update_processing_stage(adapter, 'ANSWERING', device_id='robot')
        activate(adapter, 'new')
        await queued.pop()
        self.assertEqual(adapter.client.methods(CHAT_STATUS_METHOD), [])

    async def test_cancel_phrase_preserves_original_question_in_history(self):
        adapter = _SpineAdapter()
        activate(adapter, 'old')
        adapter.gateway_runner = types.SimpleNamespace(_interrupt_and_clear_session=AsyncMock())
        await NativeJiboAdapter._dispatch_utterance(adapter, 'robot', 'stop', {}, turn_id='stop-utterance')
        record = adapter._turn_log['robot'][-1]
        self.assertEqual(record.turn_id, 'old')
        self.assertEqual(record.transcript, 'Question old')

    async def test_new_dispatch_waits_for_old_gateway_cancellation(self):
        adapter = _SpineAdapter()
        activate(adapter, 'old')
        started, finish = asyncio.Event(), asyncio.Event()
        order = []

        async def cancel(*args, **kwargs):
            order.append('cancel-start')
            started.set()
            await finish.wait()
            order.append('cancel-done')

        adapter.gateway_runner = types.SimpleNamespace(_interrupt_and_clear_session=cancel)
        adapter._begin_utterance = lambda device, text, origin, turn: activate(adapter, turn)

        async def handle(event):
            order.append('new-dispatch')

        adapter.handle_message = handle
        old = asyncio.create_task(adapter._cancel_current_turn('robot', expected_turn_id='old', stopped_locally=True))
        await started.wait()
        new = asyncio.create_task(NativeJiboAdapter._dispatch_utterance(adapter, 'robot', 'New question', {}, turn_id='new'))
        await asyncio.sleep(0)
        self.assertNotIn('new-dispatch', order)
        finish.set()
        await asyncio.wait_for(asyncio.gather(old, new), timeout=1)
        self.assertEqual(order, ['cancel-start', 'cancel-done', 'new-dispatch'])
        self.assertEqual(adapter._turn_ids['robot'], 'new')

    async def test_worker_context_cannot_borrow_a_new_turn_for_late_delivery(self):
        adapter = _SpineAdapter(capabilities=['response.delta'])
        adapter._begin_utterance = lambda device, text, origin, turn: activate(adapter, turn)
        captured = []

        async def handle(event):
            # asyncio.to_thread is the same context propagation used by the
            # installed gateway's agent worker wrapper.
            import contextvars
            captured.append(await asyncio.to_thread(contextvars.copy_context))

        adapter.handle_message = handle
        await NativeJiboAdapter._dispatch_utterance(adapter, 'robot', 'Old question', {}, turn_id='old')
        activate(adapter, 'new')
        stale = captured[0].run(asyncio.create_task, adapter.send('jibo:robot', 'Late old answer'))
        result = await stale
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)
        self.assertEqual(adapter.client.methods('speech.speak'), [])
        self.assertEqual(adapter._turn_ids['robot'], 'new')

    async def test_cancelling_old_turn_does_not_resolve_a_new_pending_escalation(self):
        adapter = _SpineAdapter()
        activate(adapter, 'old')
        escalation = types.SimpleNamespace(turn_id='new', resolve=AsyncMock())
        adapter._pending_escalations['robot'] = escalation
        adapter.gateway_runner = types.SimpleNamespace(_interrupt_and_clear_session=AsyncMock())
        await adapter._cancel_current_turn('robot', expected_turn_id='old', stopped_locally=True)
        self.assertIs(adapter._pending_escalations['robot'], escalation)
        escalation.resolve.assert_not_called()

    async def test_runner_cancel_also_releases_the_base_adapter_processing_guard(self):
        adapter = _SpineAdapter()
        activate(adapter, 'old')
        adapter.platform = types.SimpleNamespace(value='jibo')
        worker = asyncio.create_task(asyncio.sleep(100))
        adapter._session_tasks = {'session-robot': worker}
        adapter._active_sessions = {'session-robot': asyncio.Event()}
        adapter._pending_messages = {}
        adapter._expected_cancelled_tasks = set()
        adapter._discard_text_debounce = lambda _key: None
        adapter.gateway_runner = types.SimpleNamespace(_interrupt_and_clear_session=AsyncMock())
        try:
            await adapter._cancel_current_turn('robot', expected_turn_id='old', stopped_locally=True)
            self.assertTrue(worker.cancelled())
            self.assertNotIn('session-robot', adapter._active_sessions)
            self.assertNotIn('session-robot', adapter._session_tasks)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
