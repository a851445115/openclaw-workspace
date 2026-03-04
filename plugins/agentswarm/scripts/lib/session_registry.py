#!/usr/bin/env python3
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict


SESSION_STATE_FILE = os.path.join("state", "worker-sessions.json")
SESSION_STATUS_ACTIVE = "active"
SESSION_STATUS_FAILED = "failed"
SESSION_STATUS_DONE = "done"


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


def load_registry(root: str) -> Dict[str, Any]:
    path = session_state_path(root)
    if not os.path.exists(path):
        return {"sessions": {}, "updatedAt": ""}
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except Exception:
        return {"sessions": {}, "updatedAt": ""}
    sessions = loaded.get("sessions") if isinstance(loaded.get("sessions"), dict) else {}
    return {"sessions": sessions, "updatedAt": str(loaded.get("updatedAt") or "")}


def save_registry(root: str, state: Dict[str, Any]) -> None:
    path = session_state_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "sessions": state.get("sessions") if isinstance(state.get("sessions"), dict) else {},
        "updatedAt": now_iso(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=True, indent=2)
        f.write("\n")


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


def ensure_session(root: str, task_id: str, agent: str, executor: str) -> Dict[str, Any]:
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


def record_attempt(
    root: str,
    task_id: str,
    agent: str,
    executor: str,
    reason_code: str = "",
    detail: str = "",
) -> Dict[str, Any]:
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
