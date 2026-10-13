import unittest

from esml import chunk_esml, prepare_for_jibo, sanitize_esml


class EsmlTests(unittest.TestCase):
    def test_strips_thinking_and_emoji(self):
        value = sanitize_esml("<think>secret reasoning</think> Hello 😀 & welcome")
        self.assertNotIn("secret reasoning", value)
        self.assertNotIn("😀", value)
        self.assertIn("and", value)

    def test_preserves_tags_and_does_not_split_them(self):
        chunks = chunk_esml("<anim cat='happy'/> " + ("word " * 150), 120)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertEqual(chunk.count("<"), chunk.count(">"))
            self.assertLessEqual(len(chunk), 120)

    def test_plain_text_gets_safe_break(self):
        self.assertTrue(prepare_for_jibo("hello")[0].startswith("<break size='0.1'/>") )


if __name__ == "__main__":
    unittest.main()
