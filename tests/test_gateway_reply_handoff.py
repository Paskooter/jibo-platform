"""Native replies must not queue under a completed answer's gateway task.

Run the installed BasePlatformAdapter ingress, background delivery and cleanup
with the real native adapter. Only the model, robot transport and slow cleanup
are fixtures. Source-only native turn tests do not cover this ownership gap.
"""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, ProcessingOutcome

from test_turn_spine import _SpineAdapter, NativeJiboAdapter


native = sys.modules[NativeJiboAdapter.__module__]


class _GatewayAdapter(_SpineAdapter):
    # Use real ingress, source identity and delivery, including unowned sends.
    # The simpler spine fixture simulates worker context for direct unit calls.
    _dispatch_utterance = NativeJiboAdapter._dispatch_utterance
    _turn_source = NativeJiboAdapter._turn_source
    send = NativeJiboAdapter.send


class GatewayReplyHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='jibo-handoff-test-')
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {'HERMES_HOME': temporary.name})
        environment.start()
        self.addCleanup(environment.stop)
        adapter = _GatewayAdapter(capabilities=['speech.speak','input.listen','speech.speak_and_listen','turn.append'])
        with patch('gateway.platform_registry.platform_registry.is_registered',return_value=True):
            platform = Platform('jibo')
        BasePlatformAdapter.__init__(adapter, PlatformConfig(extra={},typing_indicator=False), platform)
        adapter._loop = asyncio.get_running_loop()
        self.adapter = adapter

    async def asyncTearDown(self):
        for key in list(self.adapter._session_tasks):
            await self.adapter.cancel_session_processing(key)
        await asyncio.gather(*list(self.adapter._background_tasks),return_exceptions=True)

    def accepted_event(self, text, turn_id):
        record = self.adapter._begin_utterance('robot',text,native.ORIGIN_BRIDGE,turn_id)
        return record['session_key'], MessageEvent(
            text=text, message_type=MessageType.TEXT, source=record['source'],
            message_id='message-'+turn_id,
            raw_message={'device_id':'robot','native_turn_id':turn_id,'native':{}},
        )

    async def speak_question(self, text):
        result = await self.adapter._speak_explicit_on_loop('robot',text,
            kind='answer',style='neutral',final=True,turn_id=self.adapter._turn_id_for('robot'))
        self.assertTrue(result.success,result)
        return text

    def assert_question_once(self, turn_id, text):
        spoken = self.adapter.client.methods('speech.speak_and_listen')
        self.assertEqual([(p['turn_id'],p['text']) for p in spoken],[(turn_id,text)])
        rows = [row for row in self.adapter._turn_log['robot'] if row.response==text]
        self.assertEqual([row.turn_id for row in rows],[turn_id])
        rendered = self.adapter.client.methods('turn.append')
        self.assertTrue(rendered)
        self.assertEqual({p['turn_id'] for p in rendered if p['response']==text},{turn_id})

    async def test_reply_during_completed_answer_cleanup_starts_its_own_task_and_delivers(self):
        adapter = self.adapter
        cleanup_entered, finish_cleanup, answer_delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        model_inputs = []

        async def model(event):
            model_inputs.append(event.text)
            return 'Which color do you prefer?' if event.text=='Ask me about a color' else 'Orange mixes red and yellow.'

        async def cleanup(event, outcome):
            if event.text=='Ask me about a color' and outcome is ProcessingOutcome.SUCCESS:
                cleanup_entered.set()
                await finish_cleanup.wait()
            if event.text=='orange' and outcome is ProcessingOutcome.SUCCESS:
                answer_delivered.set()

        async def busy_ack(event, session_key):
            # The runner uses the normal send path for this notice. Entering
            # this path consumes the reply's native turn before its answer.
            await adapter._send_with_retry(event.source.chat_id,
                "⚡ Interrupting current task. I'll respond to your message shortly.")
            return False

        adapter.set_message_handler(model)
        adapter.on_processing_complete = cleanup
        adapter.set_busy_session_handler(AsyncMock(side_effect=busy_ack))
        unrelated = asyncio.create_task(asyncio.Event().wait())
        adapter._session_devices['other-robot-session'] = 'another-robot'
        adapter._session_tasks['other-robot-session'] = unrelated
        adapter._active_sessions['other-robot-session'] = asyncio.Event()
        try:
            await NativeJiboAdapter._dispatch_utterance(adapter,'robot','Ask me about a color',{},turn_id='question')
            await asyncio.wait_for(cleanup_entered.wait(),2)
            self.assertNotIn('robot',adapter._active_turns)
            key = next(key for key,device in adapter._session_devices.items() if device=='robot')
            previous_task = adapter._session_tasks[key]
            self.assertFalse(previous_task.done())
            await NativeJiboAdapter._dispatch_utterance(adapter,'robot','orange',{
                'method':'input.transcript','params':{'reply_to_turn_id':'question'}},turn_id='reply')
            await asyncio.wait_for(answer_delivered.wait(),2)
            self.assertEqual(model_inputs,['Ask me about a color','orange'])
            adapter._busy_session_handler.assert_not_awaited()
            self.assertEqual([p['text'] for p in adapter.client.methods('speech.speak')],
                ['Orange mixes red and yellow.'])
            self.assertTrue(previous_task.done())
            self.assertFalse(unrelated.done())
            self.assertEqual(adapter._pending_messages,{})
            reply = next(row for row in adapter._turn_log['robot'] if row.turn_id=='reply')
            self.assertEqual(reply.transcript,'orange')
            self.assertEqual(reply.response,'Orange mixes red and yellow.')
        finally:
            finish_cleanup.set()
            for key in list(adapter._session_tasks):
                await adapter.cancel_session_processing(key)
            await asyncio.gather(*list(adapter._background_tasks),return_exceptions=True)
            unrelated.cancel()
            await asyncio.gather(unrelated,return_exceptions=True)

    async def test_recovery_send_cannot_consume_an_active_question_or_duplicate_its_final(self):
        adapter = self.adapter
        model_started, release_model, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def model(event):
            model_started.set()
            await release_model.wait()
            return await self.speak_question("What's your favorite color?")

        async def cleanup(event, outcome):
            if outcome is ProcessingOutcome.SUCCESS:
                completed.set()

        adapter.set_message_handler(model)
        adapter.on_processing_complete = cleanup
        await adapter._dispatch_utterance('robot','Ask me my favorite color',{},turn_id='question')
        await asyncio.wait_for(model_started.wait(),2)
        owner = adapter._active_turns['robot']
        # Same call path as the real gateway's boot delivery-ledger recovery:
        # adapter.send(), outside an accepted native input's context.
        self.assertIsNone(native._NATIVE_TURN.get())
        result = await adapter.send('jibo:robot','Recovered reply: Orange is a warm color.')
        self.assertFalse(result.success)
        self.assertTrue(result.retryable)
        self.assertEqual(adapter._turn_ids['robot'],'question')
        self.assertIs(adapter._active_turns['robot'],owner)
        self.assertEqual(adapter.client.requests,[])
        release_model.set()
        await asyncio.wait_for(completed.wait(),2)
        self.assert_question_once('question',"What's your favorite color?")
        self.assertNotIn('robot',adapter._active_turns)

    async def test_queued_reply_rebinds_its_event_instead_of_inheriting_previous_worker(self):
        adapter = self.adapter
        previous_cleanup, release_cleanup, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def model(event):
            if event.text=='first':
                return 'The previous answer.'
            return await self.speak_question("What's your favorite color?")

        async def cleanup(event, outcome):
            if outcome is ProcessingOutcome.SUCCESS:
                if event.text=='first':
                    previous_cleanup.set()
                    await release_cleanup.wait()
                else:
                    completed.set()

        adapter.set_message_handler(model)
        adapter.on_processing_complete = cleanup
        await adapter._dispatch_utterance('robot','first',{},turn_id='previous')
        await asyncio.wait_for(previous_cleanup.wait(),2)
        key, queued = self.accepted_event('Ask me my favorite color','queued-question')
        adapter._pending_messages[key] = queued
        release_cleanup.set()
        # Exercise the installed base adapter's actual queue-drain task. It
        # is spawned inside the previous worker's ContextVar scope.
        await asyncio.wait_for(completed.wait(),2)
        self.assert_question_once('queued-question',"What's your favorite color?")
        self.assertEqual(adapter._pending_messages,{})

    async def test_worker_started_without_context_uses_accepted_event_for_tool_and_final(self):
        adapter = self.adapter
        completed = asyncio.Event()

        async def model(event):
            return await self.speak_question("What's your favorite color?")

        async def cleanup(event, outcome):
            if outcome is ProcessingOutcome.SUCCESS:
                completed.set()

        adapter.set_message_handler(model)
        adapter.on_processing_complete = cleanup
        key, event = self.accepted_event('Ask me my favorite color','accepted-question')
        self.assertIsNone(native._NATIVE_TURN.get())
        self.assertTrue(adapter._start_session_processing(event,key))
        await asyncio.wait_for(completed.wait(),2)
        self.assert_question_once('accepted-question',"What's your favorite color?")

    async def test_idle_recovery_owns_a_separate_nonempty_turn(self):
        result = await self.adapter.send('jibo:robot','Recovered reply: Orange is a warm color.')
        self.assertTrue(result.success,result)
        spoken = self.adapter.client.methods('speech.speak')
        self.assertEqual(len(spoken),1)
        self.assertTrue(spoken[0]['turn_id'])
        self.assertEqual([row.turn_id for row in self.adapter._turn_log['robot']],[spoken[0]['turn_id']])
        self.assertNotIn('robot',self.adapter._active_turns)
        self.assertNotIn('robot',self.adapter._turn_ids)

    async def test_new_input_supersedes_recovery_without_cancelling_shared_boot_sender(self):
        adapter = self.adapter
        speaking, release_speech, answer_delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        request = adapter.client.request

        async def delayed_request(method,params=None,timeout=None):
            result = await request(method,params,timeout)
            if method=='speech.speak' and params['text'].startswith('Recovered reply'):
                speaking.set()
                await release_speech.wait()
            return result

        async def model(event):
            return 'Orange mixes red and yellow.'

        async def cleanup(event, outcome):
            if outcome is ProcessingOutcome.SUCCESS:
                answer_delivered.set()

        sender_continued = []

        async def boot_sender():
            result = await adapter.send('jibo:robot','Recovered reply: A previous answer.')
            sender_continued.append(True)  # Other platforms can still recover.
            return result

        adapter.client.request = delayed_request
        adapter.set_message_handler(model)
        adapter.on_processing_complete = cleanup
        sender = asyncio.create_task(boot_sender())
        try:
            await asyncio.wait_for(speaking.wait(),2)
            recovery_id = adapter._turn_ids['robot']
            await adapter._dispatch_utterance('robot','orange',{},turn_id='new-reply')
            release_speech.set()
            result = await asyncio.wait_for(sender,2)
            await asyncio.wait_for(answer_delivered.wait(),2)
            self.assertFalse(result.success)
            self.assertEqual(sender_continued,[True])
            self.assertNotEqual(recovery_id,'new-reply')
            reply = next(row for row in adapter._turn_log['robot'] if row.turn_id=='new-reply')
            self.assertEqual(reply.transcript,'orange')
            self.assertEqual(reply.response,'Orange mixes red and yellow.')
        finally:
            release_speech.set()
            await asyncio.gather(sender,return_exceptions=True)
