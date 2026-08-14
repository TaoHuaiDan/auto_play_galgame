from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wintypes
import importlib
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path
from typing import Any


class PlatformAutomationError(RuntimeError):
    """Raised when a local screen/input operation is unavailable or fails."""


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
    "UP": 0x26,
    "RIGHT": 0x27,
    "DOWN": 0x28,
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


def _send_key_event(vk: int, key_up: bool = False) -> None:
    if sys.platform != "win32":
        raise PlatformAutomationError("当前实现的按键后端只支持 Windows")
    flags = 0x0002 if key_up else 0
    event = _Input(type=1, ki=_KeyInput(wVk=vk, dwFlags=flags))
    sent = ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_Input))
    if sent != 1:
        raise PlatformAutomationError(f"SendInput 按键失败，错误码={ctypes.GetLastError()}")


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
    ctypes.windll.user32.SetCursorPos(int(x), int(y))
    for index in range(clicks):
        down_event = _Input(type=0, mi=_MouseInput(dwFlags=down))
        up_event = _Input(type=0, mi=_MouseInput(dwFlags=up))
        ctypes.windll.user32.SendInput(1, ctypes.byref(down_event), ctypes.sizeof(_Input))
        ctypes.windll.user32.SendInput(1, ctypes.byref(up_event), ctypes.sizeof(_Input))
        if index + 1 < clicks and delay:
            time.sleep(delay)
    return {"x": int(x), "y": int(y), "button": button, "clicks": clicks}


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
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    if not user32.SetForegroundWindow(hwnd):
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


async def _windows_ocr_async(path: Path, language: str) -> dict[str, Any]:
    """Use the built-in Windows.Media.Ocr API when a PyWinRT bridge exists."""

    import_error: Exception | None = None
    modules: tuple[Any, Any, Any, Any] | None = None
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
        engine = None
        selected_language = "user_profile"
        language_tag = _windows_ocr_language(language)
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
        return {
            "available": True,
            "status": "ok",
            "backend": "windows_ocr",
            "text": "\n".join(lines),
            "language": selected_language,
        }
    except Exception as exc:
        return {
            "available": True,
            "status": "error",
            "backend": "windows_ocr",
            "text": "",
            "message": str(exc),
        }


def _run_windows_ocr(path: Path, language: str, timeout_sec: int) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        return await asyncio.wait_for(_windows_ocr_async(path, language), timeout=max(1, timeout_sec))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.run(run())
        except asyncio.TimeoutError:
            return {
                "available": True,
                "status": "timeout",
                "backend": "windows_ocr",
                "text": "",
                "message": "Windows OCR 超时。",
            }
        except RuntimeError as exc:
            return {
                "available": True,
                "status": "error",
                "backend": "windows_ocr",
                "text": "",
                "message": str(exc),
            }

    # FastMCP may execute a synchronous tool while its host event loop is
    # active. Run WinRT's async API in a short-lived local worker thread so
    # the MCP loop stays responsive and asyncio.run remains legal.
    result_box: list[dict[str, Any]] = []

    def worker() -> None:
        try:
            result_box.append(asyncio.run(run()))
        except asyncio.TimeoutError:
            result_box.append(
                {
                    "available": True,
                    "status": "timeout",
                    "backend": "windows_ocr",
                    "text": "",
                    "message": "Windows OCR 超时。",
                }
            )
        except Exception as exc:
            result_box.append(
                {
                    "available": True,
                    "status": "error",
                    "backend": "windows_ocr",
                    "text": "",
                    "message": str(exc),
                }
            )

    thread = threading.Thread(target=worker, name="galgame-windows-ocr", daemon=True)
    thread.start()
    thread.join(timeout=max(1, timeout_sec) + 1)
    if thread.is_alive():
        return {
            "available": True,
            "status": "timeout",
            "backend": "windows_ocr",
            "text": "",
            "message": "Windows OCR 超时。",
        }
    if result_box:
        return result_box[0]
    return {
        "available": True,
        "status": "error",
        "backend": "windows_ocr",
        "text": "",
        "message": "Windows OCR 工作线程没有返回结果。",
    }


def ocr_image(image_path: str, language: str = "auto", psm: int = 6, timeout_sec: int = 30) -> dict[str, Any]:
    """Run local OCR, preferring Windows OCR and falling back to Tesseract."""

    path = Path(image_path).expanduser().resolve()
    if not path.exists():
        raise PlatformAutomationError(f"找不到图片: {path}")
    timeout = max(1, min(int(timeout_sec), 120))
    preference = os.environ.get("GALGAME_MCP_OCR_BACKEND", "auto").strip().casefold()
    attempts: list[dict[str, Any]] = []
    if preference in {"auto", "windows", "windows_ocr"} and sys.platform == "win32":
        windows_result = _run_windows_ocr(path, language=language, timeout_sec=timeout)
        windows_result["image_path"] = str(path)
        if windows_result.get("status") == "ok":
            return windows_result
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
            return tesseract_result
        attempts.append(tesseract_result)

    messages = [item.get("message") or item.get("stderr") for item in attempts]
    messages = [str(message) for message in messages if message]
    return {
        "available": False,
        "status": "missing_dependency" if not attempts else "error",
        "backend": "local",
        "text": "",
        "message": "；".join(messages) or "未找到可用的本地 OCR 后端；可安装项目的 [windows-ocr] extra 或系统 Tesseract。",
        "image_path": str(path),
        "attempts": [{key: value for key, value in item.items() if key != "text"} for item in attempts],
    }
