"""Evidence and text-episode primitives for the local VN runtime.

The runtime deliberately keeps this module free of capture, OCR, and MCP
dependencies.  It turns an OCR/parse result into a small set of information
channels and gives the autoplay loop one conservative policy decision:
``safe_to_advance``.  The same functions are usable by offline replay tests.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any


EVIDENCE_SCHEMA_VERSION = "1.0"
EVIDENCE_CHANNELS = (
    "dialogue",
    "speaker",
    "choice",
    "scene_label",
    "system_ui",
    "transient_story_text",
    "unknown_text",
    "visual_transition",
)


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def text_fingerprint(text: Any, *, channel: str = "dialogue") -> str | None:
    """Return a stable cross-process fingerprint for one text episode."""

    compact = _compact(text)
    if not compact:
        return None
    digest = hashlib.sha256(f"{channel}\0{compact}".encode("utf-8")).hexdigest()
    return digest


def episode_id(text: Any, *, channel: str = "dialogue") -> str | None:
    fingerprint = text_fingerprint(text, channel=channel)
    return f"episode_{fingerprint[:16]}" if fingerprint else None


@dataclass
class TextEpisodeTracker:
    """Track changing/stable text without performing OCR itself.

    A tracker is intentionally process-local.  The deterministic episode id
    comes from :func:`episode_id`, so observations remain joinable after an
    MCP restart while lifecycle counters remain cheap and local to one play
    loop.
    """

    stable_samples: int = 2
    _fingerprint: str | None = None
    _episode_id: str | None = None
    _sample_count: int = 0
    _consumed: bool = False

    def __post_init__(self) -> None:
        self.stable_samples = max(1, min(int(self.stable_samples), 10))

    def observe(
        self,
        text: Any,
        *,
        channel: str = "dialogue",
        recognized: bool = True,
        confidence: float | None = None,
    ) -> dict[str, Any] | None:
        compact = _compact(text)
        fingerprint = text_fingerprint(compact, channel=channel)
        if not compact or fingerprint is None:
            return None

        changed = fingerprint != self._fingerprint
        if changed:
            self._fingerprint = fingerprint
            self._episode_id = f"episode_{fingerprint[:16]}"
            self._sample_count = 1
            self._consumed = False
        else:
            self._sample_count += 1

        if not recognized:
            status = "UNKNOWN"
        elif changed:
            status = "NEW"
        elif self._sample_count < self.stable_samples:
            status = "QUIET_CANDIDATE"
        else:
            status = "STABLE"

        result: dict[str, Any] = {
            "episode_id": self._episode_id,
            "channel": channel,
            "fingerprint": fingerprint,
            "status": status,
            "sample_count": self._sample_count,
            "recognized": bool(recognized),
            "consumed": self._consumed,
        }
        if confidence is not None:
            result["confidence"] = max(0.0, min(float(confidence), 1.0))
        return result

    def mark_consumed(self) -> dict[str, Any] | None:
        if self._episode_id is None:
            return None
        self._consumed = True
        return {
            "episode_id": self._episode_id,
            "channel": "dialogue",
            "fingerprint": self._fingerprint,
            "status": "CONSUMED",
            "sample_count": self._sample_count,
            "recognized": True,
            "consumed": True,
        }


def _channel(
    status: str,
    *,
    resolved: bool,
    blocking: bool = False,
    text: str | None = None,
    confidence: float | None = None,
    details: list[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "resolved": bool(resolved),
        "blocking": bool(blocking),
    }
    if text:
        result["text"] = text
    if confidence is not None:
        result["confidence"] = max(0.0, min(float(confidence), 1.0))
    if details:
        result["details"] = list(details)
    return result


def build_frame_evidence(
    parsed: dict[str, Any] | None,
    *,
    screen_type: str | None = None,
    ocr_available: bool = True,
    transition_active: bool | None = None,
    allow_unknown_with_story: bool = False,
) -> dict[str, Any]:
    """Resolve parsed text into channels and a conservative advance policy."""

    parsed = parsed if isinstance(parsed, dict) else {}
    dialogue = str(parsed.get("dialogue") or "").strip()
    speaker = str(parsed.get("speaker") or "").strip()
    choices = [str(item).strip() for item in (parsed.get("choices") or []) if str(item).strip()]
    ui_lines = [str(item).strip() for item in (parsed.get("ui_lines") or []) if str(item).strip()]
    unknown_lines = [
        str(item).strip() for item in (parsed.get("unknown_lines") or []) if str(item).strip()
    ]
    unknown_story_lines = [
        str(item).strip()
        for item in (parsed.get("unknown_story_lines") or [])
        if str(item).strip()
    ]
    ignored_lines = [
        item for item in (parsed.get("ignored_lines") or []) if isinstance(item, dict)
    ]
    unclassified_lines = [
        str(item).strip() for item in (parsed.get("unparsed_lines") or []) if str(item).strip()
    ]
    confidence = parsed.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None

    is_settings = str(screen_type or parsed.get("screen_type") or "").casefold() == "settings"
    # Keep the legacy speaker-only candidate path: some engines render a
    # punctuation-only/very short line after the name label, and Windows OCR
    # may omit that line.  Unclassified text is deliberately *not* story
    # text: it is a scene-label/unknown candidate and must block autoplay.
    unknown_candidates: list[str] = []
    for line in [*unknown_lines, *unclassified_lines]:
        if line and line not in unknown_candidates:
            unknown_candidates.append(line)
    if "unknown_story_lines" not in parsed:
        # Older callers do not provide OCR geometry metadata.  Preserve the
        # fail-closed behavior instead of allowing a caller to mark arbitrary
        # unknown text as harmless story-adjacent noise.
        unknown_story_lines = list(unknown_candidates)
    has_story = bool(dialogue or speaker)
    non_blocking_unknown = bool(
        allow_unknown_with_story
        and has_story
        and not choices
        and not unknown_story_lines
        and unknown_candidates
    )
    unknown_blocks = bool(unknown_candidates) and not non_blocking_unknown
    if transition_active is None:
        transition_active = not has_story and not choices and not unknown_blocks and not is_settings
    unknown_details = ["unknown_text"] if unknown_candidates else None
    if non_blocking_unknown:
        unknown_details = ["unknown_text", "non_blocking_story_context"]
    scene_details = ["unclassified_text"] if unclassified_lines else None
    if non_blocking_unknown and scene_details:
        scene_details.append("non_blocking_story_context")
    channels: dict[str, dict[str, Any]] = {
        "dialogue": _channel(
            "stable" if dialogue else "absent",
            resolved=bool(dialogue),
            text=dialogue or None,
            confidence=confidence,
        ),
        "speaker": _channel(
            "present" if speaker else "absent",
            resolved=bool(speaker),
            text=speaker or None,
            confidence=confidence,
        ),
        "choice": _channel(
            "present" if choices else "absent",
            resolved=not choices,
            blocking=bool(choices),
            details=["choice_pending"] if choices else None,
        ),
        "scene_label": _channel(
            "candidate" if unclassified_lines else "absent",
            resolved=not unclassified_lines or non_blocking_unknown,
            blocking=bool(unclassified_lines) and not non_blocking_unknown,
            text="\n".join(unclassified_lines) if unclassified_lines else None,
            details=scene_details,
        ),
        "system_ui": _channel(
            "present" if is_settings or ui_lines else "absent",
            resolved=True,
            blocking=is_settings,
            details=["settings"] if is_settings else ("known_ui_residue",) if ui_lines else None,
        ),
        "transient_story_text": _channel(
            "candidate" if unclassified_lines else "absent",
            resolved=not unclassified_lines or non_blocking_unknown,
            blocking=bool(unclassified_lines) and not non_blocking_unknown,
            text="\n".join(unclassified_lines) if unclassified_lines else None,
            confidence=confidence,
            details=scene_details,
        ),
        "unknown_text": _channel(
            "present" if unknown_candidates else "absent",
            resolved=not unknown_candidates or non_blocking_unknown,
            blocking=unknown_blocks,
            text="\n".join(unknown_candidates) if unknown_candidates else None,
            details=unknown_details,
        ),
        "visual_transition": _channel(
            "active" if transition_active else "inactive",
            resolved=not transition_active,
            blocking=bool(transition_active),
            details=["dialogue_not_resolved"] if transition_active else None,
        ),
    }

    blocking_reasons: list[str] = []
    if not ocr_available:
        blocking_reasons.append("ocr_unavailable")
    if is_settings:
        blocking_reasons.append("system_ui")
    if choices:
        blocking_reasons.append("choice_pending")
    if unknown_blocks:
        blocking_reasons.append("unknown_text")
    if not has_story and not choices and not unknown_candidates:
        blocking_reasons.append("dialogue_unresolved")

    safe = not blocking_reasons
    evidence: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "channels": channels,
        "safe_to_advance": safe,
        "blocking_reasons": blocking_reasons,
        "unresolved_channels": [
            name for name, value in channels.items() if value.get("blocking")
        ],
    }
    if ignored_lines:
        evidence["ignored_ocr_lines"] = ignored_lines[:64]
    if non_blocking_unknown:
        evidence["non_blocking_unknown_text"] = True
    current_text = dialogue
    if current_text:
        current_episode_id = episode_id(current_text, channel="dialogue")
        if current_episode_id:
            evidence["current_episode"] = {
                "episode_id": current_episode_id,
                "channel": "dialogue",
                "fingerprint": text_fingerprint(current_text, channel="dialogue"),
                "status": "RECOGNIZED",
                "recognized": True,
                "consumed": False,
            }
    return evidence


def evidence_blocks_advance(evidence: dict[str, Any] | None) -> bool:
    """Return True only when the evidence explicitly blocks an advance."""

    return isinstance(evidence, dict) and evidence.get("safe_to_advance") is False
