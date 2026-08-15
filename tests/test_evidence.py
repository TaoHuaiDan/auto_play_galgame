from __future__ import annotations

import unittest

from galgame_mcp.evidence import TextEpisodeTracker, build_frame_evidence


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


if __name__ == "__main__":
    unittest.main()
