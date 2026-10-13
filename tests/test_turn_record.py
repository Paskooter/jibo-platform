"""The turn record is the contract; these tests are what it promises."""

import importlib.util
import json
import pathlib
import sys
import unittest


_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "jibo_platform_turn_record_tests"
_PACKAGE_SPEC = importlib.util.spec_from_file_location(
    _PACKAGE_NAME,
    _PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(_PLUGIN_DIR)],
)
_PACKAGE = importlib.util.module_from_spec(_PACKAGE_SPEC)
sys.modules[_PACKAGE_NAME] = _PACKAGE
assert _PACKAGE_SPEC.loader is not None
_PACKAGE_SPEC.loader.exec_module(_PACKAGE)

from jibo_platform_turn_record_tests.turn_record import (  # noqa: E402
    BRIDGE_FRAME_BYTES,
    BRIDGE_FRAME_HEADROOM,
    BRIDGE_MEDIA_ITEMS,
    BRIDGE_TEXT_CHARS,
    MAX_INLINE_DATA_CHARS,
    exceeds_thumbnail,
    MAX_TRANSCRIPT_CHARS,
    ORIGIN_HERMES,
    ORIGIN_PHOENIX,
    ORIGIN_PHOENIX_ESCALATED,
    SPEECH_HERMES,
    SPEECH_NONE,
    SPEECH_PHOENIX,
    STATUS_FAILED,
    TurnRecord,
    TurnRecordError,
    merge_records,
    new_turn_id,
)


def _encoded(value) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _phoenix_turn(**overrides):
    payload = {
        "turn_id": "ph-1",
        "device_id": "robot-one",
        "origin": ORIGIN_PHOENIX,
        "transcript": "what's the weather",
        "response": "It's seventy-two and sunny.",
        "intent": "weather.forecast",
        "skill": "weather-skill",
        "timing": {"asr_ms": 410, "nlu_ms": 22, "skill_ms": 180},
    }
    payload.update(overrides)
    return payload


