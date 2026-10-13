"""Question delivery owns listening; model/history completion cannot delay it."""
import asyncio
import unittest
from unittest.mock import AsyncMock

from test_turn_spine import _SpineAdapter
from test_turn_cancellation import activate


class FollowUpTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, *, atomic=True, extra=None):
        caps = ['input.listen', 'speech.speak', 'response.delta', 'turn.append']
        if atomic:
            caps.append('speech.speak_and_listen')
        adapter = _SpineAdapter(capabilities=caps, extra=extra)
        adapter._loop = asyncio.get_running_loop()
        activate(adapter, 'question')
        return adapter

    async def speak(self, adapter, text='Which color do you prefer?'):
        return await asyncio.to_thread(adapter.speak_explicit, text, final=True, device_id='robot')

    async def test_explicit_question_pairs_the_final_audio_with_listening_before_history(self):
        adapter = self.adapter()
        entered, release = asyncio.Event(), asyncio.Event()
        async def record(*args, **kwargs):
            entered.set()
            await release.wait()
        adapter.record_turn = record
        speaking = asyncio.create_task(self.speak(adapter))
        await entered.wait()
        requests = adapter.client.methods('speech.speak_and_listen')
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0]['final'])
        self.assertEqual(requests[0]['listen_timeout_ms'], 9000)
        self.assertEqual(adapter.client.methods('input.listen'), [])
        release.set()
        self.assertTrue((await speaking).success)
        # A slow model tail cannot open another window after the first expires.
        await adapter.send('jibo:robot', 'Which color do you prefer?')
        self.assertEqual(len(adapter.client.methods('speech.speak_and_listen')), 1)
        self.assertEqual(adapter.client.methods('input.listen'), [])

    async def test_legacy_robot_opens_once_after_explicit_audio_without_waiting_for_model_final(self):
        adapter = self.adapter(atomic=False)
        self.assertTrue((await self.speak(adapter)).success)
        methods = [method for method, _ in adapter.client.requests]
        self.assertLess(methods.index('speech.speak'), methods.index('input.listen'))
        self.assertLess(methods.index('input.listen'), methods.index('turn.append'))
        await adapter.send('jibo:robot', 'Which color do you prefer?')
        self.assertEqual(len(adapter.client.methods('input.listen')), 1)

    async def test_repeated_user_replies_do_not_hit_an_implicit_four_question_limit(self):
        adapter = self.adapter()
        for index in range(6):
            activate(adapter, 'question-' + str(index))
            self.assertTrue((await self.speak(adapter)).success)
            await adapter.send('jibo:robot', 'Which color do you prefer?')
        self.assertEqual(len(adapter.client.methods('speech.speak_and_listen')), 6)

    async def test_statement_disabled_setting_and_explicit_limit_do_not_open_microphone(self):
        for extra, text in (({}, 'The answer is blue.'), ({'follow_up': {'enabled': False}}, 'Which color?'),
                            ({'follow_up': {'max_consecutive': 0}}, 'Which color?')):
            with self.subTest(extra=extra, text=text):
                adapter = self.adapter(extra=extra)
                self.assertTrue((await self.speak(adapter, text)).success)
                await adapter.send('jibo:robot', text)
                self.assertEqual(adapter.client.methods('speech.speak_and_listen'), [])
                self.assertEqual(adapter.client.methods('input.listen'), [])

    async def test_question_with_closing_formatting_keeps_its_bounded_reply_window(self):
        adapter = self.adapter(extra={'follow_up': {'timeout_ms': 12000}})
        self.assertEqual(adapter._follow_up_timeout('robot', 'Which color?”', 'question'), 12000)
        self.assertEqual(adapter._follow_up_timeout('robot', '**Which color?**', 'question'), 12000)
        self.assertIsNone(adapter._follow_up_timeout('robot', 'Which color?', 'stale'))

    async def test_model_finalization_cannot_bypass_an_explicit_positive_question_limit(self):
        adapter = self.adapter(extra={'follow_up': {'max_consecutive': 1}})
        self.assertTrue((await self.speak(adapter)).success)
        await adapter.send('jibo:robot', 'Which color?')
        activate(adapter, 'second-question')
        self.assertTrue((await self.speak(adapter)).success)
        await adapter.send('jibo:robot', 'Which color?')
        self.assertEqual(len(adapter.client.methods('speech.speak_and_listen')), 1)
        self.assertEqual(adapter.client.methods('input.listen'), [])

    async def test_replacement_during_legacy_microphone_open_keeps_its_own_turn_state(self):
        adapter = self.adapter(atomic=False)
        async def replace(*args, **kwargs):
            activate(adapter, 'replacement')
            adapter._processing_stages['robot'] = 'replacement-work'
        adapter._maybe_listen_again = replace
        self.assertFalse((await self.speak(adapter)).success)
        self.assertEqual(adapter._turn_id_for('robot'), 'replacement')
        self.assertEqual(adapter._processing_stages['robot'], 'replacement-work')
        self.assertEqual(adapter.client.methods('turn.append'), [])

    async def test_a_phoenix_owned_answer_does_not_listen_before_phoenix_speaks(self):
        adapter = self.adapter()
        await adapter._complete_turn('robot', 'Which color?', escalation=None, spoken=False)
        self.assertEqual(adapter.client.methods('input.listen'), [])

    async def test_stream_final_question_uses_the_same_atomic_audio_boundary(self):
        adapter = self.adapter()
        await adapter._speak_segment('robot', 'Which color do you prefer?', final=True, turn_id='question')
        self.assertEqual(len(adapter.client.methods('speech.speak_and_listen')), 1)
        await adapter._complete_turn('robot', 'Which color do you prefer?', escalation=None, spoken=True)
        self.assertEqual(adapter.client.methods('input.listen'), [])
