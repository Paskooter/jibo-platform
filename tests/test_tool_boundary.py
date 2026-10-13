"""Exercise Hermes' real draft consumer across a model -> tool -> model task."""
import unittest

from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from test_turn_spine import _SpineAdapter
from test_turn_cancellation import activate


class ToolBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_boundary_keeps_turn_open_until_the_actual_final_answer(self):
        adapter = _SpineAdapter(capabilities=['response.delta', 'turn.append', 'environment.observe'])
        activate(adapter, 'looking')
        consumer = GatewayStreamConsumer(adapter, 'jibo:robot', StreamConsumerConfig(cursor=''))
        consumer._use_draft_streaming = True
        consumer._draft_id = 91
        self.assertTrue(await consumer._send_or_edit('I will check the camera.', finalize=False))
        self.assertTrue(await consumer._send_or_edit('I will check the camera.', finalize=True, is_turn_final=False))
        self.assertEqual(adapter._turn_ids['robot'], 'looking')
        self.assertEqual(adapter.client.methods('turn.append'), [])
        self.assertFalse(any(call.get('final') for call in adapter.client.methods('response.delta')))
        self.assertEqual(adapter._streams['robot'].draft_id, 91)
        # An actual native tool can still run after the consumer has flushed
        # the model's preamble at the tool boundary.
        await adapter._request_native('robot', 'environment.observe', {'direction': 'left'})
        self.assertEqual(adapter.client.methods('environment.observe')[0]['owner_turn_id'], 'looking')
        final = 'I can see a desk and a lamp.'
        self.assertTrue(await consumer._send_or_edit(final, finalize=True, is_turn_final=True))
        self.assertNotIn('robot', adapter._turn_ids)
        self.assertEqual(adapter.client.methods('turn.append')[-1]['response'], final)
