from __future__ import annotations

import ctypes
import tempfile
import unittest
from unittest.mock import call, patch

from galgame_mcp import platform as platform_module
from galgame_mcp.platform import (
    _bgrx_to_rgba,
    _key_to_vk,
    _restore_if_minimized,
    _send_mouse_event,
    _send_mouse_move,
    hold_key,
    ocr_image,
    post_window_click,
    post_window_key,
    post_window_wheel,
    rapidocr_image,
)


class _FakeUser32:
    def __init__(self, minimized: bool) -> None:
        self.minimized = minimized
        self.show_window_calls: list[tuple[int, int]] = []

    def IsIconic(self, hwnd: int) -> bool:
        return self.minimized

    def ShowWindow(self, hwnd: int, command: int) -> None:
        self.show_window_calls.append((hwnd, command))


class PlatformBufferTests(unittest.TestCase):
    def test_bgrx_to_rgba_swaps_channels_and_uses_opaque_alpha(self) -> None:
        bgrx = bytes((3, 2, 1, 0, 40, 50, 60, 17))
        self.assertEqual(_bgrx_to_rgba(bgrx), bytearray((1, 2, 3, 255, 60, 50, 40, 255)))

    def test_arrow_key_aliases_are_supported(self) -> None:
        self.assertEqual(_key_to_vk("ARROWDOWN"), _key_to_vk("DOWN"))
        self.assertEqual(_key_to_vk("ArrowLeft"), _key_to_vk("LEFT"))

    def test_only_minimized_windows_are_restored(self) -> None:
        normal = _FakeUser32(minimized=False)
        self.assertFalse(_restore_if_minimized(normal, 101))
        self.assertEqual(normal.show_window_calls, [])

        minimized = _FakeUser32(minimized=True)
        self.assertTrue(_restore_if_minimized(minimized, 202))
        self.assertEqual(minimized.show_window_calls, [(202, 9)])

    def test_mouse_event_checks_windows_send_input_count(self) -> None:
        with patch("galgame_mcp.platform.ctypes.windll.user32.SendInput", return_value=1) as send:
            _send_mouse_event(0x0002)
        send.assert_called_once()

        with patch("galgame_mcp.platform.ctypes.windll.user32.SendInput", return_value=0):
            with self.assertRaisesRegex(RuntimeError, "SendInput 鼠标事件失败"):
                _send_mouse_event(0x0004)

    def test_absolute_mouse_move_checks_windows_send_input_count(self) -> None:
        with patch(
            "galgame_mcp.platform.ctypes.windll.user32.GetSystemMetrics",
            side_effect=[0, 0, 1707, 1067],
        ), patch("galgame_mcp.platform.ctypes.windll.user32.SendInput", return_value=1) as send:
            _send_mouse_move(850, 900)
        send.assert_called_once()

    def test_hold_key_releases_a_chord_in_reverse_order(self) -> None:
        with patch("galgame_mcp.platform._send_key_event") as send, patch(
            "galgame_mcp.platform.time.sleep"
        ) as sleep:
            result = hold_key("CTRL+S", hold_seconds=0.25)

        self.assertEqual(result, {"key": "CTRL+S", "hold_seconds": 0.25})
        self.assertEqual(
            send.call_args_list,
            [call(0x11), call(ord("S")), call(ord("S"), key_up=True), call(0x11, key_up=True)],
        )
        sleep.assert_called_once_with(0.25)

    def test_rapidocr_result_keeps_contract_regions_and_backend_metadata(self) -> None:
        class FakeRapidResult:
            txts = ("「……？」",)
            boxes = [[[4.0, 5.0], [104.0, 5.0], [104.0, 35.0], [4.0, 35.0]]]
            scores = (0.97,)
            elapse = 0.012

        class FakeEngine:
            def __call__(self, _path: str) -> FakeRapidResult:
                return FakeRapidResult()

        with tempfile.NamedTemporaryFile(suffix=".png") as image, patch.object(
            platform_module, "_RAPIDOCR_ENGINE", FakeEngine()
        ), patch.object(platform_module, "_RAPIDOCR_INIT_ERROR", None):
            result = rapidocr_image(image.name)

        self.assertTrue(result["available"])
        self.assertTrue(result["execution_success"])
        self.assertTrue(result["usable"])
        self.assertEqual(result["backend"], "rapidocr_ppocrv6_small")
        self.assertEqual(result["model"], "PP-OCRv6-small-ONNX")
        self.assertEqual(result["text"], "「……？」")
        self.assertEqual(result["regions"][0]["x"], 4.0)
        self.assertEqual(result["regions"][0]["width"], 100.0)

    def test_rapidocr_missing_bbox_marks_full_window_geometry_unreliable(self) -> None:
        class FakeRapidResult:
            txts = ("unknown",)
            boxes = ()
            scores = (0.8,)
            elapse = 0.012

        class FakeEngine:
            def __call__(self, _path: str) -> FakeRapidResult:
                return FakeRapidResult()

        with tempfile.NamedTemporaryFile(suffix=".png") as image, patch.object(
            platform_module, "_RAPIDOCR_ENGINE", FakeEngine()
        ), patch.object(platform_module, "_RAPIDOCR_INIT_ERROR", None), patch.object(
            platform_module, "_rapidocr_png_size", return_value=(100, 100)
        ):
            result = rapidocr_image(image.name)

        self.assertTrue(result["execution_success"])
        self.assertTrue(result["usable"])
        self.assertTrue(result["regions"][0]["synthetic"])
        self.assertFalse(result["regions"][0]["geometry_reliable"])
        self.assertEqual(result["regions"][0]["source"], "rapidocr_full_image")

    def test_windows_ocr_preloads_onnx_runtime_before_first_request(self) -> None:
        order: list[str] = []

        def preload() -> None:
            order.append("onnxruntime")

        def windows(_path: object, *, language: str, timeout_sec: int) -> dict[str, object]:
            order.append("windows_ocr")
            return {
                "available": True,
                "execution_success": True,
                "usable": True,
                "status": "ok",
                "backend": "windows_ocr",
                "text": "对白",
                "regions": [],
            }

        with tempfile.NamedTemporaryFile(suffix=".png") as image, patch.object(
            platform_module.sys, "platform", "win32"
        ), patch.object(
            platform_module, "_preload_rapidocr_runtime", side_effect=preload
        ) as preload_call, patch.object(
            platform_module, "_run_windows_ocr", side_effect=windows
        ):
            result = ocr_image(image.name)

        self.assertEqual(result["backend"], "windows_ocr")
        self.assertEqual(order, ["onnxruntime", "windows_ocr"])
        preload_call.assert_called_once_with()

    def test_rapidocr_dll_failure_is_not_reported_as_missing_dependency(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".png") as image, patch.object(
            platform_module, "_RAPIDOCR_ENGINE", None
        ), patch.object(
            platform_module,
            "_RAPIDOCR_INIT_ERROR",
            ImportError("DLL load failed while importing onnxruntime_pybind11_state"),
        ):
            result = rapidocr_image(image.name)

        self.assertEqual(result["status"], "init_error")
        self.assertTrue(result["available"])
        self.assertFalse(result["execution_success"])


