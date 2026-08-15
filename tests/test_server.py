from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import galgame_mcp.server as server_module
from galgame_mcp.server import (
    _capture_for_session,
    _auto_return_from_settings,
    _batch_dialogue_item,
    _bottom_text_snapshot,
    _bottom_story_hash,
    _compare_bottom_text,
    _filter_ocr_result_to_region,
    _fast_capture_has_text,
    _layout_profile_for_capture,
    _process_local_text,
    _choice_click_point_from_payload,
    _probe_full_window_for_choices,
    _window_center_screen_point,
    _wait_for_text_hash_stable,
    attach_game as server_attach_game,
    advance_game as server_advance_game,
    background_click as server_background_click,
    background_press_key as server_background_press_key,
    background_scroll as server_background_scroll,
    click_screen as server_click_screen,
    perform_game_action as server_perform_game_action,
    play_until_choice as server_play_until_choice,
    observe_game as server_observe_game,
    select_choice as server_select_choice,
)


class TimingProfileTests(unittest.TestCase):
    @staticmethod
    def _frame(text: str, path: str) -> tuple[dict[str, object], Path]:
        return (
            {
                "capture_scope": "window_dialogue_region",
                "width": 600,
                "height": 180,
                "ocr": {"available": True},
                "_ocr_regions": [
                    {"text": text, "x": 20, "y": 40, "width": 300, "height": 30}
                ],
                "processed_text": {
                    "speaker": "角色A",
                    "dialogue": text,
                    "choices": [],
                },
            },
            Path(path),
        )

    def test_bottom_story_hash_ignores_ui_residue_and_whitespace(self) -> None:
        first = {
            "capture_scope": "window_dialogue_region",
            "height": 180,
            "ocr": {"available": True},
            "_ocr_regions": [
                {"text": "  新  的  台词 ", "y": 40, "height": 20},
                {"text": "SAVE", "y": 140, "height": 20},
            ],
        }
        second = {
            **first,
            "_ocr_regions": [
                {"text": "新的台词", "y": 40, "height": 20},
                {"text": "AUTO", "y": 140, "height": 20},
            ],
        }
        self.assertEqual(_bottom_story_hash(first), _bottom_story_hash(second))

    def test_wait_for_text_hash_stable_requires_change_and_repeated_hash(self) -> None:
        first_payload, first_path = self._frame("旧台词", "C:/old.png")
        partial_payload, partial_path = self._frame("新", "C:/partial.png")
        stable_payload, stable_path = self._frame("新的完整台词", "C:/stable.png")
        captures = [partial_payload, stable_payload, stable_payload]

        with patch(
            "galgame_mcp.server._capture_processed_frame",
            side_effect=[
                (item, path)
                for item, path in zip(
                    captures,
                    [partial_path, stable_path, stable_path],
                )
            ],
        ) as capture, patch(
            "galgame_mcp.server._auto_return_from_settings",
            side_effect=[
                (partial_payload, partial_path),
                (stable_payload, stable_path),
                (stable_payload, stable_path),
            ],
        ), patch("galgame_mcp.server.time.sleep"):
            payload, image_path, result = _wait_for_text_hash_stable(
                first_payload=first_payload,
                first_image_path=first_path,
                before_snapshot={
                    "available": True,
                    "detected": True,
                    "text": "旧台词",
                },
                timing={
                    "strategy": "text_hash",
                    "settle_timeout_seconds": 2.0,
                    "settle_poll_seconds": 0.02,
                    "stable_samples": 2,
                    "require_text_change": True,
                },
                window_title="测试游戏",
                capture_mode="window",
                session={"session_id": "test-session"},
                language="auto",
                include_raw_text=False,
                ocr_region=None,
                include_image=False,
                action_event=None,
                background=True,
                background_input_method="send",
                auto_return_from_settings=True,
            )

        self.assertTrue(result["settled"])
        self.assertEqual(result["reason"], "text_hash_stable")
        self.assertEqual(result["extra_frames"], 3)
        self.assertEqual(payload, stable_payload)
        self.assertEqual(image_path, stable_path)
        self.assertEqual(capture.call_count, 3)

    def test_play_stops_without_extra_click_when_settle_times_out(self) -> None:
        session = {
            "session_id": "test-session",
            "game": {
                "window_title": "测试游戏",
                "control": {"advance_key": "SPACE"},
                "timing_profile": {
                    "strategy": "text_hash",
                    "settle_timeout_seconds": 0.0,
                    "stable_samples": 2,
                    "require_text_change": True,
                },
            },
            "current_state": {},
        }
        old_frame, old_path = self._frame("仍未变化", "C:/old.png")
        actions: list[dict[str, object]] = []

        with patch("galgame_mcp.server.STORE.get_session", return_value=session), patch(
            "galgame_mcp.server._capture_processed_frame",
            side_effect=[(old_frame, old_path), (old_frame, old_path)],
        ), patch(
            "galgame_mcp.server._auto_return_from_settings",
            side_effect=lambda payload, *_args, **_kwargs: (payload, old_path),
        ), patch(
            "galgame_mcp.server._advance_input_for_batch",
            return_value=("background_click", {"queued": True}),
        ), patch(
            "galgame_mcp.server.STORE.record_action",
            side_effect=lambda *_args, **kwargs: actions.append(kwargs) or {"event_id": "action-1"},
        ), patch("galgame_mcp.server._remember_bottom_snapshot"):
            result = server_play_until_choice(
                max_steps=3,
                wait_seconds=0,
                record_text=False,
                session_id="test-session",
            )

        if isinstance(result, list):
            result = json.loads(result[0])
        self.assertEqual(result["stop_reason"], "timing_settle_timeout")
        self.assertEqual(result["steps_advanced"], 1)
        self.assertEqual(len(actions), 1)
        self.assertEqual(result["timing"]["timeout_checks"], 1)


class SettingsRecoveryTests(unittest.TestCase):
    def _arguments(self) -> dict[str, object]:
        return {
            "title": "测试游戏",
            "capture_mode": "window",
            "ocr": False,
            "record_text": False,
            "language": "auto",
            "include_raw_text": False,
            "enabled": True,
        }

    def test_does_not_guess_when_return_button_is_missing(self) -> None:
        payload = {
            "screen_type": "settings",
            "image_path": "C:/frame.png",
            "window": {"x": 0, "y": 0},
            "_ocr_regions": [{"text": "系统设置", "x": 10, "y": 10, "width": 100, "height": 20}],
        }

        with patch("galgame_mcp.server.native_focus_window") as focus, patch(
            "galgame_mcp.server.native_click_screen"
        ) as click:
            updated, image_path = _auto_return_from_settings(
                payload,
                {"session_id": "test-session"},
                **self._arguments(),
            )

        self.assertEqual(image_path, Path("C:/frame.png"))
        self.assertFalse(updated["auto_recovery"]["returned"])
        self.assertEqual(updated["auto_recovery"]["reason"], "return_button_not_detected")
        focus.assert_not_called()
        click.assert_not_called()

    def test_clicks_only_an_explicit_return_button(self) -> None:
        payload = {
            "screen_type": "settings",
            "image_path": "C:/settings.png",
            "window": {"x": 100, "y": 50},
            "_ocr_regions": [
                {"text": "回 到 游 戏", "x": 200, "y": 300, "width": 120, "height": 40}
            ],
        }
        followup_path = Path("C:/after-settings.png")

        with patch(
            "galgame_mcp.server.native_focus_window", return_value={"hwnd": 1, "title": "测试游戏"}
        ), patch(
            "galgame_mcp.server.native_click_screen", return_value={"clicked": True}
        ) as click, patch(
            "galgame_mcp.server.STORE.record_action", return_value={"event_id": "action-1"}
        ), patch(
            "galgame_mcp.server._capture_for_session",
            return_value=({"image_path": str(followup_path), "window": {}}, followup_path),
        ), patch(
            "galgame_mcp.server._process_local_text", side_effect=lambda item, *_args, **_kwargs: item
        ):
            updated, image_path = _auto_return_from_settings(
                payload,
                {"session_id": "test-session"},
                **self._arguments(),
            )

        self.assertEqual(image_path, followup_path)
        self.assertTrue(updated["auto_recovery"]["returned"])
        click.assert_called_once_with(x=360, y=370, button="left", clicks=1, interval_ms=0)


