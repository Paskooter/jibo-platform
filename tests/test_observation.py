import importlib
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from PIL import Image
from test_turn_spine import _SpineAdapter, NativeJiboAdapter

package = NativeJiboAdapter.__module__.rsplit('.', 1)[0]
observation = importlib.import_module(package + '.observation')
native_tools = importlib.import_module(package + '.native_tools')


class ObservationTests(unittest.TestCase):
    def test_native_photo_url_uses_configured_robot_and_only_the_returned_id(self):
        result = observation.camera_url('robot-one.example.test', {'id': 'photo-1', 'url': 'http://127.0.0.1:8486/private'})
        self.assertEqual(result, 'http://robot-one.example.test:8486/media/photo?id=photo-1')
        with self.assertRaises(ValueError):
            observation.camera_url('robot-one.example.test', {'id': '../private'})
        with self.assertRaises(ValueError):
            observation.camera_url('user:secret@robot-one.example.test', {'id': 'photo'})

    def test_camera_artifact_is_a_valid_bounded_host_image_and_retention_is_limited(self):
        source = io.BytesIO()
        Image.new('RGB', (80, 40), 'red').save(source, format='JPEG')
        class Opener:
            def open(self, *args, **kwargs):
                return io.BytesIO(source.getvalue())
        with tempfile.TemporaryDirectory() as directory, patch.object(observation, 'build_opener', return_value=Opener()), patch.object(observation, 'MAX_PHOTOS', 2):
            root = Path(directory)
            for _ in range(4):
                result = observation.save_camera_image('http://robot/photo', root)
                # Simulate clock skew/coarse timestamps on existing captures.
                for saved in root.iterdir():
                    os.utime(saved, (2000000000, 2000000000))
            self.assertEqual(len(list(root.iterdir())), 2)
            with Image.open(result['local_path']) as saved:
                self.assertEqual(saved.format, 'JPEG')
                self.assertEqual(saved.size, (80, 40))

    def test_invalid_photo_bytes_leave_no_partial_artifact(self):
        opener = types.SimpleNamespace(open=lambda *args, **kwargs: io.BytesIO(b'not an image'))
        with tempfile.TemporaryDirectory() as directory, patch.object(observation, 'build_opener', return_value=opener):
            with self.assertRaises(Exception):
                observation.save_camera_image('http://robot/photo', Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_capture_returns_same_image_for_vision_and_display_without_loopback_url(self):
        adapter = _SpineAdapter()
        adapter.native_call = lambda *args, **kwargs: types.SimpleNamespace(success=True, raw_response={
            'image': {'id': 'photo-1', 'url': 'http://127.0.0.1:8486/media/photo?id=photo-1', 'captured_at': 123},
            'environment': {'presence': {'present': None}},
        })
        shown = []
        adapter.display_local_image = lambda path, **kwargs: shown.append(path) or types.SimpleNamespace(success=True)
        with tempfile.TemporaryDirectory() as directory:
            adapter._media_cache_directory = lambda: Path(directory)
            with patch.object(observation, 'save_camera_image', return_value={'local_path': '/tmp/camera.jpg', 'url': 'http://robot:8486/media/photo?id=photo-1'}):
                result = adapter.observe_environment({'direction': 'left'}, device_id='robot')
        self.assertTrue(result.success, result.error)
        self.assertEqual(shown, ['/tmp/camera.jpg'])
        self.assertEqual(result.raw_response['image']['local_path'], '/tmp/camera.jpg')
        self.assertEqual(result.raw_response['image']['captured_at'], 123)
        self.assertTrue(result.raw_response['image']['shown'])

    def test_sensor_only_request_never_fetches_or_shows_a_photo(self):
        adapter = _SpineAdapter()
        methods = []
        adapter.native_call = lambda method, *args, **kwargs: methods.append(method) or types.SimpleNamespace(success=True, raw_response={})
        with patch.object(observation, 'save_camera_image') as save:
            result = adapter.observe_environment({}, capture=False, device_id='robot')
        self.assertTrue(result.success)
        self.assertEqual(methods, ['environment.get'])
        save.assert_not_called()

    def test_cancelled_turn_during_download_never_presents_late_image(self):
        adapter = _SpineAdapter()
        checks = []
        def check(*args):
            checks.append(True)
            if len(checks) == 3:
                raise RuntimeError('turn cancelled')
        adapter._assert_turn_current = check
        adapter.native_call = lambda *args, **kwargs: types.SimpleNamespace(success=True, raw_response={'image': {'id': 'photo-1'}})
        with tempfile.TemporaryDirectory() as directory:
            adapter._media_cache_directory = lambda: Path(directory)
            with patch.object(observation, 'save_camera_image', return_value={'local_path': '/tmp/camera.jpg'}), patch.object(adapter, 'display_local_image') as display:
                result = adapter.observe_environment({}, device_id='robot')
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)
        display.assert_not_called()

    def test_tool_rejects_ambiguous_or_non_boolean_capture_options(self):
        with patch.object(native_tools, 'get_active_adapter', return_value=_SpineAdapter()):
            for args in ({'capture': 'false'}, {'capture': False, 'direction': 'left'},
                         {'direction': 'left', 'position': {'x': 1, 'y': 0, 'z': 0}}):
                self.assertFalse(json.loads(native_tools.jibo_observe_tool(args))['success'])
