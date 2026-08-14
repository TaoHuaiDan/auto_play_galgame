from __future__ import annotations

import re
from typing import Any


_CHOICE_PATTERNS = [
    re.compile(r"^\s*[①②③④⑤⑥⑦⑧⑨⑩]\s*(?P<label>.+?)\s*$"),
    re.compile(r"^\s*(?:\d{1,2}\s*[.)、:：]|[（(]\s*\d{1,2}\s*[)）])\s*(?P<label>.+?)\s*$"),
    re.compile(r"^\s*[-*•]\s+(?P<label>.+?)\s*$"),
]
_BRACKET_SPEAKER = re.compile(
    r"^\s*[【\[](?P<speaker>[^】\]]{1,30})[】\]]\s*(?::|：)?\s*(?P<text>.*)$"
)
_COLON_SPEAKER = re.compile(
    r"^\s*(?P<speaker>[^:：\n]{1,20})\s*[:：]\s+(?P<text>.+?)\s*$"
)
_NOISE = re.compile(r"^\s*(?:[-_=~·•]{3,}|>>+|skip|auto|save|load)\s*$", re.I)


def _normalise_lines(raw_text: str) -> list[str]:
    raw_text = (raw_text or "").replace("\ufeff", "").replace("\u200b", "")
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    result = []
    for line in raw_text.split("\n"):
        cleaned = re.sub(r"[ \t\u3000]+", " ", line).strip()
        if cleaned and not _NOISE.fullmatch(cleaned):
            result.append(cleaned)
    return result


def parse_screen_text(raw_text: str) -> dict[str, Any]:
    """Heuristically split OCR/clipboard text into speaker, dialogue and choices.

    The parser is deliberately conservative: it only labels a speaker when OCR
    provides a clear bracket or ``name: text`` marker. Ambiguous lines remain in
    ``unparsed_lines`` so Codex can resolve them with visual context.
    """

    lines = _normalise_lines(raw_text)
    choice_records: list[dict[str, Any]] = []
    dialogue_lines: list[str] = []
    unparsed_lines: list[str] = []
    speaker: str | None = None
    explicit_speaker = False

    for line_number, line in enumerate(lines, start=1):
        choice_match = None
        for pattern in _CHOICE_PATTERNS:
            choice_match = pattern.match(line)
            if choice_match:
                break
        if choice_match:
            choice_records.append(
                {
                    "option_id": str(len(choice_records) + 1),
                    "label": choice_match.group("label").strip(),
                    "line": line_number,
                    "raw": line,
                }
            )
            continue

        if speaker is None:
            bracket_match = _BRACKET_SPEAKER.match(line)
            if bracket_match and bracket_match.group("speaker").strip():
                speaker = bracket_match.group("speaker").strip()
                explicit_speaker = True
                remainder = bracket_match.group("text").strip()
                if remainder:
                    dialogue_lines.append(remainder)
                continue
            colon_match = _COLON_SPEAKER.match(line)
            if colon_match:
                candidate = colon_match.group("speaker").strip()
                if len(candidate) <= 20 and not any(char in candidate for char in "，。！？,.!?"):
                    speaker = candidate
                    explicit_speaker = True
                    dialogue_lines.append(colon_match.group("text").strip())
                    continue

        if line.startswith(("【", "[")) and not explicit_speaker:
            unparsed_lines.append(line)
        else:
            dialogue_lines.append(line)

    dialogue = "\n".join(dialogue_lines).strip()
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
    return {
        "raw_text": raw_text,
        "clean_text": "\n".join(lines),
        "speaker": speaker,
        "dialogue": dialogue,
        "choices": choices,
        "choice_records": choice_records,
        "unparsed_lines": unparsed_lines,
        "line_count": len(lines),
        "confidence": confidence,
    }