class AttachGameTests(unittest.TestCase):
    def _session(self) -> dict[str, object]:
        return {"session_id": "test-session"}

    def _configuration(self) -> dict[str, object]:
        return {"game": {"window_title": "测试游戏"}}

    def test_attach_does_not_focus_by_default(self) -> None:
        window = {"hwnd": 7, "title": "测试游戏", "x": 0, "y": 0, "width": 1000, "height": 800}
        with patch.object(server_module.STORE, "get_session", return_value=self._session()), patch.object(
            server_module.STORE, "configure_game", return_value=self._configuration()
        ), patch.object(server_module.STORE, "record_action", return_value={"event_id": "attach-1"}), patch(
            "galgame_mcp.server.native_get_window_rect", return_value=window
        ) as get_rect, patch("galgame_mcp.server.native_focus_window") as focus:
            result = server_attach_game(window_title="测试游戏", session_id="test-session")

        get_rect.assert_called_once_with("测试游戏")
        focus.assert_not_called()
        self.assertIsNone(result["focus"])
        self.assertFalse(result["focus_requested"])
        self.assertEqual(result["window"], window)

    def test_attach_focus_requires_explicit_opt_in(self) -> None:
        window = {"hwnd": 7, "title": "测试游戏"}
        with patch.object(server_module.STORE, "get_session", return_value=self._session()), patch.object(
            server_module.STORE, "configure_game", return_value=self._configuration()
        ), patch.object(server_module.STORE, "record_action", return_value={"event_id": "attach-1"}), patch(
            "galgame_mcp.server.native_focus_window", return_value=window
        ) as focus, patch("galgame_mcp.server.native_get_window_rect") as get_rect:
            result = server_attach_game(
                window_title="测试游戏",
                session_id="test-session",
                focus_window=True,
            )

        focus.assert_called_once_with("测试游戏")
        get_rect.assert_not_called()
        self.assertEqual(result["focus"], window)
        self.assertTrue(result["focus_requested"])


class ForegroundSafetyTests(unittest.TestCase):
    def _session(self) -> dict[str, object]:
        return {"session_id": "test-session", "game": {"window_title": "测试游戏"}}

    def _payload(self) -> dict[str, object]:
        return {
            "image_path": "C:/frame.png",
            "width": 1000,
            "height": 800,
            "window": {"x": 0, "y": 0, "width": 1000, "height": 800},
            "ocr": {"available": False},
        }

    def test_observe_does_not_focus_by_default_even_for_desktop_capture(self) -> None:
        payload = self._payload()
        with patch.object(server_module.STORE, "get_session", return_value=self._session()), patch(
            "galgame_mcp.server.native_focus_window"
        ) as focus, patch(
            "galgame_mcp.server._capture_for_session",
            return_value=(payload, Path("C:/frame.png")),
        ), patch(
            "galgame_mcp.server._process_capture_text", side_effect=lambda item, *_args, **_kwargs: item
        ), patch(
            "galgame_mcp.server._auto_return_from_settings",
            side_effect=lambda item, *_args, **_kwargs: (item, Path("C:/frame.png")),
        ), patch("galgame_mcp.server._remember_bottom_snapshot"):
            server_observe_game(
                capture_mode="desktop",
                ocr=False,
                record_text=False,
                session_id="test-session",
            )

        focus.assert_not_called()

    def test_observe_focus_is_explicit_opt_in(self) -> None:
        payload = self._payload()
        with patch.object(server_module.STORE, "get_session", return_value=self._session()), patch(
            "galgame_mcp.server.native_focus_window", return_value={"hwnd": 7, "title": "测试游戏"}
        ) as focus, patch(
            "galgame_mcp.server._capture_for_session",
            return_value=(payload, Path("C:/frame.png")),
        ), patch(
            "galgame_mcp.server._process_capture_text", side_effect=lambda item, *_args, **_kwargs: item
        ), patch(
            "galgame_mcp.server._auto_return_from_settings",
            side_effect=lambda item, *_args, **_kwargs: (item, Path("C:/frame.png")),
        ), patch("galgame_mcp.server._remember_bottom_snapshot"), patch.object(
            server_module.STORE, "record_action", return_value={"event_id": "focus-1"}
        ):
            server_observe_game(
                capture_mode="desktop",
                focus_before_capture=True,
                ocr=False,
                record_text=False,
                session_id="test-session",
            )

        focus.assert_called_once_with("测试游戏")


class FastWindowCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        server_module._WINDOW_FULL_CAPTURE_CACHE.clear()

    def tearDown(self) -> None:
        server_module._WINDOW_FULL_CAPTURE_CACHE.clear()

    def test_first_window_frame_is_full_and_followup_uses_dialogue_region(self) -> None:
        session = {"session_id": "test-session"}
        full_dimensions = {
            "x": 10,
            "y": 20,
            "width": 1000,
            "height": 800,
            "hwnd": 7,
            "title": "测试游戏",
            "capture_method": "PrintWindow",
            "occluded_capture": True,
            "minimized": False,
        }
        region_dimensions = {
            "x": 10,
            "y": 20,
            "width": 700,
            "height": 184,
            "hwnd": 7,
            "title": "测试游戏",
            "capture_method": "window_region_bitblt",
            "occluded_capture": False,
            "minimized": False,
        }
        with patch.object(
            server_module.STORE, "get_session", return_value=session
        ), patch.object(
            server_module.STORE, "session_dir", return_value=Path("C:/test-session")
        ), patch.object(
            server_module.STORE,
            "record_screenshot",
            return_value={"event_id": "screenshot-1"},
        ), patch(
            "galgame_mcp.server.capture_window_png",
            return_value=(b"full", full_dimensions),
        ) as full_capture, patch(
            "galgame_mcp.server.native_get_window_rect",
            return_value={"x": 10, "y": 20, "width": 1000, "height": 800, "hwnd": 7, "title": "测试游戏"},
        ), patch(
            "galgame_mcp.server.capture_window_region_png",
            return_value=(b"region", region_dimensions),
        ) as region_capture, patch(
            "pathlib.Path.mkdir"
        ), patch(
            "pathlib.Path.write_bytes"
        ):
            first, _ = _capture_for_session(
                window_title="测试游戏",
                capture_mode="window",
                session_id="test-session",
                fast_region={
                    "x": 0.1,
                    "y": 0.7,
                    "width": 0.7,
                    "height": 0.23,
                    "coordinate_space": "normalized",
                },
            )
            second, _ = _capture_for_session(
                window_title="测试游戏",
                capture_mode="window",
                session_id="test-session",
                fast_region={
                    "x": 0.1,
                    "y": 0.7,
                    "width": 0.7,
                    "height": 0.23,
                    "coordinate_space": "normalized",
                },
            )

        self.assertEqual(first["capture_scope"], "window_full")
        self.assertEqual((first["width"], first["height"]), (1000, 800))
        self.assertEqual(second["capture_scope"], "window_dialogue_region")
        self.assertEqual((second["width"], second["height"]), (700, 184))
        self.assertEqual(second["window"]["width"], 1000)
        self.assertEqual(second["window"]["height"], 800)
        self.assertEqual(second["capture_region"], {
            "x": 100,
            "y": 560,
            "width": 700,
            "height": 184,
            "coordinate_space": "window_pixels",
        })
        full_capture.assert_called_once_with("测试游戏")
        region_capture.assert_called_once_with("测试游戏", 100, 560, 700, 184)

    def test_region_frame_keeps_all_local_dialogue_rows_for_verification(self) -> None:
        snapshot = _bottom_text_snapshot(
            {
                "capture_scope": "window_dialogue_region",
                "width": 700,
                "height": 184,
                "ocr": {"available": True},
                "_dialogue_ocr_regions": [
                    {"text": "人物名", "x": 20, "y": 20, "width": 80, "height": 20},
                    {"text": "对白内容", "x": 20, "y": 80, "width": 200, "height": 30},
                ],
            }
        )
        self.assertTrue(snapshot["detected"])
        self.assertEqual(snapshot["text"], "人物名\n对白内容")
        self.assertEqual(snapshot["threshold_y"], 0)

    def test_region_ocr_coordinates_are_translated_for_settings_recovery(self) -> None:
        payload = {
            "capture_scope": "window_dialogue_region",
            "capture_region": {"x": 100, "y": 500},
            "width": 700,
            "height": 184,
            "window": {"x": 10, "y": 20, "width": 1000, "height": 800},
        }
        session = {"session_id": "test-session", "current_state": {}}
        ocr_result = {
            "available": True,
            "status": "ok",
            "backend": "windows_ocr",
            "text": "回到游戏",
            "regions": [{"text": "回到游戏", "x": 12, "y": 24, "width": 100, "height": 20}],
        }
        with patch("galgame_mcp.server.native_ocr_image", return_value=ocr_result):
            processed = _process_local_text(
                payload,
                Path("C:/region.png"),
                session,
                ocr=True,
                record_text=False,
                language="auto",
                include_raw_text=False,
                ocr_region=None,
            )

        self.assertEqual(processed["_dialogue_ocr_regions"][0]["x"], 12)
        self.assertEqual(processed["_dialogue_ocr_regions"][0]["y"], 24)
        self.assertEqual(processed["_ocr_regions"][0]["x"], 112)
        self.assertEqual(processed["_ocr_regions"][0]["y"], 524)

    def test_fast_crop_ui_residue_requires_full_frame_fallback(self) -> None:
        self.assertFalse(
            _fast_capture_has_text(
                {
                    "capture_scope": "window_dialogue_region",
                    "processed_text": {"speaker": None, "dialogue": "VOICE", "choices": []},
                }
            )
        )
        self.assertFalse(
            _fast_capture_has_text(
                {
                    "capture_scope": "window_dialogue_region",
                    "processed_text": {"speaker": None, "dialogue": "AUTO 1", "choices": []},
                }
            )
        )
        self.assertFalse(
            _fast_capture_has_text(
                {
                    "capture_scope": "window_dialogue_region",
                    "processed_text": {"speaker": "将臣", "dialogue": "", "choices": []},
                }
            )
        )
        self.assertTrue(
            _fast_capture_has_text(
                {
                    "capture_scope": "window_dialogue_region",
                    "processed_text": {"speaker": None, "dialogue": "「恢复后的对白」", "choices": []},
                }
            )
        )
        self.assertIsNone(
            _batch_dialogue_item(
                {
                    "processed_text": {"speaker": None, "dialogue": "AUTO 1", "choices": []},
                },
                1,
            )
        )

    def test_full_frame_fallback_keeps_dialogue_outside_crop(self) -> None:
        payload = {
            "capture_scope": "window_full",
            "width": 1000,
            "height": 800,
            "window": {"x": 0, "y": 0, "width": 1000, "height": 800},
        }
        session = {"session_id": "test-session", "current_state": {}}
        ocr_result = {
            "available": True,
            "status": "ok",
            "backend": "windows_ocr",
            "text": "「场景中的旁白」",
            "regions": [
                {"text": "「场景中的旁白」", "x": 200, "y": 300, "width": 200, "height": 30}
            ],
        }
        with patch("galgame_mcp.server.native_ocr_image", return_value=ocr_result):
            processed = _process_local_text(
                payload,
                Path("C:/full.png"),
                session,
                ocr=True,
                record_text=False,
                language="auto",
                include_raw_text=False,
                ocr_region={
                    "x": 0.10,
                    "y": 0.70,
                    "width": 0.70,
                    "height": 0.23,
                    "coordinate_space": "normalized",
                },
            )

        self.assertEqual(processed["processed_text"]["dialogue"], "「场景中的旁白」")

    def test_layout_profile_projects_dialogue_relative_regions_to_fast_capture(self) -> None:
        session = {
            "game": {
                "layout_profile": {
                    "dialogue_region": {
                        "x": 0.10,
                        "y": 0.70,
                        "width": 0.80,
                        "height": 0.25,
                        "coordinate_space": "normalized",
                    },
                    "speaker_region": {
                        "x": 0,
                        "y": 0,
                        "width": 1,
                        "height": 0.40,
                        "coordinate_space": "dialogue_region",
                    },
                    "choice_region": {
                        "x": 0.20,
                        "y": 0.20,
                        "width": 0.60,
                        "height": 0.50,
                        "coordinate_space": "normalized",
                    },
                }
            }
        }
        payload = {
            "capture_scope": "window_dialogue_region",
            "width": 800,
            "height": 200,
            "capture_region": {"x": 100, "y": 560, "width": 800, "height": 200},
            "window": {"width": 1000, "height": 800},
        }

        projected = _layout_profile_for_capture(session, payload)

        self.assertEqual(projected["speaker_region"]["coordinate_space"], "pixels")
        self.assertEqual(projected["speaker_region"]["x"], 0.0)
        self.assertEqual(projected["speaker_region"]["y"], 0.0)
        self.assertEqual(projected["speaker_region"]["width"], 800.0)
        self.assertEqual(projected["speaker_region"]["height"], 80.0)
        self.assertEqual(projected["choice_region"]["x"], 100.0)
        self.assertEqual(projected["choice_region"]["y"], -400.0)