class TurnRecordValidationTests(unittest.TestCase):
    def test_a_phoenix_turn_round_trips(self):
        record = TurnRecord.from_mapping(_phoenix_turn())
        self.assertEqual(record.origin, ORIGIN_PHOENIX)
        self.assertEqual(record.skill, "weather-skill")
        self.assertEqual(record.timing["asr_ms"], 410)
        # The speaker is never inferred from the origin; an unstated owner
        # stays unstated so a later report cannot overwrite the truth.
        self.assertEqual(record.speech_owner, SPEECH_NONE)
        self.assertEqual(
            TurnRecord.from_mapping(_phoenix_turn(speech_owner="phoenix")).speech_owner,
            SPEECH_PHOENIX,
        )
        self.assertEqual(TurnRecord.from_mapping(record.to_dict()).turn_id, "ph-1")

    def test_an_unknown_version_is_refused_rather_than_guessed_at(self):
        with self.assertRaises(TurnRecordError) as caught:
            TurnRecord.from_mapping(_phoenix_turn(v=99))
        self.assertEqual(caught.exception.field, "v")

    def test_a_record_needs_a_robot_to_belong_to(self):
        payload = _phoenix_turn()
        payload.pop("device_id")
        with self.assertRaises(TurnRecordError):
            TurnRecord.from_mapping(payload)
        # …unless the listener supplies the single configured robot.
        record = TurnRecord.from_mapping(payload, default_device_id="kitchen")
        self.assertEqual(record.device_id, "kitchen")

    def test_an_empty_turn_is_not_a_turn(self):
        with self.assertRaises(TurnRecordError):
            TurnRecord.from_mapping(_phoenix_turn(transcript="", response=""))

    def test_a_media_entry_needs_a_source_the_robot_can_open(self):
        with self.assertRaises(TurnRecordError):
            TurnRecord.from_mapping(_phoenix_turn(media=[{"kind": "image", "alt": "radar"}]))

    def test_a_relative_media_path_is_refused(self):
        # It would resolve against whichever skill is open on the robot
        # rather than against the caller — a silently wrong picture.
        with self.assertRaises(TurnRecordError) as caught:
            TurnRecord.from_mapping(
                _phoenix_turn(media=[{"kind": "image", "path": "resources/radar.png"}])
            )
        self.assertEqual(caught.exception.field, "media.path")

    def test_the_shapes_the_robot_loader_accepts_are_allowed(self):
        for path in ("/opt/jibo/media/radar.png", "file:///opt/jibo/x.png", "core://a/b.crn"):
            record = TurnRecord.from_mapping(
                _phoenix_turn(media=[{"kind": "image", "path": path}])
            )
            self.assertEqual(record.media[0]["path"], path)

    def test_inline_data_must_be_a_bounded_base64_uri(self):
        record = TurnRecord.from_mapping(
            _phoenix_turn(media=[{"kind": "image", "data": _data_uri(64)}])
        )
        self.assertTrue(record.media[0]["data"].startswith("data:image/jpeg;base64,"))
        with self.assertRaises(TurnRecordError):
            TurnRecord.from_mapping(
                _phoenix_turn(media=[{"kind": "image", "data": "not-a-data-uri"}])
            )
        with self.assertRaises(TurnRecordError):
            TurnRecord.from_mapping(
                _phoenix_turn(media=[{"kind": "image", "data": _data_uri(MAX_INLINE_DATA_CHARS + 8)}])
            )

    def test_intrinsic_dimensions_are_carried_so_the_row_does_not_jump(self):
        record = TurnRecord.from_mapping(
            _phoenix_turn(
                media=[{"kind": "image", "url": "https://h/a.png", "width": 640, "height": "480"}]
            )
        )
        self.assertEqual((record.media[0]["width"], record.media[0]["height"]), (640, 480))
        self.assertFalse(exceeds_thumbnail(record.media[0]))
        self.assertTrue(exceeds_thumbnail({"width": 1920, "height": 1080}))

    def test_media_urls_may_not_carry_credentials(self):
        with self.assertRaises(TurnRecordError):
            TurnRecord.from_mapping(
                _phoenix_turn(media=[{"kind": "image", "url": "https://user:pw@host/a.png"}])
            )
        with self.assertRaises(TurnRecordError):
            TurnRecord.from_mapping(
                _phoenix_turn(media=[{"kind": "image", "url": "file:///tmp/synthetic-private-file"}])
            )

    def test_oversized_text_is_trimmed_rather_than_rejected(self):
        record = TurnRecord.from_mapping(_phoenix_turn(transcript="a" * 9000))
        self.assertEqual(len(record.transcript), MAX_TRANSCRIPT_CHARS)
        self.assertTrue(record.transcript.endswith("…"))

    def test_a_bad_origin_is_a_hard_error(self):
        with self.assertRaises(TurnRecordError):
            TurnRecord.from_mapping(_phoenix_turn(origin="whoever"))

    def test_turn_ids_are_unique_and_prefixed_by_producer(self):
        self.assertTrue(new_turn_id(ORIGIN_PHOENIX).startswith("ph-"))
        self.assertTrue(new_turn_id(ORIGIN_HERMES).startswith("hm-"))
        self.assertNotEqual(new_turn_id(), new_turn_id())


def _data_uri(payload_chars: int) -> str:
    return "data:image/jpeg;base64," + ("A" * payload_chars)


