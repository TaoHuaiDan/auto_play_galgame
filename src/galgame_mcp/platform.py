from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wintypes
import importlib
import os
import queue
import shutil
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path
from typing import Any

from .ocr_focus import map_zoomed_ocr_result, pillow_available, temporary_zoomed_regions


class PlatformAutomationError(RuntimeError):
    """Raised when a local screen/input operation is unavailable or fails."""


# Windows.Media.Ocr is asynchronous and FastMCP calls synchronous tools while
# its event loop is active. Creating a new thread, event loop, and OcrEngine on
# every frame adds seconds of COM/WinRT startup overhead. A single daemon worker
# keeps all WinRT objects on one thread and serializes only the local OCR work;
# capture/input calls remain independent and do not touch this queue.
_OCR_REQUEST_QUEUE: queue.Queue[tuple[Path, str, int, threading.Event, list[dict[str, Any]]] | None] = queue.Queue()
_OCR_WORKER_LOCK = threading.Lock()
_OCR_WORKER_THREAD: threading.Thread | None = None
_RAPIDOCR_ENGINE_LOCK = threading.Lock()
_RAPIDOCR_ENGINE: Any | None = None
_RAPIDOCR_INIT_ERROR: Exception | None = None
_RAPIDOCR_RUNTIME_PRELOADED = False
_RAPIDOCR_RUNTIME_ERROR: Exception | None = None


def _enable_windows_dpi_awareness() -> None:
    """Use physical desktop coordinates for capture and input on Windows.

    Without an explicit DPI context, this process sees the scaled logical
    desktop (1707x1067 on a 2560x1600 display in the current environment).
    That makes screenshots smaller than the real screen and makes screen
    coordinates disagree with the game window.  Prefer per-monitor V2 and
    keep older Windows fallbacks for compatibility.
    """

    if sys.platform != "win32":
        return

    try:
        user32 = ctypes.windll.user32
        set_context = getattr(user32, "SetProcessDpiAwarenessContext", None)
        if set_context is not None:
            set_context.argtypes = [ctypes.c_void_p]
            set_context.restype = wintypes.BOOL
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4.
            if set_context(ctypes.c_void_p(-4)):
                return
    except (AttributeError, OSError, TypeError):
        pass

    try:
        shcore = ctypes.windll.shcore
        set_process_awareness = getattr(shcore, "SetProcessDpiAwareness", None)
        if set_process_awareness is not None:
            set_process_awareness.argtypes = [ctypes.c_int]
            set_process_awareness.restype = ctypes.c_long
            # PROCESS_PER_MONITOR_DPI_AWARE == 2.
            if int(set_process_awareness(2)) == 0:  # S_OK
                return
    except (AttributeError, OSError, TypeError):
        pass

    try:
        set_dpi_aware = ctypes.windll.user32.SetProcessDPIAware
        set_dpi_aware.argtypes = []
        set_dpi_aware.restype = wintypes.BOOL
        set_dpi_aware()
    except (AttributeError, OSError, TypeError):
        pass


_enable_windows_dpi_awareness()


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _rgba_to_png(width: int, height: int, rgba: bytes | bytearray) -> bytes:
    row_size = width * 4
    raw = b"".join(b"\x00" + rgba[offset : offset + row_size] for offset in range(0, len(rgba), row_size))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    try:
        compression_level = max(0, min(9, int(os.environ.get("GALGAME_MCP_PNG_COMPRESSION", "1"))))
    except ValueError:
        compression_level = 1
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(raw, level=compression_level))
        + _png_chunk(b"IEND", b"")
    )


def image_motion_score(
    first_image_path: str | Path,
    second_image_path: str | Path,
    *,
    max_width: int = 160,
) -> float | None:
    """Return a small grayscale difference score for two local PNG frames.

    This is intentionally a coarse signal for transition detection, not a
    replacement for OCR or visual understanding.  Pillow is optional; when
    it is unavailable, callers receive ``None`` and must keep the safe
    no-extra-click path.
    """

    if not pillow_available():
        return None
    try:
        from PIL import Image, ImageChops, ImageStat

        width_limit = max(32, min(int(max_width), 640))
        with Image.open(first_image_path) as first_source:
            first = first_source.convert("L")
        with Image.open(second_image_path) as second_source:
            second = second_source.convert("L")
        if first.size != second.size or first.width <= 0 or first.height <= 0:
            return None
        scale = min(1.0, width_limit / float(first.width))
        size = (
            max(1, round(first.width * scale)),
            max(1, round(first.height * scale)),
        )
        resampling = getattr(Image, "Resampling", Image).BILINEAR
        if first.size != size:
            first = first.resize(size, resampling)
            second = second.resize(size, resampling)
        difference = ImageChops.difference(first, second)
        mean = float(ImageStat.Stat(difference).mean[0]) / 255.0
        return round(max(0.0, min(mean, 1.0)), 6)
    except (OSError, TypeError, ValueError):
        return None


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 3)]


def _bgrx_to_rgba(bgrx: bytes) -> bytearray:
    """Swap opaque BGRX channels in bulk without a Python per-pixel loop."""

    if len(bgrx) % 4:
        raise ValueError("BGRX buffer length must be divisible by four")
    rgba = bytearray(bgrx)
    red = rgba[2::4]
    blue = rgba[0::4]
    rgba[0::4] = red
    rgba[2::4] = blue
    rgba[3::4] = b"\xff" * (len(rgba) // 4)
    return rgba


def capture_screen_png() -> tuple[bytes, dict[str, int]]:
    """Capture the complete primary Windows desktop as PNG bytes.

    Deliberately has no region parameters: the game is expected to run full
    screen, so OCR and visual reasoning always receive the same full-screen
    coordinate space.
    """

    if sys.platform != "win32":
        raise PlatformAutomationError("当前实现的屏幕捕获后端只支持 Windows")
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    left = 0
    top = 0
    capture_width = int(user32.GetSystemMetrics(0))
    capture_height = int(user32.GetSystemMetrics(1))
    if capture_width <= 0 or capture_height <= 0:
        raise PlatformAutomationError("屏幕尺寸无效")

    screen_dc = user32.GetDC(0)
    if not screen_dc:
        raise PlatformAutomationError("无法取得桌面 DC")
    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, capture_width, capture_height)
    if not memory_dc or not bitmap:
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(0, screen_dc)
        raise PlatformAutomationError("无法创建兼容位图")

    old_bitmap = gdi32.SelectObject(memory_dc, bitmap)
    try:
        srccopy = 0x00CC0020
        captureblt = 0x40000000
        if not gdi32.BitBlt(
            memory_dc,
            0,
            0,
            capture_width,
            capture_height,
            screen_dc,
            left,
            top,
            srccopy | captureblt,
        ):
            raise PlatformAutomationError("BitBlt 屏幕捕获失败")

        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.bmiHeader.biWidth = capture_width
        info.bmiHeader.biHeight = -capture_height  # top-down rows
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0  # BI_RGB returns BGRX bytes
        buffer = ctypes.create_string_buffer(capture_width * capture_height * 4)
        copied = gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            capture_height,
            buffer,
            ctypes.byref(info),
            0,
        )
        if copied != capture_height:
            raise PlatformAutomationError("GetDIBits 屏幕读取失败")

        rgba = _bgrx_to_rgba(buffer.raw[: capture_width * capture_height * 4])
        return _rgba_to_png(capture_width, capture_height, rgba), {
            "x": left,
            "y": top,
            "width": capture_width,
            "height": capture_height,
        }
    finally:
        gdi32.SelectObject(memory_dc, old_bitmap)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(0, screen_dc)


