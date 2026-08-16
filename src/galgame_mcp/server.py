from __future__ import annotations

import argparse
import json
import hashlib
import math
import re
import struct
import time
import copy
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from .core import SCHEMA_VERSION, SessionError, SessionStore, new_id, utc_now
from .evidence import TextEpisodeTracker, build_frame_evidence
from .platform import (
    PlatformAutomationError,
    capture_screen_png,
    capture_window_png,
    capture_window_region_png,
    click_screen as native_click_screen,
    focus_window as native_focus_window,
    hold_key as native_hold_key,
    image_motion_score as native_image_motion_score,
    ocr_image as native_ocr_image,
    rapidocr_image as native_rapidocr_image,
    get_window_rect as native_get_window_rect,
    post_window_click as native_post_window_click,
    post_window_key as native_post_window_key,
    post_window_wheel as native_post_window_wheel,
    send_key as native_send_key,
    touch_screen as native_touch_screen,
)
from .text import detect_screen_type, looks_like_ui_residue, parse_screen_text


def _parse_server_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the galgame MCP server")
    parser.add_argument(
        "--data-dir",
        dest="data_dir",
        default=None,
        help="Store session data here; overrides GALGAME_MCP_DATA_DIR and the cwd default.",
    )
    args, _unknown = parser.parse_known_args(argv)
    return args


_BOOT_ARGS = _parse_server_args()
STORE = SessionStore(root=_BOOT_ARGS.data_dir)

# The last processed dialogue snapshot lets advance_game verify a click using
# the preceding observe_game result instead of doing a second full capture and
# OCR pass before every input. It is process-local and intentionally expires;
# a stale cache must fall back to a fresh baseline capture.
_BOTTOM_SNAPSHOT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_BOTTOM_SNAPSHOT_CACHE_MAX_AGE_SECONDS = 30.0

# A VN may clear the dialogue layer briefly while changing a background,
# showing a chapter card, or finishing a short visual effect.  This budget is
# only spent after a delivered advance leaves the bottom dialogue box empty;
# normal dialogue frames keep the existing fast path.
_DEFAULT_TRANSITION_WAIT_SECONDS = 1.2
_MAX_TRANSITION_WAIT_SECONDS = 10.0
_DEFAULT_TRANSITION_ACCELERATE_DELAY_SECONDS = 0.6
_DEFAULT_TRANSITION_PROBE_INTERVAL_SECONDS = 0.2
_TRANSITION_PROBE_MAX_SAMPLES = 3
_TRANSITION_MOTION_THRESHOLD = 0.02

# Per-game timing defaults keep the current fast path unchanged.  Games with
# a typewriter effect can opt into text_hash through configure_game_timing;
# the local loop then waits for the bottom text hash to change and remain
# stable before sending the next input.
_DEFAULT_TIMING_PROFILE = {
    "strategy": "fixed",
    "post_click_wait_seconds": 0.05,
    "transition_wait_seconds": _DEFAULT_TRANSITION_WAIT_SECONDS,
    "transition_accelerate": False,
    "transition_accelerate_delay_seconds": _DEFAULT_TRANSITION_ACCELERATE_DELAY_SECONDS,
    "transition_probe_interval_seconds": _DEFAULT_TRANSITION_PROBE_INTERVAL_SECONDS,
    "settle_timeout_seconds": 4.0,
    "settle_poll_seconds": 0.12,
    "stable_samples": 3,
    "require_text_change": True,
}

# A fast dialogue-region OCR miss is not enough evidence to stop a VN.  The
# full-window fallback gets one settling second before the next background
# advance, because a recovered full frame may be the first stable frame after
# a CG/chapter transition.
_OCR_FALLBACK_SETTLE_SECONDS = 1.0

# ``play_until_choice`` is a local scheduling loop, not the story/data
# retention policy.  The default is deliberately uncapped; the normal loop
# ends at a choice, an OCR/input safety error, or the compaction threshold.
# An explicit max_steps remains available for short smoke tests.
_MAX_PLAY_STEPS = 1000
_MAX_PLAY_FRAME_ATTEMPTS = 1200

# OCR often returns these controls from the dialogue crop even when it missed
# the actual line.  Treating them as usable text would suppress the full-frame
# fallback and can make play_until_choice advance on a stale/partial frame.
_OCR_UI_ONLY_TOKENS = {
    "auto",
    "voice",
    "voic",
    "save",
    "load",
    "qsave",
    "qload",
    "system",
}

# A process-local geometry cache is deliberately used instead of trusting an
# old session file.  After the MCP is restarted, the first window observation
# must establish a fresh full-frame reference; only later observations may use
# the fast dialogue-region capture path.
_WINDOW_FULL_CAPTURE_CACHE: dict[str, dict[str, Any]] = {}


def _play_compaction_status(session_id: str) -> dict[str, Any] | None:
    """Return compaction state without breaking isolated loop unit tests."""

    try:
        return STORE.compaction_status(session_id=session_id)
    except SessionError:
        # Some server tests replace get_session with an in-memory fixture that
        # is intentionally not present in the real store. Production sessions
        # always resolve here, so this only preserves those isolated tests.
        return None


def _bounded_transition_wait(value: float, *, name: str = "transition_wait_seconds") -> float:
    try:
        return max(0.0, min(float(value), _MAX_TRANSITION_WAIT_SECONDS))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字") from exc


def _bounded_timing_number(
    value: Any,
    *,
    name: str,
    minimum: float,
    maximum: float,
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} 不能是 NaN 或无穷大")
    return max(minimum, min(number, maximum))


def _resolve_timing_profile(
    session: dict[str, Any],
    *,
    wait_seconds: float | None = None,
    transition_wait_seconds: float | None = None,
    wait_strategy: str | None = None,
) -> dict[str, Any]:
    """Merge session timing with per-call overrides without guessing a game."""

    profile = dict(_DEFAULT_TIMING_PROFILE)
    configured = (session.get("game") or {}).get("timing_profile") or {}
    if isinstance(configured, dict):
        profile.update(configured)
    if wait_seconds is not None:
        profile["post_click_wait_seconds"] = wait_seconds
    if transition_wait_seconds is not None:
        profile["transition_wait_seconds"] = transition_wait_seconds
    if wait_strategy is not None:
        profile["strategy"] = wait_strategy

    strategy = str(profile.get("strategy") or "fixed").strip().casefold()
    if strategy in {"hash", "hash_stable", "adaptive"}:
        strategy = "text_hash"
    if strategy not in {"fixed", "text_hash"}:
        raise ValueError("wait_strategy 必须是 fixed 或 text_hash")
    try:
        samples = int(profile.get("stable_samples", 3))
    except (TypeError, ValueError) as exc:
        raise ValueError("stable_samples 必须是整数") from exc
    if not 1 <= samples <= 10:
        raise ValueError("stable_samples 必须在 1 到 10 之间")
    if not isinstance(profile.get("require_text_change", True), bool):
        raise ValueError("require_text_change 必须是布尔值")
    if not isinstance(profile.get("transition_accelerate", False), bool):
        raise ValueError("transition_accelerate 必须是布尔值")
    return {
        "strategy": strategy,
        "post_click_wait_seconds": _bounded_timing_number(
            profile.get("post_click_wait_seconds", 0.05),
            name="post_click_wait_seconds",
            minimum=0.0,
            maximum=10.0,
        ),
        "transition_wait_seconds": _bounded_transition_wait(
            profile.get("transition_wait_seconds", _DEFAULT_TRANSITION_WAIT_SECONDS)
        ),
        "transition_accelerate": bool(profile.get("transition_accelerate", False)),
        "transition_accelerate_delay_seconds": _bounded_timing_number(
            profile.get(
                "transition_accelerate_delay_seconds",
                _DEFAULT_TRANSITION_ACCELERATE_DELAY_SECONDS,
            ),
            name="transition_accelerate_delay_seconds",
            minimum=0.1,
            maximum=3.0,
        ),
        "transition_probe_interval_seconds": _bounded_timing_number(
            profile.get(
                "transition_probe_interval_seconds",
                _DEFAULT_TRANSITION_PROBE_INTERVAL_SECONDS,
            ),
            name="transition_probe_interval_seconds",
            minimum=0.05,
            maximum=2.0,
        ),
        "settle_timeout_seconds": _bounded_timing_number(
            profile.get("settle_timeout_seconds", 4.0),
            name="settle_timeout_seconds",
            minimum=0.0,
            maximum=30.0,
        ),
        "settle_poll_seconds": _bounded_timing_number(
            profile.get("settle_poll_seconds", 0.12),
            name="settle_poll_seconds",
            minimum=0.02,
            maximum=2.0,
        ),
        "stable_samples": samples,
        "require_text_change": bool(profile.get("require_text_change", True)),
    }


def _transition_retry_delay(
    retry_index: int,
    requested_wait: float,
    remaining_seconds: float,
) -> float:
    """Return a short backoff delay for a possibly animated transition."""

    if remaining_seconds <= 0:
        return 0.0
    base = max(float(requested_wait), 0.10)
    delay = min(0.50, base * (1.5 ** max(0, retry_index)))
    return max(0.0, min(delay, remaining_seconds))


def _usable_story_text(value: Any) -> bool:
    """Return whether OCR text is useful enough to skip a full-frame retry."""

    compact = re.sub(r"\s+", "", str(value or ""))
    if not compact:
        return False
    if looks_like_ui_residue(compact):
        return False
    normalized = re.sub(r"[^\w]+", "", compact, flags=re.UNICODE).casefold()
    if not normalized or normalized in _OCR_UI_ONLY_TOKENS:
        return False
    ascii_tokens = re.findall(r"[a-z]+", compact.casefold())
    has_non_ascii_story_script = bool(
        re.search(r"[\u2e80-\u9fff\u3040-\u30ff\ua960-\ua97f]", compact)
    )
    if ascii_tokens and not has_non_ascii_story_script and all(
        token in _OCR_UI_ONLY_TOKENS for token in ascii_tokens
    ):
        # Also reject variants such as "AUTO 1", "VOICE 02", or
        # "SAVE/LOAD" where OCR appended a counter or split a control label.
        return False
    # Digits/symbols alone are usually counters, cursor residue, or OCR noise.
    return bool(re.search(r"[A-Za-z\u2e80-\u9fff\u3040-\u30ff\ua960-\ua97f]", compact))


def _parsed_has_story_text(parsed: dict[str, Any] | None) -> bool:
    """Check parsed OCR using only spatially classified story channels.

    ``unparsed_lines`` are intentionally excluded.  They are retained in the
    local event for visual review, but treating them as story text would let a
    chapter card or an unknown overlay bypass the focused-OCR and safety stop.
    """

    if not isinstance(parsed, dict):
        return False
    if list(parsed.get("choices") or []):
        return True
    if _usable_story_text(parsed.get("dialogue")):
        return True
    # A configured dialogue line may legitimately contain only punctuation,
    # for example an ellipsis.  The parser has already separated it from UI
    # residue, so a non-empty recognized line is still useful story state.
    if parsed.get("text_status") == "recognized" and str(parsed.get("dialogue") or "").strip():
        return True
    return False


def _mark_ocr_uncertain(
    payload: dict[str, Any],
    image_path: Path,
    *,
    reason: str = "full_window_ocr_unresolved",
) -> None:
    """Expose a hard OCR miss as a visual-review stop, never as a transition success."""

    marker = {
        "required": True,
        "status": "needs_codex_visual_review",
        "reason": reason,
        "image_path": str(image_path),
    }
    payload["ocr_uncertain"] = marker
    evidence = payload.get("evidence")
    if isinstance(evidence, dict):
        blocking = evidence.setdefault("blocking_reasons", [])
        if "ocr_uncertain" not in blocking:
            blocking.append("ocr_uncertain")
        unresolved = evidence.setdefault("unresolved_channels", [])
        if "visual_transition" not in unresolved:
            unresolved.append("visual_transition")
        evidence["ocr_uncertain"] = copy.deepcopy(marker)


