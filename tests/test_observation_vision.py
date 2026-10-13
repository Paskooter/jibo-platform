"""Exercise camera pixels through Hermes' real encoder and model-result gate."""
import base64
import importlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from PIL import Image
from run_agent import AIAgent
from tools.registry import registry
from tools import vision_tools
from test_turn_spine import _SpineAdapter, NativeJiboAdapter

package = NativeJiboAdapter.__module__.rsplit('.', 1)[0]
observation = importlib.import_module(package + '.observation')
native_tools = importlib.import_module(package + '.native_tools')


class ObservationVisionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'camera.jpg'
        Image.new('RGB', (72, 48), 'red').save(self.path, format='JPEG')
        self.payload = {'image': {'local_path': str(self.path), 'shown': True, 'captured_at': 123},
                        'environment': {'presence': {'present': None}}}

    def native_result(self):
        with patch.object(vision_tools, '_should_use_native_vision_fast_path', return_value=True):
            return observation.observation_tool_result(self.payload)

    def test_same_saved_photo_reaches_the_registry_and_active_model_with_sensor_metadata(self):
        result = self.native_result()
        self.assertIs(registry._normalize_handler_result('jibo_observe', result), result)
        model = types.SimpleNamespace(
            _content_has_image_parts=lambda parts: True,
            _model_supports_vision=lambda: True,
            _provider_supports_vision_tool_messages=lambda: True)
        parts = AIAgent._tool_result_content_for_active_model(model, 'jibo_observe', result)
        payload = json.loads(parts[0]['text'])['result']
        self.assertTrue(payload['image']['vision_attached'])
        self.assertEqual(payload['image']['captured_at'], 123)
        self.assertIsNone(payload['environment']['presence']['present'])
        prefix, encoded = parts[1]['image_url']['url'].split(',', 1)
        self.assertEqual(prefix, 'data:image/jpeg;base64')
        self.assertEqual(base64.b64decode(encoded), self.path.read_bytes())
        self.assertNotIn('vision_attached', self.payload['image'])

    def test_active_model_that_strips_images_gets_truthful_local_photo_fallback(self):
        result = self.native_result()
        for vision, provider_images, learned_rejection in ((False, True, False), (True, False, False), (True, True, True)):
            with self.subTest(vision=vision, provider_images=provider_images, learned_rejection=learned_rejection):
                model = types.SimpleNamespace(
                    provider='test', model='test',
                    _content_has_image_parts=lambda parts: True,
                    _model_supports_vision=lambda: vision,
                    _provider_supports_vision_tool_messages=lambda: provider_images,
                    _no_list_tool_content_models={('test', 'test')} if learned_rejection else set())
                content = AIAgent._tool_result_content_for_active_model(model, 'jibo_observe', result)
                payload = json.loads(content)['result']
                self.assertFalse(payload['image']['vision_attached'])
                self.assertEqual(payload['image']['local_path'], str(self.path))
                self.assertIn('vision_analyze', payload['next_step'])
                self.assertNotIn('base64', content)

    def test_configured_nonvisual_model_or_encoding_failure_preserves_saved_photo(self):
        for native in (False, True):
            with self.subTest(native=native), patch.object(vision_tools, '_should_use_native_vision_fast_path', return_value=native), \
                    patch.object(vision_tools, '_resize_image_for_vision', side_effect=OSError('unreadable')) as encode:
                result = json.loads(observation.observation_tool_result(self.payload))
                self.assertTrue(result['success'])
                self.assertFalse(result['result']['image']['vision_attached'])
                self.assertTrue(result['result']['image']['shown'])
                self.assertEqual(encode.call_count, 1 if native else 0)

    def test_sensor_only_result_does_not_encode_or_claim_visual_access(self):
        with patch.object(vision_tools, '_should_use_native_vision_fast_path') as gate:
            result = json.loads(observation.observation_tool_result({'environment': {'pose': {'fresh': False}}}))
        gate.assert_not_called()
        self.assertEqual(result, {'success': True, 'result': {'environment': {'pose': {'fresh': False}}}})

    def test_stop_during_encoding_discards_the_tool_photo(self):
        adapter = _SpineAdapter()
        adapter._turn_ids['robot'] = 'old'
        adapter.observe_environment = lambda *args, **kwargs: types.SimpleNamespace(success=True, raw_response=self.payload)
        def encode(*args, **kwargs):
            adapter._turn_ids['robot'] = 'replacement'
            return 'data:image/jpeg;base64,old'
        with patch.object(native_tools, 'get_active_adapter', return_value=adapter), \
                patch.object(vision_tools, '_should_use_native_vision_fast_path', return_value=True), \
                patch.object(vision_tools, '_resize_image_for_vision', side_effect=encode):
            result = json.loads(native_tools.jibo_observe_tool({'direction': 'right'}))
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'TURN_CANCELLED')
        self.assertNotIn('result', result)