def capture_window_png(title: str) -> tuple[bytes, dict[str, Any]]:
    """Capture the complete matching window without cropping or resizing.

    ``PrintWindow`` can render many occluded Win32/game windows while another
    application is in the foreground. It is intentionally an optional
    fallback: some GPU-exclusive or minimized games do not expose their
    rendered surface through this Windows API.
    """

    if sys.platform != "win32":
        raise PlatformAutomationError("当前实现的窗口捕获后端只支持 Windows")
    hwnd, matched_title = _find_window(title)
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise PlatformAutomationError(f"无法读取窗口位置: {matched_title}")
    capture_width = int(rect.right - rect.left)
    capture_height = int(rect.bottom - rect.top)
    if capture_width <= 0 or capture_height <= 0:
        raise PlatformAutomationError(f"窗口尺寸无效: {matched_title}")

    window_dc = user32.GetWindowDC(hwnd)
    if not window_dc:
        raise PlatformAutomationError(f"无法取得窗口 DC: {matched_title}")
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, capture_width, capture_height)
    if not memory_dc or not bitmap:
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)
        raise PlatformAutomationError("无法创建窗口兼容位图")

    old_bitmap = gdi32.SelectObject(memory_dc, bitmap)
    try:
        # PW_RENDERFULLCONTENT is supported by modern composited windows.
        rendered = bool(user32.PrintWindow(hwnd, memory_dc, 2))
        if not rendered:
            srccopy = 0x00CC0020
            captureblt = 0x40000000
            if not gdi32.BitBlt(
                memory_dc,
                0,
                0,
                capture_width,
                capture_height,
                window_dc,
                0,
                0,
                srccopy | captureblt,
            ):
                raise PlatformAutomationError(f"窗口捕获失败: {matched_title}")

        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.bmiHeader.biWidth = capture_width
        info.bmiHeader.biHeight = -capture_height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0  # BI_RGB returns BGRX bytes
        buffer = ctypes.create_string_buffer(capture_width * capture_height * 4)
        copied = gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            capture_height,
            buffer,
            ctypes.byref(info),
            0,
        )
        if copied != capture_height:
            raise PlatformAutomationError(f"无法读取窗口位图: {matched_title}")
        rgba = _bgrx_to_rgba(buffer.raw[: capture_width * capture_height * 4])
        return _rgba_to_png(capture_width, capture_height, rgba), {
            "x": int(rect.left),
            "y": int(rect.top),
            "width": capture_width,
            "height": capture_height,
            "hwnd": int(hwnd),
            "title": matched_title,
            "capture_method": "PrintWindow" if rendered else "window_dc",
            "occluded_capture": rendered,
            "minimized": bool(user32.IsIconic(hwnd)),
        }
    finally:
        gdi32.SelectObject(memory_dc, old_bitmap)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)


def capture_window_region_png(
    title: str,
    x: int,
    y: int,
    width: int,
    height: int,
) -> tuple[bytes, dict[str, Any]]:
    """Capture a window-local region through the window DC.

    This is the fast follow-up path for dialogue OCR. It does not activate the
    window or move the cursor, and it deliberately reports that it is a region
    frame so callers never mistake it for a full-window screenshot. Some GPU
    surfaces do not expose their occluded contents through ``GetWindowDC``;
    callers must validate OCR and fall back to ``capture_window_png`` when the
    region is unusable.
    """

    if sys.platform != "win32":
        raise PlatformAutomationError("当前实现的窗口捕获后端只支持 Windows")
    x = int(x)
    y = int(y)
    width = int(width)
    height = int(height)
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise PlatformAutomationError("窗口区域坐标或尺寸无效")

    hwnd, matched_title = _find_window(title)
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise PlatformAutomationError(f"无法读取窗口位置: {matched_title}")
    full_width = int(rect.right - rect.left)
    full_height = int(rect.bottom - rect.top)
    if x + width > full_width or y + height > full_height:
        raise PlatformAutomationError("窗口区域超出目标窗口范围")

    window_dc = user32.GetWindowDC(hwnd)
    if not window_dc:
        raise PlatformAutomationError(f"无法取得窗口 DC: {matched_title}")
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    if not memory_dc or not bitmap:
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        user32.ReleaseDC(hwnd, window_dc)
        raise PlatformAutomationError("无法创建区域兼容位图")

    old_bitmap = gdi32.SelectObject(memory_dc, bitmap)
    try:
        srccopy = 0x00CC0020
        captureblt = 0x40000000
        if not gdi32.BitBlt(
            memory_dc,
            0,
            0,
            width,
            height,
            window_dc,
            x,
            y,
            srccopy | captureblt,
        ):
            raise PlatformAutomationError(f"窗口区域捕获失败: {matched_title}")

        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        buffer = ctypes.create_string_buffer(width * height * 4)
        copied = gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            height,
            buffer,
            ctypes.byref(info),
            0,
        )
        if copied != height:
            raise PlatformAutomationError(f"无法读取窗口区域位图: {matched_title}")
        rgba = _bgrx_to_rgba(buffer.raw[: width * height * 4])
        return _rgba_to_png(width, height, rgba), {
            "x": int(rect.left),
            "y": int(rect.top),
            "width": width,
            "height": height,
            "hwnd": int(hwnd),
            "title": matched_title,
            "capture_method": "window_region_bitblt",
            "occluded_capture": False,
            "minimized": bool(user32.IsIconic(hwnd)),
            "full_window_width": full_width,
            "full_window_height": full_height,
            "region_x": x,
            "region_y": y,
        }
    finally:
        gdi32.SelectObject(memory_dc, old_bitmap)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)


_SPECIAL_KEYS = {
    "CTRL": 0x11,
    "CONTROL": 0x11,
    "SHIFT": 0x10,
    "ALT": 0x12,
    "WIN": 0x5B,
    "WINDOWS": 0x5B,
    "SPACE": 0x20,
    "ENTER": 0x0D,
    "RETURN": 0x0D,
    "ESC": 0x1B,
    "ESCAPE": 0x1B,
    "TAB": 0x09,
    "BACKSPACE": 0x08,
    "BS": 0x08,
    "LEFT": 0x25,
    "ARROWLEFT": 0x25,
    "UP": 0x26,
    "ARROWUP": 0x26,
    "RIGHT": 0x27,
    "ARROWRIGHT": 0x27,
    "DOWN": 0x28,
    "ARROWDOWN": 0x28,
    "HOME": 0x24,
    "END": 0x23,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
    "INSERT": 0x2D,
    "DELETE": 0x2E,
    "CAPSLOCK": 0x14,
    "NUMLOCK": 0x90,
    "F1": 0x70,
    "F2": 0x71,
    "F3": 0x72,
    "F4": 0x73,
    "F5": 0x74,
    "F6": 0x75,
    "F7": 0x76,
    "F8": 0x77,
    "F9": 0x78,
    "F10": 0x79,
    "F11": 0x7A,
    "F12": 0x7B,
}


def _key_to_vk(token: str) -> int:
    token = token.strip().upper()
    if token in _SPECIAL_KEYS:
        return _SPECIAL_KEYS[token]
    if len(token) == 1 and (token.isascii() and (token.isalnum() or token in " -_=,./\\[];'")):
        return ord(token)
    raise PlatformAutomationError(f"不支持的按键: {token}")


def _restore_if_minimized(user32: Any, hwnd: int) -> bool:
    """Restore only a minimized window, preserving normal/maximized geometry."""

    if not user32.IsIconic(hwnd):
        return False
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    return True