class BridgeMediaProjectionTests(unittest.TestCase):
    def test_only_what_the_screen_can_show_is_sent(self):
        record = TurnRecord.from_mapping(
            _phoenix_turn(
                media=[
                    {"kind": "image", "url": f"https://h/{index}.png"}
                    for index in range(4)
                ]
            )
        )
        params = record.to_bridge_params()
        self.assertEqual(len(params["media"]), BRIDGE_MEDIA_ITEMS)
        self.assertEqual(params["media"][0]["url"], "https://h/0.png")
        # The rest are reported, not silently dropped.
        self.assertEqual(params["media_omitted"], 4 - BRIDGE_MEDIA_ITEMS)

    def test_the_cheapest_handle_the_robot_can_open_is_chosen(self):
        entry = {
            "kind": "image",
            "url": "https://h/a.png",
            "data": _data_uri(64),
            "path": "/opt/jibo/media/a.png",
            "width": 320,
            "height": 240,
        }
        # An absolute path costs no fetch and no bytes.
        params = TurnRecord.from_mapping(_phoenix_turn(media=[entry])).to_bridge_params()
        self.assertEqual(params["media"][0]["path"], "/opt/jibo/media/a.png")
        self.assertNotIn("url", params["media"][0])
        self.assertNotIn("data", params["media"][0])
        self.assertEqual(params["media"][0]["width"], 320)

        # Without a path, inline data still beats a fetch inside the render path.
        no_path = {key: value for key, value in entry.items() if key != "path"}
        params = TurnRecord.from_mapping(_phoenix_turn(media=[no_path])).to_bridge_params()
        self.assertEqual(params["media"][0]["data"], _data_uri(64))
        self.assertNotIn("url", params["media"][0])

        # And a URL is still accepted when it is all there is.
        only_url = {"kind": "image", "url": "https://h/a.png"}
        params = TurnRecord.from_mapping(_phoenix_turn(media=[only_url])).to_bridge_params()
        self.assertEqual(params["media"][0]["url"], "https://h/a.png")

    def test_a_maximal_turn_with_a_maximal_thumbnail_still_fits_the_frame(self):
        # The invariant the inline cap exists to guarantee: the largest
        # record this contract accepts, carrying the largest inline image it
        # accepts, is still under Be's 24 KB inbound frame limit.
        record = TurnRecord.from_mapping(
            _phoenix_turn(
                transcript="t" * 4000,
                response="r" * 12000,
                media=[{"kind": "image", "data": _data_uri(MAX_INLINE_DATA_CHARS - 32)}],
            )
        )
        params = record.to_bridge_params()
        self.assertIn("data", params["media"][0])
        self.assertLess(_encoded(params), BRIDGE_FRAME_BYTES - BRIDGE_FRAME_HEADROOM)

    def test_inline_data_that_will_not_fit_falls_back_to_the_url(self):
        record = TurnRecord.from_mapping(
            _phoenix_turn(
                media=[
                    {
                        "kind": "image",
                        "data": _data_uri(8000),
                        "url": "https://h/thumb.jpg",
                    }
                ]
            )
        )
        params = record.to_bridge_params(frame_budget=BRIDGE_FRAME_HEADROOM + 4096)
        self.assertEqual(params["media"][0]["url"], "https://h/thumb.jpg")
        self.assertNotIn("data", params["media"][0])
        self.assertNotIn("media_omitted", params)

    def test_a_picture_that_cannot_fit_never_costs_the_turn_its_text(self):
        # Be rejects the whole frame over 24 KB before parsing it.  Losing a
        # picture is acceptable; losing the conversation is not.
        record = TurnRecord.from_mapping(
            _phoenix_turn(media=[{"kind": "image", "data": _data_uri(8000)}])
        )
        budget = BRIDGE_FRAME_HEADROOM + 4096
        params = record.to_bridge_params(frame_budget=budget)
        self.assertEqual(params.get("media", []), [])
        self.assertEqual(params["media_omitted"], 1)
        self.assertEqual(params["transcript"], "what's the weather")
        self.assertEqual(params["response"], "It's seventy-two and sunny.")
        self.assertLess(_encoded(params), budget)

    def test_a_small_thumbnail_still_travels_inline(self):
        record = TurnRecord.from_mapping(
            _phoenix_turn(media=[{"kind": "image", "data": _data_uri(2048), "width": 640}])
        )
        params = record.to_bridge_params()
        self.assertIn("data", params["media"][0])
        self.assertNotIn("media_omitted", params)
        self.assertLess(_encoded(params), BRIDGE_FRAME_BYTES - BRIDGE_FRAME_HEADROOM)


