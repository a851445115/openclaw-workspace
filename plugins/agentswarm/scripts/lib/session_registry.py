#!/usr/bin/env python3
import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict


SESSION_STATE_FILE = os.path.join("state", "worker-sessions.json")
ACTIVE_SESSION_STATE_FILE = os.path.join("state", "active-sessions.json")
SESSION_STATUS_ACTIVE = "active"
SESSION_STATUS_FAILED = "failed"
SESSION_STATUS_DONE = "done"
ACTIVE_STATUS_RUNNING = "running"
ACTIVE_STATUS_FAILED = "failed"
ACTIVE_STATUS_DONE = "done"
ACTIVE_STATUS_STOPPED = "stopped"
ACTIVE_STATUS_BLOCKED = "blocked"
ACTIVE_STATUS_SET = {
    ACTIVE_STATUS_RUNNING,
    ACTIVE_STATUS_FAILED,
    ACTIVE_STATUS_DONE,
    ACTIVE_STATUS_STOPPED,
    ACTIVE_STATUS_BLOCKED,
}
LOGGER = logging.getLogger(__name__)
_REGISTRY_LOCK = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed >= 0 else default


def normalize_token(value: Any, fallback: str = "") -> str:
    token = str(value or "").strip().lower()
    return token or fallback


def session_state_path(root: str) -> str:
    return os.path.join(root, SESSION_STATE_FILE)


def active_session_state_path(root: str) -> str:
    return os.path.join(root, ACTIVE_SESSION_STATE_FILE)