mcp = FastMCP(
    name="galgame-mcp",
    instructions=(
        "这是本地视觉小说自动游玩 MCP。默认 observe_game 在本机完成截图、OCR、文本解析和去重，只把 processed_text 与"
        "必要状态返回给 Codex；原始截图/OCR 保存在会话目录。已绑定窗口时首次使用完整窗口捕获，后续对白使用快速区域捕获，未绑定时使用全屏桌面捕获；"
        "window 模式首次捕获完整窗口，后续已知对白框优先捕获文本框区域；区域 OCR 为空或不可用时自动回退完整窗口。OCR 失败时才请求 include_image=true。识别到设置/系统菜单时，只有 OCR 明确定位到"
        "回到游戏或返回游戏按钮才允许自动左键点击；不要把 ESC 当作设置恢复键。需要游戏保持后台时，使用 background_* 工具或"
        "advance_game/background=true；background_input_method 可选 post 或 send，queued/delivered 只表示系统层结果，不代表引擎已经消费。"
        "后台滚轮使用 background_scroll，同样不激活窗口或移动真实鼠标。"
        "attach_game 默认只验证并绑定窗口，不会切换前台；focus_window=true、focus_before_capture=true 或 background=false 才是明确的前台路径。"
        "ocr_region 是文本框配置；首次窗口帧仍是完整窗口，后续本地 OCR 可使用同一范围的快速区域帧。"
        "快速对白 OCR 为空、仅识别到人物名或只得到 VOICE/AUTO 等界面残留时，会自动捕获完整窗口并再次使用 Windows OCR；"
        "完整窗口的 Windows OCR 仍没有可用剧情文本时，才会在同一张完整窗口截图上调用可选的 RapidOCR PP-OCRv6-small ONNX；"
        "RapidOCR 只作为失败保底，不参与快速对白区域的正常路径，也不再执行旧的 2 倍 focused OCR；"
        "两个后端共用同一套 layout_profile 空间分类和文本解析；execution_success、usable、story_usable 和耗时会记录在 ocr_backends。"
        "RapidOCR 已识别出正常剧情且未知框全部在剧情区域外时，未知行只记录在 processed_text/evidence，不单独阻断；剧情区域内未知框仍阻断；"
        "不做全屏候选搜索、对比度增强或多轮重试。Windows OCR 与 RapidOCR 仍无法确认时会返回"
        "ocr_uncertain、保存截图并请求 Codex 视觉复核，自动游玩会在此处停止，不会盲点。"
        "不同游戏的对白框、姓名框、选项区域和姓名/对白符号通过 configure_game_layout 写入当前会话，解析器不按游戏标题猜测符号。"
        "ocr_ignore_regions 可按坐标过滤确认无关的标题栏、固定 Logo 或底部 UI，ocr_blacklist 可按 exact/contains/regex 过滤固定 OCR 文本；动态章节标题不要整块忽略；"
        "过滤项仍保留在 raw_text、ignored_lines 和 evidence 中，不能覆盖对白或选项区域。"
        "推进后对白框暂时为空时，advance_game/play_until_choice 会在 transition_wait_seconds 的有界等待窗口内本地重试；默认不会发送额外点击。"
        "只有配置 transition_accelerate=true 且完整窗口连续探测确认画面发生明显转场时，才最多额外点击一次；稳定画面 OCR 为空仍返回 ocr_uncertain。"
        "等待策略通过 configure_game_timing 按游戏配置；fixed 适合关闭打字机动画的游戏，text_hash 会要求点击后底部文本先变化并连续稳定若干次，才允许下一次输入。"
        "需要连续无人值守推进时使用 play_until_choice；默认不设步数或 batch 字符上限，"
        "它在本地循环 OCR、记录对白并推进，直到出现游戏选项、OCR/输入安全错误或 compaction_due，"
        "期间不会逐帧把结果发给 Codex。max_steps/max_batch_chars 仅适合显式冒烟测试。"
        "不同游戏的命名输入通过 configure_game_actions 配置，再用 perform_game_action 执行；动作只允许 click/key/scroll/hold/wait/focus 等安全类型，不能执行任意代码。"
        "当 get_codex_context 返回 compaction.summary_due=true 时，调用 get_compaction_request 取得一个有界原始事件段，"
        "由 Codex 按 summary_contract 生成详细结构化总结，再调用 save_compaction；只有校验通过后 MCP 才会清除对应的原始事件，"
        "并删除不再被活动状态引用的 frames 原始截图，保留 compactions/ 下的省流文件。"
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
def get_storage_info() -> dict[str, Any]:
    """返回当前 MCP 会话数据目录及其配置来源。"""

    return STORE.storage_info()


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
    noise_flags: list[dict[str, Any]] | None = None,
    evidence: dict[str, Any] | None = None,
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
        noise_flags=noise_flags,
        evidence=evidence,
        note=note,
        session_id=session_id,
    )


@mcp.tool()
def parse_text(
    raw_text: str,
    layout_profile: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """解析文本；可用 layout_profile 临时覆盖当前会话的游戏布局配置。"""

    profile = layout_profile if layout_profile is not None else _session_layout_profile(session_id)
    return _public_parsed_text(parse_screen_text(raw_text, layout_profile=profile))


@mcp.tool()
def record_parsed_text(
    raw_text: str,
    scene_id: str | None = None,
    location: str | None = None,
    screenshot_path: str | None = None,
    source: str = "ocr",
    session_id: str | None = None,
    layout_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """解析原始文本并立即把结构化对白/选项写入当前会话。"""

    profile = layout_profile if layout_profile is not None else _session_layout_profile(session_id)
    parsed = parse_screen_text(raw_text, layout_profile=profile)
    evidence = build_frame_evidence(
        parsed,
        screen_type=parsed.get("screen_type"),
        ocr_available=True,
    )
    if parsed.get("screen_type") == "settings":
        return {
            "parsed": _public_parsed_text(parsed),
            "evidence": evidence,
            "recorded": False,
            "message": "识别为设置/系统菜单，未将控件记录为剧情对白或选项",
        }
    dialogue = parsed.get("dialogue") or None
    choices = parsed.get("choices") or None
    if not dialogue and not choices:
        return {
            "parsed": _public_parsed_text(parsed),
            "evidence": evidence,
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
        noise_flags=parsed.get("noise_flags"),
        evidence=evidence,
        session_id=session_id,
    )
    return {
        "parsed": _public_parsed_text(parsed),
        "evidence": evidence,
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
def dismiss_choice(
    choice_id: str,
    reason: str = "not_a_choice",
    source: str = "visual_review",
    session_id: str | None = None,
) -> dict[str, Any]:
    """Dismiss an OCR choice candidate after visual review without selecting it."""

    return STORE.dismiss_choice(
        choice_id=choice_id,
        reason=reason,
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
    fast_region: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    session = STORE.get_session(session_id=session_id)
    requested_mode = (capture_mode or "auto").strip().lower()
    if requested_mode not in {"auto", "desktop", "window"}:
        raise ValueError("capture_mode 必须是 auto、desktop 或 window")
    mode = "window" if requested_mode == "auto" and window_title else requested_mode
    if mode == "auto":
        mode = "desktop"
    capture_fallback: dict[str, Any] | None = None
    capture_region: dict[str, Any] | None = None
    if mode == "window":
        if not window_title:
            raise ValueError("capture_mode=window 时必须提供 window_title 或先 attach_game")
        cache_key = str(window_title).strip().casefold()
        cached_geometry = _WINDOW_FULL_CAPTURE_CACHE.get(cache_key)
        if fast_region is not None and cached_geometry is not None:
            try:
                # GetWindowRect is cheap and lets a moved/resized window
                # invalidate the old normalized region without another full
                # pixel capture.
                current_window = native_get_window_rect(window_title)
                cached_hwnd = cached_geometry.get("hwnd")
                current_hwnd = current_window.get("hwnd")
                if cached_hwnd is not None and current_hwnd is not None and int(cached_hwnd) != int(current_hwnd):
                    raise PlatformAutomationError("同名窗口句柄已变化，需要重新建立完整窗口基准")
                full_width = int(current_window["width"])
                full_height = int(current_window["height"])
                resolved_region = _resolve_ocr_region(
                    fast_region,
                    width=full_width,
                    height=full_height,
                )
                if resolved_region is None:
                    raise ValueError("fast_region 不能为空")
                png_bytes, region_dimensions = capture_window_region_png(
                    window_title,
                    resolved_region["x"],
                    resolved_region["y"],
                    resolved_region["width"],
                    resolved_region["height"],
                )
                # The image dimensions are the region dimensions, while the
                # nested window object remains the complete window geometry.
                # This distinction is what keeps background clicks in the
                # correct coordinate space.
                window_dimensions = dict(current_window)
                window_dimensions.update(
                    {
                        key: value
                        for key, value in region_dimensions.items()
                        if key in {"hwnd", "title", "capture_method", "occluded_capture", "minimized"}
                    }
                )
                dimensions = dict(region_dimensions)
                dimensions["full_window_width"] = full_width
                dimensions["full_window_height"] = full_height
                dimensions["region_x"] = resolved_region["x"]
                dimensions["region_y"] = resolved_region["y"]
                capture_region = {
                    **resolved_region,
                    "coordinate_space": "window_pixels",
                }
                dimensions["window"] = window_dimensions
                capture_scope = "window_dialogue_region"
            except (PlatformAutomationError, KeyError, TypeError, ValueError) as exc:
                # PrintWindow is the correctness fallback for games whose
                # occluded GPU surface is not exposed through GetWindowDC.
                capture_fallback = {
                    "from": "window_dialogue_region",
                    "to": "window_full",
                    "reason": str(exc),
                }
                png_bytes, dimensions = capture_window_png(window_title)
                capture_scope = "window_full"
        else:
            png_bytes, dimensions = capture_window_png(window_title)
            capture_scope = "window_full"
        # Any successful full capture (including a fallback) refreshes the
        # reference needed by subsequent fast captures.
        if capture_scope == "window_full":
            _WINDOW_FULL_CAPTURE_CACHE[cache_key] = {
                "x": int(dimensions.get("x", 0)),
                "y": int(dimensions.get("y", 0)),
                "width": int(dimensions["width"]),
                "height": int(dimensions["height"]),
                "hwnd": dimensions.get("hwnd"),
                "captured_at": time.monotonic(),
            }
    else:
        png_bytes, dimensions = capture_screen_png()
        capture_scope = "primary_screen_full"
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
        "capture_scope": capture_scope,
        "event_id": event["event_id"],
        "captured_at": utc_now(),
        "capture_mode": "window" if mode == "window" else "desktop_fullscreen",
    }
    if mode == "window":
        if capture_scope == "window_dialogue_region":
            payload["window"] = dimensions.pop("window")
            payload["capture_region"] = capture_region
        else:
            payload["window"] = dimensions
    if capture_fallback is not None:
        payload["capture_fallback"] = capture_fallback
    return payload, destination


def _capture_result(payload: dict[str, Any], image_path: Path, include_image: bool) -> Any:
    public_payload = {key: value for key, value in payload.items() if not key.startswith("_")}
    visual_review = isinstance(public_payload.get("ocr_uncertain"), dict) and bool(
        public_payload["ocr_uncertain"].get("required")
    )
    if include_image or visual_review:
        return [json.dumps(public_payload, ensure_ascii=False), Image(path=image_path)]
    return public_payload


def _public_parsed_text(parsed: dict[str, Any]) -> dict[str, Any]:
    """Remove raw OCR duplication before a result crosses the MCP boundary."""

    public = {
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
        "noise_flags": copy.deepcopy(parsed.get("noise_flags") or []),
    }
    for key in ("ui_lines", "unknown_lines", "ignored_lines", "text_status"):
        if parsed.get(key):
            public[key] = copy.deepcopy(parsed[key])
    # An explicit empty list is meaningful here: it says the parser had OCR
    # geometry and confirmed that all unknown boxes were outside the story
    # regions.  Dropping it would make Evidence fall back to the old,
    # fail-closed interpretation at the MCP boundary.
    if "unknown_story_lines" in parsed:
        public["unknown_story_lines"] = copy.deepcopy(parsed.get("unknown_story_lines") or [])
    if parsed.get("screen_type"):
        public["screen_type"] = parsed["screen_type"]
    return public


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
    if result.get("ocr_region"):
        compact["ocr_region"] = result["ocr_region"]
    for key in ("execution_success", "usable"):
        if key in result:
            compact[key] = bool(result[key])
    for key in ("elapsed_ms", "engine_elapsed_ms"):
        if key in result:
            try:
                compact[key] = round(float(result[key]), 3)
            except (TypeError, ValueError):
                pass
    if result.get("model"):
        compact["model"] = result["model"]
    return compact


def _ocr_backend_record(
    result: dict[str, Any],
    parsed: dict[str, Any] | None,
    *,
    include_raw_text: bool,
) -> dict[str, Any]:
    """Persist bounded backend diagnostics without changing the OCR contract."""

    record = _compact_ocr_result(result)
    record["story_usable"] = _parsed_has_story_text(parsed)
    record["region_count"] = len(result.get("regions") or [])
    if isinstance(parsed, dict):
        record["unknown_line_count"] = len(parsed.get("unknown_lines") or [])
        record["ignored_line_count"] = len(parsed.get("ignored_lines") or [])
    text = str(result.get("text") or "")
    if include_raw_text:
        record["raw_text"] = text[:4000]
    elif text:
        record["text_preview"] = text[:1000]
    return record


def _session_layout_profile(session_id: str | None = None) -> dict[str, Any]:
    """Read the active session's generic, game-supplied layout profile."""

    try:
        session = STORE.get_session(session_id=session_id)
    except Exception:
        return {}
    profile = (session.get("game") or {}).get("layout_profile") or {}
    return copy.deepcopy(profile) if isinstance(profile, dict) else {}


def _effective_ocr_region(
    session: dict[str, Any],
    window_title: str | None,
    override: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Resolve an explicit region, then the session profile, then full-window OCR."""

    if override is not None:
        return override
    profile = (session.get("game") or {}).get("layout_profile") or {}
    configured = profile.get("dialogue_region") if isinstance(profile, dict) else None
    if isinstance(configured, dict):
        return copy.deepcopy(configured)
    return _default_ocr_region(window_title)


def _default_ocr_region(window_title: str | None) -> dict[str, Any] | None:
    """Keep the generic fallback full-window; game profiles are never inferred by title."""

    return None


def _layout_region_values(
    region: dict[str, Any],
    *,
    default_space: str,
    full_size: tuple[int, int],
    dialogue_bounds: tuple[float, float, float, float] | None,
    current_size: tuple[int, int],
) -> tuple[float, float, float, float] | None:
    """Map a profile region to full-window pixel coordinates."""

    try:
        x = float(region["x"])
        y = float(region["y"])
        width = float(region["width"])
        height = float(region["height"])
    except (KeyError, TypeError, ValueError):
        return None
    full_width, full_height = full_size
    current_width, current_height = current_size
    coordinate_space = str(region.get("coordinate_space") or default_space).strip().casefold()
    if coordinate_space in {"dialogue_region", "dialogue_box"}:
        base = dialogue_bounds or (0.0, 0.0, float(full_width), float(full_height))
        base_x, base_y, base_width, base_height = base
        x = base_x + x * base_width
        y = base_y + y * base_height
        width *= base_width
        height *= base_height
    elif coordinate_space in {"normalized", "normalised", "relative", "fraction"}:
        x *= full_width
        y *= full_height
        width *= full_width
        height *= full_height
    elif coordinate_space in {"pixels", "pixel", "absolute"}:
        pass
    elif coordinate_space == "image":
        x *= current_width
        y *= current_height
        width *= current_width
        height *= current_height
    else:
        return None
    return x, y, width, height


def _layout_profile_for_capture(
    session: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Project a persisted layout profile into the current frame coordinates."""

    stored = (session.get("game") or {}).get("layout_profile") or {}
    if not isinstance(stored, dict) or not stored:
        return {}
    profile = copy.deepcopy(stored)
    window = payload.get("window") or {}
    try:
        image_size = (
            int(payload.get("width") or window.get("width") or 0),
            int(payload.get("height") or window.get("height") or 0),
        )
        full_size = (
            int(window.get("width") or image_size[0]),
            int(window.get("height") or image_size[1]),
        )
    except (TypeError, ValueError):
        return profile
    if min(*image_size, *full_size) <= 0:
        return profile

    capture_scope = payload.get("capture_scope")
    capture_region = payload.get("capture_region") or {}
    if capture_scope == "window_dialogue_region":
        try:
            origin = (float(capture_region.get("x", 0)), float(capture_region.get("y", 0)))
        except (TypeError, ValueError):
            origin = (0.0, 0.0)
    else:
        origin = (0.0, 0.0)
    dialogue_bounds = None
    dialogue_region = stored.get("dialogue_region")
    if isinstance(dialogue_region, dict):
        dialogue_bounds = _layout_region_values(
            dialogue_region,
            default_space="normalized",
            full_size=full_size,
            dialogue_bounds=None,
            current_size=image_size,
        )

    for key, default_space in (
        ("dialogue_region", "normalized"),
        ("speaker_region", "dialogue_region"),
        ("choice_region", "normalized"),
    ):
        region = stored.get(key)
        if not isinstance(region, dict):
            continue
        absolute = _layout_region_values(
            region,
            default_space=default_space,
            full_size=full_size,
            dialogue_bounds=dialogue_bounds,
            current_size=image_size,
        )
        if absolute is None:
            continue
        x, y, width, height = absolute
        x -= origin[0]
        y -= origin[1]
        profile[key] = {
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "coordinate_space": "pixels",
        }
    ignore_regions = stored.get("ocr_ignore_regions")
    if isinstance(ignore_regions, dict):
        ignore_regions = [
            {**value, "name": value.get("name") or name}
            for name, value in ignore_regions.items()
            if isinstance(value, dict)
        ]
    if isinstance(ignore_regions, list):
        projected_ignore_regions: list[dict[str, Any]] = []
        for index, region in enumerate(ignore_regions, start=1):
            if not isinstance(region, dict):
                continue
            absolute = _layout_region_values(
                region,
                default_space="normalized",
                full_size=full_size,
                dialogue_bounds=dialogue_bounds,
                current_size=image_size,
            )
            if absolute is None:
                continue
            x, y, width, height = absolute
            projected_ignore_regions.append(
                {
                    "name": str(region.get("name") or region.get("id") or f"region_{index}"),
                    "x": x - origin[0],
                    "y": y - origin[1],
                    "width": width,
                    "height": height,
                    "coordinate_space": "pixels",
                }
            )
        profile["ocr_ignore_regions"] = projected_ignore_regions
    profile["_capture_scope"] = capture_scope
    return profile


def _resolve_ocr_region(
    region: dict[str, Any] | None,
    *,
    width: int,
    height: int,
) -> dict[str, Any] | None:
    """Validate an OCR region and resolve it to full-image pixel coordinates."""

    if region is None:
        return None
    if not isinstance(region, dict):
        raise ValueError("ocr_region 必须是包含 x、y、width、height 的对象")
    try:
        x_value = float(region["x"])
        y_value = float(region["y"])
        width_value = float(region["width"])
        height_value = float(region["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("ocr_region 必须包含数字 x、y、width、height") from exc
    values = (x_value, y_value, width_value, height_value)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("ocr_region 不能包含 NaN 或无穷大")
    coordinate_space = str(region.get("coordinate_space") or "").strip().casefold()
    if not coordinate_space:
        coordinate_space = "normalized" if max(abs(value) for value in values) <= 1 else "pixels"
    if coordinate_space in {"normalized", "normalised", "relative", "fraction"}:
        if any(value < 0 or value > 1 for value in values):
            raise ValueError("normalized ocr_region 的坐标和尺寸必须在 0 到 1 之间")
        x_value *= width
        y_value *= height
        width_value *= width
        height_value *= height
    elif coordinate_space not in {"pixels", "pixel", "absolute"}:
        raise ValueError("ocr_region.coordinate_space 必须是 normalized 或 pixels")
    if width <= 0 or height <= 0:
        raise ValueError("截图尺寸无效，无法解析 ocr_region")
    left = max(0, min(int(round(x_value)), width))
    top = max(0, min(int(round(y_value)), height))
    right = max(left, min(int(round(x_value + width_value)), width))
    bottom = max(top, min(int(round(y_value + height_value)), height))
    if right <= left or bottom <= top:
        raise ValueError("ocr_region 必须覆盖截图中的非空区域")
    return {
        "x": left,
        "y": top,
        "width": right - left,
        "height": bottom - top,
        "coordinate_space": "pixels",
    }


def _read_png_dimensions(image_path: str | Path) -> tuple[int, int]:
    """Read dimensions without adding an image-processing dependency."""

    path = Path(image_path).expanduser().resolve()
    header = path.read_bytes()[:24]
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("ocr_region 当前要求 MCP 会话中的 PNG 截图")
    width, height = struct.unpack(">II", header[16:24])
    return int(width), int(height)


def _filter_ocr_result_to_region(
    result: dict[str, Any],
    region: dict[str, Any] | None,
    *,
    width: int,
    height: int,
) -> dict[str, Any]:
    """Filter OCR regions while retaining the original full screenshot."""

    resolved = _resolve_ocr_region(region, width=width, height=height)
    if resolved is None:
        return result
    left = float(resolved["x"])
    top = float(resolved["y"])
    right = left + float(resolved["width"])
    bottom = top + float(resolved["height"])
    selected: list[dict[str, Any]] = []
    for item in result.get("regions") or []:
        try:
            item_left = float(item.get("x", 0))
            item_top = float(item.get("y", 0))
            item_right = item_left + max(0.0, float(item.get("width", 0)))
            item_bottom = item_top + max(0.0, float(item.get("height", 0)))
        except (TypeError, ValueError):
            continue
        if item_right > left and item_left < right and item_bottom > top and item_top < bottom:
            selected.append(item)
    selected.sort(key=lambda item: (float(item.get("y", 0)), float(item.get("x", 0))))
    filtered = dict(result)
    filtered["regions"] = selected
    filtered["text"] = "\n".join(
        str(item.get("text") or "").strip() for item in selected if str(item.get("text") or "").strip()
    )
    filtered["ocr_region"] = resolved
    return filtered


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


def _bottom_text_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Build an internal fingerprint for the lower dialogue-box area.

    Full screenshots use a lower-frame threshold.  Fast follow-up captures
    already contain only the dialogue box, so every OCR region in that image
    belongs to the verification area and must be retained.
    """

    window = payload.get("window") or {}
    try:
        height = int(payload.get("height") or window.get("height") or 0)
    except (TypeError, ValueError):
        height = 0
    # KiriKiri/YuzuSoft's dialogue box begins well below the character/scene
    # layer.  A lower threshold would mistake chapter decorations or status
    # glyphs in the middle of the frame for dialogue text.
    if payload.get("capture_scope") == "window_dialogue_region":
        threshold_y = 0
    else:
        threshold_y = max(1, round(height * 0.65)) if height > 0 else 0
    regions: list[dict[str, Any]] = []
    source_regions = payload.get("_dialogue_ocr_regions")
    if source_regions is None:
        source_regions = payload.get("_ocr_regions") or []
    for region in source_regions:
        text = re.sub(r"\s+", " ", str(region.get("text") or "")).strip()
        if not text:
            continue
        try:
            top = float(region.get("y", 0))
            region_height = max(0.0, float(region.get("height", 0)))
        except (TypeError, ValueError):
            continue
        if height <= 0 or top + region_height >= threshold_y:
            regions.append({"text": text, "y": top})
    regions.sort(key=lambda item: (item["y"], item["text"]))
    text = "\n".join(item["text"] for item in regions)
    return {
        "available": bool(
            (payload.get("ocr") or {}).get("available")
            if isinstance(payload.get("ocr"), dict)
            else payload.get("ocr")
        ),
        "detected": bool(text),
        "text": text,
        "region_count": len(regions),
        "char_count": len(text),
        "threshold_y": threshold_y,
    }


def _bottom_story_text(snapshot: dict[str, Any] | None) -> str:
    """Return hash input with obvious bottom UI residue removed."""

    if not isinstance(snapshot, dict):
        return ""
    lines: list[str] = []
    for raw_line in str(snapshot.get("text") or "").splitlines():
        line = re.sub(r"\s+", "", raw_line)
        if line and _usable_story_text(line):
            lines.append(line)
    return "\n".join(lines)


def _bottom_story_hash(snapshot: dict[str, Any] | None) -> str | None:
    story_text = _bottom_story_text(snapshot)
    if not story_text:
        return None
    return hashlib.sha256(story_text.encode("utf-8")).hexdigest()


def _compare_bottom_text(
    before: dict[str, Any] | None,
    after_payload: dict[str, Any],
) -> dict[str, Any]:
    """Compare only bottom text-box OCR, never scene/background changes."""

    after = _bottom_text_snapshot(after_payload)
    if before is None:
        return {
            "method": "bottom_textbox",
            "available": False,
            "changed": False,
            "reason": "baseline_not_captured",
            "after": {
                "detected": after["detected"],
                "region_count": after["region_count"],
                "char_count": after["char_count"],
            },
        }
    available = bool(before.get("available") and after.get("available"))
    before_detected = bool(before.get("detected"))
    after_detected = bool(after.get("detected"))
    changed = bool(
        available
        and after_detected
        and before.get("text") != after.get("text")
    )
    if not available:
        reason = "ocr_unavailable"
    elif before_detected and not after_detected:
        reason = "bottom_textbox_not_detected"
    elif changed:
        reason = "bottom_textbox_changed"
    elif not before.get("detected") and not after.get("detected"):
        reason = "bottom_textbox_not_detected"
    else:
        reason = "bottom_textbox_unchanged"
    public_snapshot = lambda item: {
        "detected": bool(item.get("detected")),
        "region_count": int(item.get("region_count", 0)),
        "char_count": int(item.get("char_count", 0)),
    }
    return {
        "method": "bottom_textbox",
        "available": available,
        "changed": changed,
        "reason": reason,
        "before": public_snapshot(before),
        "after": public_snapshot(after),
    }


def _window_center_screen_point(payload: dict[str, Any]) -> tuple[int, int] | None:
    """Return a safe scene point for a background advance click."""

    window = payload.get("window")
    if not isinstance(window, dict):
        return None
    try:
        x = int(window["x"])
        y = int(window["y"])
        width = int(window["width"])
        height = int(window["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return x + width // 2, y + height // 2


_GENERIC_ACTION_ALIASES: dict[str, dict[str, Any]] = {
    # These aliases are intentionally mechanical.  A game-specific profile
    # can replace any of them without changing the MCP surface.
    "advance": {"kind": "click", "target": "window_center", "button": "left"},
    "advance_game": {"kind": "click", "target": "window_center", "button": "left"},
    "click": {"kind": "click"},
    "hide_ui": {"kind": "click", "target": "window_center", "button": "right"},
    "show_ui": {"kind": "click", "target": "window_center", "button": "right"},
    "key": {"kind": "key"},
    "press_key": {"kind": "key"},
    "scroll": {"kind": "scroll", "direction": "down"},
    "wait": {"kind": "wait"},
    "focus": {"kind": "focus"},
}
_ACTION_KINDS = {"click", "key", "scroll", "hold", "wait", "focus"}


def _resolve_game_action(
    session: dict[str, Any],
    action: str,
    parameters: dict[str, Any] | None,
) -> tuple[str, dict[str, Any], bool]:
    """Resolve a named profile action or a small generic action alias.

    Profiles contain data only; they cannot invoke arbitrary Python or shell
    code.  This keeps game-specific behavior configurable while retaining a
    bounded, auditable input vocabulary.
    """

    action_name = str(action or "").strip()
    if not action_name or len(action_name) > 64:
        raise ValueError("action 必须是 1-64 个字符")
    game = session.get("game") or {}
    profile = game.get("action_profile") or {}
    if not isinstance(profile, dict):
        profile = {}
    matched_name = next(
        (name for name in profile if str(name).casefold() == action_name.casefold()),
        None,
    )
    if matched_name is not None:
        spec = copy.deepcopy(profile[matched_name])
        configured = True
        resolved_name = str(matched_name)
    else:
        alias = _GENERIC_ACTION_ALIASES.get(action_name.casefold())
        if alias is None:
            raise ValueError(
                f"未知游戏动作: {action_name}；请先通过 configure_game_actions 配置，"
                "或使用 advance/click/key/scroll/wait 等通用动作"
            )
        spec = copy.deepcopy(alias)
        configured = False
        resolved_name = action_name
    if not isinstance(spec, dict):
        raise ValueError(f"动作 {resolved_name} 的配置必须是对象")
    if parameters is not None and not isinstance(parameters, dict):
        raise ValueError("parameters 必须是 JSON 对象")
    if parameters:
        spec.update(copy.deepcopy(parameters))
    kind = str(spec.get("kind") or spec.get("type") or "").strip().casefold()
    if kind not in _ACTION_KINDS:
        raise ValueError("动作 kind 必须是 click、key、scroll、hold、wait 或 focus")
    spec["kind"] = kind
    return resolved_name, spec, configured


def _action_window_point(
    spec: dict[str, Any],
    window_title: str | None,
) -> tuple[int, int, dict[str, Any] | None]:
    """Resolve a click point from screen or window-relative action data."""

    target = str(spec.get("target") or "").strip().casefold()
    has_x = spec.get("x") is not None
    has_y = spec.get("y") is not None
    if has_x != has_y:
        raise ValueError("click 动作的 x 和 y 必须同时提供")

    if target in {"window_center", "center", "game_center"} or (not target and not has_x):
        if not window_title:
            raise ValueError("窗口中心动作需要 window_title 或已绑定游戏窗口")
        rect = native_get_window_rect(window_title)
        return (
            int(rect["x"]) + int(rect["width"]) // 2,
            int(rect["y"]) + int(rect["height"]) // 2,
            rect,
        )

    coordinate_space = str(spec.get("coordinate_space") or "").strip().casefold()
    normalised = target in {"window_normalized", "window_normalised", "normalized", "normalised", "fraction"}
    normalised = normalised or coordinate_space in {"normalized", "normalised", "relative", "fraction"}
    if normalised:
        if not window_title:
            raise ValueError("窗口相对坐标动作需要 window_title 或已绑定游戏窗口")
        try:
            relative_x = float(spec["x"])
            relative_y = float(spec["y"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("normalized click 必须提供数字 x 和 y") from exc
        if not 0 <= relative_x <= 1 or not 0 <= relative_y <= 1:
            raise ValueError("normalized click 的 x、y 必须在 0 到 1 之间")
        rect = native_get_window_rect(window_title)
        return (
            int(round(int(rect["x"]) + relative_x * int(rect["width"]))),
            int(round(int(rect["y"]) + relative_y * int(rect["height"]))),
            rect,
        )

    if not has_x:
        raise ValueError("click 动作必须提供 x、y，或指定 target=window_center/window_normalized")
    try:
        return int(round(float(spec["x"]))), int(round(float(spec["y"]))), None
    except (TypeError, ValueError) as exc:
        raise ValueError("screen click 的 x、y 必须是数字") from exc


def _action_int(spec: dict[str, Any], name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(spec.get(name, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    return max(minimum, min(value, maximum))


def _action_float(spec: dict[str, Any], name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(spec.get(name, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} 不能是 NaN 或无穷大")
    return max(minimum, min(value, maximum))


def _action_scroll_delta(spec: dict[str, Any]) -> tuple[int, str]:
    if spec.get("delta") is not None:
        try:
            delta = int(spec["delta"])
        except (TypeError, ValueError) as exc:
            raise ValueError("scroll 的 delta 必须是整数") from exc
        if delta == 0:
            raise ValueError("scroll 的 delta 不能为 0")
        return max(-1200, min(delta, 1200)), "up" if delta > 0 else "down"
    direction = str(spec.get("direction") or "down").strip().casefold()
    deltas = {
        "up": 120,
        "down": -120,
        "上": 120,
        "下": -120,
        "wheelup": 120,
        "wheeldown": -120,
    }
    if direction not in deltas:
        raise ValueError("scroll 的 direction 必须是 up、down、上 或 下")
    return deltas[direction], "up" if deltas[direction] > 0 else "down"


def _remember_bottom_snapshot(session: dict[str, Any], payload: dict[str, Any]) -> None:
    snapshot = _bottom_text_snapshot(payload)
    if snapshot.get("available") and snapshot.get("detected"):
        _BOTTOM_SNAPSHOT_CACHE[str(session["session_id"])] = (time.monotonic(), snapshot)
    else:
        # Do not reuse an old valid frame after an OCR-uncertain result. The
        # next advance must establish a fresh baseline and can then stop for
        # Codex visual review instead of clicking on stale evidence.
        _BOTTOM_SNAPSHOT_CACHE.pop(str(session["session_id"]), None)


def _cached_bottom_snapshot(session: dict[str, Any]) -> dict[str, Any] | None:
    session_key = str(session["session_id"])
    cached = _BOTTOM_SNAPSHOT_CACHE.get(session_key)
    if cached is None:
        return None
    captured_at, snapshot = cached
    if time.monotonic() - captured_at > _BOTTOM_SNAPSHOT_CACHE_MAX_AGE_SECONDS:
        _BOTTOM_SNAPSHOT_CACHE.pop(session_key, None)
        return None
    return dict(snapshot)


def _capture_uses_fast_dialogue_region(
    *,
    mode: str,
    title: str | None,
    ocr: bool,
    include_image: bool,
    region: dict[str, Any] | None,
) -> bool:
    """Decide whether a follow-up may use the already-known dialogue box."""

    resolved_mode = (mode or "auto").strip().lower()
    uses_window = resolved_mode == "window" or (resolved_mode == "auto" and bool(title))
    # Returning an image is an explicit request for the complete visual frame;
    # callers that only need local OCR can use the smaller region PNG.
    return bool(uses_window and title and ocr and not include_image and region)


def _fast_capture_has_text(payload: dict[str, Any]) -> bool:
    """Return whether a region frame contains enough story text to trust it."""

    if payload.get("capture_scope") != "window_dialogue_region":
        return True
    processed = payload.get("processed_text") or {}
    if list(processed.get("choices") or []):
        return True
    if processed.get("text_status") in {"empty", "ui_only", "unknown"}:
        return False
    if list(processed.get("unknown_lines") or []):
        return False
    # Speaker-only frames and UI residue (VOICE/AUTO/000) are deliberately
    # treated as a miss.  The full-window OCR can recover the line or confirm
    # that the game is actually in a transition.
    return _parsed_has_story_text(processed)


def _process_capture_text(
    payload: dict[str, Any],
    image_path: Path,
    session: dict[str, Any],
    *,
    ocr: bool,
    record_text: bool,
    language: str,
    include_raw_text: bool,
    ocr_region: dict[str, Any] | None,
) -> dict[str, Any]:
    """Process a full frame or a pre-cropped dialogue frame correctly.

    A region frame is already in the requested OCR coordinate space. Applying
    the original full-window normalized region to it a second time would
    discard the dialogue, so the filter is disabled only for that internal
    capture scope.
    """

    process_region = None if payload.get("capture_scope") == "window_dialogue_region" else ocr_region
    return _process_local_text(
        payload,
        image_path,
        session,
        ocr=ocr,
        record_text=record_text,
        language=language,
        include_raw_text=include_raw_text,
        ocr_region=process_region,
    )


def _capture_processed_frame(
    *,
    window_title: str | None,
    capture_mode: str,
    session: dict[str, Any],
    ocr: bool,
    record_text: bool,
    language: str,
    include_raw_text: bool,
    ocr_region: dict[str, Any] | None,
    include_image: bool = False,
    action_event: dict[str, Any] | None = None,
    force_full: bool = False,
) -> tuple[dict[str, Any], Path]:
    """Capture/process one frame locally, using the fast region when safe."""

    fast_region = None
    if not force_full and _capture_uses_fast_dialogue_region(
        mode=capture_mode,
        title=window_title,
        ocr=ocr,
        include_image=include_image,
        region=ocr_region,
    ):
        fast_region = ocr_region
    payload, image_path = _capture_for_session(
        window_title=window_title,
        capture_mode=capture_mode,
        session_id=session["session_id"],
        fast_region=fast_region,
    )
    if action_event is not None:
        payload["action_event"] = action_event
    payload = _process_capture_text(
        payload,
        image_path,
        session,
        ocr=ocr,
        record_text=record_text,
        language=language,
        include_raw_text=include_raw_text,
        ocr_region=ocr_region,
    )
    if (
        ocr
        and fast_region is not None
        and payload.get("capture_scope") == "window_full"
        and payload.get("capture_fallback") is not None
    ):
        # The platform capture itself may fail to crop an occluded GPU window
        # and return a full frame directly. Treat that as the same correctness
        # fallback as a crop OCR miss, so recovered text still gets the guarded
        # one-second settle before an advance.
        full_text_detected = _parsed_has_story_text(payload.get("processed_text"))
        payload["ocr_fallback"] = {
            "from": "window_dialogue_region",
            "to": "window_full",
            "reason": "dialogue_region_capture_failed",
            "full_text_detected": full_text_detected,
            "settle_wait_seconds": _OCR_FALLBACK_SETTLE_SECONDS if full_text_detected else 0.0,
        }
        return payload, image_path
    if ocr and fast_region is not None and not _fast_capture_has_text(payload):
        full_payload, full_image_path = _capture_processed_frame(
            window_title=window_title,
            capture_mode=capture_mode,
            session=session,
            ocr=ocr,
            record_text=record_text,
            language=language,
            include_raw_text=include_raw_text,
            ocr_region=ocr_region,
            include_image=include_image,
            action_event=action_event,
            force_full=True,
        )
        full_payload.setdefault(
            "capture_fallback",
            {
                "from": "window_dialogue_region",
                "to": "window_full",
                "reason": "dialogue_region_ocr_empty_or_unusable",
            },
        )
        full_payload["ocr_fallback"] = {
            "from": "window_dialogue_region",
            "to": "window_full",
            "reason": "dialogue_region_ocr_empty_or_unusable",
            "full_text_detected": _parsed_has_story_text(full_payload.get("processed_text")),
            "settle_wait_seconds": _OCR_FALLBACK_SETTLE_SECONDS
            if _parsed_has_story_text(full_payload.get("processed_text"))
            else 0.0,
        }
        return full_payload, full_image_path
    return payload, image_path


def _transition_probe_state(payload: dict[str, Any]) -> str:
    """Classify one full-frame probe without treating OCR emptiness as proof."""

    if payload.get("screen_type") == "settings":
        return "settings"
    parsed = payload.get("processed_text") or {}
    if list(parsed.get("choices") or []):
        return "choice"
    evidence = payload.get("evidence") or {}
    if "unknown_text" in (evidence.get("blocking_reasons") or []):
        return "unknown_text"
    if list(parsed.get("unknown_lines") or []):
        return "unknown_text"
    bottom_snapshot = _bottom_text_snapshot(payload)
    if (
        _bottom_story_text(bottom_snapshot)
        or _parsed_has_story_text(parsed)
        or str(parsed.get("speaker") or "").strip()
    ):
        return "story_text"
    if not bool((payload.get("ocr") or {}).get("available")):
        return "ocr_unavailable"
    return "blank"


def _is_post_click_transition_ocr_candidate(payload: dict[str, Any]) -> bool:
    """Allow a post-click blank OCR frame into the bounded transition wait.

    A full-window OCR miss is normally a hard stop.  The exception is the
    short-lived frame produced while a VN is fading/changing a character or
    background immediately after an accepted advance input.  This predicate
    is deliberately narrow: any recovered story text, speaker-only text,
    choices, or unknown OCR remains a stop condition.
    """

    parsed = payload.get("processed_text") or {}
    if (
        str(parsed.get("speaker") or "").strip()
        or str(parsed.get("dialogue") or "").strip()
        or list(parsed.get("choices") or [])
        or list(parsed.get("unknown_lines") or [])
    ):
        return False
    evidence = payload.get("evidence") or {}
    channels = evidence.get("channels") or {}
    transition = channels.get("visual_transition") or {}
    if isinstance(transition, dict) and transition.get("status") == "active":
        return True
    blocking_reasons = set(evidence.get("blocking_reasons") or [])
    return {"dialogue_unresolved", "ocr_uncertain"}.issubset(blocking_reasons)


def _classify_transition_probe(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Use bounded visual samples to decide whether a blank frame is a fade."""

    states = [str(item.get("state") or "blank") for item in samples]
    for blocking_state in ("settings", "choice", "unknown_text", "story_text", "ocr_unavailable"):
        if blocking_state in states:
            return {
                "status": blocking_state,
                "confirmed": False,
                "sample_count": len(samples),
                "motion_supported": False,
            }

    scene_scores = [
        float(item["scene_change_score"])
        for item in samples
        if isinstance(item.get("scene_change_score"), (int, float))
    ]
    motion_scores = [
        float(item["motion_from_previous"])
        for item in samples
        if isinstance(item.get("motion_from_previous"), (int, float))
    ]
    motion_supported = bool(scene_scores or motion_scores)
    max_scene_change = max(scene_scores, default=0.0)
    max_motion = max(motion_scores, default=0.0)
    confirmed = bool(
        len(samples) >= 2
        and motion_supported
        and max(max_scene_change, max_motion) >= _TRANSITION_MOTION_THRESHOLD
    )
    return {
        "status": "transition_confirmed" if confirmed else "ocr_uncertain",
        "confirmed": confirmed,
        "sample_count": len(samples),
        "motion_supported": motion_supported,
        "scene_change_score": round(max_scene_change, 6),
        "max_motion_score": round(max_motion, 6),
        "motion_threshold": _TRANSITION_MOTION_THRESHOLD,
    }


def _probe_transition_after_click(
    *,
    before_image_path: Path | None,
    first_payload: dict[str, Any] | None,
    first_image_path: Path | None,
    timing: dict[str, Any],
    window_title: str | None,
    capture_mode: str,
    session: dict[str, Any],
    language: str,
    include_raw_text: bool,
    ocr_region: dict[str, Any] | None,
    action_event: dict[str, Any] | None,
    background: bool,
    background_input_method: str,
    auto_return_from_settings: bool,
) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    """Capture up to three full frames after a click and classify a blank scene.

    The helper never sends input.  It is intentionally safe to call before the
    optional one-click accelerator: a stable OCR miss returns ``ocr_uncertain``
    and leaves the decision with Codex.
    """

    current_payload = first_payload
    current_image_path = first_image_path
    if (
        current_payload is None
        or current_image_path is None
        or current_payload.get("capture_scope") == "window_dialogue_region"
    ):
        current_payload, current_image_path = _capture_processed_frame(
            window_title=window_title,
            capture_mode=capture_mode,
            session=session,
            ocr=True,
            record_text=False,
            language=language,
            include_raw_text=include_raw_text,
            ocr_region=ocr_region,
            include_image=False,
            action_event=action_event,
            force_full=True,
        )
    current_payload, current_image_path = _auto_return_from_settings(
        current_payload,
        session,
        title=window_title,
        capture_mode=capture_mode,
        ocr=True,
        record_text=False,
        language=language,
        include_raw_text=include_raw_text,
        enabled=auto_return_from_settings,
        background=background,
        background_input_method=background_input_method,
        ocr_region=ocr_region,
    )

    samples: list[dict[str, Any]] = []
    previous_image_path: Path | None = None
    started = time.perf_counter()
    probe_budget = float(timing.get("transition_wait_seconds", 0.0))
    probe_interval = float(timing.get("transition_probe_interval_seconds", 0.2))
    while True:
        scene_change_score = (
            native_image_motion_score(before_image_path, current_image_path)
            if before_image_path is not None
            else None
        )
        motion_from_previous = (
            native_image_motion_score(previous_image_path, current_image_path)
            if previous_image_path is not None
            else None
        )
        samples.append(
            {
                "state": _transition_probe_state(current_payload),
                "scene_change_score": scene_change_score,
                "motion_from_previous": motion_from_previous,
            }
        )
        if samples[-1]["state"] != "blank":
            break
        if len(samples) >= _TRANSITION_PROBE_MAX_SAMPLES:
            break
        elapsed = time.perf_counter() - started
        remaining = probe_budget - elapsed
        if remaining <= 0:
            break
        time.sleep(min(probe_interval, remaining))
        previous_image_path = current_image_path
        current_payload, current_image_path = _capture_processed_frame(
            window_title=window_title,
            capture_mode=capture_mode,
            session=session,
            ocr=True,
            record_text=False,
            language=language,
            include_raw_text=include_raw_text,
            ocr_region=ocr_region,
            include_image=False,
            action_event=action_event,
            force_full=True,
        )
        current_payload, current_image_path = _auto_return_from_settings(
            current_payload,
            session,
            title=window_title,
            capture_mode=capture_mode,
            ocr=True,
            record_text=False,
            language=language,
            include_raw_text=include_raw_text,
            enabled=auto_return_from_settings,
            background=background,
            background_input_method=background_input_method,
            ocr_region=ocr_region,
        )

    decision = _classify_transition_probe(samples)
    decision["probe_elapsed_seconds"] = round(time.perf_counter() - started, 3)
    decision["probe_interval_seconds"] = probe_interval
    decision["before_image_available"] = before_image_path is not None
    decision["max_samples"] = _TRANSITION_PROBE_MAX_SAMPLES
    return current_payload, current_image_path, decision


def _wait_for_text_hash_stable(
    *,
    first_payload: dict[str, Any],
    first_image_path: Path,
    before_snapshot: dict[str, Any] | None,
    timing: dict[str, Any],
    window_title: str | None,
    capture_mode: str,
    session: dict[str, Any],
    language: str,
    include_raw_text: bool,
    ocr_region: dict[str, Any] | None,
    include_image: bool,
    action_event: dict[str, Any] | None,
    background: bool,
    background_input_method: str,
    auto_return_from_settings: bool,
) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    """Wait locally until post-click dialogue OCR stops changing.

    This is deliberately a settle-only helper: it never sends input.  A
    caller can therefore use the returned frame for the normal choice/
    transition safety checks without turning a typewriter animation into
    repeated clicks.
    """

    before_hash = _bottom_story_hash(before_snapshot)
    current_payload = first_payload
    current_image_path = first_image_path
    candidate_hash: str | None = None
    stable_count = 0
    polls = 0
    extra_frames = 0
    started = time.perf_counter()
    timeout = float(timing["settle_timeout_seconds"])
    poll_seconds = float(timing["settle_poll_seconds"])
    required_samples = int(timing["stable_samples"])
    require_change = bool(timing["require_text_change"])

    while True:
        processed = current_payload.get("processed_text") or {}
        if list(processed.get("choices") or []):
            return current_payload, current_image_path, {
                "strategy": "text_hash",
                "settled": True,
                "reason": "choice_detected",
                "before_hash": before_hash,
                "current_hash": _bottom_story_hash(_bottom_text_snapshot(current_payload)),
                "stable_samples": stable_count,
                "polls": polls,
                "extra_frames": extra_frames,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }

        current_hash = _bottom_story_hash(_bottom_text_snapshot(current_payload))
        changed = bool(current_hash and (before_hash is None or current_hash != before_hash))
        if current_hash and (changed or not require_change):
            if current_hash == candidate_hash:
                stable_count += 1
            else:
                candidate_hash = current_hash
                stable_count = 1
            if stable_count >= required_samples:
                return current_payload, current_image_path, {
                    "strategy": "text_hash",
                    "settled": True,
                    "reason": "text_hash_stable",
                    "before_hash": before_hash,
                    "current_hash": current_hash,
                    "stable_samples": stable_count,
                    "polls": polls,
                    "extra_frames": extra_frames,
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                }
        else:
            stable_count = 0

        elapsed = time.perf_counter() - started
        if elapsed >= timeout:
            return current_payload, current_image_path, {
                "strategy": "text_hash",
                "settled": False,
                "reason": "timeout",
                "before_hash": before_hash,
                "current_hash": current_hash,
                "stable_samples": stable_count,
                "polls": polls,
                "extra_frames": extra_frames,
                "elapsed_seconds": round(elapsed, 3),
            }

        time.sleep(min(poll_seconds, max(0.0, timeout - elapsed)))
        polls += 1
        current_payload, current_image_path = _capture_processed_frame(
            window_title=window_title,
            capture_mode=capture_mode,
            session=session,
            ocr=True,
            record_text=False,
            language=language,
            include_raw_text=include_raw_text,
            ocr_region=ocr_region,
            include_image=include_image,
            action_event=action_event,
        )
        current_payload, current_image_path = _auto_return_from_settings(
            current_payload,
            session,
            title=window_title,
            capture_mode=capture_mode,
            ocr=True,
            record_text=False,
            language=language,
            include_raw_text=include_raw_text,
            enabled=auto_return_from_settings,
            background=background,
            background_input_method=background_input_method,
            ocr_region=ocr_region,
        )
        extra_frames += 1


def _batch_dialogue_item(payload: dict[str, Any], index: int) -> dict[str, Any] | None:
    """Return only compact structured story data for one local frame."""

    parsed = payload.get("processed_text") or {}
    speaker = parsed.get("speaker")
    dialogue = parsed.get("dialogue")
    choices = list(parsed.get("choices") or [])
    if (
        dialogue
        and not _usable_story_text(dialogue)
        and parsed.get("text_status") != "recognized"
        and not choices
    ):
        # Never turn a control residue such as AUTO/VOICE/000 into an input.
        dialogue = None
    # Some VN engines render a speaker label before a punctuation-only line
    # such as "……". Windows OCR commonly drops that punctuation, so retain a
    # compact speaker_only marker and let the local loop advance it instead of
    # stopping as if OCR had failed.
    if not dialogue and not choices and not speaker:
        return None
    item: dict[str, Any] = {
        "index": index,
        "speaker": speaker,
        "dialogue": dialogue,
        "choices": choices,
        "confidence": parsed.get("confidence", 0.0),
    }
    if not dialogue and speaker and not choices:
        item["text_status"] = "speaker_only"
    if parsed.get("choice_records"):
        item["choice_records"] = parsed["choice_records"]
    if parsed.get("screen_type"):
        item["screen_type"] = parsed["screen_type"]
    if parsed.get("text_status"):
        item["text_status"] = parsed["text_status"]
    evidence = payload.get("evidence") or {}
    current_episode = evidence.get("current_episode") if isinstance(evidence, dict) else None
    if isinstance(current_episode, dict) and current_episode.get("episode_id"):
        item["episode_id"] = current_episode["episode_id"]
    return item


def _choice_click_point_from_payload(
    payload: dict[str, Any],
    session: dict[str, Any],
    option_index: int,
) -> tuple[int, int] | None:
    """Resolve an option index to a screen point using the active OCR profile.

    The game profile supplies the broad choice zone; the local full-frame OCR
    supplies each option's bounding box.  This keeps click coordinates out of
    the generic MCP and also works when a window is moved or resized.
    """

    if payload.get("capture_scope") != "window_full":
        return None
    parsed = payload.get("processed_text") or {}
    choices = [str(item).strip() for item in (parsed.get("choices") or []) if str(item).strip()]
    if option_index < 1 or option_index > len(choices):
        return None
    profile = _layout_profile_for_capture(session, payload)
    choice_region = profile.get("choice_region") if isinstance(profile, dict) else None
    if not isinstance(choice_region, dict):
        return None
    try:
        image_width = int(payload.get("width") or 0)
        image_height = int(payload.get("height") or 0)
        left = float(choice_region["x"])
        top = float(choice_region["y"])
        region_width = float(choice_region["width"])
        region_height = float(choice_region["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if image_width <= 0 or image_height <= 0 or region_width <= 0 or region_height <= 0:
        return None
    regions = payload.get("_ocr_regions") or []
    candidates: list[dict[str, float | str | int]] = []
    for region in regions:
        if not isinstance(region, dict):
            continue
        text = re.sub(r"\s+", "", str(region.get("text") or ""))
        if not text:
            continue
        try:
            x = float(region.get("x", 0))
            y = float(region.get("y", 0))
            width = max(0.0, float(region.get("width", 0)))
            height = max(0.0, float(region.get("height", 0)))
        except (TypeError, ValueError):
            continue
        center_x = x + width / 2.0
        center_y = y + height / 2.0
        if not (left <= center_x <= left + region_width and top <= center_y <= top + region_height):
            continue
        matched_index: int | None = None
        for index, label in enumerate(choices, start=1):
            compact_label = re.sub(r"\s+", "", label)
            if compact_label and (text == compact_label or compact_label in text or text in compact_label):
                matched_index = index
                break
        candidates.append(
            {
                "text": text,
                "x": center_x,
                "y": center_y,
                "matched_index": matched_index or 0,
            }
        )

    matched = [item for item in candidates if int(item["matched_index"]) == option_index]
    if matched:
        candidate = matched[0]
    else:
        if len(candidates) < len(choices):
            return None
        choice_layout = str(profile.get("choice_layout") or "vertical").strip().casefold()
        axis = "x" if choice_layout == "horizontal" else "y"
        ordered = sorted(candidates, key=lambda item: (float(item[axis]), float(item["x"]), str(item["text"])))
        candidate = ordered[option_index - 1]

    window = payload.get("window") or {}
    try:
        window_x = int(window.get("x", 0))
        window_y = int(window.get("y", 0))
    except (TypeError, ValueError):
        return None
    return window_x + round(float(candidate["x"])), window_y + round(float(candidate["y"]))


def _advance_input_for_batch(
    *,
    session: dict[str, Any],
    title: str | None,
    payload: dict[str, Any],
    background: bool,
    background_input_method: str,
) -> tuple[str, dict[str, Any]]:
    """Send one local advance action using the same safe policy as advance_game."""

    game = session.get("game", {})
    control = game.get("control", {})
    key = control.get("advance_key") or "SPACE"
    if background:
        if not title:
            raise ValueError("background=true 时必须先 attach_game 绑定 window_title")
        click_point = _window_center_screen_point(payload)
        if click_point is None:
            click_point = _window_center_screen_point({"window": native_get_window_rect(title)})
        if click_point is None:
            raise ValueError("无法确定后台推进的窗口中心坐标")
        return "background_click", native_post_window_click(
            title=title,
            x=click_point[0],
            y=click_point[1],
            button="left",
            clicks=1,
            interval_ms=0,
            delivery=background_input_method,
        )
    return "foreground_key", native_send_key(key=key, presses=1, interval_ms=0)


def _process_local_text(
    payload: dict[str, Any],
    image_path: Path,
    session: dict[str, Any],
    *,
    ocr: bool,
    record_text: bool,
    language: str,
    include_raw_text: bool,
    ocr_region: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not ocr:
        return payload
    full_ocr_result = native_ocr_image(str(image_path), language=language)
    ocr_result = _filter_ocr_result_to_region(
        full_ocr_result,
        ocr_region,
        width=int(payload.get("width") or (payload.get("window") or {}).get("width") or 0),
        height=int(payload.get("height") or (payload.get("window") or {}).get("height") or 0),
    )
    raw_text = str(ocr_result.get("text") or "").strip()
    payload["ocr"] = _compact_ocr_result(ocr_result)
    source_regions = full_ocr_result.get("regions") or []
    if payload.get("capture_scope") == "window_dialogue_region":
        # OCR coordinates in a fast region image start at (0, 0). Keep that
        # local copy for bottom-text verification, and also expose a translated
        # window-local copy so settings recovery can still click a detected
        # "回到游戏" button using the complete window coordinate space.
        capture_region = payload.get("capture_region") or {}
        try:
            region_offset_x = int(capture_region.get("x", 0))
            region_offset_y = int(capture_region.get("y", 0))
        except (TypeError, ValueError):
            region_offset_x = region_offset_y = 0
        translated_regions: list[dict[str, Any]] = []
        for item in source_regions:
            translated = dict(item)
            try:
                translated["x"] = float(item.get("x", 0)) + region_offset_x
                translated["y"] = float(item.get("y", 0)) + region_offset_y
            except (TypeError, ValueError):
                pass
            translated_regions.append(translated)
        payload["_dialogue_ocr_regions"] = source_regions
        payload["_ocr_regions"] = translated_regions
    else:
        # Keep full-frame regions for settings recovery, while the dialogue-only
        # regions drive input verification and structured story parsing.
        payload["_ocr_regions"] = source_regions
    if ocr_region is not None and payload.get("capture_scope") != "window_dialogue_region":
        payload["_dialogue_ocr_regions"] = ocr_result.get("regions") or []
        payload["_ocr_region"] = ocr_result.get("ocr_region")
    full_raw_text = str(full_ocr_result.get("text") or "").strip()
    try:
        image_size = (
            int(payload.get("width") or (payload.get("window") or {}).get("width") or 0),
            int(payload.get("height") or (payload.get("window") or {}).get("height") or 0),
        )
    except (TypeError, ValueError):
        image_size = None
    frame_layout_profile = _layout_profile_for_capture(session, payload)
    full_screen_type = detect_screen_type(full_raw_text)
    if full_screen_type:
        payload["screen_type"] = full_screen_type
    if include_raw_text and (raw_text or full_raw_text):
        # When the dialogue crop misses, expose the full-frame OCR that was
        # used as the correctness fallback instead of returning an empty raw
        # string to the caller.
        payload["raw_text"] = raw_text or full_raw_text
    full_parsed: dict[str, Any] | None = None
    full_source_result: dict[str, Any] = full_ocr_result
    if (
        full_raw_text
        and payload.get("capture_scope") == "window_full"
    ):
        full_parsed = parse_screen_text(
            full_raw_text,
            regions=full_ocr_result.get("regions") or [],
            image_size=image_size,
            layout_profile=frame_layout_profile,
        )
    parsed: dict[str, Any] | None = None
    source_result = ocr_result
    if raw_text:
        parsed = parse_screen_text(
            raw_text,
            regions=ocr_result.get("regions") or [],
            image_size=image_size,
            layout_profile=frame_layout_profile,
        )

    # RapidOCR is deliberately a last-resort backend.  The fast dialogue
    # capture has already failed its Windows OCR semantic gate by the time the
    # caller reaches this full-window frame.  Keep RapidOCR on this same full
    # image so it never becomes part of the normal fast path.
    normal_story_found = _parsed_has_story_text(parsed) or _parsed_has_story_text(full_parsed)
    if (
        payload.get("capture_scope") == "window_full"
        and full_screen_type != "settings"
        and not normal_story_found
    ):
        try:
            rapid_result = native_rapidocr_image(str(image_path), language=language)
        except Exception as exc:  # pragma: no cover - optional backend boundary
            rapid_result = {
                "available": False,
                "execution_success": False,
                "usable": False,
                "status": "error",
                "backend": "rapidocr_ppocrv6_small",
                "model": "PP-OCRv6-small-ONNX",
                "text": "",
                "message": str(exc),
            }
        rapid_text = str(rapid_result.get("text") or "").strip()
        rapid_parsed: dict[str, Any] | None = None
        if rapid_text:
            rapid_parsed = parse_screen_text(
                rapid_text,
                regions=rapid_result.get("regions") or [],
                image_size=image_size,
                layout_profile=frame_layout_profile,
            )
        if rapid_parsed and rapid_parsed.get("screen_type"):
            full_screen_type = rapid_parsed.get("screen_type")
            payload["screen_type"] = full_screen_type
        payload["ocr_backends"] = {
            "windows_ocr": _ocr_backend_record(
                full_ocr_result,
                full_parsed,
                include_raw_text=include_raw_text,
            ),
            "rapidocr_ppocrv6_small": _ocr_backend_record(
                rapid_result,
                rapid_parsed,
                include_raw_text=include_raw_text,
            ),
            "fallback_reason": "full_window_ocr_semantic_unusable",
        }
        if _parsed_has_story_text(rapid_parsed):
            parsed = rapid_parsed
            full_parsed = rapid_parsed
            source_result = rapid_result
            full_source_result = rapid_result
            raw_text = rapid_text
            payload["ocr"] = _compact_ocr_result(rapid_result)
            if include_raw_text:
                payload["raw_text"] = rapid_text
            rapid_regions = rapid_result.get("regions") or []
            payload["_dialogue_ocr_regions"] = rapid_regions
            payload["_ocr_regions"] = rapid_regions

    if full_parsed and _parsed_has_story_text(full_parsed) and (
        parsed is None or not _parsed_has_story_text(parsed)
    ):
        # The crop can contain only UI residue or a partial name.  Prefer the
        # full-frame parse in that case; it can recover centered narration and
        # dialogue that fell just outside the configured crop.
        parsed = full_parsed
        source_result = full_source_result
        raw_text = str(full_source_result.get("text") or full_raw_text).strip()
    elif parsed is None and full_parsed and full_screen_type:
        parsed = full_parsed
        source_result = full_source_result
        raw_text = str(full_source_result.get("text") or full_raw_text).strip()
    if parsed is None:
        payload["evidence"] = build_frame_evidence(
            {},
            screen_type=full_screen_type,
            ocr_available=bool((payload.get("ocr") or {}).get("available")),
        )
        if (
            payload.get("capture_scope") != "window_dialogue_region"
            and bool((payload.get("ocr") or {}).get("available"))
            and full_screen_type != "settings"
        ):
            _mark_ocr_uncertain(payload, image_path)
        return payload
    if full_parsed and full_parsed.get("choices"):
        parsed["choices"] = list(full_parsed.get("choices") or [])
        parsed["choice_records"] = list(full_parsed.get("choice_records") or [])
        parsed["confidence"] = max(
            float(parsed.get("confidence") or 0.0),
            float(full_parsed.get("confidence") or 0.0),
        )
    public_parsed = _public_parsed_text(parsed)
    screen_type = full_screen_type or parsed.get("screen_type") or detect_screen_type(raw_text)
    if screen_type:
        public_parsed["screen_type"] = screen_type
        payload["screen_type"] = screen_type
    payload["processed_text"] = public_parsed
    payload["evidence"] = build_frame_evidence(
        public_parsed,
        screen_type=screen_type,
        ocr_available=bool((payload.get("ocr") or {}).get("available")),
        allow_unknown_with_story=(
            source_result.get("backend") == "rapidocr_ppocrv6_small"
        ),
    )
    # Settings controls are not story choices. Keep the screenshot and OCR
    # result, but do not pollute the route timeline with fake dialogue/options.
    if screen_type == "settings":
        return payload
    if (
        payload.get("capture_scope") != "window_dialogue_region"
        and bool((payload.get("ocr") or {}).get("available"))
        and not _parsed_has_story_text(parsed)
    ):
        _mark_ocr_uncertain(payload, image_path)
    if not record_text or not (parsed.get("dialogue") or parsed.get("choices")):
        return payload
    if _same_processed_text(session, parsed):
        payload["deduplicated"] = True
        return payload

    recorded = STORE.record_observation(
        raw_text=raw_text or full_raw_text,
        text=parsed.get("dialogue") or None,
        speaker=parsed.get("speaker"),
        choices=parsed.get("choices") or None,
        screenshot_path=str(image_path),
        source=source_result.get("backend") or "local_ocr",
        confidence=parsed.get("confidence"),
        noise_flags=parsed.get("noise_flags"),
        evidence=payload.get("evidence"),
        session_id=session["session_id"],
    )
    payload["recorded"] = {
        "observation_id": recorded.get("observation_id"),
        "event_ids": recorded.get("event_ids", []),
    }
    return payload


def _auto_return_from_settings(
    payload: dict[str, Any],
    session: dict[str, Any],
    *,
    title: str | None,
    capture_mode: str,
    ocr: bool,
    record_text: bool,
    language: str,
    include_raw_text: bool,
    enabled: bool,
    background: bool = False,
    background_input_method: str = "post",
    ocr_region: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    """Leave a detected settings page and return the first post-menu frame."""

    if not enabled or payload.get("screen_type") != "settings" or not title:
        return payload, Path(str(payload["image_path"]))

    return_markers = (
        "回到游戏",
        "返回游戏",
        "游戏に戻る",
        "ゲームに戻る",
        "returntogame",
        "backtogame",
    )
    window = payload.get("window") or {}
    offset_x = int(window.get("x") or 0)
    offset_y = int(window.get("y") or 0)
    button_point: tuple[int, int] | None = None
    for region in payload.get("_ocr_regions") or []:
        compact = re.sub(r"\s+", "", str(region.get("text") or "")).casefold()
        if any(marker.casefold() in compact for marker in return_markers):
            button_point = (
                offset_x + int(float(region.get("x", 0)) + float(region.get("width", 0)) / 2),
                offset_y + int(float(region.get("y", 0)) + float(region.get("height", 0)) / 2),
            )
            break
    if button_point is None:
        payload["auto_recovery"] = {
            "detected": "settings",
            "returned": False,
            "method": "click",
            "reason": "return_button_not_detected",
        }
        return payload, Path(str(payload["image_path"]))

    if background:
        focused = None
        click_result = native_post_window_click(
            title=title,
            x=button_point[0],
            y=button_point[1],
            button="left",
            clicks=1,
            interval_ms=0,
            delivery=background_input_method,
        )
    else:
        focused = native_focus_window(title)
        click_result = native_click_screen(
            x=button_point[0],
            y=button_point[1],
            button="left",
            clicks=1,
            interval_ms=0,
        )
    time.sleep(0.2)
    action = STORE.record_action(
        "auto_return_from_settings",
        {
            "screen_type": "settings",
            "method": "click",
            "button_point": {"x": button_point[0], "y": button_point[1]},
            "focus": focused,
            "background": background,
            **click_result,
        },
        session_id=session["session_id"],
    )
    followup, followup_path = _capture_for_session(
        window_title=title,
        capture_mode=capture_mode,
        session_id=session["session_id"],
    )
    if payload.get("action_event") is not None:
        followup["action_event"] = payload["action_event"]
    followup["auto_recovery"] = {
        "detected": "settings",
        "method": "click",
        "button_point": {"x": button_point[0], "y": button_point[1]},
        "action_event": action,
    }
    followup = _process_capture_text(
        followup,
        followup_path,
        session,
        ocr=ocr,
        record_text=record_text,
        language=language,
        include_raw_text=include_raw_text,
        ocr_region=ocr_region,
    )
    if followup.get("screen_type") == "settings":
        followup["auto_recovery"]["returned"] = False
    else:
        followup["auto_recovery"]["returned"] = True
    return followup, followup_path


@mcp.tool()
def configure_game_layout(
    profile: dict[str, Any],
    session_id: str | None = None,
) -> dict[str, Any]:
    """保存当前游戏的 OCR/布局 profile，供后续本地解析和自动游玩使用。

    profile 中的符号使用 ``speaker_markers``/``dialogue_markers``，每项为
    ``{"open": "...", "close": "...", "allow_unclosed": false}``；
    ``dialogue_region`` 是完整窗口中的文本框范围，``speaker_region`` 默认
    相对于对白框，``choice_region`` 默认相对于完整窗口。三个区域都支持
    normalized 或 pixels 坐标。传 ``{}`` 可恢复保守的通用解析。
    """

    return STORE.configure_game_layout(profile=profile, session_id=session_id)


@mcp.tool()
def configure_game_actions(
    actions: dict[str, Any],
    session_id: str | None = None,
) -> dict[str, Any]:
    """保存当前游戏的命名动作 profile。

    每个动作是 ``{"kind": "click|key|scroll|hold|wait|focus", ...}``；
    click 支持 ``target="window_center"``、``target="window_normalized"``
    或显式屏幕坐标，key/scroll/hold/wait/focus 使用对应参数。动作 profile
    只保存数据，不执行任意代码；传空对象可清除当前游戏的自定义动作。
    """

    return STORE.configure_game_actions(profile=actions, session_id=session_id)


@mcp.tool()
def configure_game_timing(
    profile: dict[str, Any],
    session_id: str | None = None,
) -> dict[str, Any]:
    """保存当前游戏的点击后等待与打字机动画稳定策略。

    ``strategy="fixed"`` 保持快速固定等待；``strategy="text_hash"``
    会在每次推进后本地轮询底部文本框，要求文本先发生变化并连续稳定
    ``stable_samples`` 次，才允许下一次输入。若某个游戏支持点击跳过转场，
    可显式设置 ``transition_accelerate=true``；MCP 会用完整窗口多帧画面变化
    和 OCR 排除条件确认转场后，最多额外点击一次。传空对象可清除当前游戏的
    自定义 timing profile。
    """

    return STORE.configure_game_timing(profile=profile, session_id=session_id)


@mcp.tool()
def perform_game_action(
    action: str,
    parameters: dict[str, Any] | None = None,
    window_title: str | None = None,
    background: bool | None = None,
    wait_seconds: float | None = None,
    record: bool = True,
    session_id: str | None = None,
) -> dict[str, Any]:
    """执行一个通用或当前游戏 profile 中的命名动作。

    ``background`` 未指定时，绑定窗口的 click/key/scroll 默认走后台窗口
    消息；hold/focus/wait 默认不抢焦点。后台 click 的坐标是屏幕坐标，窗口
    相对坐标会在本地通过窗口矩形转换，真实鼠标不会移动。
    """

    session: dict[str, Any] = {}
    has_session = False
    if session_id is not None:
        session = STORE.get_session(session_id=session_id)
        has_session = True
    else:
        try:
            session = STORE.get_session()
            has_session = True
        except Exception:
            session = {}
    resolved_name, spec, configured = _resolve_game_action(session, action, parameters)
    game = session.get("game") or {}
    title = (window_title or game.get("window_title") or spec.get("window_title") or "").strip() or None
    kind = spec["kind"]

    profile_background = spec.get("background")
    if profile_background is not None and not isinstance(profile_background, bool):
        raise ValueError("动作 background 必须是布尔值")
    if background is None:
        if isinstance(profile_background, bool):
            effective_background = profile_background
        else:
            effective_background = bool(title and kind in {"click", "key", "scroll"})
    else:
        effective_background = bool(background)
    if kind in {"focus", "hold", "wait"} and effective_background:
        raise ValueError(f"{kind} 动作不能使用 background=true")
    if kind == "scroll" and not effective_background:
        raise ValueError("scroll 动作目前只支持后台窗口消息，请使用 background=true")

    delivery = str(spec.get("delivery") or ("send" if effective_background else "foreground")).strip().casefold()
    if effective_background and delivery not in {"post", "send"}:
        raise ValueError("后台动作 delivery 必须是 post 或 send")

    input_result: dict[str, Any]
    resolved_point: dict[str, Any] | None = None
    if kind == "click":
        button = str(spec.get("button") or "left").strip().casefold()
        if button not in {"left", "right", "middle"}:
            raise ValueError("click 的 button 必须是 left、right 或 middle")
        x, y, rect = _action_window_point(spec, title)
        clicks = _action_int(spec, "clicks", 1, 1, 10)
        interval_ms = _action_int(spec, "interval_ms", 0, 0, 2000)
        resolved_point = {"x": x, "y": y, "coordinate_space": "screen"}
        if rect is not None:
            resolved_point["window"] = {
                key: rect[key] for key in ("x", "y", "width", "height") if key in rect
            }
        if effective_background:
            if not title:
                raise ValueError("后台 click 必须提供 window_title 或先 attach_game")
            input_result = native_post_window_click(
                title=title,
                x=x,
                y=y,
                button=button,
                clicks=clicks,
                interval_ms=interval_ms,
                delivery=delivery,
            )
        else:
            input_result = native_click_screen(
                x=x,
                y=y,
                button=button,
                clicks=clicks,
                interval_ms=interval_ms,
            )
    elif kind == "key":
        key = str(spec.get("key") or "").strip()
        if not key:
            raise ValueError("key 动作必须提供 key")
        presses = _action_int(spec, "presses", 1, 1, 20)
        interval_ms = _action_int(spec, "interval_ms", 0, 0, 2000)
        if effective_background:
            if not title:
                raise ValueError("后台 key 必须提供 window_title 或先 attach_game")
            input_result = native_post_window_key(
                title=title,
                key=key,
                presses=presses,
                interval_ms=interval_ms,
                delivery=delivery,
            )
        else:
            input_result = native_send_key(key=key, presses=presses, interval_ms=interval_ms)
    elif kind == "scroll":
        if not title:
            raise ValueError("后台 scroll 必须提供 window_title 或先 attach_game")
        delta, direction = _action_scroll_delta(spec)
        clicks = _action_int(spec, "clicks", 1, 1, 20)
        interval_ms = _action_int(spec, "interval_ms", 0, 0, 2000)
        x = spec.get("x")
        y = spec.get("y")
        if x is None or y is None:
            x = y = None
        else:
            x, y, _ = _action_window_point(spec, title)
        input_result = native_post_window_wheel(
            title=title,
            x=x,
            y=y,
            delta=delta,
            clicks=clicks,
            interval_ms=interval_ms,
            delivery=delivery,
        )
        input_result.setdefault("direction", direction)
    elif kind == "hold":
        key = str(spec.get("key") or "").strip()
        if not key:
            raise ValueError("hold 动作必须提供 key")
        duration = _action_float(spec, "hold_seconds", float(spec.get("seconds", 1.0)), 0.01, 30.0)
        input_result = native_hold_key(key=key, hold_seconds=duration)
    elif kind == "focus":
        if not title:
            raise ValueError("focus 动作必须提供 window_title 或先 attach_game")
        input_result = native_focus_window(title)
    else:  # wait
        duration = _action_float(
            spec,
            "seconds",
            float(spec.get("duration", 0.0)),
            0.0,
            30.0,
        )
        time.sleep(duration)
        input_result = {"seconds": duration, "input_method": "local_wait"}

    if kind != "wait":
        post_wait = wait_seconds
        if post_wait is None:
            post_wait = spec.get("wait_seconds", 0.0)
        try:
            post_wait_value = max(0.0, min(float(post_wait or 0.0), 30.0))
        except (TypeError, ValueError) as exc:
            raise ValueError("wait_seconds 必须是数字") from exc
        if not math.isfinite(post_wait_value):
            raise ValueError("wait_seconds 不能是 NaN 或无穷大")
        if post_wait_value:
            time.sleep(post_wait_value)
    else:
        post_wait_value = float(input_result.get("seconds", 0.0))

    record_event = None
    if record and has_session:
        record_event = STORE.record_action(
            "game_action",
            {
                "action": resolved_name,
                "configured": configured,
                "kind": kind,
                "window_title": title,
                "background": effective_background,
                "delivery": delivery if effective_background else None,
                "parameters": copy.deepcopy(spec),
                "resolved_point": resolved_point,
                "wait_seconds": post_wait_value,
                **input_result,
            },
            session_id=session_id,
        )
    return {
        "action": resolved_name,
        "configured": configured,
        "kind": kind,
        "window_title": title,
        "background": effective_background,
        "delivery": delivery if effective_background else None,
        "input": input_result,
        "resolved_point": resolved_point,
        "wait_seconds": post_wait_value,
        "recorded": (
            {"event_id": record_event.get("event_id")} if record_event else None
        ),
    }


@mcp.tool()
def attach_game(
    window_title: str,
    advance_key: str = "SPACE",
    advance_hold_seconds: float | None = None,
    choice_mode: str = "number",
    session_id: str | None = None,
    focus_window: bool = False,
) -> dict[str, Any]:
    """绑定已经打开的游戏窗口，不默认切换前台；需要时显式传 ``focus_window=true``。"""

    session = STORE.get_session(session_id=session_id)
    # Binding is a read-only operation. ``focus_window`` is deliberately an
    # explicit opt-in because SetForegroundWindow steals the user's current
    # application even when later operations use background window messages.
    focused = native_focus_window(window_title) if focus_window else None
    window_info = focused or native_get_window_rect(window_title)
    configuration = STORE.configure_game(
        window_title=window_title,
        advance_key=advance_key,
        advance_hold_seconds=advance_hold_seconds,
        choice_mode=choice_mode,
        session_id=session["session_id"],
    )
    STORE.record_action(
        "attach_game",
        {
            "window_title": window_title,
            "advance_key": advance_key,
            "advance_hold_seconds": advance_hold_seconds,
            "choice_mode": choice_mode,
            "focus_requested": focus_window,
            "window": window_info,
        },
        session_id=session["session_id"],
    )
    return {
        "attached": True,
        "focus": focused,
        "focus_requested": focus_window,
        "window": window_info,
        **configuration,
    }


@mcp.tool(structured_output=False)
def observe_game(
    window_title: str | None = None,
    ocr: bool = True,
    record_text: bool = True,
    language: str = "auto",
    include_image: bool = False,
    session_id: str | None = None,
    capture_mode: str = "auto",
    focus_before_capture: bool = False,
    include_raw_text: bool = False,
    auto_return_from_settings: bool = True,
    ocr_region: dict[str, Any] | None = None,
) -> Any:
    """本地截图并 OCR；首次窗口帧完整保存，后续对白优先使用快速文本框帧。"""

    session = STORE.get_session(session_id=session_id)
    title = window_title or session.get("game", {}).get("window_title")
    effective_ocr_region = _effective_ocr_region(session, title, ocr_region)
    mode = (capture_mode or "auto").strip().lower()
    # Capturing must not change the user's foreground application implicitly.
    # Callers that intentionally need a foreground/desktop capture can opt in.
    should_focus = bool(focus_before_capture)
    if title and should_focus:
        focused = native_focus_window(title)
        STORE.record_action("focus_window", {"title": title, **focused}, session_id=session["session_id"])
    fast_region = effective_ocr_region if _capture_uses_fast_dialogue_region(
        mode=mode,
        title=title,
        ocr=ocr,
        include_image=include_image,
        region=effective_ocr_region,
    ) else None
    payload, image_path = _capture_for_session(
        window_title=title,
        capture_mode=mode,
        session_id=session["session_id"],
        fast_region=fast_region,
    )
    payload = _process_capture_text(
        payload,
        image_path,
        session,
        ocr=ocr,
        record_text=record_text,
        language=language,
        include_raw_text=include_raw_text,
        ocr_region=effective_ocr_region,
    )
    if ocr and fast_region is not None and not _fast_capture_has_text(payload):
        # A region frame can be blank during a transition or can expose an
        # occluded GPU surface. One full-frame fallback keeps settings recovery
        # and title/loading screens observable without slowing normal dialogue.
        full_payload, full_image_path = _capture_for_session(
            window_title=title,
            capture_mode=mode,
            session_id=session["session_id"],
            fast_region=None,
        )
        full_payload.setdefault(
            "capture_fallback",
            {
                "from": "window_dialogue_region",
                "to": "window_full",
                "reason": "dialogue_region_ocr_empty",
            },
        )
        payload, image_path = full_payload, full_image_path
        payload = _process_capture_text(
            payload,
            image_path,
            session,
            ocr=ocr,
            record_text=record_text,
            language=language,
            include_raw_text=include_raw_text,
            ocr_region=effective_ocr_region,
        )
    payload, image_path = _auto_return_from_settings(
        payload,
        session,
        title=title,
        capture_mode=mode,
        ocr=ocr,
        record_text=record_text,
        language=language,
        include_raw_text=include_raw_text,
        enabled=auto_return_from_settings,
        background=bool(title and not should_focus),
        ocr_region=effective_ocr_region,
    )
    _remember_bottom_snapshot(session, payload)
    return _capture_result(payload, image_path, include_image)


@mcp.tool(structured_output=False)
def advance_game(
    wait_seconds: float | None = None,
    transition_wait_seconds: float | None = None,
    wait_strategy: str | None = None,
    hold_seconds: float | None = None,
    ocr: bool = True,
    record_text: bool = True,
    language: str = "auto",
    include_image: bool = False,
    session_id: str | None = None,
    capture_mode: str = "auto",
    include_raw_text: bool = False,
    auto_return_from_settings: bool = True,
    background: bool = True,
    background_input_method: str = "send",
    ocr_region: dict[str, Any] | None = None,
) -> Any:
    """用绑定的 advance_key 推进一段对白/动画，再返回新的截图。

    ``background=True`` 直接向窗口中心发送后台左键，不激活窗口、不移动
    鼠标；千恋＊万花不使用后台空格。后台验证会优先复用最近一次 observe_game
    的底部 OCR，过期时才重新截图。
    """

    session = STORE.get_session(session_id=session_id)
    game = session.get("game", {})
    title = game.get("window_title")
    timing = _resolve_timing_profile(
        session,
        wait_seconds=wait_seconds,
        transition_wait_seconds=transition_wait_seconds,
        wait_strategy=wait_strategy,
    )
    duration = timing["post_click_wait_seconds"]
    transition_wait = timing["transition_wait_seconds"]
    hash_settle_enabled = timing["strategy"] == "text_hash" and ocr
    effective_ocr_region = _effective_ocr_region(session, title, ocr_region)
    fast_region = effective_ocr_region if _capture_uses_fast_dialogue_region(
        mode=capture_mode,
        title=title,
        ocr=ocr,
        include_image=include_image,
        region=effective_ocr_region,
    ) else None
    if background and not title:
        raise ValueError("background=true 时必须先 attach_game 绑定 window_title")
    if title and not background:
        focused = native_focus_window(title)
    else:
        focused = None
    before_bottom_text: dict[str, Any] | None = None
    baseline: dict[str, Any] | None = None
    baseline_path: Path | None = None
    transition_baseline_image_path: Path | None = None
    transition_acceleration_enabled = bool(
        ocr
        and timing["transition_accelerate"]
        and timing["strategy"] == "fixed"
    )
    if ocr:
        before_bottom_text = _cached_bottom_snapshot(session)
        if before_bottom_text is None:
            baseline, baseline_path = _capture_for_session(
                window_title=title,
                capture_mode=capture_mode,
                session_id=session["session_id"],
                fast_region=fast_region,
            )
            baseline = _process_capture_text(
                baseline,
                baseline_path,
                session,
                ocr=True,
                record_text=False,
                language=language,
                include_raw_text=False,
                ocr_region=effective_ocr_region,
            )
            if fast_region is not None and not _fast_capture_has_text(baseline):
                baseline, baseline_path = _capture_for_session(
                    window_title=title,
                    capture_mode=capture_mode,
                    session_id=session["session_id"],
                    fast_region=None,
                )
                baseline = _process_capture_text(
                    baseline,
                    baseline_path,
                    session,
                    ocr=True,
                    record_text=False,
                    language=language,
                    include_raw_text=False,
                    ocr_region=effective_ocr_region,
                )
            before_bottom_text = _bottom_text_snapshot(baseline)
    if transition_acceleration_enabled:
        # Motion comparison needs a complete pre-click frame.  Do not OCR it:
        # this optional path adds one local capture, not another OCR pass.
        if baseline_path is not None and baseline and baseline.get("capture_scope") != "window_dialogue_region":
            transition_baseline_image_path = baseline_path
        else:
            transition_baseline, transition_baseline_image_path = _capture_for_session(
                window_title=title,
                capture_mode=capture_mode,
                session_id=session["session_id"],
                fast_region=None,
            )
            if transition_baseline.get("capture_scope") == "window_dialogue_region":
                transition_baseline_image_path = None
    if (
        ocr
        and baseline is not None
        and isinstance(baseline.get("ocr_uncertain"), dict)
        and baseline["ocr_uncertain"].get("required")
    ):
        baseline["advance_blocked"] = {
            "required": True,
            "reason": "ocr_uncertain",
            "message": "全窗口 OCR 无法确认当前画面，已阻止推进并请求 Codex 视觉复核。",
        }
        _remember_bottom_snapshot(session, baseline)
        return _capture_result(
            baseline,
            baseline_path or Path(str(baseline["image_path"])),
            include_image,
        )
    control = game.get("control", {})
    key = control.get("advance_key") or "SPACE"
    configured_hold = control.get("advance_hold_seconds", 0.0)
    requested_hold = configured_hold if hold_seconds is None else hold_seconds
    try:
        hold_duration = max(0.0, min(float(requested_hold or 0.0), 30.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("hold_seconds 必须是数字") from exc
    input_type = "foreground_key"
    if hold_duration > 0:
        if background:
            raise ValueError("background=true 不支持 hold_seconds；后台推进使用离散左键")
        key_result = native_hold_key(key=key, hold_seconds=hold_duration)
    elif background:
        click_point = _window_center_screen_point(baseline or {})
        if click_point is None:
            if not title:
                raise ValueError("background=true 时必须先 attach_game 绑定 window_title")
            click_point = _window_center_screen_point({"window": native_get_window_rect(title)})
        if click_point is None:
            raise ValueError("无法确定后台推进的窗口中心坐标")
        key_result = native_post_window_click(
            title=title,
            x=click_point[0],
            y=click_point[1],
            button="left",
            clicks=1,
            interval_ms=0,
            delivery=background_input_method,
        )
        input_type = "background_click"
    else:
        key_result = native_send_key(key=key, presses=1, interval_ms=0)
    if duration:
        time.sleep(duration)
    action = STORE.record_action(
        "advance_game",
        {
            "key": key,
            "input_type": input_type,
            "hold_seconds": hold_duration,
            "wait_seconds": duration,
            "wait_strategy": timing["strategy"],
            "transition_accelerate": timing["transition_accelerate"],
            "transition_accelerate_delay_seconds": timing["transition_accelerate_delay_seconds"],
            "transition_probe_interval_seconds": timing["transition_probe_interval_seconds"],
            "settle_timeout_seconds": timing["settle_timeout_seconds"],
            "settle_poll_seconds": timing["settle_poll_seconds"],
            "stable_samples": timing["stable_samples"],
            "focus": focused,
            "background": background,
            **key_result,
        },
        session_id=session["session_id"],
    )
    payload, image_path = _capture_for_session(
        window_title=title,
        capture_mode=capture_mode,
        session_id=session["session_id"],
        fast_region=fast_region,
    )
    payload["action_event"] = action
    payload = _process_capture_text(
        payload,
        image_path,
        session,
        ocr=ocr,
        record_text=record_text and not hash_settle_enabled,
        language=language,
        include_raw_text=include_raw_text,
        ocr_region=effective_ocr_region,
    )
    if ocr and fast_region is not None and not _fast_capture_has_text(payload):
        # Region capture is the normal path; use one complete frame only when
        # the region was blank/unreadable so settings and transitions remain
        # distinguishable from a broken capture backend.
        full_payload, full_image_path = _capture_for_session(
            window_title=title,
            capture_mode=capture_mode,
            session_id=session["session_id"],
            fast_region=None,
        )
        full_payload["action_event"] = action
        full_payload.setdefault(
            "capture_fallback",
            {
                "from": "window_dialogue_region",
                "to": "window_full",
                "reason": "dialogue_region_ocr_empty",
            },
        )
        payload, image_path = full_payload, full_image_path
        payload = _process_capture_text(
            payload,
            image_path,
            session,
            ocr=ocr,
            record_text=record_text and not hash_settle_enabled,
            language=language,
            include_raw_text=include_raw_text,
            ocr_region=effective_ocr_region,
        )
    payload, image_path = _auto_return_from_settings(
        payload,
        session,
        title=title,
        capture_mode=capture_mode,
        ocr=ocr,
        record_text=record_text and not hash_settle_enabled,
        language=language,
        include_raw_text=include_raw_text,
        enabled=auto_return_from_settings,
        background=background,
        background_input_method=background_input_method,
        ocr_region=effective_ocr_region,
    )
    timing_wait: dict[str, Any] | None = None
    if hash_settle_enabled:
        payload, image_path, timing_wait = _wait_for_text_hash_stable(
            first_payload=payload,
            first_image_path=image_path,
            before_snapshot=before_bottom_text,
            timing=timing,
            window_title=title,
            capture_mode=capture_mode,
            session=session,
            language=language,
            include_raw_text=include_raw_text,
            ocr_region=effective_ocr_region,
            include_image=include_image,
            action_event=action,
            background=background,
            background_input_method=background_input_method,
            auto_return_from_settings=auto_return_from_settings,
        )
        if timing_wait.get("settled") and record_text:
            payload = _process_capture_text(
                payload,
                image_path,
                session,
                ocr=True,
                record_text=True,
                language=language,
                include_raw_text=include_raw_text,
                ocr_region=effective_ocr_region,
            )
    transition_acceleration: dict[str, Any] | None = None
    if (
        transition_acceleration_enabled
        and before_bottom_text
        and before_bottom_text.get("detected")
        and not _bottom_text_snapshot(payload).get("detected")
    ):
        payload, image_path, transition_acceleration = _probe_transition_after_click(
            before_image_path=transition_baseline_image_path,
            first_payload=payload,
            first_image_path=image_path,
            timing=timing,
            window_title=title,
            capture_mode=capture_mode,
            session=session,
            language=language,
            include_raw_text=include_raw_text,
            ocr_region=effective_ocr_region,
            action_event=action,
            background=background,
            background_input_method=background_input_method,
            auto_return_from_settings=auto_return_from_settings,
        )
        if transition_acceleration.get("confirmed"):
            if hold_duration > 0:
                transition_acceleration.update(
                    {
                        "extra_click_sent": False,
                        "extra_click_skipped_reason": "hold_action_not_discrete",
                    }
                )
            else:
                extra_delay = timing["transition_accelerate_delay_seconds"]
                if extra_delay:
                    time.sleep(extra_delay)
                if background:
                    click_point = _window_center_screen_point(payload)
                    if click_point is None:
                        if not title:
                            raise ValueError("background=true 时必须先 attach_game 绑定 window_title")
                        click_point = _window_center_screen_point({"window": native_get_window_rect(title)})
                    if click_point is None:
                        raise ValueError("无法确定后台推进的窗口中心坐标")
                    extra_result = native_post_window_click(
                        title=title,
                        x=click_point[0],
                        y=click_point[1],
                        button="left",
                        clicks=1,
                        interval_ms=0,
                        delivery=background_input_method,
                    )
                    extra_input_type = "background_click"
                else:
                    extra_result = native_send_key(key=key, presses=1, interval_ms=0)
                    extra_input_type = "foreground_key"
                extra_action = STORE.record_action(
                    "advance_game_transition_accelerate",
                    {
                        "key": key,
                        "input_type": extra_input_type,
                        "wait_seconds": duration,
                        "wait_strategy": timing["strategy"],
                        "transition_accelerate_delay_seconds": extra_delay,
                        "background": background,
                        "trigger": transition_acceleration,
                        **extra_result,
                    },
                    session_id=session["session_id"],
                )
                transition_acceleration.update(
                    {
                        "extra_click_sent": True,
                        "extra_click_action": extra_action,
                    }
                )
                if duration:
                    time.sleep(duration)
                payload, image_path = _capture_processed_frame(
                    window_title=title,
                    capture_mode=capture_mode,
                    session=session,
                    ocr=ocr,
                    record_text=record_text,
                    language=language,
                    include_raw_text=include_raw_text,
                    ocr_region=effective_ocr_region,
                    include_image=include_image,
                    action_event=extra_action,
                    force_full=True,
                )
                payload, image_path = _auto_return_from_settings(
                    payload,
                    session,
                    title=title,
                    capture_mode=capture_mode,
                    ocr=ocr,
                    record_text=record_text,
                    language=language,
                    include_raw_text=include_raw_text,
                    enabled=auto_return_from_settings,
                    background=background,
                    background_input_method=background_input_method,
                    ocr_region=effective_ocr_region,
                )
    verification = _compare_bottom_text(before_bottom_text, payload)
    if timing_wait is not None:
        verification["timing_wait"] = timing_wait
    if transition_acceleration is not None:
        verification["transition_acceleration"] = transition_acceleration
    transition_retries = 0
    transition_waited = 0.0
    if (
        ocr
        and before_bottom_text
        and before_bottom_text.get("detected")
        and not _bottom_text_snapshot(payload).get("detected")
        and transition_wait > 0
    ):
        # A click can briefly clear the dialogue panel during a fade/transition.
        # Wait locally with a small backoff.  No additional input is sent while
        # the game is settling, so this cannot turn a transition into a blind
        # option click.
        transition_started = time.perf_counter()
        while True:
            elapsed = time.perf_counter() - transition_started
            remaining = transition_wait - elapsed
            delay = _transition_retry_delay(transition_retries, duration, remaining)
            if delay <= 0:
                break
            time.sleep(delay)
            transition_waited += delay
            transition_retries += 1
            retry_payload, retry_image_path = _capture_processed_frame(
                window_title=title,
                capture_mode=capture_mode,
                session=session,
                ocr=ocr,
                record_text=record_text,
                language=language,
                include_raw_text=include_raw_text,
                ocr_region=effective_ocr_region,
                include_image=include_image,
                action_event=action,
            )
            retry_payload, retry_image_path = _auto_return_from_settings(
                retry_payload,
                session,
                title=title,
                capture_mode=capture_mode,
                ocr=ocr,
                record_text=record_text,
                language=language,
                include_raw_text=include_raw_text,
                enabled=auto_return_from_settings,
                background=background,
                background_input_method=background_input_method,
                ocr_region=effective_ocr_region,
            )
            payload, image_path = retry_payload, retry_image_path
            verification = _compare_bottom_text(before_bottom_text, payload)
            if _bottom_text_snapshot(payload).get("detected"):
                break
        verification = _compare_bottom_text(before_bottom_text, payload)
        verification["settle_retries"] = transition_retries
        verification["transition_waited_seconds"] = round(transition_waited, 3)
        verification["transition_settled"] = bool(_bottom_text_snapshot(payload).get("detected"))
    if transition_acceleration is not None:
        payload["transition_acceleration"] = transition_acceleration
    payload["input_verification"] = verification
    _remember_bottom_snapshot(session, payload)
    return _capture_result(payload, image_path, include_image)


def _probe_full_window_for_choices(
    *,
    title: str | None,
    capture_mode: str,
    session: dict[str, Any],
    ocr: bool,
    record_text: bool,
    language: str,
    include_raw_text: bool,
) -> tuple[dict[str, Any], Path]:
    """Run a full-frame choice OCR only after the dialogue area goes blank."""

    payload, image_path = _capture_for_session(
        window_title=title,
        capture_mode=capture_mode,
        session_id=session["session_id"],
        fast_region=None,
    )
    payload = _process_capture_text(
        payload,
        image_path,
        session,
        ocr=ocr,
        record_text=record_text,
        language=language,
        include_raw_text=include_raw_text,
        ocr_region=None,
    )
    return payload, image_path


@mcp.tool(structured_output=False)
def play_until_choice(
    max_steps: int | None = None,
    wait_seconds: float | None = None,
    transition_wait_seconds: float | None = None,
    wait_strategy: str | None = None,
    ocr: bool = True,
    record_text: bool = True,
    language: str = "auto",
    include_image: bool = False,
    session_id: str | None = None,
    capture_mode: str = "auto",
    include_raw_text: bool = False,
    auto_return_from_settings: bool = True,
    background: bool = True,
    background_input_method: str = "send",
    ocr_region: dict[str, Any] | None = None,
    max_batch_chars: int | None = None,
) -> Any:
    """Locally read and advance many dialogue frames until a game choice appears.

    Every intermediate capture, OCR result, parsed dialogue, and advance action
    stays inside the local MCP process/session. Codex receives one compact batch
    only when a choice is detected or a safety limit stops the loop.
    """

    if not ocr:
        raise ValueError("play_until_choice 必须启用 ocr=true")
    if max_steps is None:
        step_limit: int | None = None
    else:
        try:
            step_limit = max(0, min(int(max_steps), _MAX_PLAY_STEPS))
        except (TypeError, ValueError) as exc:
            raise ValueError("max_steps 必须是数字或 null") from exc
    if max_batch_chars is None:
        batch_char_limit: int | None = None
    else:
        try:
            batch_char_limit = max(1000, min(int(max_batch_chars), 100000))
        except (TypeError, ValueError) as exc:
            raise ValueError("max_batch_chars 必须是数字或 null") from exc
    session = STORE.get_session(session_id=session_id)
    timing = _resolve_timing_profile(
        session,
        wait_seconds=wait_seconds,
        transition_wait_seconds=transition_wait_seconds,
        wait_strategy=wait_strategy,
    )
    duration = timing["post_click_wait_seconds"]
    transition_wait = timing["transition_wait_seconds"]
    game = session.get("game", {})
    title = game.get("window_title")
    transition_acceleration_enabled = bool(
        timing["transition_accelerate"] and timing["strategy"] == "fixed"
    )
    if background and not title:
        raise ValueError("background=true 时必须先 attach_game 绑定 window_title")
    if title and not background:
        focused = native_focus_window(title)
    else:
        focused = None
    effective_ocr_region = _effective_ocr_region(session, title, ocr_region)
    started = time.perf_counter()
    batch: list[dict[str, Any]] = []
    seen_items: set[tuple[str, str, tuple[str, ...]]] = set()
    batch_chars = 0
    advance_count = 0
    frame_count = 0
    settings_recoveries = 0
    stop_reason = "not_stopped"
    final_payload: dict[str, Any] | None = None
    final_image_path: Path | None = None
    internal_error: dict[str, Any] | None = None
    last_action: dict[str, Any] | None = None
    last_item_key: tuple[str, str, tuple[str, ...]] | None = None
    unchanged_item_frames = 0
    transition_started_at: float | None = None
    transition_retry_index = 0
    transition_waited = 0.0
    transition_probe_attempts = 0
    transition_probe_count = 0
    transition_acceleration_count = 0
    transition_acceleration_records: list[dict[str, Any]] = []
    ocr_fallback_settles = 0.0
    pending_frame: tuple[dict[str, Any], Path] | None = None
    pending_settle_before: dict[str, Any] | None = None
    timing_waits: list[dict[str, Any]] = []
    timing_settle_failed = False
    episode_tracker = TextEpisodeTracker(stable_samples=timing["stable_samples"])
    # Explicit max_steps calls retain a bounded frame budget for smoke tests.
    # The normal unattended path leaves this unset and relies on OCR/input
    # safety stops plus the compaction threshold instead.
    frame_attempt_limit = (
        None
        if step_limit is None
        else max(10, min(_MAX_PLAY_FRAME_ATTEMPTS, step_limit * 3 + 6))
    )
    frame_attempts = 0
    compaction_status = _play_compaction_status(session["session_id"])

    def probe_choice_frame() -> tuple[dict[str, Any], Path]:
        probe_payload, probe_image_path = _probe_full_window_for_choices(
            title=title,
            capture_mode=capture_mode,
            session=session,
            ocr=True,
            record_text=record_text,
            language=language,
            include_raw_text=include_raw_text,
        )
        return _auto_return_from_settings(
            probe_payload,
            session,
            title=title,
            capture_mode=capture_mode,
            ocr=True,
            record_text=record_text,
            language=language,
            include_raw_text=include_raw_text,
            enabled=auto_return_from_settings,
            background=background,
            background_input_method=background_input_method,
            ocr_region=effective_ocr_region,
        )

    while True:
        if compaction_status and compaction_status.get("summary_due"):
            stop_reason = "compaction_due"
            break
        frame_attempts += 1
        if frame_attempt_limit is not None and frame_attempts > frame_attempt_limit:
            stop_reason = "frame_safety_limit"
            break
        if pending_frame is not None:
            payload, image_path = pending_frame
            pending_frame = None
        else:
            try:
                payload, image_path = _capture_processed_frame(
                    window_title=title,
                    capture_mode=capture_mode,
                    session=session,
                    ocr=True,
                    # In text-hash mode, do not persist partial typewriter
                    # frames.  The stable frame is recorded below after the
                    # local settle check succeeds.
                    record_text=record_text if pending_settle_before is None else False,
                    language=language,
                    include_raw_text=include_raw_text,
                    ocr_region=effective_ocr_region,
                    include_image=include_image,
                    action_event=last_action,
                )
                payload, image_path = _auto_return_from_settings(
                    payload,
                    session,
                    title=title,
                    capture_mode=capture_mode,
                    ocr=True,
                    record_text=record_text if pending_settle_before is None else False,
                    language=language,
                    include_raw_text=include_raw_text,
                    enabled=auto_return_from_settings,
                    background=background,
                    background_input_method=background_input_method,
                    ocr_region=effective_ocr_region,
                )
                if pending_settle_before is not None and timing["strategy"] == "text_hash":
                    payload, image_path, timing_wait = _wait_for_text_hash_stable(
                        first_payload=payload,
                        first_image_path=image_path,
                        before_snapshot=pending_settle_before,
                        timing=timing,
                        window_title=title,
                        capture_mode=capture_mode,
                        session=session,
                        language=language,
                        include_raw_text=include_raw_text,
                        ocr_region=effective_ocr_region,
                        include_image=include_image,
                        action_event=last_action,
                        background=background,
                        background_input_method=background_input_method,
                        auto_return_from_settings=auto_return_from_settings,
                    )
                    pending_settle_before = None
                    timing_waits.append(timing_wait)
                    frame_count += int(timing_wait.get("extra_frames", 0))
                    if not timing_wait.get("settled"):
                        # A typewriter/transition that did not settle within
                        # the configured budget is not safe to click through.
                        # Return the last local frame for Codex inspection and
                        # stop before the normal dialogue branch can emit one
                        # more input.
                        timing_settle_failed = True
                    if timing_wait.get("settled") and record_text:
                        # Re-run OCR only on the final stable frame so partial
                        # typewriter text never becomes a story event.
                        payload = _process_capture_text(
                            payload,
                            image_path,
                            session,
                            ocr=True,
                            record_text=True,
                            language=language,
                            include_raw_text=include_raw_text,
                            ocr_region=effective_ocr_region,
                        )
            except Exception as exc:
                internal_error = {
                    "stage": "frame_processing",
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(limit=12),
                }
                stop_reason = "internal_error"
                break
        final_payload, final_image_path = payload, image_path
        frame_count += 1

        if timing_settle_failed:
            stop_reason = "timing_settle_timeout"
            break

        if payload.get("screen_type") == "settings":
            recovery = payload.get("auto_recovery") or {}
            if recovery.get("returned"):
                settings_recoveries += 1
                continue
            stop_reason = "settings_return_button_not_detected"
            break

        ocr_uncertain = payload.get("ocr_uncertain") or {}
        post_click_transition_candidate = bool(
            ocr_uncertain.get("required")
            and advance_count > 0
            and last_action is not None
            and _is_post_click_transition_ocr_candidate(payload)
        )
        if ocr_uncertain.get("required") and not post_click_transition_candidate:
            stop_reason = "ocr_uncertain"
            break

        item = _batch_dialogue_item(payload, len(batch) + 1)
        parsed = payload.get("processed_text") or {}
        choices = list(parsed.get("choices") or [])
        if choices:
            if item is not None:
                item_key = (
                    str(item.get("speaker") or "旁白"),
                    str(item.get("dialogue") or ""),
                    tuple(str(choice) for choice in choices),
                )
                if item_key not in seen_items:
                    batch.append(item)
            stop_reason = "choice_detected"
            break

        evidence = payload.get("evidence")
        if not isinstance(evidence, dict):
            evidence = build_frame_evidence(
                parsed,
                screen_type=payload.get("screen_type"),
                ocr_available=bool((payload.get("ocr") or {}).get("available")),
            )
            payload["evidence"] = evidence
        if "unknown_text" in (evidence.get("blocking_reasons") or []):
            # Unknown text is different from a blank transition: it may be a
            # new choice or story UI.  Escalate to Codex instead of treating
            # it as dialogue and sending a potentially unsafe click.
            stop_reason = "unknown_text_detected"
            break
        # Raw events are retained until Codex confirms a semantic summary.
        # Stop before the next input once the store crosses its threshold so a
        # long unattended run cannot keep growing events.jsonl indefinitely.
        compaction_status = _play_compaction_status(session["session_id"])
        if compaction_status and compaction_status.get("summary_due"):
            stop_reason = "compaction_due"
            break
        # A zero-step call is still allowed to return the current frame, but
        # it must never enter the blank-frame retry loop and accidentally
        # advance the game.
        if step_limit is not None and advance_count >= step_limit:
            stop_reason = "max_steps"
            break

        fallback_settle_seconds = 0.0
        ocr_fallback = payload.get("ocr_fallback") or {}
        if ocr_fallback.get("full_text_detected"):
            try:
                fallback_settle_seconds = max(
                    0.0,
                    min(
                        float(ocr_fallback.get("settle_wait_seconds", _OCR_FALLBACK_SETTLE_SECONDS)),
                        _OCR_FALLBACK_SETTLE_SECONDS,
                    ),
                )
            except (TypeError, ValueError):
                fallback_settle_seconds = _OCR_FALLBACK_SETTLE_SECONDS
            if fallback_settle_seconds:
                # The full frame recovered text that the crop missed. Let the
                # recovered frame settle before sending exactly one advance;
                # this applies even when the parser classified it as an
                # unparsed centered line or chapter card.
                time.sleep(fallback_settle_seconds)
                ocr_fallback_settles += fallback_settle_seconds

        if item is None:
            if not (payload.get("ocr") or {}).get("available"):
                stop_reason = "ocr_unavailable"
                break
            # A VN can briefly expose only a transition frame while changing a
            # background or showing a chapter card.  Give it a bounded local
            # settle window before probing the complete frame for choices.
            if transition_started_at is None:
                transition_started_at = time.perf_counter()
            elapsed = time.perf_counter() - transition_started_at
            remaining = transition_wait - elapsed
            if transition_probe_attempts == 0:
                delay = _transition_retry_delay(transition_retry_index, duration, remaining)
                if delay > 0:
                    time.sleep(delay)
                    transition_waited += delay
                    transition_retry_index += 1
                    continue

                # Once the settle window expires, inspect the complete frame
                # once so centered choices and transition UI become visible;
                # do not send another blind click that could select an option.
                transition_probe_attempts = 1
                transition_probe_count += 1
                try:
                    probe_payload, probe_image_path = probe_choice_frame()
                except Exception as exc:
                    internal_error = {
                        "stage": "blank_frame_probe",
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "traceback": traceback.format_exc(limit=12),
                    }
                    stop_reason = "internal_error"
                    break
                final_payload, final_image_path = probe_payload, probe_image_path
                frame_count += 1
                if probe_payload.get("screen_type") == "settings":
                    recovery = probe_payload.get("auto_recovery") or {}
                    if recovery.get("returned"):
                        settings_recoveries += 1
                        transition_started_at = None
                        transition_retry_index = 0
                        transition_probe_attempts = 0
                        continue
                    stop_reason = "settings_return_button_not_detected"
                    break
                probe_choices = list((probe_payload.get("processed_text") or {}).get("choices") or [])
                if probe_choices:
                    probe_item = _batch_dialogue_item(probe_payload, len(batch) + 1)
                    if probe_item is not None:
                        item_key = (
                            str(probe_item.get("speaker") or "旁白"),
                            str(probe_item.get("dialogue") or ""),
                            tuple(str(choice) for choice in probe_choices),
                        )
                        if item_key not in seen_items:
                            batch.append(probe_item)
                    stop_reason = "choice_detected"
                    break
                probe_item = _batch_dialogue_item(probe_payload, len(batch) + 1)
                if probe_item is not None and _bottom_text_snapshot(probe_payload).get("detected"):
                    # The full probe found a real bottom dialogue frame after
                    # the fast crop was blank.  Process that exact frame on the
                    # next iteration; recapturing the same fast crop here could
                    # otherwise reset the transition timer forever when OCR
                    # sees only a stable UI residue.
                    transition_started_at = None
                    transition_retry_index = 0
                    transition_probe_attempts = 0
                    pending_frame = (probe_payload, probe_image_path)
                    continue
                stop_reason = "dialogue_not_detected"
                break
            stop_reason = "dialogue_not_detected"
            break
        transition_started_at = None
        transition_retry_index = 0
        transition_probe_attempts = 0
        dialogue_text = str(item.get("dialogue") or "").strip()
        if dialogue_text:
            episode = episode_tracker.observe(
                dialogue_text,
                channel="dialogue",
                recognized=True,
                confidence=item.get("confidence"),
            )
            if episode:
                item["episode"] = episode
        item_key = (
            str(item.get("speaker") or "旁白"),
            str(item.get("dialogue") or ""),
            tuple(str(choice) for choice in choices),
        )
        if item_key == last_item_key:
            unchanged_item_frames += 1
        else:
            last_item_key = item_key
            unchanged_item_frames = 0
        if unchanged_item_frames >= 2 and advance_count > 0:
            # Several delivered clicks without a new bottom-text frame usually
            # means a choice overlay is waiting. Probe the full window before
            # sending another click, because another blind click could select
            # a default option.
            try:
                probe_payload, probe_image_path = probe_choice_frame()
            except Exception as exc:
                internal_error = {
                    "stage": "unchanged_frame_probe",
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(limit=12),
                }
                stop_reason = "internal_error"
                break
            final_payload, final_image_path = probe_payload, probe_image_path
            frame_count += 1
            if probe_payload.get("screen_type") == "settings":
                recovery = probe_payload.get("auto_recovery") or {}
                if recovery.get("returned"):
                    settings_recoveries += 1
                    last_item_key = None
                    unchanged_item_frames = 0
                    continue
                stop_reason = "settings_return_button_not_detected"
                break
            probe_choices = list((probe_payload.get("processed_text") or {}).get("choices") or [])
            if probe_choices:
                probe_item = _batch_dialogue_item(probe_payload, len(batch) + 1)
                if probe_item is not None:
                    probe_key = (
                        str(probe_item.get("speaker") or "旁白"),
                        str(probe_item.get("dialogue") or ""),
                        tuple(str(choice) for choice in probe_choices),
                    )
                    if probe_key not in seen_items:
                        batch.append(probe_item)
                stop_reason = "choice_detected"
                break
            stop_reason = "dialogue_not_advancing"
            break
        if item_key not in seen_items:
            item_chars = len(str(item.get("dialogue") or "")) + sum(len(str(choice)) for choice in choices)
            if (
                batch_char_limit is not None
                and batch
                and batch_chars + item_chars > batch_char_limit
            ):
                stop_reason = "batch_char_limit"
                break
            seen_items.add(item_key)
            batch.append(item)
            batch_chars += item_chars

        if step_limit is not None and advance_count >= step_limit:
            stop_reason = "max_steps"
            break

        transition_baseline_image_path: Path | None = None
        if transition_acceleration_enabled:
            if payload.get("capture_scope") != "window_dialogue_region":
                transition_baseline_image_path = image_path
            else:
                transition_baseline, transition_baseline_image_path = _capture_for_session(
                    window_title=title,
                    capture_mode=capture_mode,
                    session_id=session["session_id"],
                    fast_region=None,
                )
                if transition_baseline.get("capture_scope") == "window_dialogue_region":
                    transition_baseline_image_path = None

        input_type, input_result = _advance_input_for_batch(
            session=session,
            title=title,
            payload=payload,
            background=background,
            background_input_method=background_input_method,
        )
        before_click_snapshot = _bottom_text_snapshot(payload)
        last_action = STORE.record_action(
            "play_until_choice_advance",
            {
                "key": game.get("control", {}).get("advance_key") or "SPACE",
                "input_type": input_type,
                "wait_seconds": duration,
                "wait_strategy": timing["strategy"],
                "settle_timeout_seconds": timing["settle_timeout_seconds"],
                "settle_poll_seconds": timing["settle_poll_seconds"],
                "stable_samples": timing["stable_samples"],
                "ocr_fallback_settle_seconds": fallback_settle_seconds,
                "background": background,
                **input_result,
            },
            session_id=session["session_id"],
        )
        advance_count += 1
        if timing["strategy"] == "text_hash":
            pending_settle_before = before_click_snapshot
        if duration:
            time.sleep(duration)

        if transition_acceleration_enabled and transition_baseline_image_path is not None:
            try:
                first_after_payload, first_after_image_path = _capture_processed_frame(
                    window_title=title,
                    capture_mode=capture_mode,
                    session=session,
                    ocr=True,
                    record_text=record_text,
                    language=language,
                    include_raw_text=include_raw_text,
                    ocr_region=effective_ocr_region,
                    include_image=include_image,
                    action_event=last_action,
                )
                first_after_payload, first_after_image_path = _auto_return_from_settings(
                    first_after_payload,
                    session,
                    title=title,
                    capture_mode=capture_mode,
                    ocr=True,
                    record_text=record_text,
                    language=language,
                    include_raw_text=include_raw_text,
                    enabled=auto_return_from_settings,
                    background=background,
                    background_input_method=background_input_method,
                    ocr_region=effective_ocr_region,
                )
            except Exception as exc:
                internal_error = {
                    "stage": "post_click_transition_candidate",
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(limit=12),
                }
                stop_reason = "internal_error"
                break
            first_after_state = _transition_probe_state(first_after_payload)
            if first_after_state != "blank":
                final_payload, final_image_path = first_after_payload, first_after_image_path
                pending_frame = (first_after_payload, first_after_image_path)
            else:
                try:
                    transition_payload, transition_image_path, transition_decision = _probe_transition_after_click(
                        before_image_path=transition_baseline_image_path,
                        first_payload=first_after_payload,
                        first_image_path=first_after_image_path,
                        timing=timing,
                        window_title=title,
                        capture_mode=capture_mode,
                        session=session,
                        language=language,
                        include_raw_text=include_raw_text,
                        ocr_region=effective_ocr_region,
                        action_event=last_action,
                        background=background,
                        background_input_method=background_input_method,
                        auto_return_from_settings=auto_return_from_settings,
                    )
                except Exception as exc:
                    internal_error = {
                        "stage": "transition_probe",
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "traceback": traceback.format_exc(limit=12),
                    }
                    stop_reason = "internal_error"
                    break
                final_payload, final_image_path = transition_payload, transition_image_path
                frame_count += max(0, int(transition_decision.get("sample_count", 1)) - 1)
                if first_after_payload.get("capture_scope") == "window_dialogue_region":
                    frame_count += 1
                transition_acceleration_records.append(transition_decision)
                pending_frame = (transition_payload, transition_image_path)
                if not transition_decision.get("confirmed"):
                    continue
                extra_delay = timing["transition_accelerate_delay_seconds"]
                if extra_delay:
                    time.sleep(extra_delay)
                extra_input_type, extra_input_result = _advance_input_for_batch(
                    session=session,
                    title=title,
                    payload=transition_payload,
                    background=background,
                    background_input_method=background_input_method,
                )
                last_action = STORE.record_action(
                    "play_until_choice_transition_accelerate",
                    {
                        "key": game.get("control", {}).get("advance_key") or "SPACE",
                        "input_type": extra_input_type,
                        "wait_seconds": duration,
                        "wait_strategy": timing["strategy"],
                        "transition_accelerate_delay_seconds": extra_delay,
                        "background": background,
                        "trigger": transition_decision,
                        **extra_input_result,
                    },
                    session_id=session["session_id"],
                )
                transition_decision["extra_click_sent"] = True
                transition_decision["extra_click_action"] = last_action
                transition_acceleration_count += 1
                advance_count += 1
                if first_after_payload.get("capture_scope") != "window_dialogue_region":
                    # The helper samples are no longer pending after the
                    # extra click, so count the last probe as processed too.
                    frame_count += 1
                if duration:
                    time.sleep(duration)
                try:
                    accelerated_payload, accelerated_image_path = _capture_processed_frame(
                        window_title=title,
                        capture_mode=capture_mode,
                        session=session,
                        ocr=True,
                        record_text=record_text,
                        language=language,
                        include_raw_text=include_raw_text,
                        ocr_region=effective_ocr_region,
                        include_image=include_image,
                        action_event=last_action,
                        force_full=True,
                    )
                    accelerated_payload, accelerated_image_path = _auto_return_from_settings(
                        accelerated_payload,
                        session,
                        title=title,
                        capture_mode=capture_mode,
                        ocr=True,
                        record_text=record_text,
                        language=language,
                        include_raw_text=include_raw_text,
                        enabled=auto_return_from_settings,
                        background=background,
                        background_input_method=background_input_method,
                        ocr_region=effective_ocr_region,
                    )
                except Exception as exc:
                    internal_error = {
                        "stage": "transition_accelerated_capture",
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "traceback": traceback.format_exc(limit=12),
                    }
                    stop_reason = "internal_error"
                    break
                final_payload, final_image_path = accelerated_payload, accelerated_image_path
                pending_frame = (accelerated_payload, accelerated_image_path)

    _remember_bottom_snapshot(session, final_payload or {})
    public_final: dict[str, Any] = {}
    if final_payload is not None:
        for key in (
            "capture_scope",
            "capture_mode",
            "width",
            "height",
            "window",
            "capture_region",
            "capture_fallback",
            "ocr_fallback",
            "ocr",
            "ocr_backends",
            "processed_text",
            "evidence",
            "screen_type",
            "auto_recovery",
            "ocr_uncertain",
            "advance_blocked",
        ):
            if key in final_payload:
                public_final[key] = final_payload[key]
        if include_raw_text and final_payload.get("raw_text"):
            public_final["raw_text"] = final_payload["raw_text"]
    response_batch = batch
    if stop_reason == "compaction_due":
        # The raw source is already available through get_compaction_request;
        # returning the whole pre-compaction batch here would defeat the local
        # token-saving design.
        response_batch = []
    response = {
        "session_id": session["session_id"],
        "stop_reason": stop_reason,
        "choice_detected": stop_reason == "choice_detected",
        "steps_advanced": advance_count,
        "frames_processed": frame_count,
        "settings_recoveries": settings_recoveries,
        "batch": response_batch,
        "final": public_final,
        "last_action": last_action,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }
    compaction_status = _play_compaction_status(session["session_id"])
    if compaction_status is not None:
        response["compaction"] = compaction_status
    if stop_reason == "compaction_due":
        response["batch_omitted_for_compaction"] = {
            "event_count": len(batch),
            "character_count": batch_chars,
            "reason": "use get_compaction_request instead of returning raw batch text",
            "raw_batch_stored_locally": True,
            "next_tool": "get_compaction_request",
            "candidate_available": bool(
                compaction_status and compaction_status.get("candidate_available")
            ),
            "candidate_block_reason": (
                compaction_status.get("candidate_block_reason")
                if compaction_status
                else None
            ),
        }
    if transition_waited or transition_probe_count:
        response["transition_wait"] = {
            "budget_seconds": transition_wait,
            "waited_seconds": round(transition_waited, 3),
            "retry_count": transition_retry_index,
            "full_probe_attempts": transition_probe_count,
        }
    if transition_acceleration_enabled or transition_acceleration_records:
        response["transition_acceleration"] = {
            "enabled": transition_acceleration_enabled,
            "extra_clicks": transition_acceleration_count,
            "probes": transition_acceleration_records[-8:],
        }
    if ocr_fallback_settles:
        response["ocr_fallback_settle_seconds"] = round(ocr_fallback_settles, 3)
    response["timing"] = {
        "strategy": timing["strategy"],
        "post_click_wait_seconds": timing["post_click_wait_seconds"],
        "transition_wait_seconds": timing["transition_wait_seconds"],
        "transition_accelerate": timing["transition_accelerate"],
        "transition_accelerate_delay_seconds": timing["transition_accelerate_delay_seconds"],
        "transition_probe_interval_seconds": timing["transition_probe_interval_seconds"],
        "settle_timeout_seconds": timing["settle_timeout_seconds"],
        "settle_poll_seconds": timing["settle_poll_seconds"],
        "stable_samples": timing["stable_samples"],
        "require_text_change": timing["require_text_change"],
    }
    if timing_waits:
        response["timing"]["settle_checks"] = len(timing_waits)
        response["timing"]["settled_checks"] = sum(
            1 for item in timing_waits if item.get("settled")
        )
        response["timing"]["timeout_checks"] = sum(
            1 for item in timing_waits if item.get("reason") == "timeout"
        )
        response["timing"]["last_settle"] = timing_waits[-1]
    if stop_reason == "frame_safety_limit":
        response["safety_limit"] = {
            "kind": "frame_attempts",
            "limit": frame_attempt_limit,
            "message": "OCR/转场没有形成可推进状态，已停止继续捕获；不会发送额外输入。",
        }
    # Keep the normal path compact, but give Codex one visual escape hatch for
    # an unusual frame that Windows OCR cannot read.  This is deliberately a
    # stop-and-inspect fallback rather than a second OCR backend or another
    # blind input attempt.
    manual_intervention = stop_reason in {
        "dialogue_not_detected",
        "ocr_uncertain",
        "ocr_unavailable",
        "timing_settle_timeout",
        "unknown_text_detected",
    }
    if manual_intervention:
        response["manual_intervention"] = {
            "required": True,
            "reason": (
                "timing_settle_timeout"
                if stop_reason == "timing_settle_timeout"
                else (
                    "unknown_text_detected"
                    if stop_reason == "unknown_text_detected"
                    else "ocr_uncertain"
                    if stop_reason == "ocr_uncertain"
                    else "ocr_frame_empty"
                )
            ),
            "image_path": str(final_image_path) if final_image_path is not None else None,
        }
    if internal_error is not None:
        response["internal_error"] = internal_error
    if (include_image or manual_intervention) and final_image_path is not None:
        return [json.dumps(response, ensure_ascii=False), Image(path=final_image_path)]
    return response


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
    auto_return_from_settings: bool = True,
    background: bool = True,
    background_input_method: str = "send",
    ocr_region: dict[str, Any] | None = None,
) -> Any:
    """选择视觉小说选项；传 choice_id 会同步把对应记录标记为已选择。

    后台模式下，如果当前布局 profile 提供 choice_region，会优先用完整帧
    OCR 的选项 bounding box 发送窗口点击；数字键只作为无法定位坐标时的
    兼容回退。显式 mode=click 时也可以省略 x/y。
    """

    if option_index < 1 or option_index > 99:
        raise ValueError("option_index 必须在 1-99 之间")
    session = STORE.get_session(session_id=session_id)
    game = session.get("game", {})
    title = game.get("window_title")
    effective_ocr_region = _effective_ocr_region(session, title, ocr_region)
    fast_region = effective_ocr_region if _capture_uses_fast_dialogue_region(
        mode=capture_mode,
        title=title,
        ocr=ocr,
        include_image=include_image,
        region=effective_ocr_region,
    ) else None
    if background and not title:
        raise ValueError("background=true 时必须先 attach_game 绑定 window_title")
    if title and not background:
        focused = native_focus_window(title)
    else:
        focused = None
    selected_mode = (mode or game.get("control", {}).get("choice_mode") or "number").lower()
    auto_click_point: tuple[int, int] | None = None
    stored_profile = (game.get("layout_profile") or {}) if isinstance(game, dict) else {}
    should_resolve_choice_click = (
        ocr
        and isinstance(stored_profile, dict)
        and isinstance(stored_profile.get("choice_region"), dict)
        and (
            (selected_mode == "click" and (x is None or y is None))
            or (background and selected_mode == "number")
        )
    )
    if should_resolve_choice_click:
        choice_payload, choice_image_path = _capture_for_session(
            window_title=title,
            capture_mode=capture_mode,
            session_id=session["session_id"],
            fast_region=None,
        )
        choice_payload = _process_capture_text(
            choice_payload,
            choice_image_path,
            session,
            ocr=True,
            record_text=False,
            language=language,
            include_raw_text=False,
            ocr_region=effective_ocr_region,
        )
        auto_click_point = _choice_click_point_from_payload(
            choice_payload,
            session,
            option_index,
        )
        if auto_click_point is not None:
            x, y = auto_click_point
            selected_mode = "click"
        elif mode and selected_mode == "click":
            raise ValueError(
                "无法从当前完整窗口 OCR 定位选项；请传 mode=click 的 x、y，或完善 layout_profile.choice_region"
            )
    action_payload: dict[str, Any] = {
        "option_index": option_index,
        "mode": selected_mode,
        "focus": focused,
        "background": background,
    }
    if auto_click_point is not None:
        action_payload["input_strategy"] = "ocr_choice_region"
        action_payload["auto_click_point"] = {
            "x": auto_click_point[0],
            "y": auto_click_point[1],
        }
    if selected_mode == "number":
        action_payload["input"] = (
            native_post_window_key(
                title=title,
                key=str(option_index),
                presses=1,
                interval_ms=0,
                delivery=background_input_method,
            )
            if background
            else native_send_key(str(option_index), presses=1, interval_ms=0)
        )
    elif selected_mode == "arrow":
        if background:
            steps = [
                native_post_window_key(
                    title=title,
                    key="HOME",
                    presses=1,
                    interval_ms=0,
                    delivery=background_input_method,
                )
            ]
            if option_index > 1:
                steps.append(
                    native_post_window_key(
                        title=title,
                        key="DOWN",
                        presses=option_index - 1,
                        interval_ms=10,
                        delivery=background_input_method,
                    )
                )
            steps.append(
                native_post_window_key(
                    title=title,
                    key="ENTER",
                    presses=1,
                    interval_ms=0,
                    delivery=background_input_method,
                )
            )
            action_payload["input"] = {
                "input_method": (
                    "window_message"
                    if background_input_method == "post"
                    else "window_send_message"
                ),
                "background": True,
                "delivery": background_input_method,
                "steps": steps,
                "messages_posted": sum(int(item.get("messages_posted", 0)) for item in steps),
            }
        else:
            native_send_key("HOME", presses=1, interval_ms=0)
            if option_index > 1:
                native_send_key("DOWN", presses=option_index - 1, interval_ms=10)
            action_payload["input"] = native_send_key("ENTER", presses=1, interval_ms=0)
    elif selected_mode == "key":
        if not key:
            raise ValueError("mode=key 时必须提供 key")
        action_payload["input"] = (
            native_post_window_key(
                title=title,
                key=key,
                presses=1,
                interval_ms=0,
                delivery=background_input_method,
            )
            if background
            else native_send_key(key, presses=1, interval_ms=0)
        )
    elif selected_mode == "click":
        if x is None or y is None:
            raise ValueError("mode=click 时必须提供 x 和 y")
        action_payload["input"] = (
            native_post_window_click(
                title=title,
                x=x,
                y=y,
                button="left",
                clicks=1,
                interval_ms=100,
                delivery=background_input_method,
            )
            if background
            else native_click_screen(x=x, y=y, button="left", clicks=1, interval_ms=100)
        )
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
        fast_region=fast_region,
    )
    payload["action_event"] = action
    if resolved is not None:
        payload["choice_resolution"] = resolved
    payload = _process_capture_text(
        payload,
        image_path,
        session,
        ocr=ocr,
        record_text=record_text,
        language=language,
        include_raw_text=include_raw_text,
        ocr_region=effective_ocr_region,
    )
    if ocr and fast_region is not None and not _fast_capture_has_text(payload):
        full_payload, full_image_path = _capture_for_session(
            window_title=title,
            capture_mode=capture_mode,
            session_id=session["session_id"],
            fast_region=None,
        )
        full_payload["action_event"] = action
        if resolved is not None:
            full_payload["choice_resolution"] = resolved
        full_payload.setdefault(
            "capture_fallback",
            {
                "from": "window_dialogue_region",
                "to": "window_full",
                "reason": "dialogue_region_ocr_empty",
            },
        )
        payload, image_path = full_payload, full_image_path
        payload = _process_capture_text(
            payload,
            image_path,
            session,
            ocr=ocr,
            record_text=record_text,
            language=language,
            include_raw_text=include_raw_text,
            ocr_region=effective_ocr_region,
        )
    payload, image_path = _auto_return_from_settings(
        payload,
        session,
        title=title,
        capture_mode=capture_mode,
        ocr=ocr,
        record_text=record_text,
        language=language,
        include_raw_text=include_raw_text,
        enabled=auto_return_from_settings,
        background=background,
        background_input_method=background_input_method,
        ocr_region=effective_ocr_region,
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
    ocr_region: dict[str, Any] | None = None,
    layout_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """在本地 OCR；ocr_region 只过滤识别结果，不修改原始图片。"""

    try:
        image_width, image_height = _read_png_dimensions(image_path)
    except (OSError, ValueError):
        image_width = image_height = 0
    full_result = native_ocr_image(image_path=image_path, language=language, psm=psm)
    result = _filter_ocr_result_to_region(
        full_result,
        ocr_region,
        width=image_width,
        height=image_height,
    )
    raw_text = str(result.get("text") or "").strip()
    response = _compact_ocr_result(result)
    response["image_path"] = result.get("image_path") or str(Path(image_path).expanduser().resolve())
    profile = layout_profile if layout_profile is not None else _session_layout_profile(session_id)
    parsed: dict[str, Any] | None = None
    evidence: dict[str, Any]
    if raw_text:
        parsed = parse_screen_text(
            raw_text,
            regions=result.get("regions") or [],
            image_size=(image_width, image_height) if image_width and image_height else None,
            layout_profile=profile,
        )
        response["processed_text"] = _public_parsed_text(parsed)
        evidence = build_frame_evidence(
            parsed,
            screen_type=parsed.get("screen_type"),
            ocr_available=bool(result.get("available")),
        )
        if include_raw_text:
            response["raw_text"] = raw_text
    else:
        evidence = build_frame_evidence(
            {},
            ocr_available=bool(result.get("available")),
        )
    response["evidence"] = evidence
    if record and raw_text:
        assert parsed is not None
        observation = STORE.record_observation(
            raw_text=raw_text,
            text=parsed.get("dialogue") or None,
            speaker=parsed.get("speaker"),
            choices=parsed.get("choices") or None,
            screenshot_path=result.get("image_path"),
            source=result.get("backend") or "local_ocr",
            confidence=parsed.get("confidence"),
            noise_flags=parsed.get("noise_flags"),
            evidence=evidence,
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
    """向当前前台窗口发送按键或组合键，例如 ENTER、SPACE、ESC、CTRL+S、DOWN。"""

    result = native_send_key(key=key, presses=presses, interval_ms=interval_ms)
    if record and (session_id is not None or _active_session_exists()):
        STORE.record_action("press_key", result, session_id=session_id)
    return result


@mcp.tool()
def background_press_key(
    window_title: str,
    key: str,
    presses: int = 1,
    interval_ms: int = 80,
    delivery: str = "post",
    record: bool = True,
    session_id: str | None = None,
) -> dict[str, Any]:
    """向指定后台窗口发送按键，不激活窗口、不改变鼠标位置；delivery 可选 post 或 send。"""

    result = native_post_window_key(
        title=window_title,
        key=key,
        presses=presses,
        interval_ms=interval_ms,
        delivery=delivery,
    )
    if record and (session_id is not None or _active_session_exists()):
        STORE.record_action("background_press_key", result, session_id=session_id)
    return result


@mcp.tool()
def hold_key(
    key: str,
    hold_seconds: float = 1.0,
    record: bool = True,
    session_id: str | None = None,
) -> dict[str, Any]:
    """按住按键一段时间；适用于 Ctrl 快进等依赖按键持续状态的游戏。"""

    result = native_hold_key(key=key, hold_seconds=hold_seconds)
    if record and (session_id is not None or _active_session_exists()):
        STORE.record_action("hold_key", result, session_id=session_id)
    return result


@mcp.tool()
def background_click(
    window_title: str,
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
    interval_ms: int = 100,
    delivery: str = "post",
    record: bool = True,
    session_id: str | None = None,
) -> dict[str, Any]:
    """向指定后台窗口发送客户区点击，不移动真实鼠标；delivery 可选 post 或 send。"""

    result = native_post_window_click(
        title=window_title,
        x=x,
        y=y,
        button=button,
        clicks=clicks,
        interval_ms=interval_ms,
        delivery=delivery,
    )
    if record and (session_id is not None or _active_session_exists()):
        STORE.record_action("background_click", result, session_id=session_id)
    return result


@mcp.tool()
def background_scroll(
    window_title: str,
    direction: str = "down",
    x: int | None = None,
    y: int | None = None,
    clicks: int = 1,
    interval_ms: int = 100,
    delivery: str = "post",
    record: bool = True,
    session_id: str | None = None,
) -> dict[str, Any]:
    """向后台窗口发送滚轮消息；默认在窗口中心向下滚一格。"""

    direction_key = (direction or "down").strip().lower()
    deltas = {
        "up": 120,
        "down": -120,
        "上": 120,
        "下": -120,
        "wheelup": 120,
        "wheeldown": -120,
    }
    if direction_key not in deltas:
        raise ValueError("direction 必须是 up、down、上 或 下")
    result = native_post_window_wheel(
        title=window_title,
        x=x,
        y=y,
        delta=deltas[direction_key],
        clicks=clicks,
        interval_ms=interval_ms,
        delivery=delivery,
    )
    if record and (session_id is not None or _active_session_exists()):
        STORE.record_action("background_scroll", result, session_id=session_id)
    return result


@mcp.tool()
def click_screen(
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
    interval_ms: int = 100,
    input_method: str = "mouse",
    record: bool = True,
    session_id: str | None = None,
) -> dict[str, Any]:
    """在屏幕坐标点击游戏界面；可显式选择 mouse 或 touch 输入。"""

    method = (input_method or "mouse").strip().lower()
    if method == "mouse":
        result = native_click_screen(x=x, y=y, button=button, clicks=clicks, interval_ms=interval_ms)
    elif method == "touch":
        if (button or "left").strip().lower() != "left":
            raise ValueError("touch 输入只支持 left 点击")
        result = native_touch_screen(
            x=x,
            y=y,
            taps=clicks,
            hold_ms=80,
            interval_ms=interval_ms,
        )
    else:
        raise ValueError("input_method 必须是 mouse 或 touch")
    result["input_method"] = method
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
def get_compaction_status(
    threshold_bytes: int | None = None,
    keep_recent_events: int | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """检查当前会话是否达到 Codex 分段总结阈值。"""

    return STORE.compaction_status(
        threshold_bytes=threshold_bytes,
        keep_recent_events=keep_recent_events,
        session_id=session_id,
    )


@mcp.tool()
def get_compaction_request(
    threshold_bytes: int | None = None,
    keep_recent_events: int | None = None,
    max_source_chars: int = 120_000,
    session_id: str | None = None,
) -> dict[str, Any]:
    """取得一段待由 Codex 总结的原始结构化事件。"""

    return STORE.get_compaction_request(
        threshold_bytes=threshold_bytes,
        keep_recent_events=keep_recent_events,
        max_source_chars=max_source_chars,
        session_id=session_id,
    )


@mcp.tool()
def save_compaction(
    request_id: str,
    summary: dict[str, Any],
    session_id: str | None = None,
) -> dict[str, Any]:
    """保存 Codex 的剧情压缩总结，并在校验成功后清除对应原始事件。"""

    return STORE.save_compaction(
        request_id=request_id,
        summary=summary,
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

    global STORE
    args = _parse_server_args()
    requested_root = Path(args.data_dir).expanduser().resolve() if args.data_dir else None
    if requested_root is not None and requested_root != STORE.root:
        STORE = SessionStore(root=args.data_dir)
        _BOTTOM_SNAPSHOT_CACHE.clear()
        _WINDOW_FULL_CAPTURE_CACHE.clear()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
