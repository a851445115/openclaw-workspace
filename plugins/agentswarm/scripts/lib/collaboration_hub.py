#!/usr/bin/env python3
import hashlib
import json
import logging
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - non-posix fallback
    fcntl = None


LOGGER = logging.getLogger(__name__)

COLLAB_MESSAGES_FILE = os.path.join("state", "collab.messages.jsonl")
COLLAB_THREADS_FILE = os.path.join("state", "collab.threads.json")
COLLAB_LOCK_FILE = os.path.join("state", "collab.state.lock")
COLLAB_APPEND_TRANSACTION_FILE = os.path.join("state", "collab.append.transaction.json")
COLLAB_POLICY_FILE = os.path.join("plugins", "agentswarm", "config", "collaboration-policy.json")
DEDUPE_SCOPE_VALUES = {"task", "task_thread", "task_thread_agent"}
APPEND_TRANSACTION_VERSION = 1

MESSAGE_TYPES = {"handoff", "consult", "question", "answer", "decision"}
REQUIRED_FIELDS = [
    "taskId",
    "threadId",
    "fromAgent",
    "toAgent",
    "messageType",
    "summary",
    "evidence",
    "request",
    "deadline",
    "createdAt",
]
ROUND_COUNTING_TYPES = {"question", "consult"}

DEFAULT_POLICY = {
    "enabled": True,
    "maxRoundsPerThread": 3,
    "questionDedupeEnabled": True,
    "questionDedupeScope": "task",
    "timeoutMinutes": 30,
    "visibilityMode": "handoff_visible",
}

_STATE_LOCK = threading.RLock()


