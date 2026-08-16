from __future__ import annotations

import re
from typing import Any


_CHOICE_PATTERNS = [
    re.compile(r"^\s*[①②③④⑤⑥⑦⑧⑨⑩]\s*(?P<label>.+?)\s*$"),
    re.compile(r"^\s*(?:\d{1,2}\s*[.)、:：]|[（(]\s*\d{1,2}\s*[)）])\s*(?P<label>.+?)\s*$"),
    re.compile(r"^\s*[-*•]\s+(?P<label>.+?)\s*$"),
]
_COLON_SPEAKER = re.compile(
    r"^\s*(?P<speaker>[^:：\n]{1,20})\s*[:：]\s+(?P<text>.+?)\s*$"
)
_NOISE = re.compile(r"^\s*(?:[-_=~·•]{3,}|>>+|skip|auto|save|load)\s*$", re.I)
_DIALOGUE_CHAR = r"\u2e80-\u9fff\u3040-\u30ff\ua960-\ua97f，。！？、；：,.!?"
_BETWEEN_DIALOGUE_CHAR_SPACE = re.compile(rf"(?<=[{_DIALOGUE_CHAR}])[ \t]+(?=[{_DIALOGUE_CHAR}])")
_OCR_SYMBOL = re.compile(rf"[{_DIALOGUE_CHAR}A-Za-z0-9]", re.UNICODE)
_UI_WORDS = {
    "auto",
    "back",
    "config",
    "language",
    "load",
    "menu",
    "next",
    "qload",
    "qsave",
    "return",
    "save",
    "skip",
    "system",
    "voice",
}
_STORY_PUNCTUATION = "。！？；：，、,.!?;:"


def _within_edit_distance(left: str, right: str, limit: int = 2) -> bool:
    """Return whether two short OCR tokens are within a tiny edit budget."""

    if abs(len(left) - len(right)) > limit:
        return False
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_char in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_char != right_char),
                )
            )
        if min(current) > limit:
            return False
        previous = current
    return previous[-1] <= limit


def _looks_like_ui_token(token: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(token or "").casefold())
    if not normalized:
        return False
    if normalized in _UI_WORDS:
        return True
    # OCR often turns VOICE into variants such as ``VOlCf`` or ``VO'CE``.
    # Require the same two-character prefix before allowing a fuzzy match, so
    # ordinary short English dialogue is not swallowed by the UI filter.
    for word in _UI_WORDS:
        if (
            len(normalized) >= 4
            and len(normalized) <= len(word) + 1
            and normalized[:2] == word[:2]
        ):
            if _within_edit_distance(normalized, word, limit=2):
                return True
    return False


def _looks_like_ui_residue(
    line: str,
    layout_profile: dict[str, Any] | None = None,
) -> bool:
    """Recognize short control/name residue without naming one game glyph.

    Full-window OCR frequently returns labels such as ``SAVE LOAD`` or a
    short mixed ASCII/digit string from a name/status icon.  They are not
    rejected as story text merely because they are short: configured speaker
    or dialogue markers and explicit choice prefixes always win.
    """

    stripped = str(line or "").strip()
    if not stripped:
        return False
    marker_openers, _ = _marker_tokens(layout_profile)
    if any(stripped.startswith(marker) for marker in marker_openers):
        return False
    if re.match(r"^\s*(?:[-*•]\s+|\d{1,2}\s*[.)、:：])", stripped):
        return False
    compact = _compact_ocr_text(stripped).casefold()
    if not compact:
        return False
    # Keep punctuation as a separator when counting controls.  Compacting
    # first would merge ``SAVE LOAD Q.SAVE`` into ``saveloadq.save`` and hide
    # the individual UI tokens from the classifier.
    words = re.findall(r"[a-z]+", stripped.casefold())
    # Count script characters, not the CJK punctuation block.  OCR residue
    # often contains brackets such as ``《》``; counting those as CJK would
    # hide an otherwise obvious ``SAVE/LOAD`` control cluster.
    cjk_count = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\ua960-\ua97f]", compact))
    ui_hits = sum(1 for word in words if _looks_like_ui_token(word))
    compact_letters = re.sub(r"[^a-z0-9]", "", stripped.casefold())
    if _looks_like_ui_token(compact_letters):
        ui_hits = max(ui_hits, 1)
    if ui_hits >= 2 and cjk_count <= 3:
        return True
    # A single control word inside a short phrase is not enough to classify
    # the whole line as UI.  For example, the perfectly valid English
    # dialogue ``Save me`` contains ``save`` but is not a SAVE button.  A
    # lone control token, or a cluster made entirely from control tokens, is
    # still treated as residue.
    compact_is_ui_token = _looks_like_ui_token(compact_letters)
    if ui_hits >= 1 and len(compact) <= 24 and cjk_count <= 2 and (
        len(words) == 1
        or compact_is_ui_token
        or all(_looks_like_ui_token(word) for word in words)
    ):
        return True
    if any(char in stripped for char in _STORY_PUNCTUATION + "「『【《〈"):
        return False
    # Conservative fallback for mixed short OCR fragments such as ``Levy9``
    # or ``V创0``.  Do not classify a plain short English sentence such as
    # ``yes`` as UI: that is a legitimate line in another visual novel.
    if re.fullmatch(r"\d{2,}", compact):
        return True
    # A dialogue crop can turn a short punctuation line into a digit/symbol
    # cluster such as ``000 00 |`` or ``0 ×``.  With no letters/CJK and no
    # story punctuation, this is safer to classify as UI/OCR residue so the
    # caller can run its full-frame/focused fallback instead of advancing on
    # a false positive.  Marker-prefixed lines already returned above remain
    # eligible for legitimate punctuation-only dialogue.
    if re.search(r"\d", compact) and not re.search(
        r"[A-Za-z\u2e80-\u9fff\u3040-\u30ff\ua960-\ua97f]", compact
    ) and not re.search(rf"[{_STORY_PUNCTUATION}]", compact):
        return True
    mixed_fragment = bool(re.search(r"\d", compact) or cjk_count and words)
    return bool(words and mixed_fragment and len(compact) <= 14 and cjk_count <= 2)