class InputVerificationTests(unittest.TestCase):
    def test_background_advance_sends_click_without_background_key(self) -> None:
        session = {
            "session_id": "test-session",
            "game": {
                "window_title": "测试游戏",
                "control": {"advance_key": "SPACE", "advance_hold_seconds": 0.0},
            },
        }
        frame_path = Path("C:/after.png")
        payload = {
            "image_path": str(frame_path),
            "width": 1000,
            "height": 800,
            "window": {"x": 0, "y": 0, "width": 1000, "height": 800},
            "ocr": {"available": False},
        }

        with patch("galgame_mcp.server.STORE.get_session", return_value=session), patch(
            "galgame_mcp.server.native_get_window_rect",
            return_value={"x": 0, "y": 0, "width": 1000, "height": 800},
        ), patch(
            "galgame_mcp.server.native_post_window_click",
            return_value={"button": "left", "background": True},
        ) as click, patch(
            "galgame_mcp.server.native_post_window_key"
        ) as key, patch(
            "galgame_mcp.server.STORE.record_action",
            return_value={"event_id": "action-1"},
        ), patch(
            "galgame_mcp.server._capture_for_session",
            return_value=(payload, frame_path),
        ), patch(
            "galgame_mcp.server._process_local_text",
            side_effect=lambda item, *_args, **_kwargs: item,
        ), patch(
            "galgame_mcp.server._auto_return_from_settings",
            side_effect=lambda item, *_args, **_kwargs: (item, frame_path),
        ):
            result = server_advance_game(
                background=True,
                ocr=False,
                record_text=False,
                wait_seconds=0,
                session_id="test-session",
            )

        click.assert_called_once_with(
            title="测试游戏",
            x=500,
            y=400,
            button="left",
            clicks=1,
            interval_ms=0,
            delivery="send",
        )
        key.assert_not_called()
        self.assertEqual(result["input_verification"]["reason"], "baseline_not_captured")

    def test_background_advance_uses_window_center(self) -> None:
        self.assertEqual(
            _window_center_screen_point(
                {"window": {"x": -11, "y": -11, "width": 2582, "height": 1550}}
            ),
            (1280, 764),
        )
        self.assertIsNone(_window_center_screen_point({"window": {"x": 0}}))

    def test_advance_waits_for_transient_transition_before_reporting_blank(self) -> None:
        session = {
            "session_id": "test-session",
            "game": {
                "window_title": "测试游戏",
                "control": {"advance_key": "SPACE", "advance_hold_seconds": 0.0},
            },
        }
        window = {"x": 0, "y": 0, "width": 1000, "height": 800}
        before = {
            "image_path": "C:/before.png",
            "capture_scope": "window_dialogue_region",
            "width": 800,
            "height": 200,
            "window": window,
            "ocr": {"available": True},
            "_dialogue_ocr_regions": [{"text": "旧对白", "y": 40, "height": 40}],
            "processed_text": {"speaker": "将臣", "dialogue": "旧对白", "choices": []},
        }
        blank = {
            **before,
            "image_path": "C:/blank.png",
            "_dialogue_ocr_regions": [],
            "processed_text": {"speaker": None, "dialogue": "", "choices": []},
        }
        settled = {
            **before,
            "image_path": "C:/settled.png",
            "_dialogue_ocr_regions": [{"text": "新对白", "y": 40, "height": 40}],
            "processed_text": {"speaker": "将臣", "dialogue": "新对白", "choices": []},
        }

        with patch("galgame_mcp.server.STORE.get_session", return_value=session), patch(
            "galgame_mcp.server.native_post_window_click",
            return_value={"button": "left", "background": True},
        ), patch(
            "galgame_mcp.server.STORE.record_action", return_value={"event_id": "action-1"}
        ), patch(
            "galgame_mcp.server._capture_for_session",
            side_effect=[(before, Path("C:/before.png")), (blank, Path("C:/blank.png"))],
        ), patch(
            "galgame_mcp.server._process_capture_text", side_effect=lambda payload, *_args, **_kwargs: payload
        ), patch(
            "galgame_mcp.server._capture_processed_frame",
            return_value=(settled, Path("C:/settled.png")),
        ) as retry_capture, patch(
            "galgame_mcp.server._auto_return_from_settings",
            side_effect=lambda payload, *_args, **_kwargs: (payload, Path(str(payload["image_path"]))),
        ), patch("galgame_mcp.server._remember_bottom_snapshot"):
            result = server_advance_game(
                background=True,
                ocr=True,
                record_text=False,
                wait_seconds=0,
                transition_wait_seconds=0.25,
                session_id="test-session",
            )

        retry_capture.assert_called_once()
        self.assertTrue(result["input_verification"]["changed"])
        self.assertTrue(result["input_verification"]["transition_settled"])
        self.assertEqual(result["input_verification"]["settle_retries"], 1)

    def test_play_until_choice_waits_through_transition_without_extra_click(self) -> None:
        session = {
            "session_id": "test-session",
            "game": {"window_title": "测试游戏", "control": {"advance_key": "SPACE"}},
            "current_state": {},
        }
        frames = [
            {
                "ocr": {"available": True},
                "processed_text": {"speaker": None, "dialogue": "", "choices": []},
            },
            {
                "ocr": {"available": True},
                "_dialogue_ocr_regions": [{"text": "转场后的对白", "y": 40, "height": 40}],
                "processed_text": {"speaker": "将臣", "dialogue": "转场后的对白", "choices": []},
            },
            {
                "ocr": {"available": True},
                "processed_text": {"speaker": None, "dialogue": "", "choices": ["继续", "返回"]},
            },
        ]
        capture_index = 0
        actions: list[dict[str, object]] = []

        def capture_frame(**_kwargs: object) -> tuple[dict[str, object], Path]:
            nonlocal capture_index
            index = min(capture_index, len(frames) - 1)
            capture_index += 1
            return frames[index], Path(f"C:/transition-{index}.png")

        with patch("galgame_mcp.server.STORE.get_session", return_value=session), patch(
            "galgame_mcp.server._capture_processed_frame", side_effect=capture_frame
        ), patch(
            "galgame_mcp.server._auto_return_from_settings",
            side_effect=lambda payload, *_args, **_kwargs: (payload, Path(f"C:/{capture_index}.png")),
        ), patch(
            "galgame_mcp.server._advance_input_for_batch",
            return_value=("background_click", {"queued": True}),
        ), patch(
            "galgame_mcp.server.STORE.record_action",
            side_effect=lambda *args, **kwargs: actions.append({"args": args, **kwargs})
            or {"event_id": "action-1"},
        ), patch("galgame_mcp.server._remember_bottom_snapshot"):
            result = server_play_until_choice(
                max_steps=3,
                wait_seconds=0,
                transition_wait_seconds=0.25,
                record_text=False,
                session_id="test-session",
            )

        self.assertEqual(result["stop_reason"], "choice_detected")
        self.assertEqual(result["steps_advanced"], 1)
        self.assertEqual(len(actions), 1)
        self.assertGreater(result["transition_wait"]["waited_seconds"], 0)
        self.assertEqual(result["batch"][0]["dialogue"], "转场后的对白")

    def test_normalized_ocr_region_keeps_dialogue_and_excludes_ui(self) -> None:
        result = {
            "available": True,
            "status": "ok",
            "backend": "windows_ocr",
            "text": "scene\ndialogue\nui",
            "regions": [
                {"text": "scene", "x": 100, "y": 100, "width": 80, "height": 20},
                {"text": "dialogue", "x": 600, "y": 1200, "width": 300, "height": 50},
                {"text": "ui", "x": 1000, "y": 1460, "width": 200, "height": 30},
            ],
        }

        filtered = _filter_ocr_result_to_region(
            result,
            {
                "x": 0.18,
                "y": 0.70,
                "width": 0.70,
                "height": 0.23,
                "coordinate_space": "normalized",
            },
            width=2582,
            height=1550,
        )

        self.assertEqual(filtered["text"], "dialogue")
        self.assertEqual([item["text"] for item in filtered["regions"]], ["dialogue"])
        self.assertEqual(
            filtered["ocr_region"],
            {"x": 465, "y": 1085, "width": 1807, "height": 357, "coordinate_space": "pixels"},
        )

    def test_verifies_dialogue_box_delta_not_scene_text(self) -> None:
        before = {
            "height": 1000,
            "ocr": {"available": True},
            "_ocr_regions": [
                {"text": "人物名", "x": 100, "y": 120, "width": 80, "height": 20},
                {"text": "上一句对白", "x": 100, "y": 760, "width": 260, "height": 30},
            ],
        }
        after = {
            "height": 1000,
            "ocr": {"available": True},
            "_ocr_regions": [
                {"text": "人物名", "x": 100, "y": 120, "width": 80, "height": 20},
                {"text": "下一句对白", "x": 100, "y": 760, "width": 260, "height": 30},
            ],
        }

        result = _compare_bottom_text(_bottom_text_snapshot(before), after)

        self.assertTrue(result["available"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["reason"], "bottom_textbox_changed")
        self.assertEqual(result["before"]["char_count"], len("上一句对白"))
        self.assertEqual(result["after"]["char_count"], len("下一句对白"))

    def test_temporary_blank_dialogue_is_not_counted_as_advance(self) -> None:
        before = {
            "height": 1000,
            "ocr": {"available": True},
            "_ocr_regions": [{"text": "上一句对白", "y": 760, "height": 30}],
        }
        after = {
            "height": 1000,
            "ocr": {"available": True},
            "_ocr_regions": [],
        }

        result = _compare_bottom_text(_bottom_text_snapshot(before), after)

        self.assertTrue(result["available"])
        self.assertFalse(result["changed"])
        self.assertEqual(result["reason"], "bottom_textbox_not_detected")

    def test_click_screen_can_explicitly_use_touch_backend(self) -> None:
        with patch(
            "galgame_mcp.server.native_touch_screen",
            return_value={"input_method": "touch", "touch_events_sent": 2},
        ) as touch:
            result = server_click_screen(
                x=850,
                y=900,
                input_method="touch",
                record=False,
            )

        touch.assert_called_once_with(x=850, y=900, taps=1, hold_ms=80, interval_ms=100)
        self.assertEqual(result["input_method"], "touch")

    def test_background_press_key_dispatches_to_window_message_backend(self) -> None:
        with patch(
            "galgame_mcp.server.native_post_window_key",
            return_value={"input_method": "window_message", "queued": True},
        ) as post:
            result = server_background_press_key(
                window_title="测试游戏",
                key="SPACE",
                record=False,
            )

        post.assert_called_once_with(
            title="测试游戏", key="SPACE", presses=1, interval_ms=80, delivery="post"
        )
        self.assertTrue(result["queued"])

    def test_background_scroll_dispatches_to_window_message_backend(self) -> None:
        with patch(
            "galgame_mcp.server.native_post_window_wheel",
            return_value={"input_method": "window_message", "queued": True},
        ) as wheel:
            result = server_background_scroll(
                window_title="测试游戏",
                direction="down",
                x=850,
                y=900,
                record=False,
            )

        wheel.assert_called_once_with(
            title="测试游戏",
            x=850,
            y=900,
            delta=-120,
            clicks=1,
            interval_ms=100,
            delivery="post",
        )
        self.assertTrue(result["queued"])

    def test_background_click_dispatches_to_window_message_backend(self) -> None:
        with patch(
            "galgame_mcp.server.native_post_window_click",
            return_value={"input_method": "window_message", "queued": True},
        ) as post:
            result = server_background_click(
                window_title="测试游戏",
                x=850,
                y=900,
                record=False,
            )

        post.assert_called_once_with(
            title="测试游戏",
            x=850,
            y=900,
            button="left",
            clicks=1,
            interval_ms=100,
            delivery="post",
        )
        self.assertTrue(result["queued"])

    def test_named_action_profile_dispatches_background_click_at_window_center(self) -> None:
        session = {
            "session_id": "test-session",
            "game": {
                "window_title": "测试游戏",
                "action_profile": {
                    "hide_ui": {
                        "kind": "click",
                        "target": "window_center",
                        "button": "right",
                        "delivery": "send",
                    }
                },
            },
        }
        with patch("galgame_mcp.server.STORE.get_session", return_value=session), patch(
            "galgame_mcp.server.native_get_window_rect",
            return_value={"x": 100, "y": 50, "width": 1000, "height": 800},
        ), patch(
            "galgame_mcp.server.native_post_window_click",
            return_value={"input_method": "window_send_message", "delivered": True},
        ) as click, patch(
            "galgame_mcp.server.STORE.record_action",
            return_value={"event_id": "action-1"},
        ):
            result = server_perform_game_action(
                action="hide_ui",
                background=True,
                session_id="test-session",
            )

        click.assert_called_once_with(
            title="测试游戏",
            x=600,
            y=450,
            button="right",
            clicks=1,
            interval_ms=0,
            delivery="send",
        )
        self.assertTrue(result["configured"])
        self.assertEqual(result["kind"], "click")
        self.assertEqual(result["resolved_point"]["coordinate_space"], "screen")
        self.assertTrue(result["recorded"])


class BatchPlayTests(unittest.TestCase):
    def test_full_choice_probe_preserves_capture_path(self) -> None:
        session = {
            "session_id": "test-session",
            "game": {"window_title": "测试游戏"},
            "current_state": {},
        }
        payload = {"image_path": "C:/choice.png", "capture_scope": "window_full"}
        path = Path("C:/choice.png")
        with patch(
            "galgame_mcp.server._capture_for_session",
            return_value=(payload, path),
        ), patch(
            "galgame_mcp.server._process_capture_text",
            return_value=payload,
        ) as process:
            result_payload, result_path = _probe_full_window_for_choices(
                title="测试游戏",
                capture_mode="window",
                session=session,
                ocr=True,
                record_text=False,
                language="auto",
                include_raw_text=False,
            )

        self.assertIs(result_payload, payload)
        self.assertEqual(result_path, path)
        process.assert_called_once()

    def test_choice_click_point_uses_profile_and_ocr_geometry(self) -> None:
        session = {
            "session_id": "test-session",
            "game": {
                "layout_profile": {
                    "choice_region": {
                        "x": 0.2,
                        "y": 0.2,
                        "width": 0.6,
                        "height": 0.5,
                        "coordinate_space": "normalized",
                    },
                    "choice_layout": "vertical",
                }
            },
        }
        payload = {
            "capture_scope": "window_full",
            "width": 1000,
            "height": 800,
            "window": {"x": 100, "y": 50, "width": 1000, "height": 800},
            "processed_text": {"choices": ["说实话", "敷衍过去"]},
            "_ocr_regions": [
                {"text": "说 实 话", "x": 400, "y": 300, "width": 100, "height": 30},
                {"text": "敷 衍 过 去", "x": 400, "y": 500, "width": 140, "height": 30},
            ],
        }

        self.assertEqual(_choice_click_point_from_payload(payload, session, 1), (550, 365))
        self.assertEqual(_choice_click_point_from_payload(payload, session, 2), (570, 565))

    def test_background_number_choice_uses_profile_click_before_key(self) -> None:
        session = {
            "session_id": "test-session",
            "game": {
                "window_title": "测试游戏",
                "control": {"choice_mode": "number"},
                "layout_profile": {
                    "choice_region": {
                        "x": 0.2,
                        "y": 0.2,
                        "width": 0.6,
                        "height": 0.5,
                        "coordinate_space": "normalized",
                    },
                    "choice_layout": "vertical",
                },
            },
            "current_state": {},
        }
        frame = {
            "image_path": "C:/choice.png",
            "capture_scope": "window_full",
            "width": 1000,
            "height": 800,
            "window": {"x": 100, "y": 50, "width": 1000, "height": 800},
            "_ocr_regions": [
                {"text": "说 实 话", "x": 400, "y": 300, "width": 100, "height": 30},
                {"text": "敷 衍 过 去", "x": 400, "y": 500, "width": 140, "height": 30},
            ],
        }
        process_calls = 0

        def process(payload: dict[str, object], *_args: object, **_kwargs: object) -> dict[str, object]:
            nonlocal process_calls
            process_calls += 1
            result = dict(payload)
            result["_ocr_regions"] = frame["_ocr_regions"]
            result["processed_text"] = (
                {"choices": ["说实话", "敷衍过去"]}
                if process_calls == 1
                else {"dialogue": "选择后的对白", "choices": []}
            )
            return result

        with patch("galgame_mcp.server.STORE.get_session", return_value=session), patch(
            "galgame_mcp.server._capture_for_session",
            return_value=(frame, Path("C:/choice.png")),
        ), patch("galgame_mcp.server._process_capture_text", side_effect=process), patch(
            "galgame_mcp.server.native_post_window_click",
            return_value={"input_method": "window_message", "queued": True},
        ) as click, patch("galgame_mcp.server.native_post_window_key") as key, patch(
            "galgame_mcp.server.STORE.record_action", return_value={"event_id": "action-1"}
        ), patch(
            "galgame_mcp.server._auto_return_from_settings",
            side_effect=lambda payload, *_args, **_kwargs: (payload, Path("C:/choice.png")),
        ):
            result = server_select_choice(
                option_index=1,
                wait_seconds=0,
                ocr=True,
                record_text=False,
                session_id="test-session",
                capture_mode="window",
                background=True,
            )

        click.assert_called_once_with(
            title="测试游戏",
            x=550,
            y=365,
            button="left",
            clicks=1,
            interval_ms=100,
            delivery="send",
        )
        key.assert_not_called()
        self.assertEqual(result["processed_text"]["dialogue"], "选择后的对白")

    def test_play_until_choice_stops_after_blank_full_probe(self) -> None:
        session = {
            "session_id": "test-session",
            "game": {
                "window_title": "测试游戏",
                "control": {"advance_key": "SPACE"},
            },
            "current_state": {},
        }
        blank_fast = {
            "capture_scope": "window_dialogue_region",
            "ocr": {"available": True},
            "processed_text": {"speaker": None, "dialogue": "", "choices": []},
        }
        full_noise = {
            "capture_scope": "window_full",
            "ocr": {"available": True},
            "processed_text": {"speaker": None, "dialogue": "CHAPTER 1 · 1", "choices": []},
        }
        with patch("galgame_mcp.server.STORE.get_session", return_value=session), patch(
            "galgame_mcp.server._capture_processed_frame",
            return_value=(blank_fast, Path("C:/blank.png")),
        ), patch(
            "galgame_mcp.server._probe_full_window_for_choices",
            return_value=(full_noise, Path("C:/full.png")),
        ), patch(
            "galgame_mcp.server._auto_return_from_settings",
            side_effect=lambda payload, *_args, **_kwargs: (payload, Path("C:/frame.png")),
        ), patch("galgame_mcp.server._remember_bottom_snapshot"):
            result = server_play_until_choice(
                max_steps=10,
                wait_seconds=0,
                record_text=False,
                session_id="test-session",
            )

        self.assertIsInstance(result, list)
        response = json.loads(result[0])
        self.assertEqual(response["stop_reason"], "dialogue_not_detected")
        self.assertEqual(response["steps_advanced"], 0)
        self.assertLessEqual(response["frames_processed"], 12)
        self.assertTrue(response["manual_intervention"]["required"])

    def test_play_until_choice_processes_probe_frame_without_recapture_loop(self) -> None:
        session = {
            "session_id": "test-session",
            "game": {
                "window_title": "测试游戏",
                "control": {"advance_key": "SPACE"},
            },
            "current_state": {},
        }
        blank_fast = {
            "capture_scope": "window_dialogue_region",
            "ocr": {"available": True},
            "processed_text": {"speaker": None, "dialogue": "", "choices": []},
        }
        probe_dialogue = {
            "capture_scope": "window_full",
            "height": 800,
            "ocr": {"available": True},
            "_dialogue_ocr_regions": [{"text": "OCR残留", "y": 700, "height": 30}],
            "processed_text": {"speaker": "将臣", "dialogue": "探测到的对白", "choices": []},
        }
        actions: list[dict[str, object]] = []
        with patch("galgame_mcp.server.STORE.get_session", return_value=session), patch(
            "galgame_mcp.server._capture_processed_frame",
            return_value=(blank_fast, Path("C:/blank.png")),
        ), patch(
            "galgame_mcp.server._probe_full_window_for_choices",
            return_value=(probe_dialogue, Path("C:/probe.png")),
        ), patch(
            "galgame_mcp.server._auto_return_from_settings",
            side_effect=lambda payload, *_args, **_kwargs: (payload, Path("C:/frame.png")),
        ), patch(
            "galgame_mcp.server._advance_input_for_batch",
            return_value=("background_click", {"queued": True}),
        ), patch(
            "galgame_mcp.server.STORE.record_action",
            side_effect=lambda *_args, **kwargs: actions.append(kwargs) or {"event_id": "action-1"},
        ), patch("galgame_mcp.server._remember_bottom_snapshot"):
            result = server_play_until_choice(
                max_steps=1,
                wait_seconds=0,
                transition_wait_seconds=0.2,
                record_text=False,
                session_id="test-session",
            )

        self.assertEqual(result["stop_reason"], "max_steps")
        self.assertEqual(result["steps_advanced"], 1)
        self.assertEqual(len(actions), 1)
        self.assertLessEqual(result["frames_processed"], 6)

    def test_play_until_choice_probes_full_window_after_repeated_dialogue(self) -> None:
        session = {
            "session_id": "test-session",
            "game": {
                "window_title": "测试游戏",
                "control": {"advance_key": "SPACE"},
            },
            "current_state": {},
        }
        dialogue_frame = {
            "capture_scope": "window_dialogue_region",
            "width": 700,
            "height": 180,
            "window": {"x": 0, "y": 0, "width": 1000, "height": 800},
            "ocr": {"available": True},
            "processed_text": {
                "speaker": "将臣",
                "dialogue": "没有变化的对白",
                "choices": [],
                "confidence": 0.9,
            },
        }
        choice_frame = {
            "capture_scope": "window_full",
            "width": 1000,
            "height": 800,
            "window": {"x": 0, "y": 0, "width": 1000, "height": 800},
            "ocr": {"available": True},
            "processed_text": {
                "speaker": None,
                "dialogue": "",
                "choices": ["说实话", "敷衍过去"],
                "choice_records": [
                    {"option_id": "1", "label": "说实话", "line": 1},
                    {"option_id": "2", "label": "敷衍过去", "line": 2},
                ],
                "confidence": 0.78,
            },
        }
        frames = [dialogue_frame, dialogue_frame, dialogue_frame, choice_frame]
        capture_index = 0
        actions: list[dict[str, object]] = []
        probe_calls: list[dict[str, object]] = []

        def capture_frame(**kwargs: object) -> tuple[dict[str, object], Path]:
            nonlocal capture_index
            index = min(capture_index, len(frames) - 1)
            capture_index += 1
            probe_calls.append(kwargs)
            return frames[index], Path(f"C:/batch-{index}.png")

        with patch("galgame_mcp.server.STORE.get_session", return_value=session), patch(
            "galgame_mcp.server._capture_processed_frame", side_effect=capture_frame
        ), patch(
            "galgame_mcp.server._probe_full_window_for_choices",
            return_value=(choice_frame, Path("C:/choice.png")),
        ) as probe, patch(
            "galgame_mcp.server._auto_return_from_settings",
            side_effect=lambda payload, *_args, **_kwargs: (payload, Path("C:/frame.png")),
        ), patch(
            "galgame_mcp.server._advance_input_for_batch",
            return_value=("background_click", {"queued": True}),
        ), patch(
            "galgame_mcp.server.STORE.record_action",
            side_effect=lambda *_args, **kwargs: actions.append(kwargs) or {"event_id": "action-1"},
        ), patch("galgame_mcp.server._remember_bottom_snapshot"):
            result = server_play_until_choice(
                max_steps=10,
                wait_seconds=0,
                record_text=False,
                session_id="test-session",
            )

        self.assertEqual(result["stop_reason"], "choice_detected")
        self.assertEqual(result["steps_advanced"], 2)
        probe.assert_called_once()

    def test_play_until_choice_advances_speaker_only_frame(self) -> None:
        session = {
            "session_id": "test-session",
            "game": {
                "window_title": "测试游戏",
                "control": {"advance_key": "SPACE"},
            },
            "current_state": {},
        }
        frames = [
            {
                "ocr": {"available": True},
                "processed_text": {"speaker": "将臣", "dialogue": "", "choices": []},
            },
            {
                "ocr": {"available": True},
                "processed_text": {"speaker": None, "dialogue": "下一句", "choices": [], "confidence": 0.9},
            },
            {
                "ocr": {"available": True},
                "processed_text": {"speaker": None, "dialogue": "", "choices": ["继续", "返回"]},
            },
        ]
        frame_paths = [Path("C:/frame-speaker.png"), Path("C:/frame-next.png"), Path("C:/frame-choice.png")]
        capture_index = 0
        actions: list[dict[str, object]] = []

        def capture_frame(**_kwargs: object) -> tuple[dict[str, object], Path]:
            nonlocal capture_index
            index = min(capture_index, len(frames) - 1)
            capture_index += 1
            return frames[index], frame_paths[index]

        with patch("galgame_mcp.server.STORE.get_session", return_value=session), patch(
            "galgame_mcp.server._capture_processed_frame", side_effect=capture_frame
        ), patch(
            "galgame_mcp.server._auto_return_from_settings",
            side_effect=lambda payload, *_args, **_kwargs: (payload, frame_paths[0]),
        ), patch(
            "galgame_mcp.server._advance_input_for_batch",
            return_value=("background_click", {"queued": True}),
        ), patch(
            "galgame_mcp.server.STORE.record_action",
            side_effect=lambda *_args, **kwargs: actions.append(kwargs) or {"event_id": "action-1"},
        ), patch("galgame_mcp.server._remember_bottom_snapshot"):
            result = server_play_until_choice(
                max_steps=3,
                wait_seconds=0,
                record_text=False,
                session_id="test-session",
            )

        self.assertEqual(result["stop_reason"], "choice_detected")
        self.assertEqual(result["steps_advanced"], 2)
        self.assertEqual(result["batch"][0]["text_status"], "speaker_only")
        self.assertEqual(result["batch"][1]["dialogue"], "下一句")

    def test_play_until_choice_waits_after_full_ocr_recovery(self) -> None:
        session = {
            "session_id": "test-session",
            "game": {
                "window_title": "测试游戏",
                "control": {"advance_key": "SPACE"},
            },
            "current_state": {},
        }
        frames = [
            {
                "capture_scope": "window_full",
                "width": 1000,
                "height": 800,
                "window": {"x": 0, "y": 0, "width": 1000, "height": 800},
                "ocr": {"available": True},
                "ocr_fallback": {
                    "full_text_detected": True,
                    "settle_wait_seconds": 1.0,
                },
                "processed_text": {
                    "speaker": None,
                    "dialogue": "恢复后的对白",
                    "choices": [],
                },
            },
            {
                "capture_scope": "window_full",
                "width": 1000,
                "height": 800,
                "window": {"x": 0, "y": 0, "width": 1000, "height": 800},
                "ocr": {"available": True},
                "processed_text": {
                    "speaker": None,
                    "dialogue": "",
                    "choices": ["继续", "返回"],
                },
            },
        ]
        frame_paths = [Path("C:/recovered.png"), Path("C:/choice.png")]
        actions: list[dict[str, object]] = []

        def capture_frame(**_kwargs: object) -> tuple[dict[str, object], Path]:
            index = min(len(actions), len(frames) - 1)
            return frames[index], frame_paths[index]

        with patch("galgame_mcp.server.STORE.get_session", return_value=session), patch(
            "galgame_mcp.server._capture_processed_frame", side_effect=capture_frame
        ), patch(
            "galgame_mcp.server._auto_return_from_settings",
            side_effect=lambda payload, *_args, **_kwargs: (payload, frame_paths[0]),
        ), patch(
            "galgame_mcp.server._advance_input_for_batch",
            return_value=("background_click", {"queued": True}),
        ), patch(
            "galgame_mcp.server.STORE.record_action",
            side_effect=lambda *args, **kwargs: actions.append({"args": args, **kwargs})
            or {"event_id": "action-1"},
        ), patch("galgame_mcp.server._remember_bottom_snapshot"), patch(
            "galgame_mcp.server.time.sleep"
        ) as sleep:
            result = server_play_until_choice(
                max_steps=2,
                wait_seconds=0,
                record_text=False,
                session_id="test-session",
            )

        self.assertEqual(result["stop_reason"], "choice_detected")
        sleep.assert_any_call(1.0)
        self.assertEqual(actions[0]["args"][1]["ocr_fallback_settle_seconds"], 1.0)
        self.assertEqual(result["ocr_fallback_settle_seconds"], 1.0)

    def test_play_until_choice_advances_after_unparsed_full_ocr_recovery(self) -> None:
        session = {
            "session_id": "test-session",
            "game": {
                "window_title": "测试游戏",
                "control": {"advance_key": "SPACE"},
            },
            "current_state": {},
        }
        frames = [
            {
                "capture_scope": "window_full",
                "width": 1000,
                "height": 800,
                "window": {"x": 0, "y": 0, "width": 1000, "height": 800},
                "ocr": {"available": True},
                "ocr_fallback": {
                    "full_text_detected": True,
                    "settle_wait_seconds": 1.0,
                },
                "processed_text": {
                    "speaker": None,
                    "dialogue": "",
                    "choices": [],
                    "unparsed_lines": ["章节过场文字"],
                },
            },
            {
                "capture_scope": "window_full",
                "width": 1000,
                "height": 800,
                "window": {"x": 0, "y": 0, "width": 1000, "height": 800},
                "ocr": {"available": True},
                "processed_text": {
                    "speaker": None,
                    "dialogue": "",
                    "choices": ["继续", "返回"],
                },
            },
        ]
        frame_paths = [Path("C:/chapter-card.png"), Path("C:/choice.png")]
        actions: list[dict[str, object]] = []

        def capture_frame(**_kwargs: object) -> tuple[dict[str, object], Path]:
            index = min(len(actions), len(frames) - 1)
            return frames[index], frame_paths[index]

        with patch("galgame_mcp.server.STORE.get_session", return_value=session), patch(
            "galgame_mcp.server._capture_processed_frame", side_effect=capture_frame
        ), patch(
            "galgame_mcp.server._auto_return_from_settings",
            side_effect=lambda payload, *_args, **_kwargs: (payload, frame_paths[0]),
        ), patch(
            "galgame_mcp.server._advance_input_for_batch",
            return_value=("background_click", {"queued": True}),
        ), patch(
            "galgame_mcp.server.STORE.record_action",
            side_effect=lambda *args, **kwargs: actions.append({"args": args, **kwargs})
            or {"event_id": "action-1"},
        ), patch("galgame_mcp.server._remember_bottom_snapshot"), patch(
            "galgame_mcp.server.time.sleep"
        ) as sleep:
            result = server_play_until_choice(
                max_steps=2,
                wait_seconds=0,
                record_text=False,
                session_id="test-session",
            )

        self.assertEqual(result["stop_reason"], "choice_detected")
        self.assertEqual(result["steps_advanced"], 1)
        self.assertEqual(result["batch"][0]["text_status"], "full_frame_fallback")
        self.assertEqual(result["batch"][0]["dialogue"], "章节过场文字")
        sleep.assert_any_call(1.0)
        self.assertEqual(actions[0]["args"][1]["ocr_fallback_settle_seconds"], 1.0)

    def test_play_until_choice_retries_transient_blank_dialogue(self) -> None:
        session = {
            "session_id": "test-session",
            "game": {
                "window_title": "测试游戏",
                "control": {"advance_key": "SPACE"},
            },
            "current_state": {},
        }
        frames = [
            {
                "ocr": {"available": True},
                "processed_text": {"speaker": "将臣", "dialogue": "", "choices": []},
            },
            {
                "ocr": {"available": True},
                "processed_text": {"speaker": "将臣", "dialogue": "稳定后的对白", "choices": []},
            },
            {
                "ocr": {"available": True},
                "processed_text": {"speaker": None, "dialogue": "", "choices": ["继续", "返回"]},
            },
        ]
        frame_paths = [Path("C:/frame-blank.png"), Path("C:/frame-dialogue.png"), Path("C:/frame-choice.png")]
        actions: list[dict[str, object]] = []
        capture_index = 0

        def capture_frame(**_kwargs: object) -> tuple[dict[str, object], Path]:
            nonlocal capture_index
            index = min(capture_index, len(frames) - 1)
            capture_index += 1
            return frames[index], frame_paths[index]

        with patch("galgame_mcp.server.STORE.get_session", return_value=session), patch(
            "galgame_mcp.server._capture_processed_frame", side_effect=capture_frame
        ), patch(
            "galgame_mcp.server._auto_return_from_settings",
            side_effect=lambda payload, *_args, **_kwargs: (payload, frame_paths[0]),
        ), patch(
            "galgame_mcp.server._advance_input_for_batch",
            return_value=("background_click", {"queued": True}),
        ), patch(
            "galgame_mcp.server.STORE.record_action",
            side_effect=lambda *_args, **kwargs: actions.append(kwargs) or {"event_id": "action-1"},
        ), patch("galgame_mcp.server._remember_bottom_snapshot"):
            result = server_play_until_choice(
                max_steps=3,
                wait_seconds=0,
                record_text=False,
                session_id="test-session",
            )

        self.assertEqual(result["stop_reason"], "choice_detected")
        self.assertEqual(result["steps_advanced"], 2)
        self.assertEqual(result["batch"][0]["text_status"], "speaker_only")
        self.assertEqual(result["batch"][1]["dialogue"], "稳定后的对白")

    def test_play_until_choice_returns_one_local_dialogue_batch(self) -> None:
        session = {
            "session_id": "test-session",
            "game": {
                "window_title": "测试游戏",
                "control": {"advance_key": "SPACE"},
            },
            "current_state": {},
        }
        frames = [
            {
                "capture_scope": "window_dialogue_region",
                "width": 700,
                "height": 180,
                "window": {"x": 0, "y": 0, "width": 1000, "height": 800},
                "ocr": {"available": True},
                "processed_text": {
                    "speaker": "芦花",
                    "dialogue": "第一句",
                    "choices": [],
                    "confidence": 0.9,
                },
            },
            {
                "capture_scope": "window_dialogue_region",
                "width": 700,
                "height": 180,
                "window": {"x": 0, "y": 0, "width": 1000, "height": 800},
                "ocr": {"available": True},
                "processed_text": {
                    "speaker": None,
                    "dialogue": "选项前",
                    "choices": ["继续", "返回"],
                    "choice_records": [
                        {"option_id": "1", "label": "继续", "line": "1. 继续"},
                        {"option_id": "2", "label": "返回", "line": "2. 返回"},
                    ],
                    "confidence": 0.8,
                },
            },
        ]
        frame_paths = [Path("C:/frame-1.png"), Path("C:/frame-2.png")]
        actions: list[dict[str, object]] = []

        def capture_frame(**_kwargs: object) -> tuple[dict[str, object], Path]:
            index = len(actions)
            return frames[index], frame_paths[index]

        with patch("galgame_mcp.server.STORE.get_session", return_value=session), patch(
            "galgame_mcp.server._capture_processed_frame", side_effect=capture_frame
        ) as capture, patch(
            "galgame_mcp.server._auto_return_from_settings",
            side_effect=lambda payload, *_args, **_kwargs: (payload, Path(str(payload["image_path"])) if "image_path" in payload else frame_paths[0]),
        ), patch(
            "galgame_mcp.server._advance_input_for_batch",
            return_value=("background_click", {"queued": True}),
        ) as advance, patch(
            "galgame_mcp.server.STORE.record_action",
            side_effect=lambda *_args, **kwargs: actions.append(kwargs) or {"event_id": "action-1"},
        ), patch("galgame_mcp.server._remember_bottom_snapshot"):
            result = server_play_until_choice(
                max_steps=3,
                wait_seconds=0,
                record_text=False,
                session_id="test-session",
            )

        self.assertEqual(result["stop_reason"], "choice_detected")
        self.assertTrue(result["choice_detected"])
        self.assertEqual(result["steps_advanced"], 1)
        self.assertEqual(result["frames_processed"], 2)
        self.assertEqual(len(result["batch"]), 2)
        self.assertEqual(result["batch"][1]["choices"], ["继续", "返回"])
        self.assertEqual(capture.call_count, 2)
        advance.assert_called_once()

if __name__ == "__main__":
    unittest.main()
