from __future__ import annotations

import copy
import json
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
    """Small, crash-tolerant JSON event store for visual-novel sessions.

    Each session is a directory containing ``session.json`` and optional assets.
    The session file is rewritten atomically after every mutating operation, so a
    Codex run can be interrupted and resumed without losing the previous turn.
    """

    def __init__(self, root: str | Path | None = None):
        configured_root = root or os.environ.get("GALGAME_MCP_DATA_DIR")
        self.root = Path(configured_root or (Path.cwd() / ".galgame_sessions")).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._active_file = self.root / "active_session.txt"
        self._lock = threading.RLock()

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

    def _load_locked(self, session_id: str) -> dict[str, Any]:
        path = self.session_path(session_id)
        if not path.exists():
            raise SessionError(f"找不到会话: {session_id}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SessionError(f"会话文件损坏: {path}: {exc.msg}") from exc

    def _save_locked(self, session: dict[str, Any]) -> None:
        session["updated_at"] = utc_now()
        directory = self.session_dir(session["session_id"])
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "session.json"
        temporary = directory / "session.json.tmp"
        temporary.write_text(
            json.dumps(session, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)

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
                    "control": {"advance_key": "SPACE", "choice_mode": "number"},
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
                    "last_screenshot": None,
                },
                "timeline": [],
                "choices": [],
                "metadata": metadata or {},
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
                    summaries.append(self._summary(json.loads(path.read_text(encoding="utf-8"))))
                except (OSError, json.JSONDecodeError, KeyError, TypeError):
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
                    if choice.get("selected_option_id") is None
                ],
            }

    # ---------- event recording ----------

    def _append_event_locked(
        self,
        session: dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        event = {
            "event_id": new_id("evt"),
            "seq": len(session["timeline"]) + 1,
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
        note: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not any(
            value is not None and value != "" and value != []
            for value in (raw_text, text, speaker, scene_id, location, choices, screenshot_path, note)
        ):
            raise SessionError("observation 至少需要文本、场景、选项、截图或备注之一")
        with self._lock:
            session = self._require_locked(session_id)
            observation_id = new_id("obs")
            event_ids: list[str] = []
            observation_event = self._append_event_locked(
                session,
                "observation",
                {
                    "observation_id": observation_id,
                    "source": source or "codex",
                    "screenshot_path": screenshot_path,
                    "raw_text": raw_text,
                },
            )
            event_ids.append(observation_event["event_id"])
            state = session["current_state"]
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
                if choice.get("selected_option_id") is None
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
            context: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "session": self._summary(session),
                "current_state": current_state,
                "recent_events": recent,
                "unresolved_choices": unresolved,
                "recent_dialogue": recent_dialogue,
                "notes": notes,
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
            "dialogue": ("seq", "type", "scene_id", "speaker", "text", "source", "confidence"),
            "choice": ("seq", "type", "choice_id", "scene_id", "prompt", "options", "selected_option_id", "selected_label", "source"),
            "choice_resolved": ("seq", "type", "choice_id", "selected_index", "selected_label", "source"),
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
                1 for choice in session.get("choices", []) if choice.get("selected_option_id") is None
            ),
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
            choice for choice in session.get("choices", []) if choice.get("selected_option_id") is None
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
            elif event_type == "scene":
                lines.append(f"- [{event.get('seq')}] 场景：{event.get('scene_id', '')}")
            elif event_type == "action":
                lines.append(f"- [{event.get('seq')}] 操作：{event.get('action', '')}")
            elif event_type == "note":
                lines.append(f"- [{event.get('seq')}] 备注：{event.get('text', '')}")
        return "\n".join(lines)
