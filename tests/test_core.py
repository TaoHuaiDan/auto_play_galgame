from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from galgame_mcp.core import SessionStore


class SessionStoreTests(unittest.TestCase):
    def test_data_directory_precedence_and_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            explicit = base / "explicit"
            environment = base / "environment"

            with patch.dict(os.environ, {"GALGAME_MCP_DATA_DIR": str(environment)}):
                from_environment = SessionStore()
                self.assertEqual(from_environment.root, environment.resolve())
                self.assertEqual(from_environment.root_source, "environment")

                from_argument = SessionStore(explicit)
                self.assertEqual(from_argument.root, explicit.resolve())
                self.assertEqual(from_argument.root_source, "argument")

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("GALGAME_MCP_DATA_DIR", None)
                with patch("galgame_mcp.core.Path.cwd", return_value=base):
                    from_cwd = SessionStore()
                self.assertEqual(from_cwd.root, (base / ".galgame_sessions").resolve())
                self.assertEqual(from_cwd.root_source, "cwd_default")

    def test_record_context_export_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SessionStore(root)
            summary = store.create_session("测试视觉小说", session_id="test-session")
            self.assertEqual(summary["session_id"], "test-session")
            configured = store.configure_game_layout(
                {
                    "dialogue_region": {
                        "x": 0.1,
                        "y": 0.7,
                        "width": 0.8,
                        "height": 0.25,
                        "coordinate_space": "normalized",
                    },
                    "speaker_region": {"x": 0, "y": 0, "width": 1, "height": 0.4},
                    "speaker_markers": [{"open": "<", "close": ">", "allow_unclosed": True}],
                    "dialogue_markers": [{"open": "{", "close": "}"}],
                    "ocr_ignore_regions": [
                        {
                            "name": "fixed_footer",
                            "x": 0,
                            "y": 0.9,
                            "width": 1,
                            "height": 0.1,
                            "coordinate_space": "normalized",
                        },
                    ],
                    "ocr_blacklist": [
                        {"text": "RIDDLE JOKER", "match": "exact"},
                    ],
                }
            )
            self.assertEqual(configured["layout_profile"]["speaker_markers"][0]["open"], "<")
            self.assertEqual(store.get_session()["game"]["layout_profile"]["dialogue_region"]["width"], 0.8)
            self.assertEqual(
                store.get_session()["game"]["layout_profile"]["ocr_ignore_regions"][0]["name"],
                "fixed_footer",
            )
            self.assertEqual(
                store.get_session()["game"]["layout_profile"]["ocr_blacklist"][0]["match"],
                "exact",
            )

            configured_actions = store.configure_game_actions(
                {
                    "hide_ui": {
                        "kind": "click",
                        "target": "window_center",
                        "button": "right",
                        "delivery": "send",
                    },
                    "skip_line": {"kind": "key", "key": "RIGHT"},
                }
            )
            self.assertEqual(configured_actions["action_profile"]["hide_ui"]["button"], "right")
            self.assertEqual(store.get_session()["game"]["action_profile"]["skip_line"]["key"], "RIGHT")

            configured_timing = store.configure_game_timing(
                {
                    "strategy": "adaptive",
                    "post_click_wait_seconds": -1,
                    "settle_timeout_seconds": 5.5,
                    "settle_poll_seconds": 0.15,
                    "stable_samples": 4,
                    "require_text_change": True,
                    "transition_accelerate": True,
                    "transition_accelerate_delay_seconds": 0.7,
                    "transition_probe_interval_seconds": 0.1,
                }
            )
            self.assertEqual(configured_timing["timing_profile"]["strategy"], "text_hash")
            self.assertEqual(configured_timing["timing_profile"]["post_click_wait_seconds"], 0.0)
            self.assertTrue(configured_timing["timing_profile"]["transition_accelerate"])
            self.assertEqual(
                configured_timing["timing_profile"]["transition_accelerate_delay_seconds"],
                0.7,
            )
            self.assertEqual(store.get_session()["game"]["timing_profile"]["stable_samples"], 4)

            configured_exact_timing = store.configure_game_timing(
                {"strategy": "text_hash", "stable_samples": 2}
            )
            self.assertEqual(configured_exact_timing["timing_profile"]["strategy"], "text_hash")

            observation = store.record_observation(
                raw_text="【小葵】\n今天放学后要一起回家吗？\n1. 答应\n2. 婉拒",
                scene_id="prologue-001",
                location="教室",
                speaker="小葵",
                text="今天放学后要一起回家吗？",
                choices=["答应", "婉拒"],
                screenshot_path="C:/local/frame.png",
                source="test",
                confidence=0.95,
            )
            self.assertEqual(len(observation["event_ids"]), 5)

            state = store.get_current_state()
            self.assertEqual(state["current_state"]["speaker"], "小葵")
            self.assertEqual(len(state["unresolved_choices"]), 1)
            choice_id = state["unresolved_choices"][0]["choice_id"]

            store.record_choice(
                options=["答应", "婉拒"],
                choice_id=choice_id,
                selected_index=1,
                source="test-policy",
            )
            store.set_story_variable("aoi_affection", "2", value_type="integer")
            store.add_note("测试路线已选择答应", kind="inference")

            context = store.build_context(recent_events=10)
            self.assertEqual(context["unresolved_choices"], [])
            self.assertIn("小葵", context["codex_markdown"])
            self.assertIn("aoi_affection", context["codex_markdown"])
            self.assertEqual(store.search_story("放学")["count"], 2)

            compact = store.build_context(recent_events=10, include_markdown=False, compact=True)
            compact_text = json.dumps(compact, ensure_ascii=False)
            self.assertNotIn("raw_text", compact_text)
            self.assertNotIn("screenshot_path", compact_text)
            self.assertNotIn("last_screenshot", compact["current_state"])
            self.assertIn("今天放学后要一起回家吗？", compact_text)

            exported = store.export_session("jsonl")
            self.assertTrue(Path(exported["path"]).exists())
            rows = Path(exported["path"]).read_text(encoding="utf-8").splitlines()
            self.assertGreaterEqual(len(rows), 2)
            self.assertEqual(json.loads(rows[0])["record_type"], "session")

            reloaded = SessionStore(root)
            resumed = reloaded.get_current_state("test-session")
            self.assertEqual(resumed["current_state"]["variables"]["aoi_affection"], 2)
            self.assertEqual(resumed["timeline_count"], store.get_current_state()["timeline_count"])
            self.assertEqual(
                reloaded.get_session("test-session")["game"]["timing_profile"]["strategy"],
                "text_hash",
            )

    def test_dismissed_choice_is_not_a_route_decision_or_unresolved_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionStore(Path(temporary))
            store.create_session("误报测试", session_id="dismiss-session")
            recorded = store.record_choice(
                options=["普通对白被识别成选项"],
                source="windows_ocr",
                session_id="dismiss-session",
            )
            choice_id = recorded["choice"]["choice_id"]

            dismissed = store.dismiss_choice(
                choice_id=choice_id,
                reason="false_positive_visual_review",
                session_id="dismiss-session",
            )

            self.assertTrue(dismissed["choice"]["dismissed"])
            self.assertIsNone(dismissed["choice"]["selected_option_id"])
            state = store.get_current_state("dismiss-session")
            self.assertEqual(state["unresolved_choices"], [])
            self.assertEqual(state["session"]["unresolved_choice_count"], 0)
            self.assertEqual(dismissed["event"]["type"], "choice_dismissed")

    def test_dismissing_false_choice_releases_compaction_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionStore(
                Path(temporary),
                compaction_threshold_bytes=16_384,
                compaction_keep_recent_events=1,
            )
            store.create_session("压缩误报测试", session_id="dismiss-compaction")
            recorded = store.record_choice(
                options=["对白被误识别成选项"],
                source="windows_ocr",
                session_id="dismiss-compaction",
            )
            for index in range(4):
                store.record_dialogue(
                    f"第 {index} 句 " + ("后续对白" * 1_500),
                    speaker="角色A",
                    session_id="dismiss-compaction",
                )

            blocked = store.compaction_status(session_id="dismiss-compaction")
            self.assertTrue(blocked["summary_due"])
            self.assertEqual(blocked["candidate_block_reason"], "unresolved_choice_in_compaction_prefix")

            store.dismiss_choice(
                choice_id=recorded["choice"]["choice_id"],
                reason="false_positive_visual_review",
                session_id="dismiss-compaction",
            )
            request = store.get_compaction_request(session_id="dismiss-compaction")

            self.assertIsNotNone(request["request"])
            self.assertGreater(request["request"]["source"]["event_count"], 0)

    def test_codex_compaction_purges_only_validated_raw_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionStore(
                Path(temporary),
                compaction_threshold_bytes=16_384,
                compaction_keep_recent_events=4,
            )
            store.create_session("压缩测试", session_id="compact-session")
            for index in range(10):
                store.record_dialogue(f"第 {index} 句 " + ("重要剧情信息。" * 260), speaker="角色A")

            status = store.compaction_status(session_id="compact-session")
            self.assertTrue(status["summary_due"])
            request = store.get_compaction_request(
                max_source_chars=40_000,
                session_id="compact-session",
            )
            self.assertTrue(request["request"])
            source = request["request"]["source"]
            self.assertGreater(source["event_count"], 0)

            frames = Path(temporary) / "compact-session" / "frames"
            orphan_frame = frames / "orphan.png"
            orphan_frame.parent.mkdir(parents=True, exist_ok=True)
            orphan_frame.write_bytes(b"orphan")
            self.assertTrue(orphan_frame.exists())

            saved = store.save_compaction(
                request_id=request["request"]["request_id"],
                summary={
                    "story_summary": "前段确认角色A在放学后提出了关键请求，路线仍需结合后续对白判断。",
                    "key_facts": ["角色A提出关键请求"],
                    "decisions": [],
                    "unresolved_threads": ["后续需要确认请求的结果"],
                    "ocr_uncertainties": [],
                    "loss_notes": [],
                },
                session_id="compact-session",
            )
            self.assertTrue(saved["raw_purged"])
            self.assertEqual(saved["purged_event_count"], source["event_count"])
            segment_path = Path(saved["path"])
            self.assertTrue(segment_path.exists())
            segment_text = segment_path.read_text(encoding="utf-8")
            self.assertNotIn("重要剧情信息。重要剧情信息。重要剧情信息。", segment_text)
            self.assertFalse(orphan_frame.exists())
            self.assertGreaterEqual(saved["raw_artifacts"]["frames_deleted"], 1)

            state = store.get_current_state("compact-session")
            self.assertEqual(state["timeline_count"], 10 - source["event_count"])
            store.record_dialogue("压缩后的新对白", speaker="角色B", session_id="compact-session")
            new_session = store.get_session("compact-session")
            self.assertGreater(new_session["timeline"][-1]["seq"], source["seq_end"])

            context = store.build_context(session_id="compact-session", include_markdown=False, compact=True)
            self.assertEqual(len(context["compacted_summaries"]), 1)
            self.assertIn("角色A", context["compacted_summaries"][0]["summary"]["story_summary"])

            reloaded = SessionStore(Path(temporary), compaction_threshold_bytes=16_384, compaction_keep_recent_events=4)
            reloaded_context = reloaded.build_context(
                session_id="compact-session",
                include_markdown=False,
                compact=True,
            )
            self.assertEqual(len(reloaded_context["compacted_summaries"]), 1)

    def test_event_journal_keeps_checkpoint_small_and_reloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SessionStore(root)
            store.create_session("日志测试", session_id="journal-session")
            for index in range(20):
                store.record_dialogue(
                    f"第 {index} 句 " + ("长对白内容。" * 80),
                    speaker="角色A",
                    session_id="journal-session",
                )

            checkpoint = root / "journal-session" / "session.json"
            journal = root / "journal-session" / "events.jsonl"
            self.assertTrue(journal.exists())
            self.assertLess(checkpoint.stat().st_size, journal.stat().st_size)
            checkpoint_payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(checkpoint_payload["timeline"], [])

            reloaded = SessionStore(root)
            state = reloaded.get_current_state("journal-session")
            self.assertEqual(state["timeline_count"], 20)
            self.assertEqual(state["current_state"]["speaker"], "角色A")


if __name__ == "__main__":
    unittest.main()