def _write_json_atomic(path: str, payload: Dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=True, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        LOGGER.exception("failed to persist session registry atomically: path=%s", path)
        raise
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def load_registry(root: str) -> Dict[str, Any]:
    path = session_state_path(root)
    if not os.path.exists(path):
        return {"sessions": {}, "updatedAt": ""}
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except Exception:
        LOGGER.warning("failed to load session registry: path=%s", path, exc_info=True)
        return {"sessions": {}, "updatedAt": ""}
    sessions = loaded.get("sessions") if isinstance(loaded.get("sessions"), dict) else {}
    return {"sessions": sessions, "updatedAt": str(loaded.get("updatedAt") or "")}


def load_active_sessions(root: str) -> Dict[str, Any]:
    path = active_session_state_path(root)
    if not os.path.exists(path):
        return {"sessions": {}, "updatedAt": ""}
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except Exception:
        LOGGER.warning("failed to load active session registry: path=%s", path, exc_info=True)
        return {"sessions": {}, "updatedAt": ""}
    sessions = loaded.get("sessions") if isinstance(loaded.get("sessions"), dict) else {}
    return {"sessions": sessions, "updatedAt": str(loaded.get("updatedAt") or "")}


def save_registry(root: str, state: Dict[str, Any]) -> None:
    with _REGISTRY_LOCK:
        path = session_state_path(root)
        payload = {
            "sessions": state.get("sessions") if isinstance(state.get("sessions"), dict) else {},
            "updatedAt": now_iso(),
        }
        _write_json_atomic(path, payload)


def save_active_sessions(root: str, state: Dict[str, Any]) -> None:
    with _REGISTRY_LOCK:
        path = active_session_state_path(root)
        payload = {
            "sessions": state.get("sessions") if isinstance(state.get("sessions"), dict) else {},
            "updatedAt": now_iso(),
        }
        _write_json_atomic(path, payload)


def session_key(task_id: str, agent: str, executor: str) -> str:
    return "|".join(
        [
            str(task_id or "").strip(),
            normalize_token(agent, fallback="unknown"),
            normalize_token(executor, fallback="unknown"),
        ]
    )


def make_session_id(task_id: str, agent: str, executor: str) -> str:
    base = f"{task_id}-{agent}-{executor}"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-")
    suffix = int(time.time() * 1000)
    return f"ws-{safe or 'session'}-{suffix}"


def normalize_active_status(value: Any, fallback: str = ACTIVE_STATUS_RUNNING) -> str:
    token = normalize_token(value, fallback=fallback)
    return token if token in ACTIVE_STATUS_SET else fallback


def _safe_pid(value: Any, default: int = 0) -> int:
    pid = safe_int(value, default=default)
    return pid if pid > 0 else 0


def _normalize_session_entry(entry: Dict[str, Any], task_id: str, agent: str, executor: str) -> Dict[str, Any]:
    now = now_iso()
    created_at = str(entry.get("createdAt") or now)
    return {
        "taskId": str(task_id or "").strip(),
        "agent": normalize_token(agent, fallback="unknown"),
        "executor": normalize_token(executor, fallback="unknown"),
        "sessionId": str(entry.get("sessionId") or make_session_id(task_id, agent, executor)),
        "createdAt": created_at,
        "lastActiveAt": str(entry.get("lastActiveAt") or created_at),
        "status": str(entry.get("status") or SESSION_STATUS_ACTIVE),
        "retryCount": safe_int(entry.get("retryCount"), 0),
        "lastReasonCode": str(entry.get("lastReasonCode") or ""),
        "lastDetail": str(entry.get("lastDetail") or ""),
    }


def _normalize_active_session_entry(
    entry: Dict[str, Any],
    task_id: str,
    worktree_path: str = "",
    pid: Any = 0,
    tmux_session: str = "",
    status: str = ACTIVE_STATUS_RUNNING,
) -> Dict[str, Any]:
    now = now_iso()
    existing = entry if isinstance(entry, dict) else {}
    start_time = str(existing.get("startTime") or now)
    return {
        "taskId": str(task_id or "").strip(),
        "worktreePath": str(worktree_path or existing.get("worktreePath") or ""),
        "pid": _safe_pid(pid if pid else existing.get("pid"), 0),
        "tmuxSession": str(tmux_session or existing.get("tmuxSession") or ""),
        "startTime": start_time,
        "lastHeartbeat": now,
        "status": normalize_active_status(status or existing.get("status"), fallback=ACTIVE_STATUS_RUNNING),
    }


def ensure_session(root: str, task_id: str, agent: str, executor: str) -> Dict[str, Any]:
    with _REGISTRY_LOCK:
        key = session_key(task_id, agent, executor)
        state = load_registry(root)
        sessions = state.setdefault("sessions", {})
        existing = sessions.get(key) if isinstance(sessions.get(key), dict) else {}
        created = not bool(existing)
        entry = _normalize_session_entry(existing, task_id, agent, executor)
        entry["lastActiveAt"] = now_iso()
        entry["status"] = SESSION_STATUS_ACTIVE
        sessions[key] = entry
        save_registry(root, state)
        return {"created": created, "key": key, "session": dict(entry)}


def upsert_active_session(
    root: str,
    task_id: str,
    worktree_path: str = "",
    pid: Any = 0,
    tmux_session: str = "",
    status: str = ACTIVE_STATUS_RUNNING,
) -> Dict[str, Any]:
    task_key = str(task_id or "").strip()
    if not task_key:
        return {"created": False, "taskId": "", "activeSession": {}}
    with _REGISTRY_LOCK:
        state = load_active_sessions(root)
        sessions = state.setdefault("sessions", {})
        existing = sessions.get(task_key) if isinstance(sessions.get(task_key), dict) else {}
        created = not bool(existing)
        row = _normalize_active_session_entry(
            existing,
            task_key,
            worktree_path=worktree_path,
            pid=pid,
            tmux_session=tmux_session,
            status=status,
        )
        sessions[task_key] = row
        save_active_sessions(root, state)
        return {"created": created, "taskId": task_key, "activeSession": dict(row)}


def heartbeat_active_session(
    root: str,
    task_id: str,
    pid: Any = 0,
    tmux_session: str = "",
    worktree_path: str = "",
) -> Dict[str, Any]:
    task_key = str(task_id or "").strip()
    if not task_key:
        return {"created": False, "taskId": "", "activeSession": {}}
    with _REGISTRY_LOCK:
        state = load_active_sessions(root)
        sessions = state.setdefault("sessions", {})
        existing = sessions.get(task_key) if isinstance(sessions.get(task_key), dict) else {}
        created = not bool(existing)
        row = _normalize_active_session_entry(
            existing,
            task_key,
            worktree_path=worktree_path,
            pid=pid,
            tmux_session=tmux_session,
            status=ACTIVE_STATUS_RUNNING,
        )
        sessions[task_key] = row
        save_active_sessions(root, state)
        return {"created": created, "taskId": task_key, "activeSession": dict(row)}


def mark_active_session_status(root: str, task_id: str, status: str) -> Dict[str, Any]:
    task_key = str(task_id or "").strip()
    if not task_key:
        return {"created": False, "taskId": "", "activeSession": {}}
    with _REGISTRY_LOCK:
        state = load_active_sessions(root)
        sessions = state.setdefault("sessions", {})
        existing = sessions.get(task_key) if isinstance(sessions.get(task_key), dict) else {}
        created = not bool(existing)
        row = _normalize_active_session_entry(
            existing,
            task_key,
            worktree_path="",
            pid=0,
            tmux_session="",
            status=normalize_active_status(status, fallback=ACTIVE_STATUS_RUNNING),
        )
        sessions[task_key] = row
        save_active_sessions(root, state)
        return {"created": created, "taskId": task_key, "activeSession": dict(row)}


def record_attempt(
    root: str,
    task_id: str,
    agent: str,
    executor: str,
    reason_code: str = "",
    detail: str = "",
) -> Dict[str, Any]:
    with _REGISTRY_LOCK:
        ensured = ensure_session(root, task_id, agent, executor)
        key = str(ensured.get("key") or session_key(task_id, agent, executor))
        state = load_registry(root)
        sessions = state.setdefault("sessions", {})
        entry = _normalize_session_entry(
            sessions.get(key) if isinstance(sessions.get(key), dict) else {},
            task_id,
            agent,
            executor,
        )
        entry["retryCount"] = safe_int(entry.get("retryCount"), 0) + 1
        entry["status"] = SESSION_STATUS_ACTIVE
        entry["lastActiveAt"] = now_iso()
        if reason_code:
            entry["lastReasonCode"] = str(reason_code)
        if detail:
            entry["lastDetail"] = str(detail)
        sessions[key] = entry
        save_registry(root, state)
        return {"created": bool(ensured.get("created")), "key": key, "session": dict(entry)}


def mark_failed(
    root: str,
    task_id: str,
    agent: str,
    executor: str,
    reason_code: str = "",
    detail: str = "",
) -> Dict[str, Any]:
    with _REGISTRY_LOCK:
        ensured = ensure_session(root, task_id, agent, executor)
        key = str(ensured.get("key") or session_key(task_id, agent, executor))
        state = load_registry(root)
        sessions = state.setdefault("sessions", {})
        entry = _normalize_session_entry(
            sessions.get(key) if isinstance(sessions.get(key), dict) else {},
            task_id,
            agent,
            executor,
        )
        entry["status"] = SESSION_STATUS_FAILED
        entry["lastActiveAt"] = now_iso()
        if reason_code:
            entry["lastReasonCode"] = str(reason_code)
        if detail:
            entry["lastDetail"] = str(detail)
        sessions[key] = entry
        save_registry(root, state)
        return {"created": bool(ensured.get("created")), "key": key, "session": dict(entry)}


def mark_done(root: str, task_id: str, agent: str, executor: str) -> Dict[str, Any]:
    with _REGISTRY_LOCK:
        ensured = ensure_session(root, task_id, agent, executor)
        key = str(ensured.get("key") or session_key(task_id, agent, executor))
        state = load_registry(root)
        sessions = state.setdefault("sessions", {})
        entry = _normalize_session_entry(
            sessions.get(key) if isinstance(sessions.get(key), dict) else {},
            task_id,
            agent,
            executor,
        )
        entry["status"] = SESSION_STATUS_DONE
        entry["lastActiveAt"] = now_iso()
        entry["lastReasonCode"] = "done"
        sessions[key] = entry
        save_registry(root, state)
        return {"created": bool(ensured.get("created")), "key": key, "session": dict(entry)}


def mark_task_done(root: str, task_id: str) -> Dict[str, Any]:
    task_key = str(task_id or "").strip()
    if not task_key:
        return {"taskId": "", "updated": 0, "keys": []}

    updated_keys = []
    with _REGISTRY_LOCK:
        state = load_registry(root)
        sessions = state.setdefault("sessions", {})
        now = now_iso()
        for key, raw in list(sessions.items()):
            if not isinstance(raw, dict):
                continue
            row_task_id = str(raw.get("taskId") or "").strip()
            if row_task_id != task_key:
                continue
            entry = _normalize_session_entry(
                raw,
                row_task_id,
                str(raw.get("agent") or ""),
                str(raw.get("executor") or ""),
            )
            entry["status"] = SESSION_STATUS_DONE
            entry["lastActiveAt"] = now
            entry["lastReasonCode"] = "done"
            sessions[key] = entry
            updated_keys.append(str(key))
        if updated_keys:
            save_registry(root, state)

    return {"taskId": task_key, "updated": len(updated_keys), "keys": updated_keys}


def build_session_metadata(payload: Dict[str, Any]) -> Dict[str, Any]:
    session = payload.get("session") if isinstance(payload.get("session"), dict) else payload
    if not isinstance(session, dict):
        return {}
    return {
        "taskId": str(session.get("taskId") or ""),
        "agent": normalize_token(session.get("agent"), fallback="unknown"),
        "executor": normalize_token(session.get("executor"), fallback="unknown"),
        "sessionId": str(session.get("sessionId") or ""),
        "createdAt": str(session.get("createdAt") or ""),
        "lastActiveAt": str(session.get("lastActiveAt") or ""),
        "status": str(session.get("status") or ""),
        "retryCount": safe_int(session.get("retryCount"), 0),
        "lastReasonCode": str(session.get("lastReasonCode") or ""),
        "lastDetail": str(session.get("lastDetail") or ""),
    }
