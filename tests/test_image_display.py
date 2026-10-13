import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch


_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "jibo_platform_image_tests"
_PACKAGE_SPEC = importlib.util.spec_from_file_location(
    _PACKAGE_NAME,
    _PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(_PLUGIN_DIR)],
)
_PACKAGE = importlib.util.module_from_spec(_PACKAGE_SPEC)
sys.modules[_PACKAGE_NAME] = _PACKAGE
assert _PACKAGE_SPEC.loader is not None
_PACKAGE_SPEC.loader.exec_module(_PACKAGE)

from jibo_platform_image_tests.rom import RomSession  # noqa: E402
from jibo_platform_image_tests.image_tool import (  # noqa: E402
    JIBO_DISPLAY_IMAGE_SCHEMA,
    JIBO_SHOW_EYE_SCHEMA,
    jibo_display_image_tool,
    jibo_show_eye_tool,
)
from jibo_platform_image_tests.status import on_post_llm_call  # noqa: E402
from jibo_platform_image_tests.media import JIBO_SCREEN_SIZE, letterbox_image  # noqa: E402


class ImageDisplayTests(unittest.IsolatedAsyncioTestCase):
    def test_letterbox_image_has_fixed_jibo_canvas(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / "source.jpg"
            output_dir = pathlib.Path(tmp) / "out"
            Image.new("RGB", (640, 352), (200, 40, 20)).save(source, format="JPEG")

            output = letterbox_image(str(source), output_dir)

            with Image.open(output) as image:
                self.assertEqual(image.size, JIBO_SCREEN_SIZE)
                self.assertEqual(image.getpixel((0, 0)), (0, 0, 0))
                center = image.getpixel((640, 360))
                self.assertTrue(all(abs(actual - expected) <= 3 for actual, expected in zip(center, (200, 40, 20))))

    def test_transparent_images_are_fitted_before_compositing(self):
        from PIL import Image

        for size in ((200, 100), (2000, 1000)):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as tmp:
                source = pathlib.Path(tmp) / "transparent.png"
                Image.new("RGBA", size, (200, 40, 20, 128)).save(source)
                output = letterbox_image(str(source), pathlib.Path(tmp) / "out")
                with Image.open(output) as image:
                    self.assertEqual(image.size, JIBO_SCREEN_SIZE)
                    self.assertEqual(image.getpixel((0, 0)), (0, 0, 0))
                    center = image.getpixel((640, 360))
                    self.assertTrue(all(abs(actual - expected) <= 3
                                        for actual, expected in zip(center, (100, 20, 10))))

    def test_palette_transparency_is_preserved_on_the_black_canvas(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / "palette.png"
            palette = Image.new("P", (200, 100), 1)
            palette.putpalette([0, 255, 0, 220, 40, 20] + [0] * (768 - 6))
            palette.paste(0, (80, 30, 120, 70))
            palette.save(source, transparency=0)
            output = letterbox_image(str(source), pathlib.Path(tmp) / "out")
            with Image.open(output) as image:
                center = image.getpixel((640, 360))
                self.assertLess(max(center), 20)
                red = image.getpixel((580, 360))
                self.assertTrue(all(abs(actual - expected) <= 3
                                    for actual, expected in zip(red, (220, 40, 20))))

    def test_normalization_publishes_only_a_complete_image(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / "source.png"
            output_dir = pathlib.Path(tmp) / "out"
            Image.new("RGB", (200, 100), "red").save(source)
            save = Image.Image.save

            def observe_save(image, target, *args, **kwargs):
                save(image, target, *args, **kwargs)
                self.assertEqual([p for p in output_dir.iterdir() if not p.name.startswith(".")], [])

            with patch.object(Image.Image, "save", observe_save):
                output = letterbox_image(str(source), output_dir)
            with Image.open(output) as image:
                image.load()
                self.assertEqual(image.size, JIBO_SCREEN_SIZE)
            self.assertEqual(list(output_dir.iterdir()), [output])

    def test_failed_normalization_leaves_no_partial_cache_file(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / "source.png"
            output_dir = pathlib.Path(tmp) / "out"
            Image.new("RGB", (200, 100), "red").save(source)
            with self.assertRaisesRegex(ValueError, "exceeds"):
                letterbox_image(str(source), output_dir, max_bytes=1)
            self.assertEqual(list(output_dir.iterdir()), [])

    async def test_display_eye_uses_eye_view(self):
        class ProbeRom(RomSession):
            def __init__(self):
                self.commands = []

            async def command(self, command, timeout=5.0):
                self.commands.append((command, timeout))
                return {"ok": True}

        rom = ProbeRom()
        result = await rom.display_eye()

        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            rom.commands,
            [
                (
                    {
                        "Type": "Display",
                        "View": {"Type": "Eye", "Name": "default"},
                    },
                    5.0,
                )
            ],
        )

    async def test_display_image_uses_remote_image_view(self):
        class ProbeRom(RomSession):
            def __init__(self):
                self.commands = []

            async def command(self, command, timeout=5.0):
                self.commands.append((command, timeout))
                return {"ok": True}

        rom = ProbeRom()
        result = await rom.display_image(
            "https://images.example.test/cat.jpg",
            name="hermes-image",
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            rom.commands,
            [
                (
                    {
                        "Type": "Display",
                        "View": {
                            "Type": "Image",
                            "Name": "hermes-image",
                            "Image": {
                                "src": "https://images.example.test/cat.jpg",
                                "name": "hermes-image",
                                "set": "",
                            },
                        },
                    },
                    5.0,
                )
            ],
        )

    def test_image_tools_have_explicit_safe_inputs(self):
        self.assertEqual(JIBO_DISPLAY_IMAGE_SCHEMA["name"], "jibo_display_image")
        self.assertEqual(JIBO_SHOW_EYE_SCHEMA["name"], "jibo_show_eye")
        self.assertEqual(
            JIBO_DISPLAY_IMAGE_SCHEMA["parameters"]["required"],
            ["url"],
        )
        self.assertEqual(
            JIBO_DISPLAY_IMAGE_SCHEMA["parameters"]["properties"]["url"]["format"],
            "uri",
        )

    def test_image_tool_delegates_to_adapter(self):
        class FakeAdapter:
            def __init__(self):
                self.image_calls = []
                self.eye_calls = 0

            def display_image(self, url, *, name):
                self.image_calls.append((url, name))
                return type("Result", (), {"success": True, "message_id": "image-tx", "error": None})()

            def show_eye(self, *, name):
                self.eye_calls += 1
                return type("Result", (), {"success": True, "message_id": "eye-tx", "error": None})()

        fake = FakeAdapter()
        import jibo_platform_image_tests.image_tool as image_module

        original = image_module.get_active_adapter
        image_module.get_active_adapter = lambda: fake
        try:
            image_result = jibo_display_image_tool(
                {"url": "https://images.example.test/cat.jpg", "name": "cat"},
                platform="jibo",
            )
            eye_result = jibo_show_eye_tool({}, platform="jibo")
        finally:
            image_module.get_active_adapter = original

        self.assertTrue(json.loads(image_result)["success"])
        self.assertTrue(json.loads(eye_result)["success"])
        self.assertEqual(fake.image_calls, [("https://images.example.test/cat.jpg", "cat")])
        self.assertEqual(fake.eye_calls, 1)

    def test_post_llm_auto_displays_local_image_before_finishing(self):
        class FakeAdapter:
            def __init__(self):
                self.calls = []

            def display_local_image(self, path):
                self.calls.append(("image", path))

            def finish_processing(self, *, success):
                self.calls.append(("finish", success))

        fake = FakeAdapter()
        import jibo_platform_image_tests.status as status_module

        original_adapter = status_module.get_active_adapter
        original_paths = status_module.extract_local_image_paths
        status_module.get_active_adapter = lambda: fake
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        fixture = str(pathlib.Path(directory.name) / "synthetic.jpg")
        status_module.extract_local_image_paths = lambda _text: [fixture]
        try:
            on_post_llm_call(
                platform="jibo",
                session_id="session-1",
                turn_id="turn-1",
                assistant_response="I found the camera image.",
            )
        finally:
            status_module.get_active_adapter = original_adapter
            status_module.extract_local_image_paths = original_paths

        self.assertEqual(
            fake.calls,
            [("image", fixture), ("finish", True)],
        )


if __name__ == "__main__":
    unittest.main()