def looks_like_ui_residue(
    line: str,
    layout_profile: dict[str, Any] | None = None,
) -> bool:
    """Public conservative UI-residue check shared by capture and parsing.

    Keeping this small classifier in the text module makes the fast-capture
    path use the same safety decision as the structured parser.  It is still
    intentionally conservative: an ambiguous short fragment is rejected as
    a reason to trust a crop, never deleted from the raw OCR record.
    """

    return _looks_like_ui_residue(line, layout_profile)


def _is_strong_ui_residue(line: str, layout_profile: dict[str, Any] | None = None) -> bool:
    """Return only high-confidence UI residue, ignoring ambiguous fragments."""

    stripped = str(line or "").strip()
    compact = _compact_ocr_text(stripped).casefold()
    words = re.findall(r"[a-z]+", stripped.casefold())
    cjk_count = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\ua960-\ua97f]", compact))
    compact_letters = re.sub(r"[^a-z0-9]", "", stripped.casefold())
    ui_hits = sum(1 for word in words if _looks_like_ui_token(word))
    if _looks_like_ui_token(compact_letters):
        ui_hits = max(ui_hits, 1)
    if ui_hits >= 2 and cjk_count <= 3:
        return True
    if ui_hits >= 1 and (
        len(words) == 1
        or _looks_like_ui_token(compact_letters)
        or all(_looks_like_ui_token(word) for word in words)
    ):
        return True
    return bool(re.fullmatch(r"\d{2,}", compact))


def _marker_specs(profile: dict[str, Any] | None, key: str) -> list[dict[str, Any]]:
    """Return validated-looking marker records without imposing a symbol set.

    Marker characters are deliberately supplied by the active game's layout
    profile. The parser accepts the documented object form and a compact
    two-item list form so profiles remain easy to author by hand.
    """

    if not isinstance(profile, dict):
        return []
    raw_markers = profile.get(key) or []
    if isinstance(raw_markers, dict):
        raw_markers = [raw_markers]
    if not isinstance(raw_markers, (list, tuple)):
        return []
    markers: list[dict[str, Any]] = []
    for item in raw_markers:
        if isinstance(item, dict):
            opener = str(item.get("open") or item.get("opener") or "").strip()
            closer = str(item.get("close") or item.get("closer") or "").strip()
            allow_unclosed = bool(item.get("allow_unclosed", False))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            opener = str(item[0] or "").strip()
            closer = str(item[1] or "").strip()
            allow_unclosed = bool(item[2]) if len(item) >= 3 else False
        else:
            continue
        if not opener:
            continue
        markers.append(
            {
                "open": opener,
                "close": closer,
                "allow_unclosed": allow_unclosed,
            }
        )
    return markers


