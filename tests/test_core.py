from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from galgame_mcp.core import SessionStore


class SessionStoreTests(unittest.TestCase):
    def test_record_context_export_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SessionStore(root)
            summary = store.create_session("测试视觉小说", session_id="test-session")
            self.assertEqual(summary["session_id"], "test-session")

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


if __name__ == "__main__":
    unittest.main()
