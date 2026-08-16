from __future__ import annotations

import unittest

from galgame_mcp.evidence import TextEpisodeTracker, build_frame_evidence
from galgame_mcp.text import parse_screen_text


class EvidenceTests(unittest.TestCase):
    def test_episode_tracker_requires_stable_samples_and_changes_id(self) -> None:
        tracker = TextEpisodeTracker(stable_samples=2)

        first = tracker.observe("第一句", confidence=0.8)
        second = tracker.observe("第一句", confidence=0.8)
        changed = tracker.observe("第二句", confidence=0.9)

        self.assertEqual(first["status"], "NEW")
        self.assertEqual(second["status"], "STABLE")
        self.assertNotEqual(first["episode_id"], changed["episode_id"])
        self.assertEqual(changed["status"], "NEW")

    def test_speaker_only_frame_exposes_a_separate_channel(self) -> None:
        evidence = build_frame_evidence(
            {"speaker": "角色A", "dialogue": "", "choices": []},
        )

        self.assertTrue(evidence["safe_to_advance"])
        self.assertEqual(evidence["blocking_reasons"], [])
        self.assertEqual(evidence["channels"]["speaker"]["status"], "present")

    def test_unknown_text_and_choices_block_advance(self) -> None:
        unknown = build_frame_evidence(
            {
                "dialogue": "已识别对白",
                "choices": [],
                "unknown_lines": ["未确认文字"],
            }
        )
        choice = build_frame_evidence(
            {"dialogue": "", "choices": ["选项一", "选项二"]},
        )

        self.assertFalse(unknown["safe_to_advance"])
        self.assertIn("unknown_text", unknown["blocking_reasons"])
        self.assertFalse(choice["safe_to_advance"])
        self.assertIn("choice_pending", choice["blocking_reasons"])

    def test_unparsed_text_is_not_resolved_as_transient_story(self) -> None:
        evidence = build_frame_evidence(
            {"dialogue": "", "speaker": "", "choices": [], "unparsed_lines": ["Chapter 1"]}
        )

        self.assertFalse(evidence["safe_to_advance"])
        self.assertIn("unknown_text", evidence["blocking_reasons"])
        self.assertFalse(evidence["channels"]["transient_story_text"]["resolved"])
        self.assertTrue(evidence["channels"]["scene_label"]["blocking"])

    def test_rapidocr_unknown_decoration_outside_story_region_is_recorded_without_blocking(self) -> None:
        evidence = build_frame_evidence(
            {
                "dialogue": "「正常对白」",
                "choices": [],
                "unknown_lines": ["RIDDLE JOKER"],
                "unknown_story_lines": [],
            },
            allow_unknown_with_story=True,
        )

        self.assertTrue(evidence["safe_to_advance"])
        self.assertNotIn("unknown_text", evidence["blocking_reasons"])
        self.assertTrue(evidence["non_blocking_unknown_text"])
        self.assertFalse(evidence["channels"]["unknown_text"]["blocking"])
        self.assertIn("RIDDLE JOKER", evidence["channels"]["unknown_text"]["text"])

    def test_rapidocr_unknown_text_inside_story_region_still_blocks(self) -> None:
        evidence = build_frame_evidence(
            {
                "dialogue": "「正常对白」",
                "choices": [],
                "unknown_lines": ["可能是选项"],
                "unknown_story_lines": ["可能是选项"],
            },
            allow_unknown_with_story=True,
        )

        self.assertFalse(evidence["safe_to_advance"])
        self.assertIn("unknown_text", evidence["blocking_reasons"])

    def test_rapidocr_synthetic_geometry_cannot_bypass_unknown_block(self) -> None:
        parsed = parse_screen_text(
            "dialogue\nunknown",
            regions=[
                {"text": "dialogue", "x": 200, "y": 580, "width": 180, "height": 30},
                {
                    "text": "unknown",
                    "x": 0,
                    "y": 0,
                    "width": 1000,
                    "height": 800,
                    "synthetic": True,
                    "source": "rapidocr_full_image",
                },
            ],
            image_size=(1000, 800),
            layout_profile={
                "dialogue_region": {
                    "x": 0.10,
                    "y": 0.70,
                    "width": 0.80,
                    "height": 0.22,
                    "coordinate_space": "normalized",
                }
            },
        )
        evidence = build_frame_evidence(parsed, allow_unknown_with_story=True)

        self.assertEqual(parsed["unknown_story_lines"], ["unknown"])
        self.assertFalse(evidence["safe_to_advance"])
        self.assertIn("unknown_text", evidence["blocking_reasons"])


if __name__ == "__main__":
    unittest.main()