class BackgroundWindowMessageTests(unittest.TestCase):
    def test_window_key_queues_messages_without_using_send_input(self) -> None:
        with patch(
            "galgame_mcp.platform._find_window", return_value=(123, "测试游戏")
        ), patch(
            "galgame_mcp.platform._post_window_message"
        ) as post, patch(
            "galgame_mcp.platform.ctypes.windll.user32.MapVirtualKeyW", return_value=57
        ), patch("galgame_mcp.platform.time.sleep"):
            result = post_window_key("测试游戏", "SPACE", presses=2, interval_ms=10)

        self.assertEqual(result["input_method"], "window_message")
        self.assertTrue(result["background"])
        self.assertTrue(result["queued"])
        self.assertEqual(result["messages_posted"], 4)
        self.assertEqual(
            [item.args[:2] for item in post.call_args_list],
            [(123, 0x0100), (123, 0x0101), (123, 0x0100), (123, 0x0101)],
        )

    def test_window_click_converts_screen_coordinates_to_client_coordinates(self) -> None:
        def screen_to_client(_hwnd: int, point_arg: object) -> int:
            point = ctypes.cast(
                point_arg, ctypes.POINTER(platform_module._Point)
            ).contents
            point.x = 50
            point.y = 60
            return 1

        with patch(
            "galgame_mcp.platform._find_window", return_value=(456, "测试游戏")
        ), patch(
            "galgame_mcp.platform.ctypes.windll.user32.ScreenToClient",
            side_effect=screen_to_client,
        ), patch("galgame_mcp.platform._post_window_message") as post:
            result = post_window_click("测试游戏", 850, 900, clicks=1, interval_ms=0)

        self.assertEqual(result["client_x"], 50)
        self.assertEqual(result["client_y"], 60)
        self.assertEqual(result["messages_posted"], 3)
        self.assertEqual(
            [item.args[:2] for item in post.call_args_list],
            [(456, 0x0200), (456, 0x0201), (456, 0x0202)],
        )

    def test_window_key_can_call_window_procedure_with_send_delivery(self) -> None:
        with patch(
            "galgame_mcp.platform._find_window", return_value=(123, "测试游戏")
        ), patch(
            "galgame_mcp.platform._send_window_message"
        ) as send, patch(
            "galgame_mcp.platform.ctypes.windll.user32.MapVirtualKeyW", return_value=57
        ), patch("galgame_mcp.platform._post_window_message") as post:
            result = post_window_key(
                "测试游戏", "SPACE", presses=1, interval_ms=0, delivery="send"
            )

        self.assertEqual(result["input_method"], "window_send_message")
        self.assertFalse(result["queued"])
        self.assertTrue(result["delivered"])
        self.assertEqual(result["delivery"], "send")
        self.assertEqual(result["messages_posted"], 2)
        self.assertEqual(send.call_count, 2)
        post.assert_not_called()

    def test_window_wheel_uses_screen_coordinates_for_wheel_and_client_for_move(self) -> None:
        def screen_to_client(_hwnd: int, point_arg: object) -> int:
            point = ctypes.cast(
                point_arg, ctypes.POINTER(platform_module._Point)
            ).contents
            point.x = 50
            point.y = 60
            return 1

        with patch(
            "galgame_mcp.platform._find_window", return_value=(789, "测试游戏")
        ), patch(
            "galgame_mcp.platform.ctypes.windll.user32.ScreenToClient",
            side_effect=screen_to_client,
        ), patch("galgame_mcp.platform._post_window_message") as post:
            result = post_window_wheel(
                "测试游戏", 850, 900, delta=-120, clicks=1, interval_ms=0
            )

        self.assertEqual(result["direction"], "down")
        self.assertEqual(result["client_x"], 50)
        self.assertEqual(result["client_y"], 60)
        self.assertEqual(result["messages_posted"], 2)
        self.assertEqual(
            [item.args[:2] for item in post.call_args_list],
            [(789, 0x0200), (789, 0x020A)],
        )


if __name__ == "__main__":
    unittest.main()