class CollaborationStateError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _parse_iso(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _normalize_question_text(message: Dict[str, Any]) -> str:
    candidate = _normalize_text(message.get("request") or "")
    if candidate:
        return candidate.lower()
    return _normalize_text(message.get("summary") or "").lower()


def normalized_question_hash(message: Dict[str, Any]) -> str:
    normalized = _normalize_question_text(message)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def dedupe_key(task_id: str, message: Dict[str, Any], scope: str = "task") -> str:
    digest = normalized_question_hash(message)
    if not digest:
        return ""

    normalized_scope = str(scope or "").strip().lower()
    if normalized_scope not in DEDUPE_SCOPE_VALUES:
        normalized_scope = "task"

    segments = [str(task_id or "").strip()]
    if normalized_scope in {"task_thread", "task_thread_agent"}:
        segments.append(_normalize_text(message.get("threadId")))
    if normalized_scope == "task_thread_agent":
        segments.append(_normalize_text(message.get("fromAgent")))
        segments.append(_normalize_text(message.get("toAgent")))
    segments.append(digest)
    return "|".join(segments)


def messages_path(root: str) -> str:
    return os.path.join(root, COLLAB_MESSAGES_FILE)


def threads_path(root: str) -> str:
    return os.path.join(root, COLLAB_THREADS_FILE)


def lock_path(root: str) -> str:
    return os.path.join(root, COLLAB_LOCK_FILE)


def append_transaction_path(root: str) -> str:
    return os.path.join(root, COLLAB_APPEND_TRANSACTION_FILE)


def policy_path(root: str) -> str:
    return os.path.join(root, COLLAB_POLICY_FILE)


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
        LOGGER.exception("failed to persist collaboration json atomically: path=%s", path)
        raise
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _write_jsonl_atomic(path: str, rows: List[Dict[str, Any]]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=True, separators=(",", ":")))
                f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        LOGGER.exception("failed to persist collaboration jsonl atomically: path=%s", path)
        raise
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def load_policy(root: str, override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    policy: Dict[str, Any] = dict(DEFAULT_POLICY)

    loaded: Dict[str, Any] = {}
    if isinstance(override, dict):
        loaded = override
    else:
        path = policy_path(root)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    parsed = json.load(f)
                loaded = parsed if isinstance(parsed, dict) else {}
            except Exception:
                LOGGER.warning("failed to load collaboration policy: path=%s", path, exc_info=True)

    if loaded:
        policy["enabled"] = _coerce_bool(loaded.get("enabled"), policy["enabled"])
        policy["maxRoundsPerThread"] = max(1, _safe_int(loaded.get("maxRoundsPerThread"), policy["maxRoundsPerThread"]))
        policy["questionDedupeEnabled"] = _coerce_bool(
            loaded.get("questionDedupeEnabled"),
            policy["questionDedupeEnabled"],
        )
        dedupe_scope = str(loaded.get("questionDedupeScope") or "").strip().lower()
        if dedupe_scope in DEDUPE_SCOPE_VALUES:
            policy["questionDedupeScope"] = dedupe_scope
        policy["timeoutMinutes"] = max(0, _safe_int(loaded.get("timeoutMinutes"), policy["timeoutMinutes"]))
        visibility = str(loaded.get("visibilityMode") or "").strip()
        if visibility:
            policy["visibilityMode"] = visibility

    return policy


def validate_message(payload: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []

    if not isinstance(payload, dict):
        return {"ok": False, "errors": ["payload must be an object"]}

    for field in REQUIRED_FIELDS:
        if field not in payload:
            errors.append(f"missing required field: {field}")

    message_type = str(payload.get("messageType") or "").strip()
    if message_type and message_type not in MESSAGE_TYPES:
        errors.append(
            "invalid messageType: "
            f"{message_type} (expected one of {','.join(sorted(MESSAGE_TYPES))})"
        )

    evidence = payload.get("evidence")
    if "evidence" in payload and not isinstance(evidence, list):
        errors.append("field evidence must be a list")

    for field in REQUIRED_FIELDS:
        if field == "evidence" or field not in payload:
            continue
        value = payload.get(field)
        if str(value or "").strip() == "":
            errors.append(f"field {field} must be non-empty")

    if isinstance(evidence, list):
        for idx, item in enumerate(evidence):
            if str(item or "").strip() == "":
                errors.append(f"field evidence[{idx}] must be non-empty")

    return {"ok": not errors, "errors": errors}


def _normalize_message(payload: Dict[str, Any]) -> Dict[str, Any]:
    evidence_raw = payload.get("evidence") if isinstance(payload.get("evidence"), list) else []
    evidence: List[str] = []
    for item in evidence_raw:
        token = _normalize_text(item)
        if token and token not in evidence:
            evidence.append(token)

    message = {
        "taskId": _normalize_text(payload.get("taskId")),
        "threadId": _normalize_text(payload.get("threadId")),
        "fromAgent": _normalize_text(payload.get("fromAgent")),
        "toAgent": _normalize_text(payload.get("toAgent")),
        "messageType": _normalize_text(payload.get("messageType")).lower(),
        "summary": _normalize_text(payload.get("summary")),
        "evidence": evidence,
        "request": _normalize_text(payload.get("request")),
        "deadline": _normalize_text(payload.get("deadline")),
        "createdAt": _normalize_text(payload.get("createdAt")) or now_iso(),
    }
    q_hash = normalized_question_hash(message)
    if q_hash:
        message["normalizedQuestionHash"] = q_hash
    return message


def _default_threads_state() -> Dict[str, Any]:
    return {"threads": {}, "dedupeIndex": {}, "updatedAt": ""}


def _threads_state_unreadable(path: str, fail_closed: bool, reason: str, exc: Optional[Exception] = None):
    if fail_closed:
        if exc is not None:
            raise CollaborationStateError(f"threads_state_unreadable:{reason}") from exc
        raise CollaborationStateError(f"threads_state_unreadable:{reason}")
    if exc is not None:
        LOGGER.warning("failed to load collaboration thread state: path=%s", path, exc_info=True)
    return _default_threads_state()


def _load_threads_state(root: str, fail_closed: bool = False) -> Dict[str, Any]:
    path = threads_path(root)
    if not os.path.exists(path):
        return _default_threads_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except Exception as exc:
        return _threads_state_unreadable(path, fail_closed, "parse_failed", exc)

    if not isinstance(loaded, dict):
        return _threads_state_unreadable(path, fail_closed, "non_object")

    raw_threads = loaded.get("threads")
    raw_dedupe = loaded.get("dedupeIndex")
    if not isinstance(raw_threads, dict) or not isinstance(raw_dedupe, dict):
        return _threads_state_unreadable(path, fail_closed, "invalid_shape")

    return {
        "threads": raw_threads,
        "dedupeIndex": raw_dedupe,
        "updatedAt": str(loaded.get("updatedAt") or ""),
    }


def _save_threads_state(root: str, state: Dict[str, Any]) -> None:
    payload = {
        "threads": state.get("threads") if isinstance(state.get("threads"), dict) else {},
        "dedupeIndex": state.get("dedupeIndex") if isinstance(state.get("dedupeIndex"), dict) else {},
        "updatedAt": now_iso(),
    }
    _write_json_atomic(threads_path(root), payload)


def _messages_state_unreadable(path: str, fail_closed: bool, reason: str, exc: Optional[Exception] = None):
    if fail_closed:
        if exc is not None:
            raise CollaborationStateError(f"messages_state_unreadable:{reason}") from exc
        raise CollaborationStateError(f"messages_state_unreadable:{reason}")
    if exc is not None:
        LOGGER.warning("failed to load collaboration messages: path=%s", path, exc_info=True)
    else:
        LOGGER.warning("failed to load collaboration messages: path=%s reason=%s", path, reason)
    return []


def _load_messages(root: str, fail_closed: bool = False, strict: bool = False) -> List[Dict[str, Any]]:
    path = messages_path(root)
    if not os.path.exists(path):
        return []
    out: List[Dict[str, Any]] = []
    strict_mode = strict or fail_closed
    try:
        with open(path, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f, start=1):
                token = line.strip()
                if not token:
                    continue
                try:
                    row = json.loads(token)
                except Exception as exc:
                    if strict_mode:
                        return _messages_state_unreadable(path, fail_closed, f"line_{idx}_parse_failed", exc)
                    LOGGER.warning(
                        "skip malformed collaboration message row: path=%s line=%s",
                        path,
                        idx,
                    )
                    continue
                if not isinstance(row, dict):
                    if strict_mode:
                        return _messages_state_unreadable(path, fail_closed, f"line_{idx}_non_object")
                    LOGGER.warning(
                        "skip non-object collaboration message row: path=%s line=%s",
                        path,
                        idx,
                    )
                    continue
                out.append(row)
    except CollaborationStateError:
        raise
    except Exception as exc:
        return _messages_state_unreadable(path, fail_closed, "read_failed", exc)
    return out


def _save_messages(root: str, rows: List[Dict[str, Any]]) -> None:
    _write_jsonl_atomic(messages_path(root), rows)


def _remove_file(path: str) -> None:
    if not os.path.exists(path):
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        return


@contextmanager
def _cross_process_state_lock(root: str):
    if fcntl is None:
        raise CollaborationStateError("cross_process_lock_unavailable")
    path = lock_path(root)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except OSError as exc:  # pragma: no cover - OS-level failure
            raise CollaborationStateError("cross_process_lock_failed") from exc
        try:
            yield
        finally:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                LOGGER.warning("failed to release collaboration state lock: path=%s", path, exc_info=True)


def _message_fingerprint(message: Dict[str, Any]) -> str:
    return json.dumps(message, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _append_unique_message(rows: List[Dict[str, Any]], message: Dict[str, Any]) -> bool:
    fingerprint = _message_fingerprint(message)
    for row in rows:
        if isinstance(row, dict) and _message_fingerprint(row) == fingerprint:
            return False
    rows.append(message)
    return True


def _rebuild_threads_state(rows: List[Dict[str, Any]], dedupe_scope: str) -> Dict[str, Any]:
    normalized_scope = str(dedupe_scope or "").strip().lower()
    if normalized_scope not in DEDUPE_SCOPE_VALUES:
        normalized_scope = "task"

    state = _default_threads_state()
    threads = state["threads"]
    dedupe_index = state["dedupeIndex"]

    for row in rows:
        if not isinstance(row, dict):
            continue
        thread_id = _normalize_text(row.get("threadId"))
        if not thread_id:
            continue
        existing = threads.get(thread_id) if isinstance(threads.get(thread_id), dict) else {}
        normalized_row = _normalize_message(row)
        threads[thread_id] = _normalize_thread_entry(existing, normalized_row)
        if normalized_row.get("messageType") in ROUND_COUNTING_TYPES:
            token = dedupe_key(str(normalized_row.get("taskId") or ""), normalized_row, scope=normalized_scope)
            if token:
                dedupe_index[token] = thread_id

    state["updatedAt"] = now_iso()
    return state


def _write_append_transaction(root: str, message: Dict[str, Any], dedupe_scope: str) -> None:
    payload = {
        "version": APPEND_TRANSACTION_VERSION,
        "type": "append_message",
        "status": "prepared",
        "createdAt": now_iso(),
        "dedupeScope": dedupe_scope if dedupe_scope in DEDUPE_SCOPE_VALUES else "task",
        "message": message,
    }
    _write_json_atomic(append_transaction_path(root), payload)


def _load_append_transaction(root: str) -> Optional[Dict[str, Any]]:
    path = append_transaction_path(root)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
        raise CollaborationStateError("append_transaction_unreadable") from exc

    if not isinstance(payload, dict):
        raise CollaborationStateError("append_transaction_unreadable")

    if payload.get("type") != "append_message" or payload.get("status") != "prepared":
        raise CollaborationStateError("append_transaction_invalid")

    message = payload.get("message")
    if not isinstance(message, dict):
        raise CollaborationStateError("append_transaction_invalid")

    dedupe_scope = str(payload.get("dedupeScope") or "").strip().lower()
    if dedupe_scope not in DEDUPE_SCOPE_VALUES:
        dedupe_scope = "task"

    return {
        "message": _normalize_message(message),
        "dedupeScope": dedupe_scope,
    }


def _recover_append_transaction_if_needed(root: str) -> bool:
    transaction = _load_append_transaction(root)
    if transaction is None:
        return False

    message = transaction["message"]
    dedupe_scope = transaction["dedupeScope"]
    rows = _load_messages(root, fail_closed=True, strict=True)
    if _append_unique_message(rows, message):
        _save_messages(root, rows)
    rebuilt_state = _rebuild_threads_state(rows, dedupe_scope)
    _save_threads_state(root, rebuilt_state)
    _remove_file(append_transaction_path(root))
    return True


def _state_error_reason(exc: Exception, fallback: str) -> str:
    if isinstance(exc, CollaborationStateError):
        reason = str(exc).split(":", 1)[0].strip()
        if reason:
            return reason
    return fallback


def _normalize_thread_entry(raw: Dict[str, Any], message: Dict[str, Any]) -> Dict[str, Any]:
    participants_raw = raw.get("participants") if isinstance(raw.get("participants"), list) else []
    participants: List[str] = []
    for item in participants_raw:
        token = _normalize_text(item)
        if token and token not in participants:
            participants.append(token)

    from_agent = _normalize_text(message.get("fromAgent"))
    to_agent = _normalize_text(message.get("toAgent"))
    if from_agent and from_agent not in participants:
        participants.append(from_agent)
    if to_agent and to_agent not in participants:
        participants.append(to_agent)

    rounds = max(0, _safe_int(raw.get("rounds"), 0))
    if str(message.get("messageType") or "") in ROUND_COUNTING_TYPES:
        rounds += 1

    status = _normalize_text(raw.get("status")) or "active"
    if str(message.get("messageType") or "") == "decision":
        status = "decided"

    message_count = max(0, _safe_int(raw.get("messageCount"), 0)) + 1

    return {
        "threadId": _normalize_text(message.get("threadId")),
        "taskId": _normalize_text(message.get("taskId")),
        "participants": participants,
        "lastMessageAt": _normalize_text(message.get("createdAt")) or now_iso(),
        "status": status,
        "rounds": rounds,
        "messageCount": message_count,
        "updatedAt": now_iso(),
    }


def append_message(root: str, payload: Dict[str, Any], policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    validation = validate_message(payload)
    if not validation.get("ok"):
        return {"ok": False, "errors": validation.get("errors") or []}

    with _STATE_LOCK:
        with _cross_process_state_lock(root):
            recovery_applied = False
            try:
                recovery_applied = _recover_append_transaction_if_needed(root)
            except Exception as exc:
                LOGGER.warning("failed to recover pending collaboration append transaction", exc_info=True)
                return {"ok": False, "reason": _state_error_reason(exc, "state_recovery_failed"), "error": str(exc)}

            effective_policy = load_policy(root, override=policy)
            if not effective_policy.get("enabled", True):
                response = {"ok": False, "reason": "collaboration_disabled", "policy": effective_policy}
                if recovery_applied:
                    response["recovery"] = "applied_pending_transaction"
                return response

            message = _normalize_message(payload)
            dedupe_scope = str(effective_policy.get("questionDedupeScope") or "").strip().lower()
            if dedupe_scope not in DEDUPE_SCOPE_VALUES:
                dedupe_scope = "task"

            try:
                threads_state = _load_threads_state(root, fail_closed=True)
            except CollaborationStateError as exc:
                LOGGER.warning("collaboration append blocked by unreadable threads state", exc_info=True)
                return {"ok": False, "reason": _state_error_reason(exc, "threads_state_unreadable"), "error": str(exc)}

            dedupe_index = threads_state.setdefault("dedupeIndex", {})
            dedupe_token = ""
            if (
                effective_policy.get("questionDedupeEnabled", True)
                and message.get("messageType") in ROUND_COUNTING_TYPES
            ):
                dedupe_token = dedupe_key(str(message.get("taskId") or ""), message, scope=dedupe_scope)
                if dedupe_token:
                    owner_thread_id = str(dedupe_index.get(dedupe_token) or "")
                    if owner_thread_id:
                        response = {
                            "ok": False,
                            "reason": "duplicate_question",
                            "dedupeKey": dedupe_token,
                            "threadId": owner_thread_id,
                        }
                        if recovery_applied:
                            response["recovery"] = "applied_pending_transaction"
                        return response

            try:
                rows = _load_messages(root, fail_closed=True, strict=True)
            except CollaborationStateError as exc:
                LOGGER.warning("collaboration append blocked by unreadable messages state", exc_info=True)
                return {"ok": False, "reason": _state_error_reason(exc, "messages_state_unreadable"), "error": str(exc)}
            rows.append(message)
            _write_append_transaction(root, message, dedupe_scope)
            _save_messages(root, rows)

            threads = threads_state.setdefault("threads", {})
            thread_id = str(message.get("threadId") or "")
            existing = threads.get(thread_id) if isinstance(threads.get(thread_id), dict) else {}
            thread = _normalize_thread_entry(existing, message)
            threads[thread_id] = thread
            if dedupe_token:
                dedupe_index[dedupe_token] = thread_id

            _save_threads_state(root, threads_state)
            _remove_file(append_transaction_path(root))

            response = {
                "ok": True,
                "message": message,
                "thread": dict(thread),
                "dedupeKey": dedupe_token,
                "policy": effective_policy,
            }
            if recovery_applied:
                response["recovery"] = "applied_pending_transaction"
            return response


def get_thread(root: str, thread_id: str) -> Dict[str, Any]:
    thread_key = _normalize_text(thread_id)
    if not thread_key:
        return {}
    with _STATE_LOCK:
        with _cross_process_state_lock(root):
            state_warning = ""
            try:
                _recover_append_transaction_if_needed(root)
            except Exception as exc:
                LOGGER.warning("failed to recover state before reading thread", exc_info=True)
                state_warning = _state_error_reason(exc, "state_recovery_failed")
            state = _load_threads_state(root)
            threads = state.get("threads") if isinstance(state.get("threads"), dict) else {}
            row = threads.get(thread_key)
            out = dict(row) if isinstance(row, dict) else {}
            if state_warning:
                out["stateWarning"] = state_warning
            return out


def list_thread_messages(root: str, thread_id: str, limit: int = 0) -> List[Dict[str, Any]]:
    thread_key = _normalize_text(thread_id)
    if not thread_key:
        return []
    with _STATE_LOCK:
        with _cross_process_state_lock(root):
            state_warning = ""
            try:
                _recover_append_transaction_if_needed(root)
            except Exception as exc:
                LOGGER.warning("failed to recover state before listing thread messages", exc_info=True)
                state_warning = _state_error_reason(exc, "state_recovery_failed")
            rows = [row for row in _load_messages(root) if str(row.get("threadId") or "") == thread_key]
            if state_warning and rows:
                rows = [dict(row, stateWarning=state_warning) if isinstance(row, dict) else row for row in rows]
    if limit > 0:
        return rows[-limit:]
    return rows


def summarize_thread(root: str, thread_id: str) -> Dict[str, Any]:
    thread_key = _normalize_text(thread_id)
    if not thread_key:
        return {
            "threadId": "",
            "messageCount": 0,
            "participants": [],
            "lastMessageAt": "",
            "status": "missing",
            "rounds": 0,
        }

    thread = get_thread(root, thread_key)
    rows = list_thread_messages(root, thread_key)

    if not thread:
        return {
            "threadId": thread_key,
            "messageCount": len(rows),
            "participants": [],
            "lastMessageAt": "",
            "status": "missing",
            "rounds": 0,
        }

    by_type: Dict[str, int] = {}
    for row in rows:
        message_type = str(row.get("messageType") or "")
        if not message_type:
            continue
        by_type[message_type] = by_type.get(message_type, 0) + 1

    return {
        "threadId": thread_key,
        "taskId": str(thread.get("taskId") or ""),
        "participants": list(thread.get("participants") or []),
        "lastMessageAt": str(thread.get("lastMessageAt") or ""),
        "status": str(thread.get("status") or "active"),
        "rounds": max(0, _safe_int(thread.get("rounds"), 0)),
        "messageCount": len(rows),
        "messageTypes": by_type,
    }


def should_escalate_round_limit(thread: Dict[str, Any], max_rounds: Any) -> bool:
    if not isinstance(thread, dict):
        return False
    cap = _safe_int(max_rounds, 0)
    if cap <= 0:
        return False
    rounds = max(0, _safe_int(thread.get("rounds"), 0))
    return rounds >= cap


def should_escalate_timeout(thread: Dict[str, Any], timeout_minutes: Any, now_iso_value: str = "") -> bool:
    if not isinstance(thread, dict):
        return False

    timeout = _safe_int(timeout_minutes, 0)
    if timeout <= 0:
        return False

    last_message_at = _parse_iso(thread.get("lastMessageAt"))
    if last_message_at is None:
        return False

    now_time = _parse_iso(now_iso_value) if now_iso_value else datetime.now(timezone.utc)
    if now_time is None:
        return False

    threshold = last_message_at + timedelta(minutes=timeout)
    return now_time >= threshold