class _KeyInput(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeyInput), ("mi", _MouseInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", wintypes.DWORD), ("data", _InputUnion)]


class _Point(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _PointerInfo(ctypes.Structure):
    _fields_ = [
        ("pointerType", wintypes.UINT),
        ("pointerId", wintypes.UINT),
        ("frameId", wintypes.UINT),
        ("pointerFlags", wintypes.UINT),
        ("sourceDevice", wintypes.HANDLE),
        ("hwndTarget", wintypes.HWND),
        ("ptPixelLocation", _Point),
        ("ptHimetricLocation", _Point),
        ("ptPixelLocationRaw", _Point),
        ("ptHimetricLocationRaw", _Point),
        ("dwTime", wintypes.DWORD),
        ("historyCount", wintypes.UINT),
        ("InputData", wintypes.LONG),
        ("dwKeyStates", wintypes.DWORD),
        ("PerformanceCount", ctypes.c_ulonglong),
        ("ButtonChangeType", wintypes.DWORD),
    ]


class _PointerTouchInfo(ctypes.Structure):
    _fields_ = [
        ("pointerInfo", _PointerInfo),
        ("touchFlags", wintypes.UINT),
        ("touchMask", wintypes.UINT),
        ("rcContact", _Rect),
        ("rcContactRaw", _Rect),
        ("orientation", wintypes.UINT),
        ("pressure", wintypes.UINT),
    ]


def _send_key_event(vk: int, key_up: bool = False) -> None:
    if sys.platform != "win32":
        raise PlatformAutomationError("当前实现的按键后端只支持 Windows")
    flags = 0x0002 if key_up else 0
    event = _Input(type=1, ki=_KeyInput(wVk=vk, dwFlags=flags))
    sent = ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_Input))
    if sent != 1:
        raise PlatformAutomationError(f"SendInput 按键失败，错误码={ctypes.GetLastError()}")


def _send_mouse_event(flags: int) -> None:
    """Send one real mouse event and fail if Windows accepts none of it."""

    if sys.platform != "win32":
        raise PlatformAutomationError("当前实现的鼠标后端只支持 Windows")
    event = _Input(type=0, mi=_MouseInput(dwFlags=flags))
    sent = ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_Input))
    if sent != 1:
        raise PlatformAutomationError(f"SendInput 鼠标事件失败，错误码={ctypes.GetLastError()}")