def _marker_tokens(profile: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    openers: list[str] = []
    closers: list[str] = []
    for key in ("speaker_markers", "dialogue_markers"):
        for marker in _marker_specs(profile, key):
            if marker["open"] not in openers:
                openers.append(marker["open"])
            if marker["close"] and marker["close"] not in closers:
                closers.append(marker["close"])
    return openers, closers


def _clean_speaker_name(value: str) -> str:
    """Remove OCR-inserted inter-character spaces from a name label."""

    cleaned = re.sub(r"\s+", "", value or "").strip()
    # OCR frequently leaves punctuation where a configured opener/closer was
    # dropped. Strip non-word edges generically instead of naming one game's
    # glyphs here.
    cleaned = re.sub(r"^\W+", "", cleaned, flags=re.UNICODE)
    cleaned = re.sub(r"\W+$", "", cleaned, flags=re.UNICODE)
    return cleaned.strip()


def _clean_dialogue_spacing(
    value: str,
    layout_profile: dict[str, Any] | None = None,
) -> str:
    """Remove Windows OCR's inter-character spaces from CJK dialogue."""

    value = _BETWEEN_DIALOGUE_CHAR_SPACE.sub("", value or "")
    openers, closers = _marker_tokens(layout_profile)
    if openers:
        opener_pattern = "|".join(re.escape(token) for token in sorted(openers, key=len, reverse=True))
        value = re.sub(rf"({opener_pattern})[ \t]+", r"\1", value)
    if closers:
        closer_pattern = "|".join(re.escape(token) for token in sorted(closers, key=len, reverse=True))
        value = re.sub(rf"[ \t]+({closer_pattern})", r"\1", value)
    return value.strip()

_SETTINGS_TITLE_MARKERS = (
    "系统设置",
    "系统菜单",
    "設定画面",
    "システム設定",
    "システムメニュー",
    "systemconfig",
    "systemsettings",
    "systemmenu",
    "gamemenu",
)
_SETTINGS_OPTION_MARKERS = (
    "显示模式",
    "画面比例",
    "画面效果",
    "动画效果",
    "esc键功能",
    "功能区域开关",
    "章节标题显示时间",
    "背景音乐曲名显示时间",
    "画面設定",
    "ゲーム設定",
    "テキスト設定",
    "escキー機能",
)


def _normalise_lines(raw_text: str) -> list[str]:
    raw_text = (raw_text or "").replace("\ufeff", "").replace("\u200b", "")
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    result = []
    for line in raw_text.split("\n"):
        cleaned = re.sub(r"[ \t\u3000]+", " ", line).strip()
        if cleaned and not _NOISE.fullmatch(cleaned):
            result.append(cleaned)
    return result


def _ocr_noise_flags(
    raw_text: str,
    layout_profile: dict[str, Any] | None,
    *,
    regions: list[dict[str, Any]] | None = None,
    image_size: tuple[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Annotate likely OCR artifacts without changing the source text.

    These are deliberately hints rather than rejection rules.  The raw OCR
    remains in the observation, while Codex can decide whether a flagged line
    matters for a route decision.
    """

    flags: list[dict[str, Any]] = []
    source_lines = (raw_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    marker_groups = (
        _marker_specs(layout_profile, "speaker_markers"),
        _marker_specs(layout_profile, "dialogue_markers"),
    )
    for line_number, source_line in enumerate(source_lines, start=1):
        line = source_line.strip()
        if not line:
            continue
        code: str | None = None
        severity = "low"
        reason = ""
        ignored = _ocr_blacklist_match(
            line,
            _region_for_line(line, regions),
            image_size=image_size,
            layout_profile=layout_profile,
        )
        if ignored:
            code = "blacklisted_ocr"
            reason = str(ignored.get("reason") or "matched configured OCR ignore rule")
        elif _NOISE.fullmatch(line):
            code = "separator_or_ui"
            reason = "separator or common UI token was removed by conservative line normalization"
        elif "\ufffd" in line or "\x00" in line:
            code = "replacement_character"
            severity = "high"
            reason = "OCR contains a replacement or NUL character"
        elif _looks_like_ui_residue(line, layout_profile):
            code = "ui_residue"
            reason = "short control, status, or name-like OCR residue is not treated as story text"
        elif _BETWEEN_DIALOGUE_CHAR_SPACE.search(line):
            code = "spacing_artifact"
            reason = "spaces occur between adjacent dialogue characters"
        else:
            for markers in marker_groups:
                for marker in markers:
                    opener = str(marker.get("open") or "")
                    closer = str(marker.get("close") or "")
                    if opener and line.startswith(opener) and closer and closer not in line:
                        code = "unclosed_marker"
                        severity = "medium"
                        reason = "configured marker opener was detected without its closing marker"
                        break
                if code:
                    break
        if code is None and len(line) >= 4:
            meaningful = len(_OCR_SYMBOL.findall(line))
            symbol_count = len(re.findall(r"[^\w\s\u2e80-\u9fff\u3040-\u30ff\ua960-\ua97f]", line, re.UNICODE))
            if symbol_count >= 3 and symbol_count > meaningful:
                code = "symbol_heavy"
                severity = "medium"
                reason = "line contains an unusually high proportion of punctuation or OCR symbols"
        if code:
            flags.append(
                {
                    "code": code,
                    "severity": severity,
                    "line": line_number,
                    "text": line[:160],
                    "reason": reason,
                }
            )
        if len(flags) >= 32:
            break
    return flags


def detect_screen_type(raw_text: str) -> str | None:
    """Detect a settings/configuration page without treating its controls as story choices."""

    compact = re.sub(r"\s+", "", (raw_text or "")).casefold()
    if not compact:
        return None
    if any(marker.casefold() in compact for marker in _SETTINGS_TITLE_MARKERS):
        return "settings"
    option_hits = sum(marker.casefold() in compact for marker in _SETTINGS_OPTION_MARKERS)
    return "settings" if option_hits >= 2 else None


def _compact_ocr_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _region_for_line(
    line: str,
    regions: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Find the OCR region corresponding to a normalized text line."""

    compact_line = _compact_ocr_text(line)
    if not compact_line:
        return None
    for region in regions or []:
        if _compact_ocr_text(region.get("text")) == compact_line:
            return region
    return None


def _profile_region_bounds(
    profile: dict[str, Any] | None,
    key: str,
    image_size: tuple[int, int] | None,
) -> tuple[float, float, float, float] | None:
    """Resolve a profile region against the image supplied to the parser.

    The server projects session profiles into pixel coordinates before parsing
    captures. Direct ``parse_text`` callers may still provide normalized or
    pixel regions, so this helper supports both forms as a fallback.
    """

    if not isinstance(profile, dict) or not image_size:
        return None
    region = profile.get(key)
    if not isinstance(region, dict):
        return None
    try:
        width, height = image_size
        x = float(region["x"])
        y = float(region["y"])
        region_width = float(region["width"])
        region_height = float(region["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    coordinate_space = str(region.get("coordinate_space") or "").strip().casefold()
    if coordinate_space in {"normalized", "normalised", "relative", "fraction", "dialogue_region", "dialogue_box", "image"}:
        x *= width
        y *= height
        region_width *= width
        region_height *= height
    elif coordinate_space not in {"pixels", "pixel", "absolute", "image"}:
        return None
    return x, y, region_width, region_height


def _region_center(region: dict[str, Any]) -> tuple[float, float] | None:
    try:
        x = float(region.get("x", 0))
        y = float(region.get("y", 0))
        width = max(0.0, float(region.get("width", 0)))
        height = max(0.0, float(region.get("height", 0)))
    except (TypeError, ValueError):
        return None
    return x + width / 2.0, y + height / 2.0


def _point_in_bounds(point: tuple[float, float], bounds: tuple[float, float, float, float]) -> bool:
    x, y = point
    left, top, width, height = bounds
    return left <= x <= left + width and top <= y <= top + height


def _ocr_blacklist_specs(profile: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return validated-looking per-game OCR blacklist entries."""

    if not isinstance(profile, dict):
        return []
    raw = profile.get("ocr_blacklist") or []
    if isinstance(raw, (str, int, float)):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    specs: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            value = str(item.get("text") or item.get("value") or item.get("pattern") or "").strip()
            match = str(item.get("match") or "exact").strip().casefold()
            region = str(item.get("region") or item.get("region_name") or "").strip() or None
            reason = str(item.get("reason") or "").strip()
        else:
            value = str(item or "").strip()
            match = "exact"
            region = None
            reason = ""
        if value and match in {"exact", "contains", "regex"}:
            specs.append(
                {
                    "text": value,
                    "match": match,
                    "region": region,
                    "reason": reason,
                }
            )
    return specs


def _ocr_ignore_region_match(
    region: dict[str, Any] | None,
    *,
    image_size: tuple[int, int] | None,
    layout_profile: dict[str, Any] | None,
) -> str | None:
    """Return the fixed non-story region containing an OCR box, if any."""

    if not isinstance(region, dict) or not image_size or not isinstance(layout_profile, dict):
        return None
    center = _region_center(region)
    if center is None:
        return None
    raw_regions = layout_profile.get("ocr_ignore_regions") or []
    if isinstance(raw_regions, dict):
        raw_regions = [
            {**value, "name": value.get("name") or name}
            for name, value in raw_regions.items()
            if isinstance(value, dict)
        ]
    if not isinstance(raw_regions, (list, tuple)):
        return None
    for index, item in enumerate(raw_regions, start=1):
        if not isinstance(item, dict):
            continue
        bounds = _profile_region_bounds({"_ignore": item}, "_ignore", image_size)
        if bounds is None or not _point_in_bounds(center, bounds):
            continue
        return str(item.get("name") or item.get("id") or f"region_{index}")
    return None


def _ocr_blacklist_match(
    line: str,
    region: dict[str, Any] | None,
    *,
    image_size: tuple[int, int] | None,
    layout_profile: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Match one OCR line against text or region-scoped noise rules."""

    stripped = str(line or "").strip()
    if not stripped:
        return None
    region_name = _ocr_ignore_region_match(
        region,
        image_size=image_size,
        layout_profile=layout_profile,
    )
    # Region exclusions intentionally ignore all OCR text in that box, but
    # the line is returned as structured metadata and remains in raw_text.
    if region_name:
        return {
            "text": stripped,
            "region": region_name,
            "rule": f"region:{region_name}",
            "reason": "OCR box is inside a configured fixed non-story region",
        }

    compact_line = _compact_ocr_text(stripped).casefold()
    for spec in _ocr_blacklist_specs(layout_profile):
        required_region = spec.get("region")
        if required_region and required_region != region_name:
            continue
        value = str(spec.get("text") or "")
        match = str(spec.get("match") or "exact").casefold()
        compact_value = _compact_ocr_text(value).casefold()
        matched = False
        if match == "exact":
            matched = bool(compact_line and compact_line == compact_value)
        elif match == "contains":
            matched = bool(compact_value and compact_value in compact_line)
        elif match == "regex":
            try:
                matched = bool(re.search(value, stripped, flags=re.IGNORECASE))
            except re.error:
                matched = False
        if matched:
            return {
                "text": stripped,
                "region": region_name,
                "rule": value,
                "match": match,
                "reason": spec.get("reason") or "matched configured OCR blacklist",
            }
    return None


def _unknown_line_in_story_region(
    line: str,
    regions: list[dict[str, Any]] | None,
    image_size: tuple[int, int] | None,
    layout_profile: dict[str, Any] | None,
) -> bool:
    """Fail closed when an unknown OCR box overlaps a story-capable region."""

    if not isinstance(layout_profile, dict):
        return True
    region = _region_for_line(line, regions)
    if region is None or not image_size:
        return True
    center = _region_center(region)
    if center is None:
        return True
    for key in ("choice_region", "dialogue_region", "speaker_region"):
        bounds = _profile_region_bounds(layout_profile, key, image_size)
        if bounds is not None and _point_in_bounds(center, bounds):
            return True
    return False


def _match_marker_line(
    line: str,
    markers: list[dict[str, Any]],
) -> tuple[str, str] | None:
    """Extract a marker-delimited label using only the supplied profile."""

    stripped = line.strip()
    for marker in markers:
        opener = marker["open"]
        if not stripped.startswith(opener):
            continue
        body = stripped[len(opener) :].strip()
        closer = marker.get("close") or ""
        close_index = body.find(closer) if closer else -1
        if close_index < 0:
            if not marker.get("allow_unclosed"):
                continue
            name_text, remainder = body, ""
        else:
            name_text = body[:close_index]
            remainder = body[close_index + len(closer) :].strip()
        speaker = _clean_speaker_name(name_text)
        if not speaker:
            continue
        return speaker, remainder
    return None


def _starts_with_marker(line: str, markers: list[dict[str, Any]]) -> bool:
    stripped = line.strip()
    return any(stripped.startswith(marker["open"]) for marker in markers)


def _choice_line_allowed(
    line: str,
    regions: list[dict[str, Any]] | None,
    image_size: tuple[int, int] | None,
    layout_profile: dict[str, Any] | None,
) -> bool:
    """Apply the configured choice zone to prefixed and unprefixed choices."""

    if not isinstance(layout_profile, dict):
        return True
    choice_bounds = _profile_region_bounds(layout_profile, "choice_region", image_size)
    region = _region_for_line(line, regions)
    if choice_bounds is None:
        # When a game has a configured dialogue box but no explicit choice
        # box, text inside the dialogue box is dialogue by default.  This is
        # important for OCR artifacts such as ``- 吃完晚饭之后...``: the dash
        # can make a normal line look like a bullet-prefixed option.  Full
        # window OCR may still recognize choices outside the dialogue box;
        # a fast dialogue crop must never promote its own text to a choice.
        dialogue_bounds = _profile_region_bounds(layout_profile, "dialogue_region", image_size)
        if dialogue_bounds is not None:
            if region is None:
                return layout_profile.get("_capture_scope") != "window_dialogue_region"
            center = _region_center(region)
            if center is None:
                return False
            return not _point_in_bounds(center, dialogue_bounds)
        return True
    if region is None:
        # Raw-text callers may not have coordinates; preserve explicit numeric
        # choice parsing instead of silently discarding their input.
        return True
    center = _region_center(region)
    return center is not None and _point_in_bounds(center, choice_bounds)


def _dialogue_line_allowed(
    line: str,
    regions: list[dict[str, Any]] | None,
    image_size: tuple[int, int] | None,
    layout_profile: dict[str, Any] | None,
) -> bool:
    """Require ordinary OCR text to come from the configured dialogue box.

    The parser is intentionally permissive for direct/raw-text callers that
    have no geometry.  Once OCR regions and a game layout are available,
    text outside every known story box is kept as an unclassified candidate;
    it must not silently become dialogue.
    """

    if not isinstance(layout_profile, dict):
        return True
    dialogue_bounds = _profile_region_bounds(layout_profile, "dialogue_region", image_size)
    if dialogue_bounds is None:
        # A profile that declares only a choice zone still says that text
        # outside that zone is not safely classifiable as dialogue when OCR
        # geometry is available.  Raw-text callers without regions retain the
        # historical permissive behavior.
        choice_bounds = _profile_region_bounds(layout_profile, "choice_region", image_size)
        if choice_bounds is not None and regions:
            return False
        return True
    region = _region_for_line(line, regions)
    if region is None:
        # A dialogue-only crop is already the configured dialogue box.  For a
        # full frame, missing geometry is ambiguous and should fail closed.
        return layout_profile.get("_capture_scope") == "window_dialogue_region"
    center = _region_center(region)
    return center is not None and _point_in_bounds(center, dialogue_bounds)


def _choice_min_count(layout_profile: dict[str, Any] | None) -> int:
    """Return the configured minimum number of rows needed for a choice."""

    try:
        return max(1, min(int((layout_profile or {}).get("choice_min_count", 2)), 10))
    except (TypeError, ValueError):
        return 2


def _layout_name_label(
    line: str,
    line_index: int,
    regions: list[dict[str, Any]] | None,
    image_size: tuple[int, int] | None,
    layout_profile: dict[str, Any] | None,
) -> tuple[str, str] | None:
    """Parse a fixed-layout name box using the active game's marker profile."""

    markers = _marker_specs(layout_profile, "speaker_markers")
    if not markers:
        return None
    matched = _match_marker_line(line, markers)
    if matched is None:
        return None
    speaker_region = _region_for_line(line, regions)
    speaker_bounds = _profile_region_bounds(layout_profile, "speaker_region", image_size)
    if speaker_bounds is not None and speaker_region is not None:
        center = _region_center(speaker_region)
        if center is None or not _point_in_bounds(center, speaker_bounds):
            return None
    elif speaker_bounds is not None and not regions and line_index != 0:
        return None
    speaker, remainder = matched
    try:
        max_chars = max(1, min(int((layout_profile or {}).get("speaker_max_chars", 40)), 200))
    except (TypeError, ValueError):
        max_chars = 40
    if len(speaker) > max_chars:
        return None
    return speaker, remainder


def _layout_choice_keys(
    regions: list[dict[str, Any]] | None,
    image_size: tuple[int, int] | None,
    layout_profile: dict[str, Any] | None,
) -> set[str]:
    """Find short option rows inside the configured choice region."""

    if not regions or not image_size or not isinstance(layout_profile, dict):
        return set()
    image_width, image_height = image_size
    if image_width <= 0 or image_height <= 0:
        return set()
    if (
        layout_profile.get("_capture_scope") == "window_dialogue_region"
        and not bool(layout_profile.get("choice_detection_on_crops", False))
    ):
        return set()
    choice_bounds = _profile_region_bounds(layout_profile, "choice_region", image_size)
    if choice_bounds is None:
        return set()
    minimum_count = _choice_min_count(layout_profile)
    choice_layout = str(layout_profile.get("choice_layout") or "vertical").strip().casefold()
    if choice_layout not in {"vertical", "horizontal", "both"}:
        choice_layout = "vertical"
    excluded_text = [
        _compact_ocr_text(item).casefold()
        for item in (layout_profile.get("choice_exclude_text") or [])
        if _compact_ocr_text(item)
    ]
    candidates: list[dict[str, Any]] = []
    for region in regions:
        text = _clean_dialogue_spacing(str(region.get("text") or ""), layout_profile)
        compact = _compact_ocr_text(text)
        if not compact or len(compact) > 40:
            continue
        lowered = compact.casefold()
        if any(marker in lowered for marker in excluded_text):
            continue
        try:
            x = float(region.get("x", 0))
            y = float(region.get("y", 0))
            width = max(0.0, float(region.get("width", 0)))
            height = max(0.0, float(region.get("height", 0)))
        except (TypeError, ValueError):
            continue
        center_x = x + width / 2
        center_y = y + height / 2
        if not _point_in_bounds((center_x, center_y), choice_bounds):
            continue
        try:
            minimum_height_ratio = max(0.0, min(float(layout_profile.get("choice_min_height_ratio", 0.015)), 1.0))
        except (TypeError, ValueError):
            minimum_height_ratio = 0.015
        if height < max(12.0, image_height * minimum_height_ratio):
            continue
        candidates.append(
            {
                "key": compact,
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "center_x": center_x,
                "center_y": center_y,
            }
        )
    if len(candidates) < minimum_count:
        return set()

    # A game profile may explicitly declare that a single centered row is a
    # valid prompt.  The default remains two rows, so this branch is opt-in
    # and cannot turn an isolated OCR line into a choice by accident.
    if minimum_count == 1 and len(candidates) == 1:
        return {str(candidates[0]["key"])}

    selected: list[dict[str, Any]] = []
    for index, first in enumerate(candidates):
        for second in candidates[index + 1 :]:
            vertical_pair = (
                abs(first["center_x"] - second["center_x"]) <= image_width * 0.18
                and abs(first["center_y"] - second["center_y"]) >= image_height * 0.045
                and abs(first["center_y"] - second["center_y"]) <= image_height * 0.28
            )
            horizontal_pair = (
                abs(first["center_y"] - second["center_y"]) <= image_height * 0.08
                and abs(first["center_x"] - second["center_x"]) >= image_width * 0.06
            )
            if (choice_layout in {"vertical", "both"} and vertical_pair) or (
                choice_layout in {"horizontal", "both"} and horizontal_pair
            ):
                selected.extend((first, second))
    if len({str(item["key"]) for item in selected}) < minimum_count:
        return set()
    return {str(item["key"]) for item in selected}


def parse_screen_text(
    raw_text: str,
    *,
    regions: list[dict[str, Any]] | None = None,
    image_size: tuple[int, int] | None = None,
    layout_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Heuristically split OCR/clipboard text into speaker, dialogue and choices.

    Marker-delimited speaker/dialogue lines and unnumbered visual choices are
    interpreted only when the active game's ``layout_profile`` supplies those
    markers/regions. Without a profile the parser remains conservative and
    still handles numeric choices and ``name: dialogue`` text.
    """

    raw_lines = _normalise_lines(raw_text)
    ignored_lines: list[dict[str, Any]] = []
    lines: list[str] = []
    for line in raw_lines:
        ignored = _ocr_blacklist_match(
            line,
            _region_for_line(line, regions),
            image_size=image_size,
            layout_profile=layout_profile if isinstance(layout_profile, dict) else {},
        )
        if ignored:
            ignored_lines.append(ignored)
            continue
        lines.append(line)
    choice_records: list[dict[str, Any]] = []
    dialogue_lines: list[str] = []
    unparsed_lines: list[str] = []
    ui_lines: list[str] = []
    unknown_lines: list[str] = []
    speaker: str | None = None
    explicit_speaker = False
    profile = layout_profile if isinstance(layout_profile, dict) else {}
    noise_flags = _ocr_noise_flags(
        raw_text,
        profile,
        regions=regions,
        image_size=image_size,
    )
    layout_choice_keys = _layout_choice_keys(regions, image_size, profile)
    speaker_markers = _marker_specs(profile, "speaker_markers")
    dialogue_markers = _marker_specs(profile, "dialogue_markers")

    for line_number, line in enumerate(lines, start=1):
        choice_match = None
        for pattern in _CHOICE_PATTERNS:
            choice_match = pattern.match(line)
            if choice_match:
                break
        if choice_match:
            if not _choice_line_allowed(line, regions, image_size, profile):
                if _dialogue_line_allowed(line, regions, image_size, profile):
                    dialogue_lines.append(
                        _clean_dialogue_spacing(choice_match.group("label").strip(), profile)
                    )
                else:
                    unparsed_lines.append(line)
                    unknown_lines.append(line)
                continue
            choice_records.append(
                {
                    "option_id": str(len(choice_records) + 1),
                    "label": choice_match.group("label").strip(),
                    "line": line_number,
                    "raw": line,
                }
            )
            continue
        if _compact_ocr_text(line) in layout_choice_keys:
            choice_records.append(
                {
                    "option_id": str(len(choice_records) + 1),
                    "label": _clean_dialogue_spacing(line, profile),
                    "line": line_number,
                    "raw": line,
                }
            )
            continue

        if _looks_like_ui_residue(line, profile):
            dialogue_allowed = _dialogue_line_allowed(line, regions, image_size, profile)
            has_geometry = any(
                isinstance(profile.get(name), dict)
                for name in ("dialogue_region", "speaker_region", "choice_region")
            )
            if has_geometry and regions and not dialogue_allowed and not _is_strong_ui_residue(line, profile):
                unparsed_lines.append(line)
                unknown_lines.append(line)
            else:
                ui_lines.append(line)
            continue

        if speaker is None:
            layout_name = _layout_name_label(
                line,
                line_number - 1,
                regions,
                image_size,
                profile,
            )
            if layout_name is not None:
                speaker, remainder = layout_name
                explicit_speaker = True
                if remainder:
                    if _looks_like_ui_residue(remainder, profile):
                        ui_lines.append(remainder)
                    elif _dialogue_line_allowed(
                        remainder,
                        regions,
                        image_size,
                        profile,
                    ):
                        dialogue_lines.append(remainder)
                    else:
                        unparsed_lines.append(remainder)
                        unknown_lines.append(remainder)
                continue
            # A configured dialogue opener has priority over an ambiguous
            # speaker marker. If a game uses the same pair for both roles, it
            # should list that pair only in the role it wants recognized.
            if _starts_with_marker(line, dialogue_markers):
                if _dialogue_line_allowed(line, regions, image_size, profile):
                    dialogue_lines.append(line)
                else:
                    unparsed_lines.append(line)
                    unknown_lines.append(line)
                continue
            colon_match = _COLON_SPEAKER.match(line)
            if colon_match:
                candidate = colon_match.group("speaker").strip()
                candidate_is_residue = _looks_like_ui_residue(candidate, profile)
                if (
                    len(candidate) <= 20
                    and not any(char in candidate for char in "，。！？,.!?")
                    and not candidate_is_residue
                    and not speaker_markers
                ):
                    speaker = candidate
                    explicit_speaker = True
                    remainder = colon_match.group("text").strip()
                    if _dialogue_line_allowed(remainder, regions, image_size, profile):
                        dialogue_lines.append(remainder)
                    else:
                        unparsed_lines.append(line)
                        unknown_lines.append(line)
                    continue
                if speaker_markers:
                    # Once a game has a configured name-box profile, an
                    # unmarked ``name: text`` line is safer as unresolved OCR
                    # than as a guessed speaker.  This prevents logos or
                    # chapter labels from becoming structured dialogue.
                    unparsed_lines.append(line)
                    unknown_lines.append(line)
                    continue

        if _starts_with_marker(line, speaker_markers) and not explicit_speaker:
            unparsed_lines.append(line)
            unknown_lines.append(line)
        elif not _dialogue_line_allowed(line, regions, image_size, profile):
            unparsed_lines.append(line)
            unknown_lines.append(line)
        else:
            dialogue_lines.append(line)

    # A single bullet/prefix match is not enough evidence for a choice in a
    # normal profile.  OCR frequently turns the opening quote of a dialogue
    # line into ``-`` or another bullet.  Demote that candidate back into the
    # spatial role it actually occupies; if it is outside every known story
    # region it remains unknown and blocks autoplay for visual review.
    minimum_choice_count = _choice_min_count(profile)
    if choice_records and len(choice_records) < minimum_choice_count:
        for record in choice_records:
            raw_candidate = str(record.get("raw") or "").strip()
            candidate_line = str(record.get("label") or raw_candidate).strip()
            if not candidate_line:
                continue
            if _dialogue_line_allowed(raw_candidate or candidate_line, regions, image_size, profile):
                dialogue_lines.append(_clean_dialogue_spacing(candidate_line, profile))
            else:
                unparsed_lines.append(raw_candidate)
                unknown_lines.append(raw_candidate)
        choice_records = []

    dialogue = _clean_dialogue_spacing("\n".join(dialogue_lines), profile)
    choices = [record["label"] for record in choice_records]
    if explicit_speaker and choices:
        confidence = 0.93
    elif explicit_speaker:
        confidence = 0.86
    elif choices and dialogue:
        confidence = 0.78
    elif choices:
        confidence = 0.72
    elif dialogue:
        confidence = 0.58
    else:
        confidence = 0.0
    screen_type = detect_screen_type(raw_text)
    if choices:
        text_status = "choice"
    elif dialogue:
        text_status = "recognized"
    elif unknown_lines:
        text_status = "unknown"
    elif ui_lines:
        text_status = "ui_only"
    else:
        text_status = "empty"
    unknown_story_lines = [
        line
        for line in unknown_lines
        if _unknown_line_in_story_region(line, regions, image_size, profile)
    ]
    return {
        "raw_text": raw_text,
        "clean_text": "\n".join(lines),
        "speaker": speaker,
        "dialogue": dialogue,
        "choices": choices,
        "choice_records": choice_records,
        "unparsed_lines": unparsed_lines,
        "ui_lines": ui_lines,
        "unknown_lines": unknown_lines,
        "unknown_story_lines": unknown_story_lines,
        "ignored_lines": ignored_lines,
        "line_count": len(lines),
        "confidence": confidence,
        "screen_type": screen_type,
        "text_status": text_status,
        "noise_flags": noise_flags,
    }
