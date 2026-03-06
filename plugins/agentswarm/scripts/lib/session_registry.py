#!/usr/bin/env python3
import errno
import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional


SESSION_STATE_FILE = os.path.join("state", "worker-sessions.json")
ACTIVE_SESSION_STATE_FILE = os.path.join("state", "active-sessions.json")
ACTIVE_SESSION_POLICY_CONFIG_CANDIDATES = [
    os.path.join("config", "active-session-policy.json"),
    os.path.join("state", "active-session-policy.json"),
]
SESSION_STATUS_ACTIVE = "active"
SESSION_STATUS_FAILED = "failed"
SESSION_STATUS_DONE = "done"
ACTIVE_STATUS_RUNNING = "running"
ACTIVE_STATUS_FAILED = "failed"
ACTIVE_STATUS_DONE = "done"
ACTIVE_STATUS_STOPPED = "stopped"
ACTIVE_STATUS_BLOCKED = "blocked"
ACTIVE_STOP_REASON_STALE_PID = "stale_pid"
ACTIVE_STOP_REASON_HEARTBEAT_TIMEOUT = "heartbeat_timeout"
ACTIVE_STATUS_SET = {
    ACTIVE_STATUS_RUNNING,
    ACTIVE_STATUS_FAILED,
    ACTIVE_STATUS_DONE,
    ACTIVE_STATUS_STOPPED,
    ACTIVE_STATUS_BLOCKED,
}
DEFAULT_ACTIVE_SESSION_POLICY = {
    "heartbeatTimeoutSec": 300,
    "stalePidStatus": ACTIVE_STATUS_BLOCKED,
    "heartbeatTimeoutStatus": ACTIVE_STATUS_BLOCKED,
}
LOGGER = logging.getLogger(__name__)
_REGISTRY_LOCK = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ts_to_iso(ts: int) -> str:
    return datetime.fromtimestamp(max(0, int(ts)), tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed >= 0 else default


def normalize_token(value: Any, fallback: str = "") -> str:
    token = str(value or "").strip().lower()
    return token or fallback


def _iso_to_ts(value: Any, default: int = 0) -> int:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return int(datetime.fromisoformat(text).timestamp())
    except Exception:
        return default


def session_state_path(root: str) -> str:
    return os.path.join(root, SESSION_STATE_FILE)


def active_session_state_path(root: str) -> str:
    return os.path.join(root, ACTIVE_SESSION_STATE_FILE)


def _load_json_dict_or_empty(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


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


def normalize_terminal_active_status(value: Any, fallback: str = ACTIVE_STATUS_BLOCKED) -> str:
    status = normalize_active_status(value, fallback=fallback)
    return fallback if status == ACTIVE_STATUS_RUNNING else status


def _safe_pid(value: Any, default: int = 0) -> int:
    pid = safe_int(value, default=default)
    return pid if pid > 0 else 0


def _pid_exists(pid: Any) -> bool:
    safe_pid = _safe_pid(pid, 0)
    if safe_pid <= 0:
        return False
    try:
        os.kill(safe_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as err:
        if err.errno == errno.ESRCH:
            return False
        if err.errno == errno.EPERM:
            return True
        return True
    except Exception:
        return True
    return True


def load_active_session_policy(root: str) -> Dict[str, Any]:
    script_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    policy = dict(DEFAULT_ACTIVE_SESSION_POLICY)

    for base in [script_root, root]:
        for rel in ACTIVE_SESSION_POLICY_CONFIG_CANDIDATES:
            path = os.path.join(base, rel)
            if not os.path.exists(path):
                continue
            loaded = _load_json_dict_or_empty(path)
            if not loaded:
                continue
            timeout_sec = safe_int(loaded.get("heartbeatTimeoutSec"), policy["heartbeatTimeoutSec"])
            policy["heartbeatTimeoutSec"] = max(0, timeout_sec)
            policy["stalePidStatus"] = normalize_terminal_active_status(
                loaded.get("stalePidStatus"),
                fallback=str(policy.get("stalePidStatus") or ACTIVE_STATUS_BLOCKED),
            )
            policy["heartbeatTimeoutStatus"] = normalize_terminal_active_status(
                loaded.get("heartbeatTimeoutStatus"),
                fallback=str(policy.get("heartbeatTimeoutStatus") or ACTIVE_STATUS_BLOCKED),
            )

    return {
        "heartbeatTimeoutSec": max(0, safe_int(policy.get("heartbeatTimeoutSec"), 300)),
        "stalePidStatus": normalize_terminal_active_status(
            policy.get("stalePidStatus"),
            fallback=ACTIVE_STATUS_BLOCKED,
        ),
        "heartbeatTimeoutStatus": normalize_terminal_active_status(
            policy.get("heartbeatTimeoutStatus"),
            fallback=ACTIVE_STATUS_BLOCKED,
        ),
    }


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
    touch_heartbeat: bool = True,
    now_value: str = "",
) -> Dict[str, Any]:
    now = str(now_value or now_iso())
    existing = entry if isinstance(entry, dict) else {}
    start_time = str(existing.get("startTime") or now)
    normalized_status = normalize_active_status(status or existing.get("status"), fallback=ACTIVE_STATUS_RUNNING)
    last_heartbeat = str(existing.get("lastHeartbeat") or now)
    if touch_heartbeat:
        last_heartbeat = now
    ended_at = str(existing.get("endedAt") or "")
    stop_reason = str(existing.get("stopReason") or "")
    stop_detail = str(existing.get("stopDetail") or "")
    watchdog_at = str(existing.get("watchdogAt") or "")
    if normalized_status == ACTIVE_STATUS_RUNNING:
        ended_at = ""
        stop_reason = ""
        stop_detail = ""
        watchdog_at = ""
    return {
        "taskId": str(task_id or "").strip(),
        "worktreePath": str(worktree_path or existing.get("worktreePath") or ""),
        "pid": _safe_pid(pid if pid else existing.get("pid"), 0),
        "tmuxSession": str(tmux_session or existing.get("tmuxSession") or ""),
        "startTime": start_time,
        "lastHeartbeat": last_heartbeat,
        "status": normalized_status,
        "endedAt": ended_at,
        "stopReason": stop_reason,
        "stopDetail": stop_detail,
        "watchdogAt": watchdog_at,
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
        normalized_status = normalize_active_status(status, fallback=ACTIVE_STATUS_RUNNING)
        now_value = now_iso()
        row = _normalize_active_session_entry(
            existing,
            task_key,
            worktree_path="",
            pid=0,
            tmux_session="",
            status=normalized_status,
            touch_heartbeat=normalized_status == ACTIVE_STATUS_RUNNING,
            now_value=now_value,
        )
        if normalized_status != ACTIVE_STATUS_RUNNING:
            row["endedAt"] = str(row.get("endedAt") or now_value)
            row["stopReason"] = str(row.get("stopReason") or normalized_status)
        sessions[task_key] = row
        save_active_sessions(root, state)
        return {"created": created, "taskId": task_key, "activeSession": dict(row)}


def run_active_session_watchdog(
    root: str,
    now_ts: Optional[int] = None,
    pid_exists: Optional[Callable[[int], bool]] = None,
) -> Dict[str, Any]:
    current_ts = int(time.time()) if now_ts is None else int(now_ts)
    current_at = ts_to_iso(current_ts)
    policy = load_active_session_policy(root)
    timeout_sec = max(0, safe_int(policy.get("heartbeatTimeoutSec"), 300))
    pid_check = pid_exists or _pid_exists
    summary: Dict[str, Any] = {
        "ok": True,
        "checked": 0,
        "updated": 0,
        "stalePid": 0,
        "heartbeatTimeout": 0,
        "events": [],
        "policy": dict(policy),
    }

    with _REGISTRY_LOCK:
        state = load_active_sessions(root)
        sessions = state.setdefault("sessions", {})
        changed = False

        for task_key, raw in list(sessions.items()):
            if not isinstance(raw, dict):
                continue
            row = _normalize_active_session_entry(
                raw,
                task_key,
                worktree_path=str(raw.get("worktreePath") or ""),
                pid=raw.get("pid"),
                tmux_session=str(raw.get("tmuxSession") or ""),
                status=raw.get("status") or ACTIVE_STATUS_RUNNING,
                touch_heartbeat=False,
                now_value=current_at,
            )
            sessions[task_key] = row
            if row.get("status") != ACTIVE_STATUS_RUNNING:
                continue

            summary["checked"] = int(summary.get("checked") or 0) + 1
            pid = _safe_pid(row.get("pid"), 0)
            heartbeat_ts = _iso_to_ts(row.get("lastHeartbeat"), _iso_to_ts(row.get("startTime"), 0))
            heartbeat_age_sec = max(0, current_ts - heartbeat_ts) if heartbeat_ts > 0 else 0
            reason = ""
            detail = ""
            next_status = ""

            if pid > 0 and not bool(pid_check(pid)):
                reason = ACTIVE_STOP_REASON_STALE_PID
                detail = f"pid {pid} is not alive while active session remains running"
                next_status = str(policy.get("stalePidStatus") or ACTIVE_STATUS_BLOCKED)
                summary["stalePid"] = int(summary.get("stalePid") or 0) + 1
            elif timeout_sec > 0 and heartbeat_ts > 0 and heartbeat_age_sec > timeout_sec:
                reason = ACTIVE_STOP_REASON_HEARTBEAT_TIMEOUT
                detail = f"heartbeat idle for {heartbeat_age_sec}s (timeout {timeout_sec}s)"
                next_status = str(policy.get("heartbeatTimeoutStatus") or ACTIVE_STATUS_BLOCKED)
                summary["heartbeatTimeout"] = int(summary.get("heartbeatTimeout") or 0) + 1

            if not reason:
                continue

            terminal_status = normalize_terminal_active_status(next_status, fallback=ACTIVE_STATUS_BLOCKED)
            updated_row = _normalize_active_session_entry(
                row,
                task_key,
                worktree_path=str(row.get("worktreePath") or ""),
                pid=row.get("pid"),
                tmux_session=str(row.get("tmuxSession") or ""),
                status=terminal_status,
                touch_heartbeat=False,
                now_value=current_at,
            )
            updated_row["endedAt"] = current_at
            updated_row["stopReason"] = reason
            updated_row["stopDetail"] = detail
            updated_row["watchdogAt"] = current_at
            sessions[task_key] = updated_row
            changed = True
            summary["updated"] = int(summary.get("updated") or 0) + 1
            cast_events = summary.setdefault("events", [])
            if isinstance(cast_events, list):
                cast_events.append(
                    {
                        "taskId": task_key,
                        "status": terminal_status,
                        "reason": reason,
                        "detail": detail,
                        "pid": pid,
                        "worktreePath": str(updated_row.get("worktreePath") or ""),
                        "heartbeatAgeSec": heartbeat_age_sec,
                        "heartbeatTimeoutSec": timeout_sec,
                        "watchdogAt": current_at,
                    }
                )

        if changed:
            save_active_sessions(root, state)

    return summary


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
