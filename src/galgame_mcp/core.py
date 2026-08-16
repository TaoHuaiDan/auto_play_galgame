from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


SCHEMA_VERSION = "1.0"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class SessionError(RuntimeError):
    """Raised when an MCP operation cannot be applied to a story session."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalise_noise_flags(flags: Any) -> list[dict[str, Any]]:
    """Keep bounded, JSON-safe OCR hints alongside the raw observation."""

    if flags is None:
        return []
    if not isinstance(flags, (list, tuple)):
        raise SessionError("noise_flags 必须是数组")
    normalised: list[dict[str, Any]] = []
    for item in flags[:32]:
        if not isinstance(item, dict):
            continue
        code = _clean_text(item.get("code"))
        if not code:
            continue
        try:
            line = int(item.get("line", 0))
        except (TypeError, ValueError):
            line = 0
        normalised.append(
            {
                "code": code[:64],
                "severity": (_clean_text(item.get("severity")) or "low")[:16],
                "line": max(0, line),
                "text": (_clean_text(item.get("text")) or "")[:160],
                "reason": (_clean_text(item.get("reason")) or "")[:240],
            }
        )
    return normalised


def _normalise_layout_profile(profile: Any) -> dict[str, Any]:
    """Validate the JSON-friendly per-game OCR/layout configuration."""

    if not isinstance(profile, dict):
        raise SessionError("layout_profile 必须是 JSON 对象；传空对象可清除配置")
    try:
        copied = copy.deepcopy(profile)
        encoded = json.dumps(copied, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise SessionError("layout_profile 必须只包含 JSON 可序列化值") from exc
    if len(encoded) > 100_000:
        raise SessionError("layout_profile 过大，不能超过 100000 个字符")

    region_names = {"dialogue_region", "speaker_region", "choice_region"}
    for name in region_names:
        region = copied.get(name)
        if region is None:
            continue
        if not isinstance(region, dict):
            raise SessionError(f"layout_profile.{name} 必须是包含 x、y、width、height 的对象")
        try:
            values = [float(region[key]) for key in ("x", "y", "width", "height")]
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionError(f"layout_profile.{name} 必须包含数字 x、y、width、height") from exc
        if not all(math.isfinite(value) for value in values):
            raise SessionError(f"layout_profile.{name} 不能包含 NaN 或无穷大")
        default_space = "dialogue_region" if name == "speaker_region" else "normalized"
        coordinate_space = str(region.get("coordinate_space") or default_space).strip().casefold()
        allowed_spaces = {"normalized", "normalised", "relative", "fraction", "pixels", "pixel", "absolute"}
        if name == "speaker_region":
            allowed_spaces |= {"dialogue_region", "dialogue_box", "image"}
        if coordinate_space not in allowed_spaces:
            raise SessionError(
                f"layout_profile.{name}.coordinate_space 不受支持: {coordinate_space}"
            )
        if coordinate_space in {"normalized", "normalised", "relative", "fraction", "dialogue_region", "dialogue_box"}:
            if any(value < 0 or value > 1 for value in values):
                raise SessionError(f"layout_profile.{name} 的 normalized 坐标和尺寸必须在 0 到 1 之间")
        elif any(value < 0 for value in values):
            raise SessionError(f"layout_profile.{name} 的 pixels 坐标和尺寸不能为负数")
        region["coordinate_space"] = coordinate_space

    # Full-window fallback OCR also sees title bars, logos, chapter banners,
    # and fixed footer controls.  Keep these exclusions in the per-game
    # profile so another game can supply its own coordinates without adding a
    # title-specific branch to the parser.
    ignore_regions = copied.get("ocr_ignore_regions")
    if ignore_regions is not None:
        if isinstance(ignore_regions, dict):
            converted_regions: list[dict[str, Any]] = []
            for region_name, region in ignore_regions.items():
                if not isinstance(region, dict):
                    raise SessionError("layout_profile.ocr_ignore_regions 的每项必须是区域对象")
                converted = copy.deepcopy(region)
                converted.setdefault("name", region_name)
                converted_regions.append(converted)
            ignore_regions = converted_regions
            copied["ocr_ignore_regions"] = ignore_regions
        if not isinstance(ignore_regions, list) or len(ignore_regions) > 64:
            raise SessionError("layout_profile.ocr_ignore_regions 必须是最多 64 项的数组")
        normalised_ignore_regions: list[dict[str, Any]] = []
        for index, region in enumerate(ignore_regions, start=1):
            if not isinstance(region, dict):
                raise SessionError("layout_profile.ocr_ignore_regions 的每项必须是区域对象")
            name = _clean_text(region.get("name") or region.get("id")) or f"region_{index}"
            if len(name) > 64:
                raise SessionError("layout_profile.ocr_ignore_regions.name 不能超过 64 个字符")
            try:
                values = [float(region[key]) for key in ("x", "y", "width", "height")]
            except (KeyError, TypeError, ValueError) as exc:
                raise SessionError(
                    "layout_profile.ocr_ignore_regions 必须包含数字 x、y、width、height"
                ) from exc
            if not all(math.isfinite(value) for value in values):
                raise SessionError("layout_profile.ocr_ignore_regions 不能包含 NaN 或无穷大")
            coordinate_space = str(region.get("coordinate_space") or "normalized").strip().casefold()
            allowed_spaces = {
                "normalized",
                "normalised",
                "relative",
                "fraction",
                "pixels",
                "pixel",
                "absolute",
                "image",
            }
            if coordinate_space not in allowed_spaces:
                raise SessionError(
                    "layout_profile.ocr_ignore_regions.coordinate_space 不受支持: "
                    f"{coordinate_space}"
                )
            if coordinate_space in {"normalized", "normalised", "relative", "fraction", "image"}:
                if any(value < 0 or value > 1 for value in values):
                    raise SessionError(
                        "layout_profile.ocr_ignore_regions 的 normalized 坐标和尺寸必须在 0 到 1 之间"
                    )
            elif any(value < 0 for value in values):
                raise SessionError("layout_profile.ocr_ignore_regions 的 pixels 坐标和尺寸不能为负数")
            normalised_ignore_regions.append(
                {
                    "name": name,
                    "x": values[0],
                    "y": values[1],
                    "width": values[2],
                    "height": values[3],
                    "coordinate_space": coordinate_space,
                }
            )
        copied["ocr_ignore_regions"] = normalised_ignore_regions

    blacklist = copied.get("ocr_blacklist")
    if blacklist is not None:
        if isinstance(blacklist, (str, int, float)):
            blacklist = [blacklist]
            copied["ocr_blacklist"] = blacklist
        if not isinstance(blacklist, list) or len(blacklist) > 128:
            raise SessionError("layout_profile.ocr_blacklist 必须是最多 128 项的数组")
        normalised_blacklist: list[dict[str, Any]] = []
        for item in blacklist:
            if isinstance(item, dict):
                value = _clean_text(item.get("text") or item.get("value") or item.get("pattern"))
                match = str(item.get("match") or "exact").strip().casefold()
                region_name = _clean_text(item.get("region") or item.get("region_name"))
                reason = _clean_text(item.get("reason"))
            else:
                value = _clean_text(item)
                match = "exact"
                region_name = None
                reason = None
            if not value:
                raise SessionError("layout_profile.ocr_blacklist 的每项必须包含非空 text")
            if match not in {"exact", "contains", "regex"}:
                raise SessionError("layout_profile.ocr_blacklist.match 必须是 exact、contains 或 regex")
            if len(value) > 256:
                raise SessionError("layout_profile.ocr_blacklist.text 不能超过 256 个字符")
            if region_name and len(region_name) > 64:
                raise SessionError("layout_profile.ocr_blacklist.region 不能超过 64 个字符")
            if match == "regex":
                try:
                    re.compile(value)
                except re.error as exc:
                    raise SessionError("layout_profile.ocr_blacklist 的 regex 无效") from exc
            normalised_blacklist.append(
                {
                    "text": value,
                    "match": match,
                    **({"region": region_name} if region_name else {}),
                    **({"reason": reason[:160]} if reason else {}),
                }
            )
        copied["ocr_blacklist"] = normalised_blacklist

    for key in ("speaker_markers", "dialogue_markers"):
        markers = copied.get(key)
        if markers is None:
            continue
        if isinstance(markers, dict):
            markers = [markers]
            copied[key] = markers
        if not isinstance(markers, list) or len(markers) > 64:
            raise SessionError(f"layout_profile.{key} 必须是最多 64 项的数组")
        normalised_markers: list[dict[str, Any]] = []
        for marker in markers:
            if isinstance(marker, (list, tuple)) and len(marker) >= 2:
                marker = {
                    "open": marker[0],
                    "close": marker[1],
                    "allow_unclosed": bool(marker[2]) if len(marker) >= 3 else False,
                }
            if not isinstance(marker, dict):
                raise SessionError(f"layout_profile.{key} 的每项必须是对象或二元数组")
            opener = _clean_text(marker.get("open") or marker.get("opener"))
            closer = _clean_text(marker.get("close") or marker.get("closer")) or ""
            if not opener:
                raise SessionError(f"layout_profile.{key} 的 marker.open 不能为空")
            if len(opener) > 16 or len(closer) > 16:
                raise SessionError(f"layout_profile.{key} 的 marker 符号长度不能超过 16")
            normalised_markers.append(
                {
                    "open": opener,
                    "close": closer,
                    "allow_unclosed": bool(marker.get("allow_unclosed", False)),
                }
            )
        copied[key] = normalised_markers

    for key in ("choice_min_count", "speaker_max_chars"):
        if key in copied:
            try:
                number = int(copied[key])
            except (TypeError, ValueError) as exc:
                raise SessionError(f"layout_profile.{key} 必须是整数") from exc
            # A profile may explicitly allow a single-option prompt, but the
            # default remains two rows so one OCR bullet cannot become a
            # choice by accident.
            limits = {"choice_min_count": (1, 10), "speaker_max_chars": (1, 200)}
            minimum, maximum = limits[key]
            if not minimum <= number <= maximum:
                raise SessionError(f"layout_profile.{key} 必须在 {minimum} 到 {maximum} 之间")
            copied[key] = number
    for key in ("choice_min_height_ratio",):
        if key in copied:
            try:
                number = float(copied[key])
            except (TypeError, ValueError) as exc:
                raise SessionError(f"layout_profile.{key} 必须是数字") from exc
            if not math.isfinite(number) or not 0 <= number <= 1:
                raise SessionError(f"layout_profile.{key} 必须在 0 到 1 之间")
            copied[key] = number
    for key in ("choice_detection_on_crops",):
        if key in copied and not isinstance(copied[key], bool):
            raise SessionError(f"layout_profile.{key} 必须是布尔值")
    if "choice_layout" in copied:
        choice_layout = str(copied["choice_layout"]).strip().casefold()
        if choice_layout not in {"vertical", "horizontal", "both"}:
            raise SessionError("layout_profile.choice_layout 必须是 vertical、horizontal 或 both")
        copied["choice_layout"] = choice_layout
    return copied


def _normalise_action_profile(profile: Any) -> dict[str, dict[str, Any]]:
    """Validate JSON-friendly named game actions without hard-coding a title."""

    if not isinstance(profile, dict):
        raise SessionError("action_profile 必须是 JSON 对象；传空对象可清除配置")
    try:
        copied = copy.deepcopy(profile)
        encoded = json.dumps(copied, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise SessionError("action_profile 必须只包含 JSON 可序列化值") from exc
    if len(encoded) > 100_000:
        raise SessionError("action_profile 过大，不能超过 100000 个字符")
    if len(copied) > 64:
        raise SessionError("action_profile 最多支持 64 个命名动作")

    allowed_kinds = {"click", "key", "scroll", "hold", "wait", "focus"}
    normalised: dict[str, dict[str, Any]] = {}
    for name, value in copied.items():
        action_name = _clean_text(name)
        if not action_name or len(action_name) > 64:
            raise SessionError("action_profile 的动作名称必须是 1-64 个字符")
        if isinstance(value, str):
            value = {"kind": value}
        if not isinstance(value, dict):
            raise SessionError(f"action_profile.{action_name} 必须是对象或动作类型字符串")
        spec = copy.deepcopy(value)
        kind = _clean_text(spec.get("kind") or spec.get("type"))
        if not kind or kind.casefold() not in allowed_kinds:
            raise SessionError(
                f"action_profile.{action_name}.kind 必须是 click、key、scroll、hold、wait 或 focus"
            )
        spec["kind"] = kind.casefold()
        if "delivery" in spec:
            delivery = _clean_text(spec["delivery"]) or "send"
            if delivery.casefold() not in {"post", "send"}:
                raise SessionError(f"action_profile.{action_name}.delivery 必须是 post 或 send")
            spec["delivery"] = delivery.casefold()
        if "button" in spec:
            button = _clean_text(spec["button"]) or "left"
            if button.casefold() not in {"left", "right", "middle"}:
                raise SessionError(f"action_profile.{action_name}.button 必须是 left、right 或 middle")
            spec["button"] = button.casefold()
        if "target" in spec:
            target = _clean_text(spec["target"])
            if target is None:
                raise SessionError(f"action_profile.{action_name}.target 不能为空")
            spec["target"] = target.casefold()
        normalised[action_name] = spec
    return normalised


def _normalise_timing_profile(profile: Any) -> dict[str, Any]:
    """Validate per-game post-input settling and typewriter timing."""

    if not isinstance(profile, dict):
        raise SessionError("timing_profile 必须是 JSON 对象；传空对象可清除配置")
    try:
        copied = copy.deepcopy(profile)
        encoded = json.dumps(copied, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise SessionError("timing_profile 必须只包含 JSON 可序列化值") from exc
    if len(encoded) > 20_000:
        raise SessionError("timing_profile 过大，不能超过 20000 个字符")
    if not copied:
        return {}

    strategy = str(copied.get("strategy") or "fixed").strip().casefold()
    aliases = {"fixed", "text_hash", "hash", "hash_stable", "adaptive"}
    if strategy not in aliases:
        raise SessionError("timing_profile.strategy 必须是 fixed 或 text_hash")
    copied["strategy"] = "text_hash" if strategy in {"text_hash", "hash", "hash_stable", "adaptive"} else "fixed"

    float_limits = {
        "post_click_wait_seconds": (0.0, 10.0),
        "transition_wait_seconds": (0.0, 10.0),
        "transition_accelerate_delay_seconds": (0.1, 3.0),
        "transition_probe_interval_seconds": (0.05, 2.0),
        "settle_timeout_seconds": (0.0, 30.0),
        "settle_poll_seconds": (0.02, 2.0),
    }
    for key, (minimum, maximum) in float_limits.items():
        if key not in copied:
            continue
        try:
            value = float(copied[key])
        except (TypeError, ValueError) as exc:
            raise SessionError(f"timing_profile.{key} 必须是数字") from exc
        if not math.isfinite(value):
            raise SessionError(f"timing_profile.{key} 不能是 NaN 或无穷大")
        copied[key] = max(minimum, min(value, maximum))

    if "stable_samples" in copied:
        try:
            samples = int(copied["stable_samples"])
        except (TypeError, ValueError) as exc:
            raise SessionError("timing_profile.stable_samples 必须是整数") from exc
        if not 1 <= samples <= 10:
            raise SessionError("timing_profile.stable_samples 必须在 1 到 10 之间")
        copied["stable_samples"] = samples
    if "require_text_change" in copied and not isinstance(copied["require_text_change"], bool):
        raise SessionError("timing_profile.require_text_change 必须是布尔值")
    if "transition_accelerate" in copied and not isinstance(copied["transition_accelerate"], bool):
        raise SessionError("timing_profile.transition_accelerate 必须是布尔值")
    return copied


def _normalise_options(options: Iterable[Any] | None) -> list[dict[str, Any]]:
    """Convert simple labels or richer option objects into stable option records."""

    normalised: list[dict[str, Any]] = []
    for index, item in enumerate(options or [], start=1):
        if isinstance(item, dict):
            label = _clean_text(item.get("label") or item.get("text") or item.get("name"))
            option_id = _clean_text(item.get("option_id") or item.get("id")) or str(index)
            enabled = bool(item.get("enabled", True))
            extra = {
                key: value
                for key, value in item.items()
                if key not in {"label", "text", "name", "option_id", "id", "enabled"}
            }
        else:
            label = _clean_text(item)
            option_id = str(index)
            enabled = True
            extra = {}
        if not label:
            continue
        option = {"option_id": option_id, "label": label, "enabled": enabled}
        option.update(extra)
        normalised.append(option)
    return normalised


def _coerce_story_value(value: str, value_type: str) -> Any:
    value_type = (value_type or "string").strip().lower()
    if value_type in {"bool", "boolean"}:
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on", "是", "真"}:
            return True
        if lowered in {"false", "0", "no", "off", "否", "假"}:
            return False
        raise SessionError(f"无法把 {value!r} 解析为 boolean")
    if value_type in {"int", "integer"}:
        try:
            return int(value.strip())
        except ValueError as exc:
            raise SessionError(f"无法把 {value!r} 解析为 integer") from exc
    if value_type in {"float", "number"}:
        try:
            return float(value.strip())
        except ValueError as exc:
            raise SessionError(f"无法把 {value!r} 解析为 number") from exc
    if value_type in {"json", "object", "array"}:
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise SessionError(f"无法把 value 解析为 JSON: {exc.msg}") from exc
    if value_type in {"null", "none"}:
        return None
    return value


class SessionStore:
    """Small, crash-tolerant event store for visual-novel sessions.

    Each session is a directory containing a compact ``session.json`` checkpoint
    and an append-only ``events.jsonl`` journal.  Older sessions that still keep
    their complete timeline inline are migrated on the next write.  Keeping the
    checkpoint small is important here: autoplay can create several events per
    screen, and rewriting an ever-growing JSON document made long runs slower
    until they eventually timed out.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        compaction_threshold_bytes: int | None = None,
        compaction_keep_recent_events: int | None = None,
    ):
        if root is not None:
            configured_root = root
            self.root_source = "argument"
        else:
            configured_root = os.environ.get("GALGAME_MCP_DATA_DIR")
            self.root_source = "environment" if configured_root else "cwd_default"
        self.root = Path(configured_root or (Path.cwd() / ".galgame_sessions")).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._active_file = self.root / "active_session.txt"
        self._lock = threading.RLock()
        # A play_until_choice batch can touch the same session hundreds of
        # times.  Keep the hydrated timeline in memory and only re-read files
        # when another process actually changes the checkpoint or journal.
        self._session_cache: dict[str, dict[str, Any]] = {}
        self._session_cache_signatures: dict[str, tuple[Any, Any]] = {}
        self.compaction_threshold_bytes = self._bounded_compaction_threshold(
            compaction_threshold_bytes
            if compaction_threshold_bytes is not None
            else os.environ.get("GALGAME_MCP_COMPACTION_THRESHOLD_BYTES", 256_000)
        )
        self.compaction_keep_recent_events = self._bounded_compaction_keep_recent_events(
            compaction_keep_recent_events
            if compaction_keep_recent_events is not None
            else os.environ.get("GALGAME_MCP_COMPACTION_KEEP_RECENT_EVENTS", 24)
        )

    def storage_info(self) -> dict[str, Any]:
        """Return the resolved data directory and how it was selected."""

        return {
            "data_dir": str(self.root),
            "source": self.root_source,
            "default_data_dir": str((Path.cwd() / ".galgame_sessions").resolve()),
            "active_session_pointer": str(self._active_file),
        }

    @staticmethod
    def _bounded_compaction_threshold(value: Any) -> int:
        try:
            threshold = int(value)
        except (TypeError, ValueError) as exc:
            raise SessionError("compaction_threshold_bytes 必须是整数") from exc
        return max(16_384, min(threshold, 50_000_000))

    @staticmethod
    def _bounded_compaction_keep_recent_events(value: Any) -> int:
        try:
            keep = int(value)
        except (TypeError, ValueError) as exc:
            raise SessionError("compaction_keep_recent_events 必须是整数") from exc
        return max(4, min(keep, 500))

    # ---------- paths and persistence ----------

    @staticmethod
    def validate_session_id(session_id: str) -> str:
        session_id = str(session_id).strip()
        if not _SESSION_ID_RE.fullmatch(session_id):
            raise SessionError(
                "session_id 只能包含字母、数字、下划线和连字符，长度 1-64"
            )
        return session_id

    def session_dir(self, session_id: str) -> Path:
        return self.root / self.validate_session_id(session_id)

    def session_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "session.json"

    def event_journal_path(self, session_id: str) -> Path:
        """Return the per-session append-only event journal path."""

        return self.session_dir(session_id) / "events.jsonl"

    def _active_id_locked(self) -> str | None:
        try:
            value = self._active_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return value or None

    def _set_active_locked(self, session_id: str | None) -> None:
        if session_id is None:
            self._active_file.unlink(missing_ok=True)
            return
        self._active_file.write_text(session_id, encoding="utf-8")

    @staticmethod
    def _event_seq(event: dict[str, Any]) -> int:
        try:
            return int(event.get("seq", 0))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _event_identity(cls, event: dict[str, Any]) -> tuple[str, str]:
        event_id = event.get("event_id")
        if event_id:
            return ("event_id", str(event_id))
        seq = cls._event_seq(event)
        if seq:
            return ("seq", str(seq))
        return (
            "content",
            json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )

    @classmethod
    def _merge_timeline(
        cls,
        inline_events: Iterable[Any],
        journal_events: Iterable[Any],
        compacted_through_seq: int = 0,
    ) -> list[dict[str, Any]]:
        """Merge legacy inline events and journal rows without duplicates."""

        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for candidate in [*inline_events, *journal_events]:
            if not isinstance(candidate, dict):
                continue
            identity = cls._event_identity(candidate)
            merged.setdefault(identity, candidate)
        result = sorted(
            merged.values(),
            key=lambda event: (cls._event_seq(event), str(event.get("created_at", ""))),
        )
        return [
            event
            for event in result
            if cls._event_seq(event) > int(compacted_through_seq or 0)
        ]

    def _read_event_journal_locked(self, session_id: str) -> list[dict[str, Any]]:
        path = self.event_journal_path(session_id)
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise SessionError(f"无法读取事件日志: {path}: {exc}") from exc
        events: list[dict[str, Any]] = []
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                # A process can be interrupted halfway through the final JSONL
                # row.  Earlier complete rows remain usable; reject corruption
                # in the middle of the journal instead of silently losing it.
                if index == len(lines) - 1:
                    continue
                raise SessionError(
                    f"事件日志损坏: {path} 第 {index + 1} 行: {exc.msg}"
                ) from exc
            if isinstance(value, dict):
                events.append(value)
        return events

    @staticmethod
    def _file_signature(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return (int(stat.st_mtime_ns), int(stat.st_size))

    def _storage_signature_locked(self, session_id: str) -> tuple[Any, Any]:
        return (
            self._file_signature(self.session_path(session_id)),
            self._file_signature(self.event_journal_path(session_id)),
        )

    def _hydrate_timeline_locked(self, session: dict[str, Any]) -> dict[str, Any]:
        """Load journal rows into the in-memory session representation."""

        session_id = self.validate_session_id(str(session.get("session_id", "")))
        inline_events = session.get("timeline")
        if not isinstance(inline_events, list):
            inline_events = []
        journal_path = self.event_journal_path(session_id)
        journal_exists = journal_path.exists()
        journal_events = self._read_event_journal_locked(session_id)
        storage = session.get("storage")
        if not isinstance(storage, dict):
            storage = {}
            session["storage"] = storage
        mode = str(storage.get("mode") or "")
        try:
            journaled_through_seq = int(storage.get("journaled_through_seq", 0))
        except (TypeError, ValueError):
            journaled_through_seq = 0
        if mode == "event_journal" and journaled_through_seq > 0 and not journal_exists:
            raise SessionError(f"会话事件日志缺失: {journal_path}")

        compaction = session.get("compaction")
        compacted_through_seq = 0
        if isinstance(compaction, dict):
            try:
                compacted_through_seq = int(compaction.get("compacted_through_seq", 0))
            except (TypeError, ValueError):
                compacted_through_seq = 0
        session["timeline"] = self._merge_timeline(
            inline_events,
            journal_events,
            compacted_through_seq=compacted_through_seq,
        )
        journal_max_seq = max((self._event_seq(event) for event in journal_events), default=0)
        storage.update(
            {
                "mode": "event_journal",
                "journal_filename": "events.jsonl",
                "journaled_through_seq": max(journaled_through_seq, journal_max_seq),
                # Legacy inline sessions need one seed write.  If a journal is
                # already present, its rows are authoritative for the prefix.
                "journal_needs_seed": bool(inline_events) and not journal_events,
            }
        )
        return session

    def _append_journal_locked(
        self,
        session: dict[str, Any],
        events: Iterable[dict[str, Any]],
    ) -> int:
        rows = [event for event in events if isinstance(event, dict)]
        if not rows:
            return 0
        path = self.event_journal_path(session["session_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                for event in rows:
                    handle.write(
                        json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                        + "\n"
                    )
                handle.flush()
                # The checkpoint is written only after the journal is durable.
                # One fsync per MCP mutation is considerably cheaper than
                # rewriting the complete session document on every event.
                os.fsync(handle.fileno())
        except OSError as exc:
            raise SessionError(f"无法追加事件日志: {path}: {exc}") from exc
        return max((self._event_seq(event) for event in rows), default=0)

    def _rewrite_journal_locked(self, session: dict[str, Any]) -> None:
        """Atomically rewrite the journal after a validated compaction purge."""

        path = self.event_journal_path(session["session_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                for event in session.get("timeline", []):
                    handle.write(
                        json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                        + "\n"
                    )
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise SessionError(f"无法重写事件日志: {path}: {exc}") from exc
        storage = session.setdefault("storage", {})
        storage.update(
            {
                "mode": "event_journal",
                "journal_filename": "events.jsonl",
                "journaled_through_seq": max(
                    (self._event_seq(event) for event in session.get("timeline", [])),
                    default=0,
                ),
                "journal_needs_seed": False,
            }
        )

    @staticmethod
    def _collect_image_path_strings(value: Any, result: set[str] | None = None) -> set[str]:
        """Collect image-like paths without treating arbitrary text as files."""

        paths = result if result is not None else set()
        if isinstance(value, dict):
            for item in value.values():
                SessionStore._collect_image_path_strings(item, paths)
        elif isinstance(value, (list, tuple)):
            for item in value:
                SessionStore._collect_image_path_strings(item, paths)
        elif isinstance(value, str):
            lowered = value.lower().split("?", 1)[0]
            if lowered.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
                paths.add(value)
        return paths

    def _resolve_frame_artifact_locked(self, session: dict[str, Any], raw_path: str) -> Path | None:
        """Resolve a recorded image only if it stays inside this session's frames."""

        session_directory = self.session_dir(session["session_id"]).resolve()
        frames_directory = (session_directory / "frames").resolve()
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = session_directory / candidate
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(frames_directory)
        except ValueError:
            return None
        if resolved.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
            return None
        return resolved

    def _purge_unreferenced_frame_artifacts_locked(
        self,
        session: dict[str, Any],
        *,
        protected_values: Iterable[Any] = (),
    ) -> dict[str, Any]:
        """Delete raw frame files no longer referenced by active session data.

        Compaction removes the raw JSON event prefix first. This second pass
        reclaims the screenshots produced for that prefix, while retaining
        screenshots referenced by the remaining raw tail, current state, or a
        validated summary. All paths are constrained to ``frames/``.
        """

        frames_directory = (self.session_dir(session["session_id"]) / "frames").resolve()
        if not frames_directory.exists():
            return {
                "frames_scanned": 0,
                "frames_deleted": 0,
                "bytes_deleted": 0,
                "deletion_errors": [],
            }

        protected_paths: set[Path] = set()
        values: list[Any] = [session.get("timeline", []), session.get("current_state", {})]
        values.extend(protected_values)
        for raw_path in self._collect_image_path_strings(values):
            resolved = self._resolve_frame_artifact_locked(session, raw_path)
            if resolved is not None:
                protected_paths.add(resolved)

        scanned = 0
        deleted = 0
        bytes_deleted = 0
        deletion_errors: list[dict[str, str]] = []
        for path in frames_directory.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
                continue
            scanned += 1
            resolved = path.resolve(strict=False)
            if resolved in protected_paths:
                continue
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError as exc:
                deletion_errors.append({"path": str(path), "error": str(exc)})
                continue
            deleted += 1
            bytes_deleted += size
        return {
            "frames_scanned": scanned,
            "frames_deleted": deleted,
            "bytes_deleted": bytes_deleted,
            "deletion_errors": deletion_errors[:32],
        }

    def _load_locked(self, session_id: str) -> dict[str, Any]:
        path = self.session_path(session_id)
        if not path.exists():
            raise SessionError(f"找不到会话: {session_id}")
        session_id = self.validate_session_id(session_id)
        signature = self._storage_signature_locked(session_id)
        cached = self._session_cache.get(session_id)
        if cached is not None and self._session_cache_signatures.get(session_id) == signature:
            return cached
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SessionError(f"会话文件损坏: {path}: {exc.msg}") from exc
        if not isinstance(session, dict):
            raise SessionError(f"会话文件格式无效: {path}")
        hydrated = self._hydrate_timeline_locked(session)
        self._session_cache[session_id] = hydrated
        self._session_cache_signatures[session_id] = self._storage_signature_locked(session_id)
        return hydrated

    def _save_locked(self, session: dict[str, Any]) -> None:
        session["updated_at"] = utc_now()
        directory = self.session_dir(session["session_id"])
        directory.mkdir(parents=True, exist_ok=True)
        storage = session.setdefault("storage", {})
        if not isinstance(storage, dict):
            storage = {}
            session["storage"] = storage
        storage.setdefault("mode", "event_journal")
        storage.setdefault("journal_filename", "events.jsonl")
        try:
            journaled_through_seq = int(storage.get("journaled_through_seq", 0))
        except (TypeError, ValueError):
            journaled_through_seq = 0
        timeline = [event for event in session.get("timeline", []) if isinstance(event, dict)]
        if storage.get("journal_needs_seed"):
            self._rewrite_journal_locked(session)
        else:
            pending = [
                event
                for event in timeline
                if self._event_seq(event) > journaled_through_seq
            ]
            if pending:
                journaled_through_seq = self._append_journal_locked(session, pending)
                storage["journaled_through_seq"] = max(
                    journaled_through_seq,
                    int(storage.get("journaled_through_seq", 0) or 0),
                )
        storage["journal_needs_seed"] = False

        # The complete timeline remains available in memory and is rebuilt from
        # events.jsonl on load.  Only the small mutable checkpoint is serialized.
        checkpoint = dict(session)
        checkpoint["timeline"] = []
        target = directory / "session.json"
        temporary = directory / "session.json.tmp"
        temporary.write_text(
            json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
        session_id = self.validate_session_id(str(session["session_id"]))
        self._session_cache[session_id] = session
        self._session_cache_signatures[session_id] = self._storage_signature_locked(session_id)

    def _require_locked(self, session_id: str | None = None) -> dict[str, Any]:
        target = session_id or self._active_id_locked()
        if not target:
            raise SessionError("还没有活动会话，请先调用 start_session")
        return self._load_locked(self.validate_session_id(target))

    # ---------- session lifecycle ----------

    def create_session(
        self,
        game_name: str,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        game_name = _clean_text(game_name) or "未命名视觉小说"
        with self._lock:
            if session_id:
                session_id = self.validate_session_id(session_id)
            else:
                session_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            if self.session_path(session_id).exists():
                raise SessionError(f"会话已存在: {session_id}")
            now = utc_now()
            session: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "session_id": session_id,
                "status": "active",
                "game": {
                    "name": game_name,
                    "window_title": None,
                    "executable": None,
                    "control": {
                        "advance_key": "SPACE",
                        "advance_hold_seconds": 0.0,
                        "choice_mode": "number",
                    },
                    # Empty means generic parsing. Game-specific markers and
                    # fixed regions are supplied later through the MCP layout
                    # configuration tool and persisted with this session.
                    "layout_profile": {},
                    # Named input actions are optional and deliberately kept
                    # separate from OCR/layout settings so each game can map
                    # actions such as hide_ui or return_game independently.
                    "action_profile": {},
                    # Timing is also per-game: a typewriter VN can opt into
                    # local text-hash settling without slowing fixed-timing
                    # games such as the current 千恋＊万花 profile.
                    "timing_profile": {},
                },
                "created_at": now,
                "updated_at": now,
                "current_state": {
                    "scene_id": None,
                    "location": None,
                    "background": None,
                    "speaker": None,
                    "text": None,
                    "choices": [],
                    "selected_choice_id": None,
                    "variables": {},
                    "evidence": {},
                    "last_screenshot": None,
                },
                "timeline": [],
                "choices": [],
                "metadata": metadata or {},
                "compaction": {
                    "schema_version": "1.0",
                    "threshold_bytes": self.compaction_threshold_bytes,
                    "keep_recent_events": self.compaction_keep_recent_events,
                    "next_seq": 1,
                    "compacted_through_seq": 0,
                    "segments": [],
                    "pending": None,
                },
            }
            self._save_locked(session)
            self.session_dir(session_id).joinpath("frames").mkdir(exist_ok=True)
            self._set_active_locked(session_id)
            return self._summary(session)

    def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            summaries = []
            for path in self.root.glob("*/session.json"):
                try:
                    session_id = path.parent.name
                    summaries.append(self._summary(self._load_locked(session_id)))
                except (OSError, SessionError, json.JSONDecodeError, KeyError, TypeError):
                    continue
            summaries.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
            return summaries[: max(1, min(int(limit), 100))]

    def set_active(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session_id = self.validate_session_id(session_id)
            session = self._load_locked(session_id)
            if session.get("status") == "closed":
                raise SessionError("不能把已关闭会话设为活动会话")
            self._set_active_locked(session_id)
            return self._summary(session)

    def configure_game(
        self,
        window_title: str | None = None,
        executable: str | None = None,
        advance_key: str | None = None,
        advance_hold_seconds: float | None = None,
        choice_mode: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist the small amount of game-specific control data used by autoplay."""

        with self._lock:
            session = self._require_locked(session_id)
            game = session.setdefault("game", {})
            control = game.setdefault("control", {})
            if window_title is not None:
                game["window_title"] = _clean_text(window_title)
            if executable is not None:
                game["executable"] = _clean_text(executable)
            if advance_key is not None:
                control["advance_key"] = _clean_text(advance_key) or "SPACE"
            if advance_hold_seconds is not None:
                try:
                    duration = float(advance_hold_seconds)
                except (TypeError, ValueError) as exc:
                    raise SessionError("advance_hold_seconds 必须是数字") from exc
                control["advance_hold_seconds"] = max(0.0, min(duration, 30.0))
            if choice_mode is not None:
                mode = choice_mode.strip().lower()
                if mode not in {"number", "arrow", "click", "key"}:
                    raise SessionError("choice_mode 必须是 number、arrow、click 或 key")
                control["choice_mode"] = mode
            event = self._append_event_locked(
                session,
                "game_configured",
                {"game": copy.deepcopy(game)},
            )
            self._save_locked(session)
            return {"game": copy.deepcopy(game), "event": event}

    def configure_game_layout(
        self,
        profile: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist the active game's OCR markers and fixed screen regions."""

        normalised = _normalise_layout_profile(profile)
        with self._lock:
            session = self._require_locked(session_id)
            game = session.setdefault("game", {})
            game["layout_profile"] = normalised
            event = self._append_event_locked(
                session,
                "game_layout_configured",
                {"layout_profile": copy.deepcopy(normalised)},
            )
            self._save_locked(session)
            return {
                "session_id": session["session_id"],
                "layout_profile": copy.deepcopy(normalised),
                "event": event,
            }

    def configure_game_actions(
        self,
        profile: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist named, game-specific input actions."""

        normalised = _normalise_action_profile(profile)
        with self._lock:
            session = self._require_locked(session_id)
            game = session.setdefault("game", {})
            game["action_profile"] = normalised
            event = self._append_event_locked(
                session,
                "game_actions_configured",
                {"action_profile": copy.deepcopy(normalised)},
            )
            self._save_locked(session)
            return {
                "session_id": session["session_id"],
                "action_profile": copy.deepcopy(normalised),
                "event": event,
            }

    def configure_game_timing(
        self,
        profile: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist per-game post-input settling and typewriter timing."""

        normalised = _normalise_timing_profile(profile)
        with self._lock:
            session = self._require_locked(session_id)
            game = session.setdefault("game", {})
            game["timing_profile"] = normalised
            event = self._append_event_locked(
                session,
                "game_timing_configured",
                {"timing_profile": copy.deepcopy(normalised)},
            )
            self._save_locked(session)
            return {
                "session_id": session["session_id"],
                "timing_profile": copy.deepcopy(normalised),
                "event": event,
            }

    def close_session(self, session_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            session = self._require_locked(session_id)
            session["status"] = "closed"
            self._append_event_locked(session, "session_closed", {})
            self._save_locked(session)
            if self._active_id_locked() == session["session_id"]:
                self._set_active_locked(None)
            return self._summary(session)

    def get_session(self, session_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._require_locked(session_id))

    def get_current_state(self, session_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            session = self._require_locked(session_id)
            return {
                "session": self._summary(session),
                "current_state": copy.deepcopy(session["current_state"]),
                "timeline_count": len(session["timeline"]),
                "unresolved_choices": [
                    copy.deepcopy(choice)
                    for choice in session["choices"]
                    if choice.get("selected_option_id") is None and not choice.get("dismissed")
                ],
            }

    # ---------- Codex-driven compaction ----------

    @staticmethod
    def _event_digest(events: Sequence[dict[str, Any]]) -> str:
        encoded = json.dumps(
            list(events),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _compaction_meta_locked(self, session: dict[str, Any]) -> dict[str, Any]:
        """Return/migrate the small non-raw compaction manifest in a session."""

        meta = session.get("compaction")
        if not isinstance(meta, dict):
            meta = {}
            session["compaction"] = meta
        meta.setdefault("schema_version", "1.0")
        meta.setdefault("threshold_bytes", self.compaction_threshold_bytes)
        meta.setdefault("keep_recent_events", self.compaction_keep_recent_events)
        meta.setdefault("compacted_through_seq", 0)
        meta.setdefault("segments", [])
        meta.setdefault("checkpoints", [])
        meta.setdefault("active_checkpoint_id", None)
        meta.setdefault("pending", None)
        try:
            next_seq = int(meta.get("next_seq", 0))
        except (TypeError, ValueError):
            next_seq = 0
        max_seq = max(
            (int(event.get("seq", 0)) for event in session.get("timeline", []) if event.get("seq") is not None),
            default=0,
        )
        meta["next_seq"] = max(next_seq, max_seq + 1, 1)
        if not isinstance(meta.get("segments"), list):
            meta["segments"] = []
        if not isinstance(meta.get("checkpoints"), list):
            meta["checkpoints"] = []
        return meta

    @staticmethod
    def _compaction_summary_defaults() -> dict[str, Any]:
        return {
            "key_facts": [],
            "characters": [],
            "choices": [],
            "decisions": [],
            "unresolved_threads": [],
            "important_quotes": [],
            "ocr_uncertainties": [],
            "route_implications": [],
            "loss_notes": [],
            "variables": {},
            "last_known_state": {},
        }

    @classmethod
    def _normalise_compaction_summary(cls, summary: Any) -> dict[str, Any]:
        if not isinstance(summary, dict):
            raise SessionError("summary 必须是 JSON 对象")
        try:
            normalised = copy.deepcopy(summary)
            encoded = json.dumps(normalised, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise SessionError("summary 必须只包含 JSON 可序列化值") from exc
        if len(encoded.encode("utf-8")) > 2_000_000:
            raise SessionError("summary 过大，不能超过 2 MB")
        story_summary = _clean_text(
            normalised.get("story_summary")
            or normalised.get("summary_text")
            or normalised.get("summary")
        )
        if not story_summary:
            raise SessionError("summary 必须包含非空 story_summary")
        normalised["story_summary"] = story_summary
        normalised.pop("summary_text", None)
        if isinstance(normalised.get("summary"), str):
            normalised.pop("summary", None)
        for key, default in cls._compaction_summary_defaults().items():
            if key not in normalised:
                normalised[key] = copy.deepcopy(default)
            elif key == "variables" or key == "last_known_state":
                if not isinstance(normalised[key], dict):
                    raise SessionError(f"summary.{key} 必须是对象")
            elif not isinstance(normalised[key], list):
                raise SessionError(f"summary.{key} 必须是数组")
            if isinstance(normalised.get(key), list) and len(normalised[key]) > 10_000:
                raise SessionError(f"summary.{key} 不能超过 10000 项")
        normalised["summary_version"] = str(normalised.get("summary_version") or "1.0")
        return normalised

    def _raw_json_size_locked(self, session: dict[str, Any]) -> tuple[int, int, int]:
        """Return checkpoint bytes, journal bytes, and logical timeline bytes."""

        try:
            session_bytes = self.session_path(session["session_id"]).stat().st_size
        except OSError:
            session_bytes = len(json.dumps(session, ensure_ascii=False).encode("utf-8"))
        try:
            journal_bytes = self.event_journal_path(session["session_id"]).stat().st_size
        except OSError:
            journal_bytes = 0
        timeline_bytes = len(
            json.dumps(session.get("timeline", []), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        return session_bytes, journal_bytes, timeline_bytes

    def _compaction_candidate_locked(
        self,
        session: dict[str, Any],
        *,
        keep_recent_events: int,
        max_source_chars: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        timeline = session.get("timeline") or []
        if len(timeline) <= keep_recent_events:
            return [], "recent_event_tail_protected"
        candidate_end = len(timeline) - keep_recent_events
        unresolved_ids = {
            str(choice.get("choice_id"))
            for choice in session.get("choices", [])
            if (
                choice.get("selected_option_id") is None
                and not choice.get("dismissed")
                and choice.get("choice_id") is not None
            )
        }
        # Never cut through an unresolved decision.  The current state keeps
        # the choice too, but retaining its source event makes the first
        # Codex summary auditable and prevents accidental loss of options.
        for index, event in enumerate(timeline[:candidate_end]):
            if event.get("type") in {"choice", "choice_resolved"} and str(event.get("choice_id")) in unresolved_ids:
                candidate_end = index
                break
        if candidate_end <= 0:
            return [], "unresolved_choice_in_compaction_prefix"

        limit = max(16_384, min(int(max_source_chars), 2_000_000))
        selected: list[dict[str, Any]] = []
        current_chars = 2
        for event in timeline[:candidate_end]:
            event_chars = len(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
            if selected and current_chars + event_chars + 1 > limit:
                break
            selected.append(copy.deepcopy(event))
            current_chars += event_chars + 1
        if not selected:
            return [], "source_event_exceeds_request_limit"
        return selected, None

    def _compaction_status_locked(
        self,
        session: dict[str, Any],
        *,
        threshold_bytes: int | None = None,
        keep_recent_events: int | None = None,
    ) -> dict[str, Any]:
        meta = self._compaction_meta_locked(session)
        threshold = self._bounded_compaction_threshold(
            threshold_bytes if threshold_bytes is not None else meta.get("threshold_bytes", self.compaction_threshold_bytes)
        )
        keep = self._bounded_compaction_keep_recent_events(
            keep_recent_events if keep_recent_events is not None else meta.get("keep_recent_events", self.compaction_keep_recent_events)
        )
        session_bytes, journal_bytes, timeline_bytes = self._raw_json_size_locked(session)
        pending = meta.get("pending") if isinstance(meta.get("pending"), dict) else None
        storage_bytes = session_bytes + journal_bytes
        due = bool(pending or storage_bytes >= threshold)
        candidate, reason = self._compaction_candidate_locked(
            session,
            keep_recent_events=keep,
            max_source_chars=120_000,
        )
        if pending:
            candidate_event_count = int(pending.get("event_count", 0))
            candidate_start = pending.get("seq_start")
            candidate_end = pending.get("seq_end")
        else:
            candidate_event_count = len(candidate)
            candidate_start = candidate[0].get("seq") if candidate else None
            candidate_end = candidate[-1].get("seq") if candidate else None
        return {
            "summary_due": due,
            "threshold_bytes": threshold,
            "session_json_bytes": session_bytes,
            "event_journal_bytes": journal_bytes,
            "storage_bytes": storage_bytes,
            "raw_timeline_bytes": timeline_bytes,
            "raw_event_count": len(session.get("timeline", [])),
            "keep_recent_events": keep,
            "compacted_through_seq": int(meta.get("compacted_through_seq") or 0),
            "segment_count": len(meta.get("segments") or []),
            "pending_request_id": pending.get("request_id") if pending else None,
            "candidate_event_count": candidate_event_count,
            "candidate_seq_start": candidate_start,
            "candidate_seq_end": candidate_end,
            "candidate_available": bool(candidate or pending),
            "candidate_block_reason": reason if due and not candidate and not pending else None,
        }

    def compaction_status(
        self,
        threshold_bytes: int | None = None,
        keep_recent_events: int | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            session = self._require_locked(session_id)
            return {"session_id": session["session_id"], **self._compaction_status_locked(
                session,
                threshold_bytes=threshold_bytes,
                keep_recent_events=keep_recent_events,
            )}

    def get_compaction_request(
        self,
        threshold_bytes: int | None = None,
        keep_recent_events: int | None = None,
        max_source_chars: int = 120_000,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Prepare a bounded source segment for Codex to summarize."""

        with self._lock:
            session = self._require_locked(session_id)
            meta = self._compaction_meta_locked(session)
            status = self._compaction_status_locked(
                session,
                threshold_bytes=threshold_bytes,
                keep_recent_events=keep_recent_events,
            )
            pending = meta.get("pending") if isinstance(meta.get("pending"), dict) else None
            if not status["summary_due"]:
                return {"session_id": session["session_id"], **status, "request": None}

            if pending:
                events = [
                    copy.deepcopy(event)
                    for event in session.get("timeline", [])
                    if int(pending.get("seq_start", 0)) <= int(event.get("seq", 0)) <= int(pending.get("seq_end", 0))
                ]
                if len(events) != int(pending.get("event_count", 0)) or self._event_digest(events) != pending.get("digest"):
                    meta["pending"] = None
                    pending = None
                else:
                    request = self._compaction_request_payload(session, pending, events)
                    return {"session_id": session["session_id"], **status, "request": request}

            if not status["candidate_available"]:
                return {"session_id": session["session_id"], **status, "request": None}
            limit = max(16_384, min(int(max_source_chars), 2_000_000))
            keep = self._bounded_compaction_keep_recent_events(
                keep_recent_events
                if keep_recent_events is not None
                else meta.get("keep_recent_events", self.compaction_keep_recent_events)
            )
            events, reason = self._compaction_candidate_locked(
                session,
                keep_recent_events=keep,
                max_source_chars=limit,
            )
            if not events:
                status["candidate_available"] = False
                status["candidate_block_reason"] = reason or "no_compaction_candidate"
                return {"session_id": session["session_id"], **status, "request": None}
            pending = {
                "request_id": new_id("compact"),
                "seq_start": int(events[0]["seq"]),
                "seq_end": int(events[-1]["seq"]),
                "event_count": len(events),
                "digest": self._event_digest(events),
                "created_at": utc_now(),
            }
            meta["pending"] = pending
            self._save_locked(session)
            request = self._compaction_request_payload(session, pending, events)
            return {
                "session_id": session["session_id"],
                **self._compaction_status_locked(session, threshold_bytes=threshold_bytes, keep_recent_events=keep_recent_events),
                "request": request,
            }

    @staticmethod
    def _compaction_request_payload(
        session: dict[str, Any],
        pending: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "request_id": pending["request_id"],
            "source": {
                "session_id": session["session_id"],
                "seq_start": pending["seq_start"],
                "seq_end": pending["seq_end"],
                "event_count": pending["event_count"],
                "sha256": pending["digest"],
            },
            "events": events,
            "summary_contract": {
                "required": ["story_summary"],
                "recommended": [
                    "key_facts",
                    "characters",
                    "choices",
                    "decisions",
                    "unresolved_threads",
                    "important_quotes",
                    "ocr_uncertainties",
                    "route_implications",
                    "variables",
                    "last_known_state",
                    "loss_notes",
                ],
                "rules": [
                    "保留事件顺序和人物关系，不要把不确定 OCR 当成确定事实",
                    "完整记录每个选项、实际选择和选择后的结果；无法判断的内容写入 loss_notes 或 ocr_uncertainties",
                    "保留未解决伏笔、路线变量、重要原文短句和当前状态",
                    "只总结 source 中的内容，不凭空补写剧情",
                    "普通段落摘要是不可覆盖的底层记录；不要为了生成总纲而删除其中独有事实",
                    "真实游戏选项是大检查点边界：先记录选项出现前的状态和全部候选项，再记录选择后的分支增量",
                    "必须把 player_choice 与 narrative_decision 分开；剧情人物的决定不能伪装成玩家路线选择",
                    "多路线使用 route_id、parent_checkpoint_id 和 choice_node_id；共通剧情只去重一次，分支独有剧情必须保留",
                    "第一次大检查点可以综合已有全部段落；后续检查点只需合并上一个检查点与新增段落，不要反复重写全部历史",
                    "只有在覆盖范围、选项记录、人物首次出现、关键设定、未解决伏笔和不确定性均已核对后，才允许清理对应原始事件",
                ],
                "checkpoint_contract": {
                    "purpose": "在真实选项、路线汇合或结局处建立可回查的大检查点，供 Codex 理解共通线、分支和延迟后果",
                    "recommended_fields": [
                        "checkpoint_kind",
                        "checkpoint_id",
                        "parent_checkpoint_id",
                        "route_id",
                        "choice_node_id",
                        "source_segments",
                        "coverage",
                        "timeline",
                        "confirmed_facts",
                        "characters",
                        "relationships",
                        "player_choices",
                        "branch_deltas",
                        "open_threads",
                        "important_quotes",
                        "current_state",
                        "uncertainties",
                        "loss_notes",
                    ],
                    "checkpoint_kinds": [
                        "initial_common",
                        "choice_boundary",
                        "route_progress",
                        "route_ending",
                    ],
                    "choice_boundary_rules": [
                        "保存选择前剧情和状态，并完整保存所有可见选项文本",
                        "选择动作后，以同一个 choice_node_id 建立所选 route_id 的后续增量",
                        "记录直接后果和后来才确认的延迟后果；无法证明的因果关系标为待验证",
                        "重新游玩其他选项时，从同一 parent_checkpoint_id 创建新分支，不覆盖原路线",
                    ],
                },
            },
        }

    def save_compaction(
        self,
        request_id: str,
        summary: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate Codex's summary, persist it, then purge only its raw prefix."""

        request_id = _clean_text(request_id)
        if not request_id:
            raise SessionError("request_id 不能为空")
        normalised_summary = self._normalise_compaction_summary(summary)
        with self._lock:
            session = self._require_locked(session_id)
            meta = self._compaction_meta_locked(session)
            pending = meta.get("pending") if isinstance(meta.get("pending"), dict) else None
            if not pending or pending.get("request_id") != request_id:
                raise SessionError("compaction request 已过期或不存在，请重新调用 get_compaction_request")
            source_count = int(pending.get("event_count", 0))
            source_events = [copy.deepcopy(event) for event in session.get("timeline", [])[:source_count]]
            if len(source_events) != source_count or self._event_digest(source_events) != pending.get("digest"):
                raise SessionError("原始事件在总结期间发生变化，拒绝删除；请重新获取 compaction request")
            if source_events and (
                int(source_events[0].get("seq", 0)) != int(pending.get("seq_start", 0))
                or int(source_events[-1].get("seq", 0)) != int(pending.get("seq_end", 0))
            ):
                raise SessionError("compaction source 范围不再是当前原始事件前缀，拒绝删除")

            segment_number = len(meta.get("segments") or []) + 1
            segment_id = f"segment_{segment_number:04d}_{int(pending['seq_end']):08d}"
            relative_filename = Path("compactions") / f"{segment_id}.json"
            session_directory = self.session_dir(session["session_id"])
            destination = (session_directory / relative_filename).resolve()
            try:
                destination.relative_to(session_directory.resolve())
            except ValueError as exc:
                raise SessionError("compaction 文件路径无效") from exc
            segment_payload = {
                "record_type": "galgame_compaction",
                "schema_version": "1.0",
                "segment_id": segment_id,
                "session_id": session["session_id"],
                "created_at": utc_now(),
                "source": {
                    "seq_start": pending["seq_start"],
                    "seq_end": pending["seq_end"],
                    "event_count": source_count,
                    "sha256": pending["digest"],
                },
                "summary": normalised_summary,
            }
            normalised_summary["source_seq_start"] = pending["seq_start"]
            normalised_summary["source_seq_end"] = pending["seq_end"]
            normalised_summary["source_event_count"] = source_count
            encoded = json.dumps(segment_payload, ensure_ascii=False, indent=2) + "\n"
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            temporary.write_text(encoded, encoding="utf-8")
            # Re-read the just-written summary before changing session.json.
            json.loads(temporary.read_text(encoding="utf-8"))
            temporary.replace(destination)

            segment_meta = {
                "segment_id": segment_id,
                "filename": str(relative_filename).replace("\\", "/"),
                "seq_start": pending["seq_start"],
                "seq_end": pending["seq_end"],
                "event_count": source_count,
                "sha256": pending["digest"],
                "story_summary": normalised_summary["story_summary"],
                "created_at": segment_payload["created_at"],
            }
            meta["segments"] = list(meta.get("segments") or []) + [segment_meta]
            meta["compacted_through_seq"] = int(pending["seq_end"])
            meta["pending"] = None
            meta["next_seq"] = max(int(meta.get("next_seq", 1)), int(pending["seq_end"]) + 1)
            session["timeline"] = session["timeline"][source_count:]
            # The raw prefix is physically removed from the journal as well as
            # from the in-memory timeline.  The summary segment was committed
            # first, so an interruption before the checkpoint leaves recoverable
            # old data rather than a partially purged session.
            self._rewrite_journal_locked(session)
            self._save_locked(session)
            raw_artifacts = self._purge_unreferenced_frame_artifacts_locked(
                session,
                protected_values=(normalised_summary,),
            )
            return {
                "session_id": session["session_id"],
                "segment": segment_meta,
                "summary": copy.deepcopy(normalised_summary),
                "raw_purged": True,
                "purged_event_count": source_count,
                "remaining_raw_event_count": len(session["timeline"]),
                "raw_artifacts": raw_artifacts,
                "path": str(destination),
            }

    def _load_compaction_summaries_locked(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        session_directory = self.session_dir(session["session_id"]).resolve()
        for segment in self._compaction_meta_locked(session).get("segments") or []:
            if not isinstance(segment, dict):
                continue
            filename = segment.get("filename")
            if not filename:
                continue
            path = (session_directory / str(filename)).resolve()
            try:
                path.relative_to(session_directory)
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                summaries.append({
                    "segment_id": segment.get("segment_id"),
                    "status": "missing_or_invalid",
                    "source": {
                        "seq_start": segment.get("seq_start"),
                        "seq_end": segment.get("seq_end"),
                    },
                })
                continue
            if isinstance(payload, dict) and isinstance(payload.get("summary"), dict):
                summaries.append({
                    "segment_id": payload.get("segment_id"),
                    "source": copy.deepcopy(payload.get("source") or {}),
                    "summary": copy.deepcopy(payload["summary"]),
                })
        return summaries

    def _load_story_checkpoints_locked(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        """Load durable route checkpoints without treating them as raw events."""

        checkpoints: list[dict[str, Any]] = []
        session_directory = self.session_dir(session["session_id"]).resolve()
        for checkpoint_meta in self._compaction_meta_locked(session).get("checkpoints") or []:
            if not isinstance(checkpoint_meta, dict):
                continue
            filename = checkpoint_meta.get("filename")
            if not filename:
                continue
            path = (session_directory / str(filename)).resolve()
            try:
                path.relative_to(session_directory)
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                checkpoints.append({
                    "checkpoint_id": checkpoint_meta.get("checkpoint_id"),
                    "status": "missing_or_invalid",
                    "source": copy.deepcopy(checkpoint_meta.get("source") or {}),
                })
                continue
            if isinstance(payload, dict) and isinstance(payload.get("checkpoint"), dict):
                checkpoints.append({
                    "checkpoint_id": payload.get("checkpoint_id"),
                    "source": copy.deepcopy(payload.get("source") or {}),
                    "checkpoint": copy.deepcopy(payload["checkpoint"]),
                })
        return checkpoints

    @staticmethod
    def _normalise_story_checkpoint(checkpoint: Any) -> dict[str, Any]:
        if not isinstance(checkpoint, dict):
            raise SessionError("checkpoint 必须是 JSON 对象")
        try:
            normalised = copy.deepcopy(checkpoint)
            encoded = json.dumps(normalised, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise SessionError("checkpoint 必须只包含 JSON 可序列化值") from exc
        if len(encoded.encode("utf-8")) > 4_000_000:
            raise SessionError("checkpoint 过大，不能超过 4 MB")
        story_summary = _clean_text(normalised.get("story_summary"))
        if not story_summary:
            raise SessionError("checkpoint 必须包含非空 story_summary")
        normalised["story_summary"] = story_summary
        normalised["checkpoint_kind"] = _clean_text(normalised.get("checkpoint_kind")) or "route_progress"
        normalised["route_id"] = _clean_text(normalised.get("route_id")) or "main"
        list_fields = (
            "source_segments",
            "timeline",
            "confirmed_facts",
            "characters",
            "relationships",
            "player_choices",
            "branch_deltas",
            "open_threads",
            "important_quotes",
            "uncertainties",
            "loss_notes",
        )
        for key in list_fields:
            value = normalised.get(key)
            if value is None:
                normalised[key] = []
            elif not isinstance(value, list):
                raise SessionError(f"checkpoint.{key} 必须是数组")
            elif len(value) > 20_000:
                raise SessionError(f"checkpoint.{key} 不能超过 20000 项")
        for key in ("coverage", "current_state", "variables"):
            value = normalised.get(key)
            if value is None:
                normalised[key] = {}
            elif not isinstance(value, dict):
                raise SessionError(f"checkpoint.{key} 必须是对象")
        return normalised

    def save_story_checkpoint(
        self,
        checkpoint: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist a semantic route checkpoint while retaining segment audit records."""

        normalised = self._normalise_story_checkpoint(checkpoint)
        with self._lock:
            session = self._require_locked(session_id)
            meta = self._compaction_meta_locked(session)
            existing_ids = {
                str(item.get("checkpoint_id"))
                for item in meta.get("checkpoints") or []
                if isinstance(item, dict) and item.get("checkpoint_id")
            }
            requested_id = _clean_text(normalised.get("checkpoint_id"))
            if requested_id:
                if not _SESSION_ID_RE.fullmatch(requested_id):
                    raise SessionError("checkpoint_id 只能包含字母、数字、下划线和连字符")
                checkpoint_id = requested_id
            else:
                checkpoint_id = f"checkpoint_{len(existing_ids) + 1:04d}"
            if checkpoint_id in existing_ids:
                raise SessionError(f"checkpoint_id 已存在: {checkpoint_id}")
            normalised["checkpoint_id"] = checkpoint_id
            normalised["session_id"] = session["session_id"]
            normalised["created_at"] = utc_now()

            coverage = normalised.get("coverage") or {}
            source_seq_start = coverage.get("seq_start")
            source_seq_end = coverage.get("seq_end")
            source_segments = normalised.get("source_segments") or []
            filename = Path("checkpoints") / f"{checkpoint_id}.json"
            session_directory = self.session_dir(session["session_id"]).resolve()
            destination = (session_directory / filename).resolve()
            try:
                destination.relative_to(session_directory)
            except ValueError as exc:
                raise SessionError("checkpoint 文件路径无效") from exc
            payload = {
                "record_type": "galgame_story_checkpoint",
                "schema_version": "1.0",
                "checkpoint_id": checkpoint_id,
                "session_id": session["session_id"],
                "created_at": normalised["created_at"],
                "source": {
                    "seq_start": source_seq_start,
                    "seq_end": source_seq_end,
                    "segment_count": len(source_segments),
                    "segment_ids": [
                        item.get("segment_id") if isinstance(item, dict) else str(item)
                        for item in source_segments
                    ],
                },
                "checkpoint": normalised,
            }
            encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            temporary.write_text(encoded, encoding="utf-8")
            json.loads(temporary.read_text(encoding="utf-8"))
            temporary.replace(destination)
            checkpoint_meta = {
                "checkpoint_id": checkpoint_id,
                "filename": str(filename).replace("\\", "/"),
                "checkpoint_kind": normalised["checkpoint_kind"],
                "route_id": normalised["route_id"],
                "source": copy.deepcopy(payload["source"]),
                "story_summary": normalised["story_summary"],
                "created_at": normalised["created_at"],
            }
            meta["checkpoints"] = list(meta.get("checkpoints") or []) + [checkpoint_meta]
            meta["active_checkpoint_id"] = checkpoint_id
            self._save_locked(session)
            return {
                "session_id": session["session_id"],
                "checkpoint": checkpoint_meta,
                "payload": copy.deepcopy(normalised),
                "path": str(destination),
                "retained_segment_count": len(meta.get("segments") or []),
            }

    # ---------- event recording ----------

    def _append_event_locked(
        self,
        session: dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        meta = self._compaction_meta_locked(session)
        seq = int(meta.get("next_seq", len(session["timeline"]) + 1))
        meta["next_seq"] = seq + 1
        event = {
            "event_id": new_id("evt"),
            "seq": seq,
            "type": event_type,
            "created_at": utc_now(),
        }
        event.update(payload)
        session["timeline"].append(event)
        return event

    def record_dialogue(
        self,
        text: str,
        speaker: str | None = None,
        scene_id: str | None = None,
        translation: str | None = None,
        source: str = "manual",
        confidence: float | None = None,
        tags: Sequence[str] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        text = _clean_text(text)
        if not text:
            raise SessionError("dialogue text 不能为空")
        with self._lock:
            session = self._require_locked(session_id)
            state = session["current_state"]
            if scene_id is not None:
                state["scene_id"] = scene_id
            state["speaker"] = _clean_text(speaker) or "旁白"
            state["text"] = text
            payload: dict[str, Any] = {
                "scene_id": scene_id or state.get("scene_id"),
                "speaker": state["speaker"],
                "text": text,
                "translation": _clean_text(translation),
                "source": source or "manual",
                "tags": list(tags or []),
            }
            if confidence is not None:
                payload["confidence"] = max(0.0, min(float(confidence), 1.0))
            event = self._append_event_locked(session, "dialogue", payload)
            self._save_locked(session)
            return {"event": event, "current_state": copy.deepcopy(state)}

    def _record_choice_locked(
        self,
        session: dict[str, Any],
        options: Iterable[Any],
        prompt: str | None = None,
        scene_id: str | None = None,
        selected_index: int | None = None,
        selected_option_id: str | None = None,
        choice_id: str | None = None,
        result: str | None = None,
        source: str = "manual",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        normalised = _normalise_options(options)
        if not normalised:
            raise SessionError("至少需要一个选项")
        if selected_index is not None:
            if selected_index < 1 or selected_index > len(normalised):
                raise SessionError("selected_index 使用从 1 开始的选项序号")
            selected_option_id = normalised[selected_index - 1]["option_id"]
        if selected_option_id is not None and not any(
            item["option_id"] == str(selected_option_id) for item in normalised
        ):
            raise SessionError("selected_option_id 不在 options 中")
        choice_id = choice_id or new_id("choice")
        record = next((item for item in session["choices"] if item["choice_id"] == choice_id), None)
        if record is None:
            record = {"choice_id": choice_id}
            session["choices"].append(record)
        record.update(
            {
                "scene_id": scene_id or session["current_state"].get("scene_id"),
                "prompt": _clean_text(prompt),
                "options": normalised,
                "selected_option_id": selected_option_id,
                "selected_label": next(
                    (item["label"] for item in normalised if item["option_id"] == selected_option_id),
                    None,
                ),
                "result": _clean_text(result),
                "source": source or "manual",
                "dismissed": False,
                "dismiss_reason": None,
                "updated_at": utc_now(),
            }
        )
        event_type = "choice_resolved" if selected_option_id is not None else "choice"
        event = self._append_event_locked(
            session,
            event_type,
            {"choice_id": choice_id, **copy.deepcopy(record)},
        )
        state = session["current_state"]
        state["choices"] = copy.deepcopy(normalised)
        state["selected_choice_id"] = choice_id if selected_option_id is not None else None
        return record, event

    def record_choice(
        self,
        options: Sequence[str],
        prompt: str | None = None,
        scene_id: str | None = None,
        selected_index: int | None = None,
        choice_id: str | None = None,
        result: str | None = None,
        source: str = "manual",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            session = self._require_locked(session_id)
            record, event = self._record_choice_locked(
                session,
                options,
                prompt=prompt,
                scene_id=scene_id,
                selected_index=selected_index,
                choice_id=choice_id,
                result=result,
                source=source,
            )
            self._save_locked(session)
            return {"choice": copy.deepcopy(record), "event": event}

    def resolve_choice(
        self,
        choice_id: str,
        selected_index: int,
        result: str | None = None,
        source: str = "autoplay",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            session = self._require_locked(session_id)
            existing = next(
                (choice for choice in session["choices"] if choice.get("choice_id") == choice_id),
                None,
            )
            if existing is None:
                raise SessionError(f"找不到 choice_id: {choice_id}")
            options = [option.get("label", "") for option in existing.get("options", [])]
            record, event = self._record_choice_locked(
                session,
                options,
                prompt=existing.get("prompt"),
                scene_id=existing.get("scene_id"),
                selected_index=selected_index,
                choice_id=choice_id,
                result=result,
                source=source,
            )
            self._save_locked(session)
            return {"choice": copy.deepcopy(record), "event": event}

    def dismiss_choice(
        self,
        choice_id: str,
        reason: str = "not_a_choice",
        source: str = "visual_review",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Mark an OCR choice candidate as visually rejected.

        A false positive is neither an unanswered choice nor a selected
        option.  Keeping that distinction prevents a bad OCR line from
        blocking compaction forever or appearing as a route decision.
        """

        with self._lock:
            session = self._require_locked(session_id)
            record = next(
                (choice for choice in session["choices"] if choice.get("choice_id") == choice_id),
                None,
            )
            if record is None:
                raise SessionError(f"找不到 choice_id: {choice_id}")
            if record.get("selected_option_id") is not None:
                raise SessionError("已选择的 choice 不能标记为误报")
            record["dismissed"] = True
            record["dismiss_reason"] = _clean_text(reason) or "not_a_choice"
            record["result"] = record["dismiss_reason"]
            record["source"] = source or "visual_review"
            record["updated_at"] = utc_now()
            event = self._append_event_locked(
                session,
                "choice_dismissed",
                {"choice_id": choice_id, **copy.deepcopy(record)},
            )
            state = session["current_state"]
            if (
                state.get("selected_choice_id") == choice_id
                or state.get("choices") == record.get("options")
            ):
                state["choices"] = []
                state["selected_choice_id"] = None
            self._save_locked(session)
            return {"choice": copy.deepcopy(record), "event": event, "current_state": copy.deepcopy(state)}

    def record_scene(
        self,
        scene_id: str,
        location: str | None = None,
        background: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        scene_id = _clean_text(scene_id)
        if not scene_id:
            raise SessionError("scene_id 不能为空")
        with self._lock:
            session = self._require_locked(session_id)
            state = session["current_state"]
            state["scene_id"] = scene_id
            if location is not None:
                state["location"] = _clean_text(location)
            if background is not None:
                state["background"] = _clean_text(background)
            event = self._append_event_locked(
                session,
                "scene",
                {
                    "scene_id": scene_id,
                    "location": state.get("location"),
                    "background": state.get("background"),
                    "metadata": metadata or {},
                },
            )
            self._save_locked(session)
            return {"event": event, "current_state": copy.deepcopy(state)}

    def record_observation(
        self,
        raw_text: str | None = None,
        text: str | None = None,
        speaker: str | None = None,
        scene_id: str | None = None,
        location: str | None = None,
        choices: Sequence[str] | None = None,
        selected_index: int | None = None,
        screenshot_path: str | None = None,
        source: str = "codex",
        confidence: float | None = None,
        noise_flags: Sequence[dict[str, Any]] | None = None,
        evidence: dict[str, Any] | None = None,
        note: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not any(
            value is not None and value != "" and value != []
            for value in (raw_text, text, speaker, scene_id, location, choices, screenshot_path, evidence, note)
        ):
            raise SessionError("observation 至少需要文本、场景、选项、截图或备注之一")
        normalised_noise_flags = _normalise_noise_flags(noise_flags)
        with self._lock:
            session = self._require_locked(session_id)
            observation_id = new_id("obs")
            event_ids: list[str] = []
            observation_payload: dict[str, Any] = {
                "observation_id": observation_id,
                "source": source or "codex",
                "screenshot_path": screenshot_path,
                "raw_text": raw_text,
            }
            if normalised_noise_flags:
                observation_payload["noise_flags"] = copy.deepcopy(normalised_noise_flags)
            if isinstance(evidence, dict):
                observation_payload["evidence"] = copy.deepcopy(evidence)
            observation_event = self._append_event_locked(
                session,
                "observation",
                observation_payload,
            )
            event_ids.append(observation_event["event_id"])
            state = session["current_state"]
            if isinstance(evidence, dict):
                state["evidence"] = copy.deepcopy(evidence)
            if scene_id or location:
                if scene_id:
                    state["scene_id"] = scene_id
                if location:
                    state["location"] = location
                event = self._append_event_locked(
                    session,
                    "scene_observed",
                    {"observation_id": observation_id, "scene_id": scene_id, "location": location},
                )
                event_ids.append(event["event_id"])
            if text:
                state["speaker"] = _clean_text(speaker) or "旁白"
                state["text"] = text.strip()
                payload: dict[str, Any] = {
                    "observation_id": observation_id,
                    "scene_id": scene_id or state.get("scene_id"),
                    "speaker": state["speaker"],
                    "text": text.strip(),
                    "source": source or "codex",
                }
                if normalised_noise_flags:
                    payload["noise_flags"] = copy.deepcopy(normalised_noise_flags)
                if isinstance(evidence, dict):
                    payload["evidence"] = copy.deepcopy(evidence)
                if confidence is not None:
                    payload["confidence"] = max(0.0, min(float(confidence), 1.0))
                event = self._append_event_locked(session, "dialogue", payload)
                event_ids.append(event["event_id"])
            if choices:
                _, event = self._record_choice_locked(
                    session,
                    choices,
                    scene_id=scene_id,
                    selected_index=selected_index,
                    source=source or "codex",
                )
                event["observation_id"] = observation_id
                event_ids.append(event["event_id"])
            if screenshot_path:
                state["last_screenshot"] = screenshot_path
                event = self._append_event_locked(
                    session,
                    "screenshot",
                    {"observation_id": observation_id, "path": screenshot_path},
                )
                event_ids.append(event["event_id"])
            if note:
                event = self._append_event_locked(
                    session,
                    "note",
                    {"observation_id": observation_id, "text": note.strip(), "source": source or "codex"},
                )
                event_ids.append(event["event_id"])
            self._save_locked(session)
            return {
                "observation_id": observation_id,
                "event_ids": event_ids,
                "current_state": copy.deepcopy(state),
            }

    def record_action(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        action = _clean_text(action)
        if not action:
            raise SessionError("action 不能为空")
        with self._lock:
            session = self._require_locked(session_id)
            event = self._append_event_locked(session, "action", {"action": action, "payload": payload or {}})
            self._save_locked(session)
            return event

    def set_story_variable(
        self,
        name: str,
        value: str,
        value_type: str = "string",
        reason: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        name = _clean_text(name)
        if not name:
            raise SessionError("变量名不能为空")
        parsed = _coerce_story_value(value, value_type)
        with self._lock:
            session = self._require_locked(session_id)
            variables = session["current_state"].setdefault("variables", {})
            old_value = variables.get(name)
            variables[name] = parsed
            event = self._append_event_locked(
                session,
                "state_change",
                {"name": name, "old_value": old_value, "value": parsed, "reason": _clean_text(reason)},
            )
            self._save_locked(session)
            return {"event": event, "variables": copy.deepcopy(variables)}

    def add_note(
        self,
        text: str,
        kind: str = "note",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        text = _clean_text(text)
        if not text:
            raise SessionError("note 不能为空")
        with self._lock:
            session = self._require_locked(session_id)
            event = self._append_event_locked(session, "note", {"kind": kind or "note", "text": text})
            self._save_locked(session)
            return event

    def record_screenshot(
        self,
        path: str,
        width: int | None = None,
        height: int | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        path = str(Path(path).expanduser().resolve())
        with self._lock:
            session = self._require_locked(session_id)
            state = session["current_state"]
            state["last_screenshot"] = path
            payload: dict[str, Any] = {"path": path}
            if width is not None:
                payload["width"] = int(width)
            if height is not None:
                payload["height"] = int(height)
            event = self._append_event_locked(session, "screenshot", payload)
            self._save_locked(session)
            return event

    # ---------- search, context and export ----------

    def search_story(self, query: str, limit: int = 20, session_id: str | None = None) -> dict[str, Any]:
        query = _clean_text(query)
        if not query:
            raise SessionError("搜索词不能为空")
        needle = query.casefold()
        with self._lock:
            session = self._require_locked(session_id)
            matches = []
            for event in reversed(session["timeline"]):
                haystack = json.dumps(event, ensure_ascii=False).casefold()
                if needle in haystack:
                    matches.append(copy.deepcopy(event))
                    if len(matches) >= max(1, min(int(limit), 100)):
                        break
            return {"query": query, "count": len(matches), "matches": list(reversed(matches))}

    def build_context(
        self,
        recent_events: int = 16,
        include_markdown: bool = True,
        compact: bool = False,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            session = self._require_locked(session_id)
            limit = max(1, min(int(recent_events), 100))
            recent = copy.deepcopy(session["timeline"][-limit:])
            unresolved = [
                copy.deepcopy(choice)
                for choice in session["choices"]
                if choice.get("selected_option_id") is None and not choice.get("dismissed")
            ]
            if compact:
                recent = [
                    self._compact_event(event)
                    for event in recent
                    if event.get("type") not in {"screenshot", "observation"}
                    and not (
                        event.get("type") == "action"
                        and (event.get("action") or "") in {"focus_window", "attach_game"}
                    )
                ]
                unresolved = [self._compact_choice(choice) for choice in unresolved]
                recent_dialogue = [
                    event
                    for event in (self._compact_event(item) for item in session["timeline"])
                    if event.get("type") == "dialogue"
                ][-limit:]
                notes = [
                    {key: event.get(key) for key in ("seq", "type", "text", "source")}
                    for event in session["timeline"]
                    if event.get("type") == "note"
                ][-limit:]
                current_state = self._compact_state(session.get("current_state", {}))
            else:
                recent_dialogue = [
                    copy.deepcopy(event)
                    for event in session["timeline"]
                    if event.get("type") == "dialogue"
                ][-limit:]
                notes = [
                    copy.deepcopy(event)
                    for event in session["timeline"]
                    if event.get("type") == "note"
                ][-limit:]
                current_state = copy.deepcopy(session["current_state"])
            compacted_summaries = self._load_compaction_summaries_locked(session)
            story_checkpoints = self._load_story_checkpoints_locked(session)
            if compact and story_checkpoints:
                latest_checkpoint = story_checkpoints[-1]
                latest_source = latest_checkpoint.get("source") or {}
                try:
                    checkpoint_end = int(latest_source.get("seq_end") or 0)
                except (TypeError, ValueError):
                    checkpoint_end = 0
                if checkpoint_end:
                    # Keep all segment files on disk for audit/recovery, but the
                    # normal compact context only needs the delta after the
                    # latest semantic checkpoint.
                    compacted_summaries = [
                        item
                        for item in compacted_summaries
                        if int((item.get("source") or {}).get("seq_end") or 0) > checkpoint_end
                    ]
            context: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "session": self._summary(session),
                "current_state": current_state,
                "recent_events": recent,
                "unresolved_choices": unresolved,
                "recent_dialogue": recent_dialogue,
                "notes": notes,
                # Historical raw events may have been purged after Codex
                # returned a validated summary.  These files are the durable
                # long-term memory that must be combined with the raw tail.
                "compacted_summaries": compacted_summaries,
                "story_checkpoints": story_checkpoints,
                "compaction": self._compaction_status_locked(session),
            }
            if include_markdown:
                context["codex_markdown"] = self._render_markdown(session, limit)
            return context

    @staticmethod
    def _compact_state(state: dict[str, Any]) -> dict[str, Any]:
        return {
            "scene_id": state.get("scene_id"),
            "location": state.get("location"),
            "background": state.get("background"),
            "speaker": state.get("speaker"),
            "text": state.get("text"),
            "choices": [SessionStore._compact_choice_item(item) for item in state.get("choices", [])],
            "selected_choice_id": state.get("selected_choice_id"),
            "variables": copy.deepcopy(state.get("variables", {})),
        }

    @staticmethod
    def _compact_choice_item(item: Any) -> dict[str, Any]:
        if isinstance(item, dict):
            return {
                "option_id": item.get("option_id"),
                "label": item.get("label"),
            }
        return {"label": str(item)}

    @staticmethod
    def _compact_choice(choice: dict[str, Any]) -> dict[str, Any]:
        compact = {
            "choice_id": choice.get("choice_id"),
            "scene_id": choice.get("scene_id"),
            "prompt": choice.get("prompt"),
            "options": [SessionStore._compact_choice_item(item) for item in choice.get("options", [])],
            "selected_option_id": choice.get("selected_option_id"),
            "selected_label": choice.get("selected_label"),
            "result": choice.get("result"),
        }
        return {key: value for key, value in compact.items() if value not in (None, [], "")}

    @staticmethod
    def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
        event_type = event.get("type")
        keys_by_type = {
            "dialogue": ("seq", "type", "scene_id", "speaker", "text", "source", "confidence", "noise_flags"),
            "choice": ("seq", "type", "choice_id", "scene_id", "prompt", "options", "selected_option_id", "selected_label", "source"),
            "choice_resolved": ("seq", "type", "choice_id", "selected_index", "selected_label", "source"),
            "choice_dismissed": ("seq", "type", "choice_id", "dismiss_reason", "source"),
            "scene": ("seq", "type", "scene_id", "location", "background"),
            "scene_observed": ("seq", "type", "scene_id", "location"),
            "state_change": ("seq", "type", "name", "value", "reason"),
            "note": ("seq", "type", "text", "kind", "source"),
        }
        keys = keys_by_type.get(event_type)
        if keys:
            compact = {key: copy.deepcopy(event.get(key)) for key in keys if key in event}
            if event_type == "choice":
                compact["options"] = [SessionStore._compact_choice_item(item) for item in event.get("options", [])]
            return compact
        if event_type == "action":
            payload = event.get("payload") or {}
            kept_payload = {
                key: copy.deepcopy(payload[key])
                for key in ("option_index", "mode", "key", "wait_seconds", "choice_id")
                if key in payload
            }
            return {
                "seq": event.get("seq"),
                "type": "action",
                "action": event.get("action"),
                "payload": kept_payload,
            }
        return {"seq": event.get("seq"), "type": event_type}

    def export_session(
        self,
        output_format: str = "json",
        filename: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        output_format = (output_format or "json").strip().lower()
        if output_format not in {"json", "markdown", "md", "jsonl"}:
            raise SessionError("output_format 必须是 json、markdown 或 jsonl")
        with self._lock:
            session = self._require_locked(session_id)
            if output_format == "json":
                content = json.dumps(session, ensure_ascii=False, indent=2) + "\n"
                default_name = "session_export.json"
            elif output_format in {"markdown", "md"}:
                content = self._render_markdown(session, max(16, len(session["timeline"]))) + "\n"
                default_name = "codex_context.md"
            else:
                rows = [{"record_type": "session", **session}]
                rows.extend({"record_type": "event", **event} for event in session["timeline"])
                content = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
                default_name = "timeline.jsonl"
            filename = filename or default_name
            relative = Path(filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise SessionError("导出文件必须位于当前会话目录内")
            destination = (self.session_dir(session["session_id"]) / relative).resolve()
            try:
                destination.relative_to(self.session_dir(session["session_id"]).resolve())
            except ValueError as exc:
                raise SessionError("导出文件必须位于当前会话目录内") from exc
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
            return {
                "session_id": session["session_id"],
                "format": output_format,
                "path": str(destination),
                "bytes": len(content.encode("utf-8")),
            }

    @staticmethod
    def _summary(session: dict[str, Any]) -> dict[str, Any]:
        state = session.get("current_state", {})
        compaction = session.get("compaction") or {}
        return {
            "session_id": session["session_id"],
            "status": session.get("status", "active"),
            "game": copy.deepcopy(session.get("game", {})),
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at"),
            "scene_id": state.get("scene_id"),
            "speaker": state.get("speaker"),
            "timeline_count": len(session.get("timeline", [])),
            "unresolved_choice_count": sum(
                1
                for choice in session.get("choices", [])
                if choice.get("selected_option_id") is None and not choice.get("dismissed")
            ),
            "compaction": {
                "compacted_through_seq": int(compaction.get("compacted_through_seq") or 0),
                "segment_count": len(compaction.get("segments") or []) if isinstance(compaction.get("segments"), list) else 0,
                "checkpoint_count": len(compaction.get("checkpoints") or []) if isinstance(compaction.get("checkpoints"), list) else 0,
                "active_checkpoint_id": compaction.get("active_checkpoint_id"),
            },
        }

    @staticmethod
    def _render_markdown(session: dict[str, Any], limit: int) -> str:
        state = session["current_state"]
        lines = [
            f"# Galgame 上下文：{session['game'].get('name', '未命名视觉小说')}",
            "",
            f"- session_id: `{session['session_id']}`",
            f"- status: `{session.get('status', 'active')}`",
            f"- scene_id: `{state.get('scene_id') or ''}`",
            f"- location: {state.get('location') or '未知'}",
            "",
            "## 当前画面语义",
            "",
            f"- speaker: {state.get('speaker') or '未知'}",
            f"- text: {state.get('text') or '暂无'}",
        ]
        if state.get("last_screenshot"):
            lines.append(f"- screenshot: `{state['last_screenshot']}`")
        variables = state.get("variables") or {}
        if variables:
            lines.extend(["", "## 剧情变量", "", "```json", json.dumps(variables, ensure_ascii=False, indent=2), "```"])
        unresolved = [
            choice
            for choice in session.get("choices", [])
            if choice.get("selected_option_id") is None and not choice.get("dismissed")
        ]
        lines.extend(["", "## 待处理选项", ""])
        if unresolved:
            for choice in unresolved:
                prompt = choice.get("prompt") or "未命名选项"
                lines.append(f"- {prompt}（choice_id=`{choice['choice_id']}`）")
                for option in choice.get("options", []):
                    lines.append(f"  - `{option['option_id']}` {option['label']}")
        else:
            lines.append("- 无")
        lines.extend(["", "## 最近事件", ""])
        for event in session.get("timeline", [])[-limit:]:
            event_type = event.get("type")
            if event_type == "dialogue":
                speaker = event.get("speaker") or "旁白"
                lines.append(f"- [{event.get('seq')}] **{speaker}**：{event.get('text', '')}")
            elif event_type in {"choice", "choice_resolved"}:
                selected = event.get("selected_label") or "待选择"
                lines.append(f"- [{event.get('seq')}] 选项：{selected}")
            elif event_type == "choice_dismissed":
                lines.append(
                    f"- [{event.get('seq')}] 选项误报已驳回：{event.get('dismiss_reason') or 'not_a_choice'}"
                )
            elif event_type == "scene":
                lines.append(f"- [{event.get('seq')}] 场景：{event.get('scene_id', '')}")
            elif event_type == "action":
                lines.append(f"- [{event.get('seq')}] 操作：{event.get('action', '')}")
            elif event_type == "note":
                lines.append(f"- [{event.get('seq')}] 备注：{event.get('text', '')}")
        return "\n".join(lines)
