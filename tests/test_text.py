from __future__ import annotations

import unittest

from galgame_mcp.text import parse_screen_text


class TextParserTests(unittest.TestCase):
    def test_extracts_speaker_dialogue_and_choices(self) -> None:
        parsed = parse_screen_text("【小葵】\n今日は一緒に帰らない？\n1. はい\n2. いいえ")
        self.assertEqual(parsed["speaker"], "小葵")
        self.assertEqual(parsed["dialogue"], "今日は一緒に帰らない？")
        self.assertEqual(parsed["choices"], ["はい", "いいえ"])
        self.assertGreater(parsed["confidence"], 0.9)

    def test_keeps_ambiguous_lines(self) -> None:
        parsed = parse_screen_text("???\n1) 继续\n2) 退出")
        self.assertEqual(parsed["speaker"], None)
        self.assertEqual(parsed["choices"], ["继续", "退出"])
        self.assertEqual(parsed["unparsed_lines"], [])


if __name__ == "__main__":
    unittest.main()
