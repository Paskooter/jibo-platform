"""Incremental delivery: what the robot renders and when it starts talking."""

import importlib.util
import pathlib
import sys
import unittest


_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "jibo_platform_turn_stream_tests"
_PACKAGE_SPEC = importlib.util.spec_from_file_location(
    _PACKAGE_NAME,
    _PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(_PLUGIN_DIR)],
)
_PACKAGE = importlib.util.module_from_spec(_PACKAGE_SPEC)
sys.modules[_PACKAGE_NAME] = _PACKAGE
assert _PACKAGE_SPEC.loader is not None
_PACKAGE_SPEC.loader.exec_module(_PACKAGE)

from jibo_platform_turn_stream_tests.turn_stream import (  # noqa: E402
    ResponseStream,
    SentenceSegmenter,
)


class SentenceSegmenterTests(unittest.TestCase):
    def test_a_completed_sentence_is_released_as_soon_as_it_ends(self):
        # Released at the terminator, without waiting for the next token:
        # a robot that pauses for one more character sounds like it stalled.
        segmenter = SentenceSegmenter()
        self.assertEqual(
            segmenter.feed("The forecast is clear all day."),
            ["The forecast is clear all day."],
        )
        self.assertEqual(segmenter.pending, "")

    def test_a_partial_sentence_is_held_until_it_ends(self):
        segmenter = SentenceSegmenter()
        self.assertEqual(segmenter.feed("The forecast is clear"), [])
        self.assertEqual(segmenter.feed(" all day."), ["The forecast is clear all day."])

    def test_an_abbreviation_is_not_a_sentence_boundary(self):
        segmenter = SentenceSegmenter()
        released = segmenter.feed("I checked with Dr. Rivera about the appointment. ")
        self.assertEqual(released, ["I checked with Dr. Rivera about the appointment."])

    def test_a_decimal_point_is_not_a_sentence_boundary(self):
        segmenter = SentenceSegmenter()
        released = segmenter.feed("The temperature outside is 72.4 degrees right now. ")
        self.assertEqual(released, ["The temperature outside is 72.4 degrees right now."])

    def test_a_run_on_answer_still_starts_speaking(self):
        # No terminal punctuation at all: the robot must not wait forever.
        segmenter = SentenceSegmenter(max_chars=80)
        released = segmenter.feed(
            "so I looked at the calendar and then the weather and then the traffic "
            "and then the news and then a few other things"
        )
        self.assertTrue(released)
        self.assertLessEqual(len(released[0]), 80)

    def test_flush_returns_the_unterminated_tail(self):
        segmenter = SentenceSegmenter()
        segmenter.feed("no full stop here")
        self.assertEqual(segmenter.flush(), "no full stop here")
        self.assertEqual(segmenter.flush(), "")


class ResponseStreamTests(unittest.TestCase):
    def test_deltas_are_appends_and_never_repeat_text(self):
        stream = ResponseStream(turn_id="hm-1", min_delta_chars=0)
        first = stream.push("It is ")
        second = stream.push("It is seventy-two ")
        third = stream.push("It is seventy-two and sunny.")
        self.assertEqual(first.delta, "It is ")
        self.assertEqual(second.delta, "seventy-two ")
        self.assertEqual(third.delta, "and sunny.")
        self.assertEqual(stream.text, "It is seventy-two and sunny.")

    def test_the_first_frame_is_flagged_once_so_the_face_changes_once(self):
        stream = ResponseStream(min_delta_chars=0)
        self.assertTrue(stream.push("Hello").first)
        self.assertFalse(stream.push("Hello there").first)

    def test_a_tiny_delta_is_held_back_rather_than_sent_per_token(self):
        stream = ResponseStream(min_delta_chars=20)
        self.assertEqual(stream.push("It ").delta, "")
        self.assertEqual(stream.push("It is ").delta, "")
        step = stream.push("It is a bright and clear morning")
        self.assertEqual(step.delta, "It is a bright and clear morning")

    def test_speech_starts_at_the_first_sentence_boundary(self):
        stream = ResponseStream(min_delta_chars=0)
        self.assertEqual(stream.push("The forecast is clear all day").sentences, [])
        step = stream.push("The forecast is clear all day. Then it clouds")
        self.assertEqual(step.sentences, ["The forecast is clear all day."])

    def test_a_new_segment_after_a_tool_call_appends_instead_of_restarting(self):
        stream = ResponseStream(min_delta_chars=0)
        stream.push("Let me check that for you.")
        step = stream.push("It is seventy-two degrees.")
        self.assertIn("Let me check that for you.", stream.text)
        self.assertIn("It is seventy-two degrees.", stream.text)
        self.assertIn("It is seventy-two degrees.", step.delta)
        self.assertNotIn("Let me check", step.delta)

    def test_the_final_answer_only_speaks_what_was_not_already_spoken(self):
        stream = ResponseStream(min_delta_chars=0)
        stream.push("The forecast is clear all day. ")
        final = "The forecast is clear all day. Bring a jacket after dark."
        step = stream.finish(final)
        self.assertEqual(step.sentences, ["Bring a jacket after dark."])
        self.assertEqual(step.delta, "Bring a jacket after dark.")
        self.assertFalse(step.replace)

    def test_trailing_whitespace_is_not_mistaken_for_a_divergence(self):
        # Delta whitespace is load-bearing — the robot concatenates frames
        # verbatim — but the authoritative final answer arrives stripped. A
        # naive prefix check would call an identical answer a divergence and
        # trigger a needless full replace.
        stream = ResponseStream(min_delta_chars=0)
        stream.push("The forecast is clear all day. ")
        step = stream.finish("The forecast is clear all day.")
        self.assertFalse(step.replace)
        self.assertEqual(step.delta, "")
        self.assertEqual(step.sentences, [])

    def test_delta_whitespace_is_preserved_exactly(self):
        stream = ResponseStream(min_delta_chars=0)
        self.assertEqual(stream.push("Hello").delta, "Hello")
        self.assertEqual(stream.push("Hello  ").delta, "  ")
        self.assertEqual(stream.push("Hello  there").delta, "there")

    def test_a_stream_rebuilt_mid_turn_does_not_reuse_a_sequence_number(self):
        # Be rejects a repeated or lower seq for a turn, dropping the text.
        first = ResponseStream(turn_id="hm-1", min_delta_chars=0)
        first.next_seq()
        first.next_seq()
        second = ResponseStream(turn_id="hm-1", min_delta_chars=0, start_seq=first.seq)
        self.assertEqual(second.next_seq(), 3)

    def test_a_final_answer_that_diverges_replaces_the_stream(self):
        stream = ResponseStream(min_delta_chars=0)
        stream.push("Here is the wrong draft answer entirely. ")
        step = stream.finish("Actually the office is closed today.")
        self.assertTrue(step.replace)
        self.assertEqual(step.sentences, ["Actually the office is closed today."])
        self.assertEqual(stream.text, "Actually the office is closed today.")

    def test_a_response_that_never_streamed_is_still_delivered_whole(self):
        stream = ResponseStream(min_delta_chars=0)
        step = stream.finish("Short answer.")
        self.assertEqual(step.sentences, ["Short answer."])
        self.assertEqual(step.delta, "Short answer.")
        self.assertTrue(step.first)

    def test_sequence_numbers_are_monotonic_so_a_gap_is_detectable(self):
        stream = ResponseStream()
        self.assertEqual([stream.next_seq(), stream.next_seq(), stream.next_seq()], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
