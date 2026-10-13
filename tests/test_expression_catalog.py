"""Synthetic catalog rows only; no generated AnimDB data or assets."""
import importlib.util
import json
import pathlib
import sys
import unittest
from unittest.mock import patch


_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "jibo_platform_catalog_tests"
_PACKAGE_SPEC = importlib.util.spec_from_file_location(
    _PACKAGE_NAME,
    _PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(_PLUGIN_DIR)],
)
_PACKAGE = importlib.util.module_from_spec(_PACKAGE_SPEC)
sys.modules[_PACKAGE_NAME] = _PACKAGE
assert _PACKAGE_SPEC.loader is not None
_PACKAGE_SPEC.loader.exec_module(_PACKAGE)

from jibo_platform_catalog_tests.catalog import (  # noqa: E402
    expression_to_esml,
    search_catalog,
)
from jibo_platform_catalog_tests.catalog_tool import (  # noqa: E402
    JIBO_FIND_EXPRESSION_SCHEMA,
    JIBO_PLAY_EXPRESSION_SCHEMA,
    jibo_find_expression_tool,
    jibo_play_expression_tool,
)


class ExpressionCatalogTests(unittest.TestCase):
    def test_search_ranks_exact_name_and_returns_metadata(self):
        catalog = [
            {
                "name": "Synthetic_Happy_01",
                "categories": ["Happy"],
                "metaTerms": ["cheerful", "rom-upbeat"],
                "duration": 2.5,
                "hasAudio": False,
            },
            {
                "name": "Synthetic_Sad_01",
                "categories": ["Sad"],
                "metaTerms": ["slow"],
                "duration": 3,
                "hasAudio": False,
            },
        ]

        results = search_catalog(catalog, query="Synthetic_Happy_01", limit=5)

        self.assertEqual(results[0]["name"], "Synthetic_Happy_01")
        self.assertEqual(results[0]["categories"], ["Happy"])
        self.assertEqual(results[0]["meta_terms"], ["cheerful", "rom-upbeat"])

    def test_expression_esml_uses_catalog_name_and_safe_attributes(self):
        self.assertEqual(
            expression_to_esml("Synthetic_Happy_01", text="I did it!"),
            "<anim name='Synthetic_Happy_01'>I did it!</anim>",
        )
        self.assertNotIn("<ssa", expression_to_esml("Synthetic_SSA_001", text="Thinking"))

    def test_lookup_tool_returns_bounded_json_context(self):
        fake_catalog = [
            {
                "name": "Synthetic_Disco_01",
                "categories": ["Dance"],
                "metaTerms": ["disco", "music"],
                "duration": 4,
                "hasAudio": True,
            }
        ]
        with patch("jibo_platform_catalog_tests.catalog_tool.load_catalog", return_value=fake_catalog):
            result = json.loads(
                jibo_find_expression_tool(
                    {"query": "disco", "limit": 3},
                    platform="jibo",
                )
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["name"], "Synthetic_Disco_01")
        self.assertIn("<anim name='Synthetic_Disco_01'>", result["results"][0]["suggested_esml"])

    def test_lookup_schema_is_explicit_and_bounded(self):
        self.assertEqual(JIBO_FIND_EXPRESSION_SCHEMA["name"], "jibo_find_expression")
        props = JIBO_FIND_EXPRESSION_SCHEMA["parameters"]["properties"]
        self.assertEqual(props["limit"]["maximum"], 20)
        self.assertEqual(JIBO_FIND_EXPRESSION_SCHEMA["parameters"]["required"], ["query"])

    def test_play_tool_executes_catalog_confirmed_expression(self):
        fake_catalog = [
            {
                "name": "Synthetic_Dance_01",
                "categories": ["Dance"],
                "metaTerms": ["carlton", "dance"],
                "duration": 5,
                "hasAudio": True,
            }
        ]

        class FakeAdapter:
            def __init__(self):
                self.calls = []

            def play_expression(self, name, *, text):
                self.calls.append((name, text))
                return type("Result", (), {"success": True, "message_id": "dance-tx", "error": None})()

        fake = FakeAdapter()
        import jibo_platform_catalog_tests.catalog_tool as catalog_tool_module

        with patch("jibo_platform_catalog_tests.catalog_tool.load_catalog", return_value=fake_catalog), \
             patch("jibo_platform_catalog_tests.catalog_tool.get_active_adapter", return_value=fake):
            result = json.loads(
                jibo_play_expression_tool(
                    {"name": "Synthetic_Dance_01", "text": "Here I go!"},
                    platform="jibo",
                )
            )

        self.assertTrue(result["success"])
        self.assertEqual(fake.calls, [("Synthetic_Dance_01", "Here I go!")])

    def test_play_tool_rejects_unknown_expression(self):
        with patch("jibo_platform_catalog_tests.catalog_tool.load_catalog", return_value=[]):
            result = json.loads(
                jibo_play_expression_tool(
                    {"name": "Invented_Dance"},
                    platform="jibo",
                )
            )

        self.assertFalse(result["success"])
        self.assertIn("not found", result["error"])


if __name__ == "__main__":
    unittest.main()
