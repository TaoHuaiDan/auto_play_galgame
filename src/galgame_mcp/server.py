from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from .core import SCHEMA_VERSION, SessionStore, new_id, utc_now
from .platform import (
    PlatformAutomationError,
    capture_screen_png,
    capture_window_png,
    click_screen as native_click_screen,
    focus_window as native_focus_window,
    ocr_image as native_ocr_image,
    send_key as native_send_key,
)
from .text import parse_screen_text


STORE = SessionStore()

mcp = FastMCP(
    name="galgame-mcp",
    instructions=(
        "这是本地视觉小说自动游玩 MCP。默认 observe_game 在本机完成截图、OCR、文本解析和去重，只把 processed_text 与"
        "必要状态返回给 Codex；原始截图/OCR 保存在会话目录。已绑定窗口时默认使用完整窗口捕获，未绑定时使用全屏桌面捕获；"
        "window 模式捕获完整窗口而不是裁切。OCR 失败时才请求 include_image=true。"
    ),
)


@mcp.tool()
def start_session(
    game_name: str,
    session_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """创建并激活一个新的视觉小说会话；数据保存在 GALGAME_MCP_DATA_DIR 或项目目录下。"""

    return STORE.create_session(game_name=game_name, session_id=session_id, metadata=metadata)


@mcp.tool()
def list_sessions(limit: int = 20) -> list[dict[str, Any]]:
    """列出最近的剧情会话，便于从中断处恢复。"""

    return STORE.list_sessions(limit=limit)


@mcp.tool()
def set_active_session(session_id: str) -> dict[str, Any]:
    """切换当前活动会话。"""

    return STORE.set_active(session_id)


@mcp.tool()
def get_current_state(session_id: str | None = None) -> dict[str, Any]:
    """获取当前场景、台词、变量和未处理选项。"""

    return STORE.get_current_state(session_id=session_id)


@mcp.tool()
def record_scene(
    scene_id: str,
    location: str | None = None,
    background: str | None = None,
    metadata: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """记录场景边界或场景切换，不要求同时有台词。"""

    return STORE.record_scene(
        scene_id=scene_id,
        location=location,
        background=background,
        metadata=metadata,
        session_id=session_id,
    )


@mcp.tool()
def record_observation(
    raw_text: str | None = None,
    text: str | None = None,
    speaker: str | None = None,
    scene_id: str | None = None,
    location: str | None = None,
    choices: list[str] | None = None,
    selected_index: int | None = None,
    screenshot_path: str | None = None,
    source: str = "codex",
    confidence: float | None = None,
    note: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """一次性保存模型从画面得到的台词、角色、场景、选项、截图和备注。"""

    return STORE.record_observation(
        raw_text=raw_text,
        text=text,
        speaker=speaker,
        scene_id=scene_id,
        location=location,
        choices=choices,
        selected_index=selected_index,
        screenshot_path=screenshot_path,
        source=source,
        confidence=confidence,
        note=note,
        session_id=session_id,
    )


@mcp.tool()
def parse_text(raw_text: str) -> dict[str, Any]:
    """把 OCR、剪贴板或模型转写的原始文本拆成角色、对白、选项和未解析行。"""

    return _public_parsed_text(parse_screen_text(raw_text))


@mcp.tool()
def record_parsed_text(
    raw_text: str,
    scene_id: str | None = None,
    location: str | None = None,
    screenshot_path: str | None = None,
    source: str = "ocr",
    session_id: str | None = None,
) -> dict[str, Any]:
    """解析原始文本并立即把结构化对白/选项写入当前会话。"""

    parsed = parse_screen_text(raw_text)
    dialogue = parsed.get("dialogue") or None
    choices = parsed.get("choices") or None
    if not dialogue and not choices:
        return {
            "parsed": _public_parsed_text(parsed),
            "recorded": False,
            "message": "没有识别到可记录的对白或选项",
        }
    recorded = STORE.record_observation(
        raw_text=raw_text,
        text=dialogue,
        speaker=parsed.get("speaker"),
        scene_id=scene_id,
        location=location,
        choices=choices,
        screenshot_path=screenshot_path,
        source=source,
        confidence=parsed.get("confidence"),
        session_id=session_id,
    )
    return {
        "parsed": _public_parsed_text(parsed),
        "recorded": {
            "observation_id": recorded.get("observation_id"),
            "event_ids": recorded.get("event_ids", []),
        },
    }


@mcp.tool()
def record_dialogue(
    text: str,
    speaker: str | None = None,
    scene_id: str | None = None,
    translation: str | None = None,
    source: str = "manual",
    confidence: float | None = None,
    tags: list[str] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """记录一条可检索的对白或旁白。"""

    return STORE.record_dialogue(
        text=text,
        speaker=speaker,
        scene_id=scene_id,
        translation=translation,
        source=source,
        confidence=confidence,
        tags=tags,
        session_id=session_id,
    )


@mcp.tool()
def record_choice(
    options: list[str],
    prompt: str | None = None,
    scene_id: str | None = None,
    selected_index: int | None = None,
    choice_id: str | None = None,
    result: str | None = None,
    source: str = "manual",
    session_id: str | None = None,
) -> dict[str, Any]:
    """记录选项；selected_index 从 1 开始，不填表示等待 Codex 决策。"""

    return STORE.record_choice(
        options=options,
        prompt=prompt,
        scene_id=scene_id,
        selected_index=selected_index,
        choice_id=choice_id,
        result=result,
        source=source,
        session_id=session_id,
    )


@mcp.tool()
def set_story_variable(
    name: str,
    value: str,
    value_type: str = "string",
    reason: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """保存路线旗标或其他剧情变量；value_type 支持 string、boolean、integer、number、json、null。"""

    return STORE.set_story_variable(
        name=name,
        value=value,
        value_type=value_type,
        reason=reason,
        session_id=session_id,
    )


@mcp.tool()
def add_note(text: str, kind: str = "note", session_id: str | None = None) -> dict[str, Any]:
    """记录给后续 Codex 决策使用的推断、疑点或人工备注。"""

    return STORE.add_note(text=text, kind=kind, session_id=session_id)


def _capture_for_session(
    window_title: str | None = None,
    capture_mode: str = "desktop",
    session_id: str | None = None,
) -> tuple[dict[str, Any], Path]:
    session = STORE.get_session(session_id=session_id)
    requested_mode = (capture_mode or "auto").strip().lower()
    if requested_mode not in {"auto", "desktop", "window"}:
        raise ValueError("capture_mode 必须是 auto、desktop 或 window")
    mode = "window" if requested_mode == "auto" and window_title else requested_mode
    if mode == "auto":
        mode = "desktop"
    if mode == "window":
        if not window_title:
            raise ValueError("capture_mode=window 时必须提供 window_title 或先 attach_game")
        png_bytes, dimensions = capture_window_png(window_title)
    else:
        png_bytes, dimensions = capture_screen_png()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    destination = STORE.session_dir(session["session_id"]) / "frames" / f"{timestamp}_{new_id('frame')}.png"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(png_bytes)
    event = STORE.record_screenshot(
        path=str(destination),
        width=dimensions["width"],
        height=dimensions["height"],
        session_id=session["session_id"],
    )
    payload = {
        "image_path": str(destination),
        "width": dimensions["width"],
        "height": dimensions["height"],
        "capture_scope": "window_full" if mode == "window" else "primary_screen_full",
        "event_id": event["event_id"],
        "captured_at": utc_now(),
        "capture_mode": "window" if mode == "window" else "desktop_fullscreen",
    }
    if mode == "window":
        payload["window"] = dimensions
    return payload, destination


def _capture_result(payload: dict[str, Any], image_path: Path, include_image: bool) -> Any:
    if include_image:
        return [json.dumps(payload, ensure_ascii=False), Image(path=image_path)]
    return payload


def _public_parsed_text(parsed: dict[str, Any]) -> dict[str, Any]:
    """Remove raw OCR duplication before a result crosses the MCP boundary."""

    return {
        "speaker": parsed.get("speaker"),
        "dialogue": parsed.get("dialogue"),
        "choices": parsed.get("choices") or [],
        "choice_records": [
            {
                "option_id": record.get("option_id"),
                "label": record.get("label"),
                "line": record.get("line"),
            }
            for record in parsed.get("choice_records", [])
        ],
        "unparsed_lines": parsed.get("unparsed_lines") or [],
        "line_count": parsed.get("line_count", 0),
        "confidence": parsed.get("confidence", 0.0),
    }


def _compact_ocr_result(result: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "available": bool(result.get("available")),
        "status": result.get("status"),
        "backend": result.get("backend"),
    }
    if result.get("language"):
        compact["language"] = result["language"]
    if result.get("message"):
        compact["message"] = result["message"]
    return compact


def _same_processed_text(session: dict[str, Any], parsed: dict[str, Any]) -> bool:
    state = session.get("current_state", {})
    dialogue = parsed.get("dialogue") or None
    choices = list(parsed.get("choices") or [])
    current_choices = [
        item.get("label") if isinstance(item, dict) else str(item)
        for item in state.get("choices", [])
    ]
    return bool(dialogue or choices) and (
        state.get("text") == dialogue
        and (state.get("speaker") or "旁白") == (parsed.get("speaker") or "旁白")
        and current_choices == choices
    )


def _process_local_text(
    payload: dict[str, Any],
    image_path: Path,
    session: dict[str, Any],
    *,
    ocr: bool,
    record_text: bool,
    language: str,
    include_raw_text: bool,
) -> dict[str, Any]:
    if not ocr:
        return payload
    ocr_result = native_ocr_image(str(image_path), language=language)
    raw_text = str(ocr_result.get("text") or "").strip()
    payload["ocr"] = _compact_ocr_result(ocr_result)
    if include_raw_text and raw_text:
        payload["raw_text"] = raw_text
    if not raw_text:
        return payload

    parsed = parse_screen_text(raw_text)
    public_parsed = _public_parsed_text(parsed)
    payload["processed_text"] = public_parsed
    if not record_text or not (parsed.get("dialogue") or parsed.get("choices")):
        return payload
    if _same_processed_text(session, parsed):
        payload["deduplicated"] = True
        return payload

    recorded = STORE.record_observation(
        raw_text=raw_text,
        text=parsed.get("dialogue") or None,
        speaker=parsed.get("speaker"),
        choices=parsed.get("choices") or None,
        screenshot_path=str(image_path),
        source=ocr_result.get("backend") or "local_ocr",
        confidence=parsed.get("confidence"),
        session_id=session["session_id"],
    )
    payload["recorded"] = {
        "observation_id": recorded.get("observation_id"),
        "event_ids": recorded.get("event_ids", []),
    }
    return payload


@mcp.tool()
def attach_game(
    window_title: str,
    advance_key: str = "SPACE",
    choice_mode: str = "number",
    session_id: str | None = None,
) -> dict[str, Any]:
    """绑定已经打开的游戏窗口，并保存通用推进键与选项输入模式。"""

    session = STORE.get_session(session_id=session_id)
    focused = native_focus_window(window_title)
    configuration = STORE.configure_game(
        window_title=window_title,
        advance_key=advance_key,
        choice_mode=choice_mode,
        session_id=session["session_id"],
    )
    STORE.record_action(
        "attach_game",
        {"window_title": window_title, "advance_key": advance_key, "choice_mode": choice_mode, **focused},
        session_id=session["session_id"],
    )
    return {"attached": True, "focus": focused, **configuration}


@mcp.tool(structured_output=False)
def observe_game(
    window_title: str | None = None,
    ocr: bool = True,
    record_text: bool = True,
    language: str = "auto",
    include_image: bool = False,
    session_id: str | None = None,
    capture_mode: str = "auto",
    focus_before_capture: bool | None = None,
    include_raw_text: bool = False,
) -> Any:
    """本地截图并 OCR，默认只返回精简结构化文本；window 模式可读取被遮挡的整窗。"""

    session = STORE.get_session(session_id=session_id)
    title = window_title or session.get("game", {}).get("window_title")
    mode = (capture_mode or "auto").strip().lower()
    uses_window = mode == "window" or (mode == "auto" and bool(title))
    should_focus = not uses_window if focus_before_capture is None else bool(focus_before_capture)
    if title and should_focus:
        focused = native_focus_window(title)
        STORE.record_action("focus_window", {"title": title, **focused}, session_id=session["session_id"])
    payload, image_path = _capture_for_session(
        window_title=title,
        capture_mode=mode,
        session_id=session["session_id"],
    )
    payload = _process_local_text(
        payload,
        image_path,
        session,
        ocr=ocr,
        record_text=record_text,
        language=language,
        include_raw_text=include_raw_text,
    )
    return _capture_result(payload, image_path, include_image)


@mcp.tool(structured_output=False)
def advance_game(
    wait_seconds: float = 0.15,
    ocr: bool = True,
    record_text: bool = True,
    language: str = "auto",
    include_image: bool = False,
    session_id: str | None = None,
    capture_mode: str = "auto",
    include_raw_text: bool = False,
) -> Any:
    """用绑定的 advance_key 推进一段对白/动画，再返回新的截图。"""

    session = STORE.get_session(session_id=session_id)
    game = session.get("game", {})
    title = game.get("window_title")
    if title:
        focused = native_focus_window(title)
    else:
        focused = None
    control = game.get("control", {})
    key = control.get("advance_key") or "SPACE"
    key_result = native_send_key(key=key, presses=1, interval_ms=0)
    duration = max(0.0, min(float(wait_seconds), 10.0))
    if duration:
        time.sleep(duration)
    action = STORE.record_action(
        "advance_game",
        {"key": key, "wait_seconds": duration, "focus": focused, **key_result},
        session_id=session["session_id"],
    )
    payload, image_path = _capture_for_session(
        window_title=title,
        capture_mode=capture_mode,
        session_id=session["session_id"],
    )
    payload["action_event"] = action
    payload = _process_local_text(
        payload,
        image_path,
        session,
        ocr=ocr,
        record_text=record_text,
        language=language,
        include_raw_text=include_raw_text,
    )
    return _capture_result(payload, image_path, include_image)


@mcp.tool(structured_output=False)
def select_choice(
    option_index: int,
    choice_id: str | None = None,
    mode: str | None = None,
    key: str | None = None,
    x: int | None = None,
    y: int | None = None,
    wait_seconds: float = 0.25,
    ocr: bool = True,
    record_text: bool = True,
    language: str = "auto",
    include_image: bool = False,
    session_id: str | None = None,
    capture_mode: str = "auto",
    include_raw_text: bool = False,
) -> Any:
    """选择视觉小说选项；传 choice_id 会同步把对应记录标记为已选择。"""

    if option_index < 1 or option_index > 99:
        raise ValueError("option_index 必须在 1-99 之间")
    session = STORE.get_session(session_id=session_id)
    game = session.get("game", {})
    title = game.get("window_title")
    if title:
        focused = native_focus_window(title)
    else:
        focused = None
    selected_mode = (mode or game.get("control", {}).get("choice_mode") or "number").lower()
    action_payload: dict[str, Any] = {
        "option_index": option_index,
        "mode": selected_mode,
        "focus": focused,
    }
    if selected_mode == "number":
        action_payload["input"] = native_send_key(str(option_index), presses=1, interval_ms=0)
    elif selected_mode == "arrow":
        native_send_key("HOME", presses=1, interval_ms=0)
        if option_index > 1:
            native_send_key("DOWN", presses=option_index - 1, interval_ms=10)
        action_payload["input"] = native_send_key("ENTER", presses=1, interval_ms=0)
    elif selected_mode == "key":
        if not key:
            raise ValueError("mode=key 时必须提供 key")
        action_payload["input"] = native_send_key(key, presses=1, interval_ms=0)
    elif selected_mode == "click":
        if x is None or y is None:
            raise ValueError("mode=click 时必须提供 x 和 y")
        action_payload["input"] = native_click_screen(x=x, y=y, button="left", clicks=1, interval_ms=100)
    else:
        raise ValueError("mode 必须是 number、arrow、key 或 click")
    duration = max(0.0, min(float(wait_seconds), 10.0))
    if duration:
        time.sleep(duration)
    resolved = None
    if choice_id:
        resolved = STORE.resolve_choice(
            choice_id=choice_id,
            selected_index=option_index,
            source="autoplay",
            session_id=session["session_id"],
        )
        action_payload["choice_id"] = choice_id
    action = STORE.record_action("select_choice", action_payload, session_id=session["session_id"])
    payload, image_path = _capture_for_session(
        window_title=title,
        capture_mode=capture_mode,
        session_id=session["session_id"],
    )
    payload["action_event"] = action
    if resolved is not None:
        payload["choice_resolution"] = resolved
    payload = _process_local_text(
        payload,
        image_path,
        session,
        ocr=ocr,
        record_text=record_text,
        language=language,
        include_raw_text=include_raw_text,
    )
    return _capture_result(payload, image_path, include_image)


@mcp.tool(structured_output=False)
def capture_screen(
    include_image: bool = False,
    session_id: str | None = None,
) -> Any:
    """截取完整 Windows 主屏，保存到当前会话 frames，并可直接返回 MCP 图片内容。"""

    payload, image_path = _capture_for_session(session_id=session_id)
    return _capture_result(payload, image_path, include_image)


@mcp.tool()
def ocr_image(
    image_path: str,
    language: str = "auto",
    psm: int = 6,
    record: bool = False,
    session_id: str | None = None,
    include_raw_text: bool = False,
) -> dict[str, Any]:
    """在本地 OCR 并返回结构化文本；原始 OCR 默认只保存到本地会话。"""

    result = native_ocr_image(image_path=image_path, language=language, psm=psm)
    raw_text = str(result.get("text") or "").strip()
    response = _compact_ocr_result(result)
    response["image_path"] = result.get("image_path") or str(Path(image_path).expanduser().resolve())
    if raw_text:
        parsed = parse_screen_text(raw_text)
        response["processed_text"] = _public_parsed_text(parsed)
        if include_raw_text:
            response["raw_text"] = raw_text
    if record and raw_text:
        parsed = parse_screen_text(raw_text)
        observation = STORE.record_observation(
            raw_text=raw_text,
            text=parsed.get("dialogue") or None,
            speaker=parsed.get("speaker"),
            choices=parsed.get("choices") or None,
            screenshot_path=result.get("image_path"),
            source=result.get("backend") or "local_ocr",
            confidence=parsed.get("confidence"),
            session_id=session_id,
        )
        response["recorded_observation"] = {
            "observation_id": observation.get("observation_id"),
            "event_ids": observation.get("event_ids", []),
        }
    return response


@mcp.tool()
def focus_game_window(title: str, session_id: str | None = None) -> dict[str, Any]:
    """按标题激活游戏窗口，后续按键/点击会发送到该窗口。"""

    result = native_focus_window(title)
    if session_id is not None or _active_session_exists():
        STORE.record_action("focus_window", {"title": title, **result}, session_id=session_id)
    return result


@mcp.tool()
def press_key(
    key: str,
    presses: int = 1,
    interval_ms: int = 80,
    record: bool = True,
    session_id: str | None = None,
) -> dict[str, Any]:
    """向当前前台窗口发送按键或组合键，例如 ENTER、SPACE、CTRL+S、ARROWDOWN。"""

    result = native_send_key(key=key, presses=presses, interval_ms=interval_ms)
    if record and (session_id is not None or _active_session_exists()):
        STORE.record_action("press_key", result, session_id=session_id)
    return result


@mcp.tool()
def click_screen(
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
    interval_ms: int = 100,
    record: bool = True,
    session_id: str | None = None,
) -> dict[str, Any]:
    """在屏幕坐标点击游戏界面。"""

    result = native_click_screen(x=x, y=y, button=button, clicks=clicks, interval_ms=interval_ms)
    if record and (session_id is not None or _active_session_exists()):
        STORE.record_action("click_screen", result, session_id=session_id)
    return result


@mcp.tool()
def wait(seconds: float = 1.0, record: bool = True, session_id: str | None = None) -> dict[str, Any]:
    """等待游戏动画/转场完成，最长 30 秒。"""

    duration = max(0.0, min(float(seconds), 30.0))
    time.sleep(duration)
    result = {"seconds": duration}
    if record and (session_id is not None or _active_session_exists()):
        STORE.record_action("wait", result, session_id=session_id)
    return result


@mcp.tool()
def search_story(query: str, limit: int = 20, session_id: str | None = None) -> dict[str, Any]:
    """在当前会话的对白、选项、变量、备注和操作事件中检索。"""

    return STORE.search_story(query=query, limit=limit, session_id=session_id)


@mcp.tool()
def get_codex_context(
    recent_events: int = 8,
    include_markdown: bool = False,
    compact: bool = True,
    session_id: str | None = None,
) -> dict[str, Any]:
    """返回去除原始 OCR、截图路径和重复事件的精简路线上下文。"""

    return STORE.build_context(
        recent_events=recent_events,
        include_markdown=include_markdown,
        compact=compact,
        session_id=session_id,
    )


@mcp.tool()
def export_session(
    output_format: str = "json",
    filename: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """把会话导出为 JSON、Markdown 或 JSONL；文件始终写在当前会话目录内。"""

    return STORE.export_session(output_format=output_format, filename=filename, session_id=session_id)


@mcp.tool()
def close_session(session_id: str | None = None) -> dict[str, Any]:
    """关闭会话并清除活动会话指针；已写入的数据仍可通过 list_sessions 找回。"""

    return STORE.close_session(session_id=session_id)


@mcp.resource("galgame://active/context")
def active_context_resource() -> str:
    """只读资源：当前活动会话的精简 Codex 上下文 JSON。"""

    return json.dumps(
        STORE.build_context(recent_events=12, include_markdown=False, compact=True),
        ensure_ascii=False,
        indent=2,
    )


def _active_session_exists() -> bool:
    try:
        STORE.get_session()
    except Exception:
        return False
    return True


def main() -> None:
    """Run the server over MCP stdio, the transport used by Codex local servers."""

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
