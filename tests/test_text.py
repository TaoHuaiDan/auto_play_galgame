from __future__ import annotations

import unittest

from galgame_mcp.text import detect_screen_type, parse_screen_text


MARKER_PROFILE = {
    "speaker_markers": [
        {"open": "〖", "close": "〗"},
        {"open": "【", "close": "】"},
        {"open": "[", "close": "]"},
        {"open": "〔", "close": "〕", "allow_unclosed": True},
    ],
    "dialogue_markers": [
        {"open": "「", "close": "」"},
        {"open": "『", "close": "』"},
    ],
}
QUOTED_NAME_PROFILE = {
    **MARKER_PROFILE,
    "speaker_markers": [
        *MARKER_PROFILE["speaker_markers"],
        {"open": "『", "close": "』"},
    ],
}
CHOICE_PROFILE = {
    **MARKER_PROFILE,
    "choice_region": {
        "x": 0.20,
        "y": 0.20,
        "width": 0.60,
        "height": 0.48,
        "coordinate_space": "normalized",
    },
    "choice_layout": "vertical",
}


class TextParserTests(unittest.TestCase):
    def test_custom_symbols_are_read_from_runtime_profile(self) -> None:
        profile = {
            "speaker_markers": [{"open": "<N>", "close": "</N>", "allow_unclosed": True}],
            "dialogue_markers": [{"open": "<T>", "close": "</T>"}],
        }
        parsed = parse_screen_text(
            "<N> 旅 人 </N>\n<T> 你好 </T>",
            layout_profile=profile,
        )

        self.assertEqual(parsed["speaker"], "旅人")
        self.assertEqual(parsed["dialogue"], "<T>你好</T>")
        self.assertIsNone(parse_screen_text("<N>旅人</N>\n<T>你好</T>")["speaker"])

    def test_extracts_ocr_spaced_name_label_from_visual_novel_dialogue(self) -> None:
        parsed = parse_screen_text("〖 老 人 〗\n「 那 就 拜 托 了 」", layout_profile=MARKER_PROFILE)

        self.assertEqual(parsed["speaker"], "老人")
        self.assertEqual(parsed["dialogue"], "「那就拜托了」")
        self.assertEqual(parsed["confidence"], 0.86)

    def test_accepts_long_name_labels_without_misreading_quoted_dialogue(self) -> None:
        parsed = parse_screen_text("〖非常长的角色名字〗\n「这是对白」", layout_profile=MARKER_PROFILE)

        self.assertEqual(parsed["speaker"], "非常长的角色名字")
        self.assertEqual(parsed["dialogue"], "「这是对白」")

    def test_treats_quoted_single_line_as_dialogue_without_speaker(self) -> None:
        parsed = parse_screen_text("『先生』", layout_profile=MARKER_PROFILE)

        self.assertIsNone(parsed["speaker"])
        self.assertEqual(parsed["dialogue"], "『先生』")

    def test_uses_following_row_to_detect_quoted_name_label(self) -> None:
        parsed = parse_screen_text("『非常长的角色名字』\n「这是对白」", layout_profile=QUOTED_NAME_PROFILE)

        self.assertEqual(parsed["speaker"], "非常长的角色名字")
        self.assertEqual(parsed["dialogue"], "「这是对白」")

    def test_quoted_dialogue_before_choices_is_not_speaker(self) -> None:
        parsed = parse_screen_text("『先生』\n1. 继续\n2. 返回", layout_profile=MARKER_PROFILE)

        self.assertIsNone(parsed["speaker"])
        self.assertEqual(parsed["dialogue"], "『先生』")
        self.assertEqual(parsed["choices"], ["继续", "返回"])

    def test_detects_centered_unprefixed_choice_buttons_from_ocr_layout(self) -> None:
        raw_text = "干 恋 * 万 花\n。 CHAPTER 1 · 1\n说 实 话\n敷 衍 过 去\n漏 引 佑 rhlfrltl 榭 州 m"
        regions = [
            {"text": "干 恋 * 万 花", "x": 46, "y": 18, "width": 90, "height": 18},
            {"text": "。 CHAPTER 1 · 1", "x": 40, "y": 179, "width": 416, "height": 41},
            {"text": "说 实 话", "x": 1213, "y": 456, "width": 154, "height": 50},
            {"text": "敷 衍 过 去", "x": 1188, "y": 651, "width": 205, "height": 50},
            {"text": "漏 引 佑 rhlfrltl 榭 州 m", "x": 339, "y": 932, "width": 399, "height": 33},
        ]

        parsed = parse_screen_text(
            raw_text,
            regions=regions,
            image_size=(2582, 1550),
            layout_profile=CHOICE_PROFILE,
        )

        self.assertEqual(parsed["choices"], ["说实话", "敷衍过去"])
        self.assertNotIn("说实话", parsed["dialogue"])
        self.assertNotIn("敷衍过去", parsed["dialogue"])

    def test_choice_profile_excludes_prefixed_top_banner(self) -> None:
        parsed = parse_screen_text(
            "- cHAPTER 1 · 1",
            regions=[{"text": "- cHAPTER 1 · 1", "x": 40, "y": 179, "width": 416, "height": 41}],
            image_size=(2582, 1550),
            layout_profile=CHOICE_PROFILE,
        )

        self.assertEqual(parsed["choices"], [])
        self.assertEqual(parsed["unparsed_lines"], ["- cHAPTER 1 · 1"])

    def test_does_not_treat_wrapped_dialogue_crop_as_choice_buttons(self) -> None:
        parsed = parse_screen_text(
            "「专门跑到这地方来看庆典，最近不怕死的人也是越来越多了……」",
            regions=[
                {"text": "「专门跑到这地方", "x": 700, "y": 95, "width": 240, "height": 50},
                {"text": "来看庆典，最近不怕死的人也是", "x": 650, "y": 190, "width": 360, "height": 50},
                {"text": "越来越多了……」", "x": 760, "y": 285, "width": 220, "height": 50},
            ],
            image_size=(1807, 357),
        )

        self.assertEqual(parsed["choices"], [])
        self.assertIn("专门跑到", parsed["dialogue"])

    def test_keeps_wrapped_two_line_dialogue_under_the_name_label(self) -> None:
        parsed = parse_screen_text(
            "〖 芦 花 〗\n"
            "「 别 担 心 ， 没 事 的 。 如 果 您 需 要 喂 奶 的 话 ， 我 们 这 里 可 以 帮 您 泡\n"
            "奶 粉 ， 请 随 意 吩 咐 」",
            layout_profile=MARKER_PROFILE,
        )

        self.assertEqual(parsed["speaker"], "芦花")
        self.assertEqual(
            parsed["dialogue"],
            "「别担心，没事的。如果您需要喂奶的话，我们这里可以帮您泡\n奶粉，请随意吩咐」",
        )

    def test_extracts_speaker_dialogue_and_choices(self) -> None:
        parsed = parse_screen_text(
            "【小葵】\n今日は一緒に帰らない？\n1. はい\n2. いいえ",
            layout_profile=MARKER_PROFILE,
        )
        self.assertEqual(parsed["speaker"], "小葵")
        self.assertEqual(parsed["dialogue"], "今日は一緒に帰らない？")
        self.assertEqual(parsed["choices"], ["はい", "いいえ"])
        self.assertGreater(parsed["confidence"], 0.9)

    def test_keeps_ambiguous_lines(self) -> None:
        parsed = parse_screen_text("???\n1) 继续\n2) 退出")
        self.assertEqual(parsed["speaker"], None)
        self.assertEqual(parsed["choices"], ["继续", "退出"])
        self.assertEqual(parsed["unparsed_lines"], [])

    def test_detects_settings_page_with_spaced_ocr(self) -> None:
        raw_text = "系 统 设 置\n显 示 模 式\n画 面 比 例\n动 画 效 果"
        self.assertEqual(detect_screen_type(raw_text), "settings")

    def test_does_not_treat_settings_word_in_dialogue_as_menu(self) -> None:
        self.assertIsNone(detect_screen_type("我们稍后再设置这个计划。"))

    def test_detects_system_menu_alias(self) -> None:
        self.assertEqual(detect_screen_type("SYSTEM MENU"), "settings")

    def test_parser_exposes_settings_screen_type(self) -> None:
        parsed = parse_screen_text("系统设置\n显示模式\n画面比例")
        self.assertEqual(parsed["screen_type"], "settings")

    def test_exposes_non_destructive_ocr_noise_flags(self) -> None:
        parsed = parse_screen_text(
            "skip\n�\n〖 老 人 〗\n「 那 就 拜 托 了 」",
            layout_profile=MARKER_PROFILE,
        )

        codes = {flag["code"] for flag in parsed["noise_flags"]}
        self.assertIn("separator_or_ui", codes)
        self.assertIn("replacement_character", codes)
        self.assertIn("spacing_artifact", codes)
        self.assertIn("�", parsed["raw_text"])
        self.assertEqual(parsed["speaker"], "老人")

    def test_filters_short_ui_residue_without_deleting_raw_text(self) -> None:
        parsed = parse_screen_text(
            "SAVE LOAD Q.SAVE Q.LOAD\nVOICE\nV创0\n"
            "SAVE LOAD Q.SAVE Q.LOAD SYSTEM 《，》 > 羽《，； 0 ×\n"
            "VOlCf\nVO!CF\nVO'CE\n00000"
        )

        self.assertEqual(parsed["dialogue"], "")
        self.assertEqual(parsed["text_status"], "ui_only")
        self.assertEqual(
            parsed["ui_lines"],
            [
                "SAVE LOAD Q.SAVE Q.LOAD",
                "VOICE",
                "V创0",
                "SAVE LOAD Q.SAVE Q.LOAD SYSTEM 《，》 > 羽《，； 0 ×",
                "VOlCf",
                "VO!CF",
                "VO'CE",
                "00000",
            ],
        )
        self.assertIn("SAVE LOAD", parsed["raw_text"])

    def test_keeps_short_story_marker_out_of_ui_residue_filter(self) -> None:
        parsed = parse_screen_text("「先生」", layout_profile=MARKER_PROFILE)

        self.assertEqual(parsed["dialogue"], "「先生」")
        self.assertEqual(parsed["text_status"], "recognized")
        self.assertEqual(parsed["ui_lines"], [])

    def test_keeps_plain_short_english_dialogue(self) -> None:
        parsed = parse_screen_text("yes")

        self.assertEqual(parsed["dialogue"], "yes")
        self.assertEqual(parsed["text_status"], "recognized")

    def test_configured_marker_profile_does_not_guess_unmarked_colon_ui(self) -> None:
        parsed = parse_screen_text(
            "0」RIDDLE」OKER: C Search for RiddIe",
            layout_profile=MARKER_PROFILE,
        )

        self.assertIsNone(parsed["speaker"])
        self.assertEqual(parsed["dialogue"], "")
        self.assertEqual(parsed["text_status"], "unknown")
        self.assertEqual(parsed["unknown_lines"], ["0」RIDDLE」OKER: C Search for RiddIe"])


if __name__ == "__main__":
    unittest.main()