def _send_mouse_move(x: int, y: int) -> None:
    """Emit a real absolute mouse-move event before a button event.

    ``SetCursorPos`` updates the desktop cursor, but some games that read
    Raw Input/DirectInput do not observe it as a complete mouse packet.  The
    explicit move event makes the injected sequence look like the normal
    user path: move to the point, then press and release the button.
    """

    if sys.platform != "win32":
        raise PlatformAutomationError("当前实现的鼠标后端只支持 Windows")
    user32 = ctypes.windll.user32
    left = int(user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
    top = int(user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
    width = int(user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
    height = int(user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
    if width <= 1 or height <= 1:
        raise PlatformAutomationError("虚拟桌面尺寸无效")
    normalized_x = round((int(x) - left) * 65535 / (width - 1))
    normalized_y = round((int(y) - top) * 65535 / (height - 1))
    normalized_x = max(0, min(65535, normalized_x))
    normalized_y = max(0, min(65535, normalized_y))
    flags = 0x0001 | 0x8000 | 0x4000  # MOVE | ABSOLUTE | VIRTUALDESK
    event = _Input(
        type=0,
        mi=_MouseInput(dx=normalized_x, dy=normalized_y, dwFlags=flags),
    )
    sent = user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_Input))
    if sent != 1:
        raise PlatformAutomationError(f"SendInput 鼠标移动失败，错误码={ctypes.GetLastError()}")


def send_key(key: str, presses: int = 1, interval_ms: int = 80) -> dict[str, Any]:
    """Send a key or chord such as ``ENTER`` or ``CTRL+S``."""

    if not key or not key.strip():
        raise PlatformAutomationError("key 不能为空")
    tokens = [token for token in key.split("+") if token.strip()]
    if not tokens:
        raise PlatformAutomationError("key 不能为空")
    virtual_keys = [_key_to_vk(token) for token in tokens]
    modifiers = virtual_keys[:-1]
    main = virtual_keys[-1]
    presses = max(1, min(int(presses), 20))
    delay = max(0, min(int(interval_ms), 2000)) / 1000
    for _ in range(presses):
        for vk in modifiers:
            _send_key_event(vk)
        _send_key_event(main)
        _send_key_event(main, key_up=True)
        for vk in reversed(modifiers):
            _send_key_event(vk, key_up=True)
        if delay:
            time.sleep(delay)
    return {"key": key, "presses": presses}


def hold_key(key: str, hold_seconds: float = 1.0) -> dict[str, Any]:
    """Hold a key long enough for games that bind actions to key state."""

    if not key or not key.strip():
        raise PlatformAutomationError("key 不能为空")
    tokens = [token for token in key.split("+") if token.strip()]
    if not tokens:
        raise PlatformAutomationError("key 不能为空")
    virtual_keys = [_key_to_vk(token) for token in tokens]
    modifiers = virtual_keys[:-1]
    main = virtual_keys[-1]
    duration = max(0.01, min(float(hold_seconds), 30.0))
    pressed: list[int] = []
    try:
        for vk in modifiers:
            _send_key_event(vk)
            pressed.append(vk)
        _send_key_event(main)
        pressed.append(main)
        time.sleep(duration)
    finally:
        for vk in reversed(pressed):
            _send_key_event(vk, key_up=True)
    return {"key": key, "hold_seconds": duration}


def touch_screen(
    x: int,
    y: int,
    taps: int = 1,
    hold_ms: int = 80,
    interval_ms: int = 100,
) -> dict[str, Any]:
    """Inject a Windows touch tap as an explicit mouse alternative.

    This path is opt-in because most desktop games prefer mouse input.  The
    minimal pointer structure is intentional: optional contact/pressure data
    varies across Windows builds and is unnecessary for a tap.
    """

    if sys.platform != "win32":
        raise PlatformAutomationError("当前实现的触摸后端只支持 Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.InitializeTouchInjection.argtypes = [wintypes.UINT, wintypes.DWORD]
    user32.InitializeTouchInjection.restype = wintypes.BOOL
    user32.InjectTouchInput.argtypes = [wintypes.UINT, ctypes.POINTER(_PointerTouchInfo)]
    user32.InjectTouchInput.restype = wintypes.BOOL
    if not user32.InitializeTouchInjection(1, 0x00000003):  # TOUCH_FEEDBACK_NONE
        error = ctypes.get_last_error()
        raise PlatformAutomationError(f"初始化触摸注入失败，错误码={error}")

    left = int(user32.GetSystemMetrics(76))
    top = int(user32.GetSystemMetrics(77))
    width = int(user32.GetSystemMetrics(78))
    height = int(user32.GetSystemMetrics(79))
    x = int(x)
    y = int(y)
    if width <= 0 or height <= 0 or not (left <= x < left + width and top <= y < top + height):
        raise PlatformAutomationError("触摸坐标不在桌面范围内")

    taps = max(1, min(int(taps), 10))
    hold_seconds = max(0.01, min(int(hold_ms), 2000) / 1000)
    interval_seconds = max(0, min(int(interval_ms), 2000)) / 1000
    # Touch pointer IDs only need to be unique while a contact is active.
    pointer_id = max(1, int(time.monotonic_ns() & 0xFFFFFFFF))
    for index in range(taps):
        contact = _PointerTouchInfo()
        contact.pointerInfo.pointerType = 2  # PT_TOUCH
        contact.pointerInfo.pointerId = (pointer_id + index) & 0xFFFFFFFF
        contact.pointerInfo.ptPixelLocation = _Point(x, y)
        contact.pointerInfo.pointerFlags = 0x00010000 | 0x00000002 | 0x00000004  # DOWN|INRANGE|INCONTACT
        if not user32.InjectTouchInput(1, ctypes.byref(contact)):
            error = ctypes.get_last_error()
            raise PlatformAutomationError(f"触摸按下失败，错误码={error}")
        time.sleep(hold_seconds)
        contact.pointerInfo.pointerFlags = 0x00040000 | 0x00000002  # UP|INRANGE
        if not user32.InjectTouchInput(1, ctypes.byref(contact)):
            error = ctypes.get_last_error()
            raise PlatformAutomationError(f"触摸抬起失败，错误码={error}")
        if index + 1 < taps and interval_seconds:
            time.sleep(interval_seconds)
    return {
        "x": x,
        "y": y,
        "input_method": "touch",
        "taps": taps,
        "hold_ms": int(hold_seconds * 1000),
        "touch_events_sent": taps * 2,
    }


def click_screen(x: int, y: int, button: str = "left", clicks: int = 1, interval_ms: int = 100) -> dict[str, Any]:
    if sys.platform != "win32":
        raise PlatformAutomationError("当前实现的鼠标后端只支持 Windows")
    button = (button or "left").strip().lower()
    flags = {
        "left": (0x0002, 0x0004),
        "right": (0x0008, 0x0010),
        "middle": (0x0020, 0x0040),
    }
    if button not in flags:
        raise PlatformAutomationError("button 必须是 left、right 或 middle")
    down, up = flags[button]
    clicks = max(1, min(int(clicks), 10))
    delay = max(0, min(int(interval_ms), 2000)) / 1000
    if not ctypes.windll.user32.SetCursorPos(int(x), int(y)):
        raise PlatformAutomationError(f"设置鼠标位置失败，错误码={ctypes.GetLastError()}")
    _send_mouse_move(int(x), int(y))
    for index in range(clicks):
        _send_mouse_event(down)
        _send_mouse_event(up)
        if index + 1 < clicks and delay:
            time.sleep(delay)
    return {
        "x": int(x),
        "y": int(y),
        "button": button,
        "clicks": clicks,
        "cursor_move_events_sent": 1,
        "input_events_sent": clicks * 2 + 1,
    }


def _find_window(title: str) -> tuple[int, str]:
    if sys.platform != "win32":
        raise PlatformAutomationError("当前实现的窗口后端只支持 Windows")
    title = (title or "").strip()
    if not title:
        raise PlatformAutomationError("窗口标题不能为空")
    user32 = ctypes.windll.user32
    exact = user32.FindWindowW(None, title)
    candidates: list[tuple[int, str]] = []
    if exact:
        candidates.append((int(exact), title))

    enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    @enum_proc_type
    def enum_proc(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        window_title = buffer.value
        if title.casefold() in window_title.casefold() and int(hwnd) not in {item[0] for item in candidates}:
            candidates.append((int(hwnd), window_title))
        return True

    user32.EnumWindows(enum_proc, 0)
    if not candidates:
        raise PlatformAutomationError(f"找不到窗口: {title}")
    return candidates[0]


_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_SYSKEYDOWN = 0x0104
_WM_SYSKEYUP = 0x0105
_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WM_RBUTTONDOWN = 0x0204
_WM_RBUTTONUP = 0x0205
_WM_MBUTTONDOWN = 0x0207
_WM_MBUTTONUP = 0x0208
_WM_MOUSEWHEEL = 0x020A
_MK_LBUTTON = 0x0001
_MK_RBUTTON = 0x0002
_MK_MBUTTON = 0x0010
_WHEEL_DELTA = 120
_MAPVK_VK_TO_VSC = 0
_EXTENDED_VKS = {
    0x21,  # PAGEUP
    0x22,  # PAGEDOWN
    0x23,  # END
    0x24,  # HOME
    0x25,  # LEFT
    0x26,  # UP
    0x27,  # RIGHT
    0x28,  # DOWN
    0x2D,  # INSERT
    0x2E,  # DELETE
    0x5B,  # LEFT WINDOWS
    0x5C,  # RIGHT WINDOWS
}


def _post_window_message(hwnd: int, message: int, wparam: int = 0, lparam: int = 0) -> None:
    """Queue one message without activating the target window."""

    post_message = ctypes.windll.user32.PostMessageW
    post_message.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    post_message.restype = wintypes.BOOL
    if not post_message(int(hwnd), int(message), int(wparam), int(lparam)):
        raise PlatformAutomationError(
            f"PostMessageW 失败，消息=0x{int(message):04X}，错误码={ctypes.GetLastError()}"
        )


def _send_window_message(
    hwnd: int,
    message: int,
    wparam: int = 0,
    lparam: int = 0,
    timeout_ms: int = 500,
) -> None:
    """Call a target window procedure directly, with a bounded timeout."""

    send_message = ctypes.windll.user32.SendMessageTimeoutW
    send_message.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    send_message.restype = ctypes.c_ssize_t
    result = ctypes.c_size_t()
    timeout_ms = max(1, min(int(timeout_ms), 5000))
    # SMTO_ABORTIFHUNG prevents a stuck game window from blocking the MCP.
    delivered = send_message(
        int(hwnd),
        int(message),
        int(wparam),
        int(lparam),
        0x0002,
        timeout_ms,
        ctypes.byref(result),
    )
    if not delivered:
        raise PlatformAutomationError(
            f"SendMessageTimeoutW 失败或超时，消息=0x{int(message):04X}，窗口过程未确认"
        )


def _window_key_lparam(vk: int, key_up: bool = False, system_key: bool = False) -> int:
    """Build the repeat/scan-code lParam used by WM_KEYDOWN/WM_KEYUP."""

    map_virtual_key = ctypes.windll.user32.MapVirtualKeyW
    map_virtual_key.argtypes = [wintypes.UINT, wintypes.UINT]
    map_virtual_key.restype = wintypes.UINT
    scan_code = int(map_virtual_key(int(vk), _MAPVK_VK_TO_VSC)) & 0xFF
    lparam = 1 | (scan_code << 16)
    if int(vk) in _EXTENDED_VKS:
        lparam |= 0x01000000
    if system_key:
        lparam |= 0x20000000  # context code: ALT is held
    if key_up:
        lparam |= 0xC0000000  # previous state + transition state
    return lparam


def post_window_key(
    title: str,
    key: str,
    presses: int = 1,
    interval_ms: int = 80,
    delivery: str = "post",
) -> dict[str, Any]:
    """Deliver a key/chord directly to a window, without focus or cursor changes.

    ``delivery=post`` queues asynchronously. ``delivery=send`` calls the
    target window procedure through ``SendMessageTimeoutW`` and is useful for
    engines that do not consume their normal message queue promptly. Neither
    route proves that the game applied the action; Raw Input/DirectInput games
    can still ignore both.
    """

    if sys.platform != "win32":
        raise PlatformAutomationError("当前实现的窗口消息后端只支持 Windows")
    if not key or not key.strip():
        raise PlatformAutomationError("key 不能为空")
    tokens = [token for token in key.split("+") if token.strip()]
    if not tokens:
        raise PlatformAutomationError("key 不能为空")
    delivery = (delivery or "post").strip().lower()
    if delivery not in {"post", "send"}:
        raise PlatformAutomationError("delivery 必须是 post 或 send")
    virtual_keys = [_key_to_vk(token) for token in tokens]
    modifiers = virtual_keys[:-1]
    main = virtual_keys[-1]
    presses = max(1, min(int(presses), 20))
    delay = max(0, min(int(interval_ms), 2000)) / 1000
    hwnd, matched_title = _find_window(title)
    has_alt = 0x12 in virtual_keys
    messages_posted = 0
    deliver = _post_window_message if delivery == "post" else _send_window_message

    def post(vk: int, key_up: bool = False) -> None:
        nonlocal messages_posted
        system_key = has_alt
        message = (
            _WM_SYSKEYUP
            if key_up and system_key
            else _WM_SYSKEYDOWN
            if system_key
            else _WM_KEYUP
            if key_up
            else _WM_KEYDOWN
        )
        deliver(
            hwnd,
            message,
            wparam=vk,
            lparam=_window_key_lparam(vk, key_up=key_up, system_key=system_key),
        )
        messages_posted += 1

    for index in range(presses):
        pressed_modifiers: list[int] = []
        try:
            for vk in modifiers:
                post(vk)
                pressed_modifiers.append(vk)
            post(main)
            post(main, key_up=True)
        finally:
            # If a target rejects a later message, do not leave a modifier
            # logically held in the target's input queue.
            for vk in reversed(pressed_modifiers):
                try:
                    post(vk, key_up=True)
                except PlatformAutomationError:
                    pass
        if index + 1 < presses and delay:
            time.sleep(delay)
    return {
        "hwnd": hwnd,
        "title": matched_title,
        "key": key,
        "presses": presses,
        "input_method": "window_message" if delivery == "post" else "window_send_message",
        "background": True,
        "queued": delivery == "post",
        "delivered": delivery == "send",
        "delivery": delivery,
        "messages_posted": messages_posted,
    }


def _pack_client_point(x: int, y: int) -> int:
    """Pack a signed client coordinate pair into a mouse-message lParam."""

    if not (-32768 <= int(x) <= 32767 and -32768 <= int(y) <= 32767):
        raise PlatformAutomationError("窗口客户区坐标超出 Windows 鼠标消息范围")
    return (int(y) & 0xFFFF) << 16 | (int(x) & 0xFFFF)


def post_window_click(
    title: str,
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
    interval_ms: int = 100,
    delivery: str = "post",
) -> dict[str, Any]:
    """Deliver a client-area mouse click directly to a background window."""

    if sys.platform != "win32":
        raise PlatformAutomationError("当前实现的窗口消息后端只支持 Windows")
    button = (button or "left").strip().lower()
    messages = {
        "left": (_WM_LBUTTONDOWN, _WM_LBUTTONUP, _MK_LBUTTON),
        "right": (_WM_RBUTTONDOWN, _WM_RBUTTONUP, _MK_RBUTTON),
        "middle": (_WM_MBUTTONDOWN, _WM_MBUTTONUP, _MK_MBUTTON),
    }
    if button not in messages:
        raise PlatformAutomationError("button 必须是 left、right 或 middle")
    delivery = (delivery or "post").strip().lower()
    if delivery not in {"post", "send"}:
        raise PlatformAutomationError("delivery 必须是 post 或 send")
    clicks = max(1, min(int(clicks), 10))
    delay = max(0, min(int(interval_ms), 2000)) / 1000
    hwnd, matched_title = _find_window(title)
    point = _Point(int(x), int(y))
    screen_to_client = ctypes.windll.user32.ScreenToClient
    screen_to_client.argtypes = [wintypes.HWND, ctypes.POINTER(_Point)]
    screen_to_client.restype = wintypes.BOOL
    if not screen_to_client(hwnd, ctypes.byref(point)):
        raise PlatformAutomationError(
            f"ScreenToClient 失败，窗口={matched_title}，错误码={ctypes.GetLastError()}"
        )
    client_x = int(point.x)
    client_y = int(point.y)
    lparam = _pack_client_point(client_x, client_y)
    down_message, up_message, button_mask = messages[button]
    messages_posted = 0
    deliver = _post_window_message if delivery == "post" else _send_window_message

    def post(message: int, wparam: int = 0) -> None:
        nonlocal messages_posted
        deliver(hwnd, message, wparam=wparam, lparam=lparam)
        messages_posted += 1

    post(_WM_MOUSEMOVE)
    for index in range(clicks):
        post(down_message, button_mask)
        post(up_message)
        if index + 1 < clicks and delay:
            time.sleep(delay)
    return {
        "hwnd": hwnd,
        "title": matched_title,
        "screen_x": int(x),
        "screen_y": int(y),
        "client_x": client_x,
        "client_y": client_y,
        "button": button,
        "clicks": clicks,
        "input_method": "window_message" if delivery == "post" else "window_send_message",
        "background": True,
        "queued": delivery == "post",
        "delivered": delivery == "send",
        "delivery": delivery,
        "messages_posted": messages_posted,
    }


def post_window_wheel(
    title: str,
    x: int | None = None,
    y: int | None = None,
    delta: int = -_WHEEL_DELTA,
    clicks: int = 1,
    interval_ms: int = 100,
    delivery: str = "post",
) -> dict[str, Any]:
    """Deliver background ``WM_MOUSEWHEEL`` messages at a screen point.

    ``delta`` follows the Win32 convention: positive scrolls up and negative
    scrolls down. The real cursor is never moved. A mouse-move message is
    sent first so engines that track the hovered client area receive a
    consistent point, while the wheel message keeps its documented screen
    coordinates in ``lParam``.
    """

    if sys.platform != "win32":
        raise PlatformAutomationError("当前实现的窗口消息后端只支持 Windows")
    delivery = (delivery or "post").strip().lower()
    if delivery not in {"post", "send"}:
        raise PlatformAutomationError("delivery 必须是 post 或 send")
    if int(delta) == 0:
        raise PlatformAutomationError("delta 不能为 0")
    clicks = max(1, min(int(clicks), 20))
    delay = max(0, min(int(interval_ms), 2000)) / 1000
    hwnd, matched_title = _find_window(title)

    if x is None or y is None:
        rect = wintypes.RECT()
        get_rect = ctypes.windll.user32.GetWindowRect
        get_rect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        get_rect.restype = wintypes.BOOL
        if not get_rect(int(hwnd), ctypes.byref(rect)):
            raise PlatformAutomationError(
                f"无法读取窗口位置: {matched_title}，错误码={ctypes.GetLastError()}"
            )
        screen_x = (int(rect.left) + int(rect.right)) // 2
        screen_y = (int(rect.top) + int(rect.bottom)) // 2
    else:
        screen_x = int(x)
        screen_y = int(y)

    point = _Point(screen_x, screen_y)
    screen_to_client = ctypes.windll.user32.ScreenToClient
    screen_to_client.argtypes = [wintypes.HWND, ctypes.POINTER(_Point)]
    screen_to_client.restype = wintypes.BOOL
    if not screen_to_client(hwnd, ctypes.byref(point)):
        raise PlatformAutomationError(
            f"ScreenToClient 失败，窗口={matched_title}，错误码={ctypes.GetLastError()}"
        )
    client_lparam = _pack_client_point(int(point.x), int(point.y))
    wheel_lparam = _pack_client_point(screen_x, screen_y)
    wheel_wparam = (int(delta) & 0xFFFF) << 16
    messages_posted = 0
    deliver = _post_window_message if delivery == "post" else _send_window_message

    def post(message: int, wparam: int = 0, lparam: int = 0) -> None:
        nonlocal messages_posted
        deliver(hwnd, message, wparam=wparam, lparam=lparam)
        messages_posted += 1

    post(_WM_MOUSEMOVE, lparam=client_lparam)
    for index in range(clicks):
        post(_WM_MOUSEWHEEL, wparam=wheel_wparam, lparam=wheel_lparam)
        if index + 1 < clicks and delay:
            time.sleep(delay)
    return {
        "hwnd": hwnd,
        "title": matched_title,
        "screen_x": screen_x,
        "screen_y": screen_y,
        "client_x": int(point.x),
        "client_y": int(point.y),
        "delta": int(delta),
        "direction": "up" if int(delta) > 0 else "down",
        "clicks": clicks,
        "input_method": "window_message" if delivery == "post" else "window_send_message",
        "background": True,
        "queued": delivery == "post",
        "delivered": delivery == "send",
        "delivery": delivery,
        "messages_posted": messages_posted,
    }


def get_window_rect(title: str) -> dict[str, Any]:
    """Return the outer rectangle of the first visible matching window."""

    hwnd, matched_title = _find_window(title)
    rect = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise PlatformAutomationError(f"无法读取窗口位置: {matched_title}")
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        raise PlatformAutomationError(f"窗口尺寸无效: {matched_title}")
    return {
        "hwnd": hwnd,
        "title": matched_title,
        "x": int(rect.left),
        "y": int(rect.top),
        "width": width,
        "height": height,
    }


def focus_window(title: str) -> dict[str, Any]:
    """Bring the first visible window whose title contains ``title`` to foreground."""

    hwnd, matched_title = _find_window(title)
    user32 = ctypes.windll.user32
    # Restoring an already maximized or exclusive-fullscreen game changes its
    # window mode and can visibly shrink it.  Only restore a genuinely
    # minimized window; SetForegroundWindow is enough for normal/maximized
    # windows and preserves their current geometry.
    _restore_if_minimized(user32, hwnd)
    if user32.SetForegroundWindow(hwnd) or int(user32.GetForegroundWindow()) == int(hwnd):
        return {"hwnd": hwnd, "title": matched_title}

    # Windows normally blocks a background process from stealing focus.  Join
    # the input queues temporarily so the MCP can activate the game without
    # moving or resizing it, then detach immediately after the attempt.
    current_thread = int(ctypes.windll.kernel32.GetCurrentThreadId())
    foreground = int(user32.GetForegroundWindow())
    target_thread = int(user32.GetWindowThreadProcessId(hwnd, None))
    foreground_thread = (
        int(user32.GetWindowThreadProcessId(foreground, None)) if foreground else 0
    )
    attached: list[int] = []
    for thread_id in {target_thread, foreground_thread}:
        if thread_id and thread_id != current_thread and user32.AttachThreadInput(
            current_thread, thread_id, True
        ):
            attached.append(thread_id)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetActiveWindow(hwnd)
        focused = bool(user32.SetForegroundWindow(hwnd))
        focused = focused or int(user32.GetForegroundWindow()) == int(hwnd)
    finally:
        for thread_id in reversed(attached):
            user32.AttachThreadInput(current_thread, thread_id, False)
    if not focused:
        raise PlatformAutomationError(f"无法激活窗口: {matched_title}")
    return {"hwnd": hwnd, "title": matched_title}


def _windows_ocr_language(language: str) -> str | None:
    aliases = {
        "auto": None,
        "eng": "en-US",
        "jpn": "ja-JP",
        "japanese": "ja-JP",
        "chi_sim": "zh-CN",
        "zh": "zh-CN",
        "zh-cn": "zh-CN",
        "chs": "zh-CN",
        "kor": "ko-KR",
    }
    value = (language or "auto").strip()
    return aliases.get(value.casefold(), value or None)


async def _windows_ocr_async(
    path: Path,
    language: str,
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Use the built-in Windows.Media.Ocr API when a PyWinRT bridge exists."""

    import_error: Exception | None = None
    modules: tuple[Any, Any, Any, Any] | None = (
        runtime.get("modules") if runtime is not None else None
    )
    if modules is None:
        for prefix in ("winsdk.windows", "winrt.windows"):
            try:
                modules = (
                    importlib.import_module(f"{prefix}.media.ocr"),
                    importlib.import_module(f"{prefix}.graphics.imaging"),
                    importlib.import_module(f"{prefix}.storage"),
                    importlib.import_module(f"{prefix}.storage.streams"),
                )
                break
            except (ImportError, ModuleNotFoundError) as exc:
                import_error = exc
    if modules is None:
        message = "未安装 Windows OCR 的 PyWinRT 桥接包；请安装项目的 [windows-ocr] 可选依赖。"
        if import_error:
            message = f"{message} ({import_error})"
        return {
            "available": False,
            "status": "missing_dependency",
            "backend": "windows_ocr",
            "text": "",
            "message": message,
        }

    if runtime is not None:
        runtime["modules"] = modules
    ocr_module, imaging_module, storage_module, streams_module = modules
    try:
        access_mode = getattr(storage_module, "FileAccessMode")
        read_mode = getattr(access_mode, "READ", None)
        if read_mode is None:
            read_mode = getattr(access_mode, "read")
        file_stream = getattr(streams_module, "FileRandomAccessStream")
        stream = await file_stream.open_async(str(path), read_mode)
        decoder = await imaging_module.BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        language_tag = _windows_ocr_language(language)
        engine_cache: dict[str, tuple[Any, str]] = {}
        if runtime is not None:
            engine_cache = runtime.setdefault("engine_cache", {})
        cache_key = language_tag or "user_profile"
        cached_engine = engine_cache.get(cache_key)
        if cached_engine is not None:
            engine, selected_language = cached_engine
        else:
            engine = None
            selected_language = "user_profile"
            if language_tag:
                try:
                    globalization = importlib.import_module(
                        ocr_module.__name__.replace(".media.ocr", ".globalization")
                    )
                    language_type = getattr(globalization, "Language")
                    engine = ocr_module.OcrEngine.try_create_from_language(language_type(language_tag))
                    if engine is not None:
                        selected_language = language_tag
                except Exception:
                    engine = None
            if engine is None:
                engine = ocr_module.OcrEngine.try_create_from_user_profile_languages()
            if engine is not None and runtime is not None:
                engine_cache[cache_key] = (engine, selected_language)
        if engine is None:
            return {
                "available": False,
                "status": "language_unavailable",
                "backend": "windows_ocr",
                "text": "",
                "message": "Windows OCR 没有可用的匹配语言包。",
            }
        result = await engine.recognize_async(bitmap)
        lines = [str(line.text).strip() for line in result.lines if str(line.text).strip()]
        regions: list[dict[str, Any]] = []
        for line in result.lines:
            words = list(getattr(line, "words", []) or [])
            if not words:
                continue
            rectangles = [word.bounding_rect for word in words]
            left = min(float(rect.x) for rect in rectangles)
            top = min(float(rect.y) for rect in rectangles)
            right = max(float(rect.x + rect.width) for rect in rectangles)
            bottom = max(float(rect.y + rect.height) for rect in rectangles)
            text = str(line.text).strip()
            if text:
                regions.append(
                    {
                        "text": text,
                        "x": left,
                        "y": top,
                        "width": right - left,
                        "height": bottom - top,
                    }
                )
        return {
            "available": True,
            "status": "ok",
            "backend": "windows_ocr",
            "text": "\n".join(lines),
            "language": selected_language,
            "regions": regions,
        }
    except Exception as exc:
        return {
            "available": True,
            "status": "error",
            "backend": "windows_ocr",
            "text": "",
            "message": str(exc),
        }


def _windows_ocr_timeout_result() -> dict[str, Any]:
    return {
        "available": True,
        "status": "timeout",
        "backend": "windows_ocr",
        "text": "",
        "message": "Windows OCR 超时。",
    }


def _windows_ocr_error_result(exc: Exception) -> dict[str, Any]:
    return {
        "available": True,
        "status": "error",
        "backend": "windows_ocr",
        "text": "",
        "message": str(exc),
    }


def _windows_ocr_worker_loop() -> None:
    """Keep one asyncio/WinRT apartment alive for all OCR requests."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    runtime: dict[str, Any] = {}
    try:
        while True:
            request = _OCR_REQUEST_QUEUE.get()
            if request is None:
                return
            path, language, timeout_sec, done, result_box = request
            try:
                result = loop.run_until_complete(
                    asyncio.wait_for(
                        _windows_ocr_async(path, language, runtime=runtime),
                        timeout=max(1, timeout_sec),
                    )
                )
            except asyncio.TimeoutError:
                result = _windows_ocr_timeout_result()
            except Exception as exc:
                result = _windows_ocr_error_result(exc)
            result_box.append(result)
            done.set()
    finally:
        loop.close()


def _ensure_windows_ocr_worker() -> threading.Thread:
    global _OCR_WORKER_THREAD
    with _OCR_WORKER_LOCK:
        if _OCR_WORKER_THREAD is None or not _OCR_WORKER_THREAD.is_alive():
            _OCR_WORKER_THREAD = threading.Thread(
                target=_windows_ocr_worker_loop,
                name="galgame-windows-ocr",
                daemon=True,
            )
            _OCR_WORKER_THREAD.start()
        return _OCR_WORKER_THREAD


def _run_windows_ocr(path: Path, language: str, timeout_sec: int) -> dict[str, Any]:
    """Run OCR on the persistent local worker instead of a fresh thread."""

    _ensure_windows_ocr_worker()
    done = threading.Event()
    result_box: list[dict[str, Any]] = []
    _OCR_REQUEST_QUEUE.put((path, language, timeout_sec, done, result_box))
    if not done.wait(timeout=max(1, timeout_sec) + 1):
        return _windows_ocr_timeout_result()
    if result_box:
        return result_box[0]
    return _windows_ocr_error_result(RuntimeError("Windows OCR 工作线程没有返回结果。"))


def _preload_rapidocr_runtime() -> None:
    """Load ONNX Runtime before WinRT OCR can alter native DLL resolution.

    Windows OCR remains the primary backend.  RapidOCR's model is still lazy,
    but importing its native runtime once before the first WinRT call avoids a
    real Windows-only failure where the later fallback cannot load
    ``onnxruntime_pybind11_state``.  Missing optional dependencies are cached
    and never prevent Windows OCR from running.
    """

    global _RAPIDOCR_RUNTIME_PRELOADED, _RAPIDOCR_RUNTIME_ERROR
    if _RAPIDOCR_RUNTIME_PRELOADED or _RAPIDOCR_RUNTIME_ERROR is not None:
        return
    with _RAPIDOCR_ENGINE_LOCK:
        if _RAPIDOCR_RUNTIME_PRELOADED or _RAPIDOCR_RUNTIME_ERROR is not None:
            return
        try:
            importlib.import_module("onnxruntime")
        except Exception as exc:  # optional fallback; Windows OCR still runs
            _RAPIDOCR_RUNTIME_ERROR = exc
        else:
            _RAPIDOCR_RUNTIME_PRELOADED = True


def ocr_image(image_path: str, language: str = "auto", psm: int = 6, timeout_sec: int = 30) -> dict[str, Any]:
    """Run local OCR, preferring Windows OCR and falling back to Tesseract."""

    started = time.perf_counter()

    def finish(result: dict[str, Any]) -> dict[str, Any]:
        output = dict(result)
        output.setdefault(
            "execution_success",
            str(output.get("status") or "").casefold() in {"ok", "empty"},
        )
        output.setdefault(
            "usable",
            bool(str(output.get("text") or "").strip() or output.get("regions")),
        )
        output.setdefault("elapsed_ms", round((time.perf_counter() - started) * 1000.0, 3))
        return output

    path = Path(image_path).expanduser().resolve()
    if not path.exists():
        raise PlatformAutomationError(f"找不到图片: {path}")
    timeout = max(1, min(int(timeout_sec), 120))
    preference = os.environ.get("GALGAME_MCP_OCR_BACKEND", "auto").strip().casefold()
    attempts: list[dict[str, Any]] = []
    if preference in {"auto", "windows", "windows_ocr"} and sys.platform == "win32":
        _preload_rapidocr_runtime()
        windows_result = _run_windows_ocr(path, language=language, timeout_sec=timeout)
        windows_result["image_path"] = str(path)
        if windows_result.get("status") == "ok":
            return finish(windows_result)
        attempts.append(windows_result)

    executable = shutil.which("tesseract")
    if preference not in {"windows", "windows_ocr"} and executable:
        tesseract_language = language if language and language.casefold() != "auto" else "eng"
        command = [
            executable,
            str(path),
            "stdout",
            "--psm",
            str(max(3, min(int(psm), 13))),
            "-l",
            tesseract_language,
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PlatformAutomationError("OCR 超时") from exc
        tesseract_result = {
            "available": True,
            "status": "ok" if completed.returncode == 0 else "error",
            "backend": "tesseract",
            "returncode": completed.returncode,
            "text": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
            "image_path": str(path),
        }
        if completed.returncode == 0:
            return finish(tesseract_result)
        attempts.append(tesseract_result)

    messages = [item.get("message") or item.get("stderr") for item in attempts]
    messages = [str(message) for message in messages if message]
    return finish({
        "available": False,
        "status": "missing_dependency" if not attempts else "error",
        "backend": "local",
        "text": "",
        "message": "；".join(messages) or "未找到可用的本地 OCR 后端；可安装项目的 [windows-ocr] extra 或系统 Tesseract。",
        "image_path": str(path),
        "attempts": [{key: value for key, value in item.items() if key != "text"} for item in attempts],
    })


def _rapidocr_png_size(path: Path) -> tuple[int, int] | None:
    """Read a PNG size without requiring Pillow in the fallback backend."""

    try:
        header = path.read_bytes()[:24]
        if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")
    except (OSError, ValueError):
        return None


def _rapidocr_engine() -> tuple[Any | None, str | None]:
    """Load RapidOCR once; model initialization can download/load ONNX files."""

    global _RAPIDOCR_ENGINE, _RAPIDOCR_INIT_ERROR
    if _RAPIDOCR_ENGINE is not None:
        return _RAPIDOCR_ENGINE, None
    if _RAPIDOCR_INIT_ERROR is not None:
        return None, str(_RAPIDOCR_INIT_ERROR)
    with _RAPIDOCR_ENGINE_LOCK:
        if _RAPIDOCR_ENGINE is not None:
            return _RAPIDOCR_ENGINE, None
        if _RAPIDOCR_INIT_ERROR is not None:
            return None, str(_RAPIDOCR_INIT_ERROR)
        try:
            from rapidocr import EngineType, RapidOCR

            # rapidocr >= 3.9 defaults to PP-OCRv6 small detection and
            # recognition.  Set the engine explicitly so this fallback stays
            # on the local ONNX Runtime CPU path even if RapidOCR's global
            # defaults change later.
            _RAPIDOCR_ENGINE = RapidOCR(
                params={
                    "Det.engine_type": EngineType.ONNXRUNTIME,
                    "Cls.engine_type": EngineType.ONNXRUNTIME,
                    "Rec.engine_type": EngineType.ONNXRUNTIME,
                }
            )
            return _RAPIDOCR_ENGINE, None
        except Exception as exc:  # includes missing package/model/runtime
            _RAPIDOCR_INIT_ERROR = exc
            return None, str(exc)


def _rapidocr_regions(result: Any, image_size: tuple[int, int] | None) -> tuple[str, list[dict[str, Any]]]:
    """Convert RapidOCROutput boxes/txts/scores to the MCP region contract."""

    raw_texts = getattr(result, "txts", None)
    raw_boxes = getattr(result, "boxes", None)
    raw_scores = getattr(result, "scores", None)
    texts = list(raw_texts) if raw_texts is not None else []
    boxes = list(raw_boxes) if raw_boxes is not None else []
    scores = list(raw_scores) if raw_scores is not None else []
    regions: list[dict[str, Any]] = []
    lines: list[str] = []
    for index, value in enumerate(texts):
        text = str(value or "").strip()
        if not text:
            continue
        lines.append(text)
        region: dict[str, Any] = {"text": text}
        if index < len(boxes):
            try:
                points = list(boxes[index])
                coordinates = [(float(point[0]), float(point[1])) for point in points if len(point) >= 2]
                if coordinates:
                    left = min(point[0] for point in coordinates)
                    top = min(point[1] for point in coordinates)
                    right = max(point[0] for point in coordinates)
                    bottom = max(point[1] for point in coordinates)
                    region.update(
                        {
                            "x": left,
                            "y": top,
                            "width": max(0.0, right - left),
                            "height": max(0.0, bottom - top),
                        }
                    )
            except (TypeError, ValueError, IndexError):
                pass
        if index < len(scores):
            try:
                region["confidence"] = float(scores[index])
            except (TypeError, ValueError):
                pass
        if "x" not in region and image_size:
            region.update(
                {
                    "x": 0.0,
                    "y": 0.0,
                    "width": float(image_size[0]),
                    "height": float(image_size[1]),
                    "synthetic": True,
                    "geometry_reliable": False,
                    "source": "rapidocr_full_image",
                }
            )
        regions.append(region)
    return "\n".join(lines), regions


def rapidocr_image(image_path: str, language: str = "auto", timeout_sec: int = 30) -> dict[str, Any]:
    """Run the optional RapidOCR PP-OCRv6-small ONNX fallback locally."""

    started = time.perf_counter()
    path = Path(image_path).expanduser().resolve()
    if not path.exists():
        raise PlatformAutomationError(f"找不到图片: {path}")

    def finish(result: dict[str, Any]) -> dict[str, Any]:
        output = dict(result)
        output.setdefault("elapsed_ms", round((time.perf_counter() - started) * 1000.0, 3))
        return output

    engine, init_error = _rapidocr_engine()
    if engine is None:
        # A DLL initialization failure is an ImportError too, but it means the
        # optional backend was installed and failed at runtime.  Only a real
        # missing-module exception should be reported as missing_dependency.
        missing = isinstance(_RAPIDOCR_INIT_ERROR, ModuleNotFoundError)
        return finish(
            {
                "available": not missing,
                "execution_success": False,
                "usable": False,
                "status": "missing_dependency" if missing else "init_error",
                "backend": "rapidocr_ppocrv6_small",
                "model": "PP-OCRv6-small-ONNX",
                "text": "",
                "message": init_error or "RapidOCR 初始化失败。",
                "image_path": str(path),
            }
        )

    try:
        result = engine(str(path))
        text, regions = _rapidocr_regions(result, _rapidocr_png_size(path))
        return finish(
            {
                "available": True,
                "execution_success": True,
                "usable": bool(text or regions),
                "status": "ok" if text or regions else "empty",
                "backend": "rapidocr_ppocrv6_small",
                "model": "PP-OCRv6-small-ONNX",
                "text": text,
                "regions": regions,
                "language": language,
                "engine_elapsed_ms": round(float(getattr(result, "elapse", 0.0) or 0.0) * 1000.0, 3),
                "image_path": str(path),
            }
        )
    except Exception as exc:
        return finish(
            {
                "available": True,
                "execution_success": False,
                "usable": False,
                "status": "error",
                "backend": "rapidocr_ppocrv6_small",
                "model": "PP-OCRv6-small-ONNX",
                "text": "",
                "message": str(exc),
                "image_path": str(path),
            }
        )


def focused_ocr_image(
    image_path: str,
    regions: list[dict[str, Any]],
    *,
    language: str = "auto",
    psm: int = 6,
    timeout_sec: int = 12,
    scale: float = 2.0,
) -> dict[str, Any]:
    """Run one enlarged OCR pass over caller-supplied layout regions.

    This is intentionally a narrow fallback after normal OCR. It never
    searches for regions or applies pixel enhancement; each configured region
    is cropped and enlarged once, then sent to the existing OCR backend.
    """

    source = Path(image_path).expanduser().resolve()
    if not source.exists():
        raise PlatformAutomationError(f"找不到图片: {source}")
    if not pillow_available():
        return {
            "available": False,
            "status": "missing_dependency",
            "backend": "ocr_focus",
            "text": "",
            "message": "区域放大 OCR 需要 Pillow；可安装项目的 [ocr-focus] 可选依赖。",
            "image_path": str(source),
        }

    attempts: list[dict[str, Any]] = []
    valid_regions = [item for item in regions if isinstance(item, dict)]
    per_attempt_timeout = max(
        2,
        min(8, int(timeout_sec) // max(1, len(valid_regions))),
    )
    with temporary_zoomed_regions(source, valid_regions, scale=scale) as variants:
        for variant in variants:
            result = ocr_image(
                str(variant["path"]),
                language=language,
                psm=psm,
                timeout_sec=per_attempt_timeout,
            )
            mapped = map_zoomed_ocr_result(result, variant, source_path=source)
            attempts.append(
                {
                    "region_index": variant.get("region_index"),
                    "label": variant.get("label"),
                    "status": mapped.get("status"),
                    "backend": mapped.get("backend"),
                    "char_count": len(str(mapped.get("text") or "")),
                    # Keep the bounded per-region text so callers can still
                    # assign a result to its configured layout region when a
                    # backend returns aggregate text without word boxes.
                    "text": str(mapped.get("text") or "")[:4000],
                    "result": mapped,
                }
            )

    selected_regions: list[dict[str, Any]] = []
    fallback_text: list[str] = []
    seen: set[str] = set()
    for attempt in attempts:
        result = attempt["result"]
        # Prefer line regions over aggregate OCR text.  If the aggregate text
        # is inserted into ``seen`` first, a region with exactly the same text
        # is discarded and the downstream spatial parser loses its bbox.
        for item in result.get("regions") or []:
            item_text = str(item.get("text") or "").strip()
            if not item_text:
                continue
            key = " ".join(item_text.split()).casefold()
            if key in seen:
                continue
            seen.add(key)
            selected_regions.append(dict(item))
        text = str(result.get("text") or "").strip()
        if text:
            key = " ".join(text.split()).casefold()
            if key not in seen:
                seen.add(key)
                fallback_text.append(text)
    selected_regions.sort(
        key=lambda item: (float(item.get("y", 0)), float(item.get("x", 0)))
    )
    merged_text = "\n".join(
        str(item.get("text") or "").strip()
        for item in selected_regions
        if str(item.get("text") or "").strip()
    )
    if not merged_text:
        merged_text = "\n".join(fallback_text)
    return {
        "available": any(bool(attempt["result"].get("available")) for attempt in attempts),
        "status": "ok" if merged_text else "empty",
        "backend": "focused_ocr",
        "text": merged_text,
        "regions": selected_regions,
        "image_path": str(source),
        "scale": max(1.5, min(float(scale), 4.0)),
        "attempts": [
            {key: value for key, value in attempt.items() if key != "result"}
            for attempt in attempts
        ],
    }