class BridgeProjectionTests(unittest.TestCase):
    def test_the_rendered_projection_fits_the_robot_limits(self):
        record = TurnRecord.from_mapping(
            _phoenix_turn(response="b" * 7000, response_esml="<esml/>" * 100)
        )
        params = record.to_bridge_params(session_id="jibo:robot")
        self.assertLessEqual(len(params["response"]), BRIDGE_TEXT_CHARS)
        # Nothing the screen cannot use travels to the robot.
        self.assertNotIn("response_esml", params)
        self.assertNotIn("timing", params)
        self.assertEqual(params["session_id"], "jibo:robot")
        self.assertEqual(params["turn_id"], "ph-1")


class TranscriptProjectionTests(unittest.TestCase):
    def test_a_phoenix_turn_writes_both_halves_of_the_exchange(self):
        rows = TurnRecord.from_mapping(_phoenix_turn()).to_transcript_rows()
        self.assertEqual([row["role"] for row in rows], ["user", "assistant"])
        # Both rows are observed: the agent produced neither of them.
        self.assertTrue(all(row["observed"] for row in rows))
        self.assertEqual(rows[0]["platform_message_id"], "jibo-turn:ph-1:user")
        self.assertEqual(rows[1]["platform_message_id"], "jibo-turn:ph-1:assistant")
        self.assertEqual(rows[0]["display_metadata"]["skill"], "weather-skill")

    def test_message_ids_are_stable_so_a_retried_report_cannot_double_write(self):
        first = TurnRecord.from_mapping(_phoenix_turn()).to_transcript_rows()
        second = TurnRecord.from_mapping(_phoenix_turn()).to_transcript_rows()
        self.assertEqual(
            [row["platform_message_id"] for row in first],
            [row["platform_message_id"] for row in second],
        )

    def test_a_hermes_answered_turn_is_not_marked_observed(self):
        rows = TurnRecord.from_mapping(
            _phoenix_turn(origin=ORIGIN_PHOENIX_ESCALATED)
        ).to_transcript_rows()
        assistant = rows[1]
        self.assertFalse(assistant["observed"])

    def test_skill_attribution_is_opt_in(self):
        record = TurnRecord.from_mapping(_phoenix_turn())
        self.assertNotIn("weather-skill", record.to_transcript_rows()[1]["content"])
        attributed = record.to_transcript_rows(attribute_skill=True)[1]["content"]
        self.assertIn("weather-skill", attributed)


class MergeTests(unittest.TestCase):
    def test_a_later_report_completes_the_turn_rather_than_duplicating_it(self):
        answered = TurnRecord.from_mapping(
            {
                "turn_id": "ph-7",
                "device_id": "robot",
                "origin": ORIGIN_PHOENIX_ESCALATED,
                "transcript": "why is the sky blue",
                "response": "Because of Rayleigh scattering.",
                "speech_owner": SPEECH_HERMES,
            }
        )
        reported = TurnRecord.from_mapping(
            {
                "turn_id": "ph-7",
                "device_id": "robot",
                "origin": ORIGIN_PHOENIX,
                "transcript": "why is the sky blue",
                "skill": "answer-skill",
                "timing": {"asr_ms": 380, "total_ms": 4100},
            }
        )
        merged = merge_records(answered, reported)
        # The escalation is not downgraded to a locally handled turn…
        self.assertEqual(merged.origin, ORIGIN_PHOENIX_ESCALATED)
        # …the answer survives a report that did not repeat it…
        self.assertEqual(merged.response, "Because of Rayleigh scattering.")
        # …and the report's new facts are folded in.
        self.assertEqual(merged.skill, "answer-skill")
        self.assertEqual(merged.timing["total_ms"], 4100)
        self.assertEqual(merged.speech_owner, SPEECH_HERMES)

    def test_a_failure_report_overrides_an_optimistic_status(self):
        first = TurnRecord(turn_id="t", device_id="robot", response="hi")
        second = TurnRecord(turn_id="t", device_id="robot", status=STATUS_FAILED)
        self.assertEqual(merge_records(first, second).status, STATUS_FAILED)

    def test_records_for_different_turns_never_merge(self):
        with self.assertRaises(TurnRecordError):
            merge_records(
                TurnRecord(turn_id="a", device_id="robot"),
                TurnRecord(turn_id="b", device_id="robot"),
            )


if __name__ == "__main__":
    unittest.main()
