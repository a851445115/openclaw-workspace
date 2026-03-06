#!/usr/bin/env python3
from contextlib import contextmanager
import errno
import json
import logging
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

SCRIPT_LIB_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_LIB_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_LIB_DIR)
import failure_classifier

try:
    import fcntl
except Exception:  # pragma: no cover
    fcntl = None


RECOVERY_POLICY_CONFIG_CANDIDATES = (
    os.path.join("config", "recovery-policy.json"),
    os.path.join("state", "recovery-policy.json"),
)
RECOVERY_STATE_FILE = os.path.join("state", "recovery.state.json")
RECOVERY_STATE_LOCK_FILE = os.path.join("state", "recovery.state.lock")
RECOVERY_REASON_CODES = {"spawn_failed", "incomplete_output", "blocked_signal", "no_completion_signal", "budget_exceeded"}
DEFAULT_RECOVERY_POLICY: Dict[str, Any] = {
    "recoveryChain": ["coder", "debugger", "invest-analyst", "human"],
    "default": {"maxAttempts": 2, "cooldownSec": 180},
    "reasonPolicies": {
        "spawn_failed": {"maxAttempts": 2, "cooldownSec": 180},
        "incomplete_output": {"maxAttempts": 2, "cooldownSec": 120},
        "blocked_signal": {"maxAttempts": 2, "cooldownSec": 180},
        "no_completion_signal": {"maxAttempts": 2, "cooldownSec": 120},
    },
}
LOGGER = logging.getLogger(__name__)
_RECOVERY_STATE_LOCK = threading.RLock()
STRICT_FILE_LOCK_ENV = "STRICT_FILE_LOCK"
LOCK_TIMEOUT_ENV = "RECOVERY_STATE_LOCK_TIMEOUT_SEC"
LOCK_RETRY_ENV = "RECOVERY_STATE_LOCK_RETRY_SEC"
DEFAULT_LOCK_TIMEOUT_SEC = 5.0
DEFAULT_LOCK_RETRY_SEC = 0.05


class RecoveryStateLockError(RuntimeError):
    pass


class RecoveryStateLoadError(RuntimeError):
    pass


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        parsed = float(raw)
    except Exception:
        return default
    return parsed if parsed > 0 else default


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ts_to_iso(ts: int) -> str:
    return datetime.fromtimestamp(max(0, int(ts)), tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_int(value: Any, default: int) -> int:
    try:
        out = int(value)
    except Exception:
        return default
    return out if out >= 0 else default


def normalize_chain(chain_value: Any) -> List[str]:
    out: List[str] = []
    if isinstance(chain_value, list):
        for item in chain_value:
            role = str(item or "").strip().lower()
            if role and role not in out:
                out.append(role)
    if "human" not in out:
        out.append("human")
    if not out:
        out = ["coder", "debugger", "invest-analyst", "human"]
    return out


def merge_recovery_policy(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {
        "recoveryChain": list(base.get("recoveryChain") or []),
        "default": dict(base.get("default") or {}),
        "reasonPolicies": {
            k: dict(v)
            for k, v in (base.get("reasonPolicies") or {}).items()
            if isinstance(k, str) and isinstance(v, dict)
        },
    }
    if not isinstance(override, dict):
        return merged

    override_chain = override.get("recoveryChain")
    if isinstance(override_chain, list):
        merged["recoveryChain"] = normalize_chain(override_chain)

    override_default = override.get("default")
    if isinstance(override_default, dict):
        merged["default"].update(override_default)

    reason_policies = override.get("reasonPolicies")
    if isinstance(reason_policies, dict):
        for reason, conf in reason_policies.items():
            if not isinstance(reason, str) or not isinstance(conf, dict):
                continue
            reason_key = reason.strip().lower()
            current = dict(merged["reasonPolicies"].get(reason_key) or {})
            current.update(conf)
            merged["reasonPolicies"][reason_key] = current

    return merged


def load_recovery_policy(root: str) -> Dict[str, Any]:
    script_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    search_roots = [script_root, root]
    policy = DEFAULT_RECOVERY_POLICY

    for base in search_roots:
        for rel in RECOVERY_POLICY_CONFIG_CANDIDATES:
            path = os.path.join(base, rel)
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    policy = merge_recovery_policy(policy, loaded)
            except Exception:
                continue

    chain = normalize_chain(policy.get("recoveryChain"))
    default_conf = policy.get("default") if isinstance(policy.get("default"), dict) else {}
    reasons = policy.get("reasonPolicies") if isinstance(policy.get("reasonPolicies"), dict) else {}
    normalized_reasons: Dict[str, Dict[str, int]] = {}

    for reason in RECOVERY_REASON_CODES:
        raw = reasons.get(reason) if isinstance(reasons.get(reason), dict) else {}
        max_attempts = safe_int(raw.get("maxAttempts"), safe_int(default_conf.get("maxAttempts"), 2))
        cooldown_sec = safe_int(raw.get("cooldownSec"), safe_int(default_conf.get("cooldownSec"), 180))
        normalized_reasons[reason] = {
            "maxAttempts": max(1, max_attempts),
            "cooldownSec": max(0, cooldown_sec),
        }

    return {
        "recoveryChain": chain,
        "default": {
            "maxAttempts": max(1, safe_int(default_conf.get("maxAttempts"), 2)),
            "cooldownSec": max(0, safe_int(default_conf.get("cooldownSec"), 180)),
        },
        "reasonPolicies": normalized_reasons,
    }


def recovery_state_path(root: str) -> str:
    return os.path.join(root, RECOVERY_STATE_FILE)


def recovery_state_lock_path(root: str) -> str:
    return os.path.join(root, RECOVERY_STATE_LOCK_FILE)


def _empty_recovery_state() -> Dict[str, Any]:
    return {"entries": {}, "updatedAt": ""}


def _normalize_state_payload(state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "entries": state.get("entries") if isinstance(state.get("entries"), dict) else {},
        "updatedAt": now_iso(),
    }


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
        LOGGER.exception("failed to persist recovery state atomically: path=%s", path)
        raise
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                LOGGER.warning("failed to remove recovery state tmp file: path=%s", tmp_path, exc_info=True)


@contextmanager
def _recovery_state_guard(root: str, require_lock: bool = False):
    lock_path = recovery_state_lock_path(root)
    strict_file_lock = _env_truthy(STRICT_FILE_LOCK_ENV, default=False)
    lock_timeout_sec = _env_float(LOCK_TIMEOUT_ENV, DEFAULT_LOCK_TIMEOUT_SEC)
    lock_retry_sec = _env_float(LOCK_RETRY_ENV, DEFAULT_LOCK_RETRY_SEC)
    lock_fp = None
    lock_acquired = False
    with _RECOVERY_STATE_LOCK:
        if fcntl is None:
            if require_lock:
                message = (
                    f"failed to acquire recovery state lock: root={root} lock={lock_path} "
                    f"(fcntl unavailable; write path requires file lock; "
                    f"{STRICT_FILE_LOCK_ENV}={str(strict_file_lock).lower()})"
                )
                LOGGER.error(message)
                raise RecoveryStateLockError(message)
            if strict_file_lock:
                message = (
                    f"failed to acquire recovery state lock: root={root} lock={lock_path} "
                    f"(fcntl unavailable; non-write path requires file lock because "
                    f"{STRICT_FILE_LOCK_ENV}=true)"
                )
                LOGGER.error(message)
                raise RecoveryStateLockError(message)
        else:
            try:
                os.makedirs(os.path.dirname(lock_path), exist_ok=True)
                lock_fp = open(lock_path, "a+", encoding="utf-8")
                started_at = time.monotonic()
                while True:
                    try:
                        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        lock_acquired = True
                        break
                    except OSError as err:
                        if err.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                            raise
                        waited_sec = max(0.0, time.monotonic() - started_at)
                        if waited_sec >= lock_timeout_sec:
                            message = (
                                f"timed out waiting {waited_sec:.3f}s for recovery state lock: "
                                f"root={root} lock={lock_path}"
                            )
                            LOGGER.error(message)
                            if require_lock:
                                raise RecoveryStateLockError(message) from err
                            break
                        time.sleep(lock_retry_sec)
            except Exception as err:
                LOGGER.exception("failed to acquire recovery state lock: root=%s lock=%s", root, lock_path)
                if lock_fp is not None:
                    try:
                        lock_fp.close()
                    except Exception:
                        LOGGER.warning("failed to close lock file after acquire error: lock=%s", lock_path, exc_info=True)
                    lock_fp = None
                if require_lock:
                    raise RecoveryStateLockError(
                        f"failed to acquire recovery state lock: root={root} lock={lock_path}"
                    ) from err
        try:
            yield
        finally:
            if lock_fp is not None:
                if lock_acquired:
                    try:
                        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
                    except Exception:
                        LOGGER.warning(
                            "failed to release recovery state lock: root=%s lock=%s", root, lock_path, exc_info=True
                        )
                try:
                    lock_fp.close()
                except Exception:
                    LOGGER.warning("failed to close recovery lock file: lock=%s", lock_path, exc_info=True)


def _load_recovery_state_payload(path: str, strict: bool = False, caller: str = "") -> Dict[str, Any]:
    if not os.path.exists(path):
        return _empty_recovery_state()

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as err:
        if strict:
            message = f"failed to load recovery state in write path: caller={caller} path={path}"
            LOGGER.error(message, exc_info=True)
            raise RecoveryStateLoadError(message) from err
        LOGGER.warning("failed to load recovery state: path=%s", path, exc_info=True)
        return _empty_recovery_state()

    if not isinstance(data, dict):
        if strict:
            message = (
                f"invalid recovery state payload type in write path: "
                f"caller={caller} path={path} type={type(data).__name__}"
            )
            LOGGER.error(message)
            raise RecoveryStateLoadError(message)
        LOGGER.warning("invalid recovery state payload type: path=%s type=%s", path, type(data).__name__)
        return _empty_recovery_state()

    if "entries" in data and not isinstance(data.get("entries"), dict):
        if strict:
            message = (
                f"invalid recovery state entries in write path: caller={caller} path={path} "
                f"type={type(data.get('entries')).__name__}"
            )
            LOGGER.error(message)
            raise RecoveryStateLoadError(message)
        LOGGER.warning("invalid recovery state entries: path=%s type=%s", path, type(data.get("entries")).__name__)
        return _empty_recovery_state()

    entries = data.get("entries") if isinstance(data.get("entries"), dict) else {}
    return {"entries": entries, "updatedAt": str(data.get("updatedAt") or "")}


def _load_recovery_state_unlocked(root: str) -> Dict[str, Any]:
    path = recovery_state_path(root)
    return _load_recovery_state_payload(path, strict=False)


def _load_recovery_state_unlocked_strict(root: str, caller: str) -> Dict[str, Any]:
    path = recovery_state_path(root)
    return _load_recovery_state_payload(path, strict=True, caller=caller)


def load_recovery_state(root: str) -> Dict[str, Any]:
    return _load_recovery_state_unlocked(root)


def _save_recovery_state_unlocked(root: str, state: Dict[str, Any]) -> None:
    path = recovery_state_path(root)
    payload = _normalize_state_payload(state)
    _write_json_atomic(path, payload)


def save_recovery_state(root: str, state: Dict[str, Any]) -> None:
    with _recovery_state_guard(root, require_lock=True):
        _load_recovery_state_unlocked_strict(root, caller="save_recovery_state")
        _save_recovery_state_unlocked(root, state)


def normalize_reason(reason_code: str) -> str:
    return str(reason_code or "").strip().lower()


def should_trigger_recovery(reason_code: str) -> bool:
    return normalize_reason(reason_code) in RECOVERY_REASON_CODES


def _classification_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    signals = payload.get("signals") if isinstance(payload.get("signals"), list) else []
    return {
        "failureType": str(payload.get("failureType") or "unknown"),
        "normalizedReason": str(payload.get("normalizedReason") or ""),
        "recoveryStrategy": str(payload.get("recoveryStrategy") or ""),
        "signals": [str(item) for item in signals if str(item or "").strip()],
    }


def next_assignee_for(chain: List[str], current_assignee: str) -> str:
    current = str(current_assignee or "").strip().lower()
    if not chain:
        chain = ["coder", "debugger", "invest-analyst", "human"]
    if current not in chain:
        return chain[0]
    idx = chain.index(current)
    if idx >= len(chain) - 1:
        return chain[-1]
    return chain[idx + 1]


def get_active_cooldown(root: str, task_id: str, reason_code: str = "", now_ts: int = None) -> Dict[str, Any]:
    now_unix = int(now_ts if now_ts is not None else time.time())
    reason_filter = normalize_reason(reason_code)
    if reason_filter and reason_filter not in RECOVERY_REASON_CODES:
        return {}
    state = load_recovery_state(root)
    entries = state.get("entries") if isinstance(state.get("entries"), dict) else {}
    best: Dict[str, Any] = {}

    for key, raw in entries.items():
        if not isinstance(raw, dict):
            continue
        row_task_id = str(raw.get("taskId") or "").strip()
        if row_task_id != task_id:
            continue

        cooldown_until_ts = max(0, safe_int(raw.get("cooldownUntilTs"), 0))
        if cooldown_until_ts <= now_unix:
            continue

        reason_code = normalize_reason(str(raw.get("reasonCode") or ""))
        if reason_code not in RECOVERY_REASON_CODES and "|" in str(key):
            reason_code = normalize_reason(str(key).split("|", 1)[1])
        if reason_code not in RECOVERY_REASON_CODES:
            continue
        if reason_filter and reason_code != reason_filter:
            continue

        attempt = max(0, safe_int(raw.get("attempt"), 0))
        next_assignee = str(raw.get("nextAssignee") or "").strip().lower()
        action = str(raw.get("action") or "").strip().lower()
        if action not in {"retry", "escalate", "human"}:
            action = "human" if next_assignee == "human" else "retry"
        recovery_state = str(raw.get("recoveryState") or "").strip()
        if not recovery_state:
            recovery_state = "human_handoff" if action == "human" else "recovery_scheduled"

        candidate = {
            "reasonCode": reason_code,
            "attempt": attempt,
            "nextAssignee": next_assignee or "human",
            "action": action,
            "recoveryState": recovery_state,
            "cooldownActive": True,
            "cooldownUntilTs": cooldown_until_ts,
            "cooldownUntil": str(raw.get("cooldownUntil") or ts_to_iso(cooldown_until_ts)),
            "recoverable": action in {"retry", "human"},
            "maxAttempts": max(1, safe_int(raw.get("maxAttempts"), 2)),
            **_classification_fields(raw),
        }
        if not best or cooldown_until_ts > int(best.get("cooldownUntilTs") or 0):
            best = candidate

    return best


def decide_recovery(
    root: str,
    task_id: str,
    current_assignee: str,
    reason_code: str,
    detail: str = "",
    output_text: str = "",
    stderr: str = "",
    executor: str = "",
    now_ts: int = None,
) -> Dict[str, Any]:
    reason = normalize_reason(reason_code)
    classified = failure_classifier.classify_failure(
        reason,
        detail=detail,
        output_text=output_text,
        stderr=stderr,
        current_assignee=current_assignee,
        executor=executor,
    )
    classification_fields = _classification_fields(classified)

    if not should_trigger_recovery(reason) and str(classification_fields.get("failureType") or "") != "budget_exceeded":
        return {
            "reasonCode": reason,
            "attempt": 0,
            "nextAssignee": "human",
            "action": "escalate",
            "recoveryState": "escalated_to_human",
            "cooldownActive": False,
            "cooldownUntilTs": 0,
            "cooldownUntil": ts_to_iso(0),
            "recoverable": False,
            **classification_fields,
        }

    now_unix = int(now_ts if now_ts is not None else time.time())
    policy = load_recovery_policy(root)
    chain = normalize_chain(policy.get("recoveryChain"))
    reason_conf = (policy.get("reasonPolicies") or {}).get(reason) or {}
    max_attempts = max(1, safe_int(reason_conf.get("maxAttempts"), safe_int((policy.get("default") or {}).get("maxAttempts"), 2)))
    cooldown_sec = max(0, safe_int(reason_conf.get("cooldownSec"), safe_int((policy.get("default") or {}).get("cooldownSec"), 180)))

    failure_type = str(classification_fields.get("failureType") or "unknown")
    retry_same_assignee = False
    forced_action = ""
    forced_next = ""
    if failure_type == "context_overflow":
        retry_same_assignee = True
        forced_action = "retry"
        max_attempts = 1
        cooldown_sec = 60
    elif failure_type == "wrong_direction":
        forced_action = "escalate"
        forced_next = "human"
        max_attempts = 1
        cooldown_sec = 0
    elif failure_type == "missing_info":
        retry_same_assignee = True
        forced_action = "retry"
        max_attempts = 1
        cooldown_sec = 60
    elif failure_type == "budget_exceeded":
        forced_action = "escalate"
        forced_next = "human"
        max_attempts = 1
        cooldown_sec = 0
    elif failure_type == "continuation_stall":
        forced_action = "escalate"
        forced_next = "human"
        max_attempts = 1
        cooldown_sec = 0

    with _recovery_state_guard(root, require_lock=True):
        state = _load_recovery_state_unlocked_strict(root, caller="decide_recovery")
        entries = state.setdefault("entries", {})
        key = f"{task_id}|{reason}"
        prev = entries.get(key) if isinstance(entries.get(key), dict) else {}

        prev_attempt = max(0, safe_int(prev.get("attempt"), 0))
        prev_next = str(prev.get("nextAssignee") or "").strip().lower()
        prev_action = str(prev.get("action") or "").strip().lower()
        prev_state = str(prev.get("recoveryState") or "").strip()
        prev_cooldown_ts = max(0, safe_int(prev.get("cooldownUntilTs"), 0))

        if prev_cooldown_ts and now_unix < prev_cooldown_ts:
            chosen_next = prev_next or next_assignee_for(chain, current_assignee)
            chosen_action = prev_action if prev_action in {"retry", "escalate", "human"} else (
                "human" if chosen_next == "human" else "retry"
            )
            chosen_state = prev_state or (
                "human_handoff" if chosen_action == "human" else "recovery_scheduled"
            )
            prev_fields = _classification_fields(prev)
            return {
                "reasonCode": reason,
                "attempt": prev_attempt,
                "nextAssignee": chosen_next,
                "action": chosen_action,
                "recoveryState": chosen_state,
                "cooldownActive": True,
                "cooldownUntilTs": prev_cooldown_ts,
                "cooldownUntil": ts_to_iso(prev_cooldown_ts),
                "recoverable": chosen_action in {"retry", "human"},
                "maxAttempts": max_attempts,
                **(prev_fields if str(prev_fields.get("failureType") or "") != "unknown" else classification_fields),
            }

        attempt = prev_attempt + 1
        next_assignee = next_assignee_for(chain, current_assignee)
        if retry_same_assignee:
            current = str(current_assignee or "").strip().lower()
            next_assignee = current or next_assignee

        if reason == "incomplete_output" and next_assignee == "human" and attempt <= max_attempts:
            non_human_chain = [role for role in chain if role != "human"]
            if non_human_chain:
                next_assignee = non_human_chain[0]

        if attempt > max_attempts:
            action = "escalate"
            next_assignee = "human"
            recovery_state = "escalated_to_human"
        elif forced_action == "escalate":
            action = "escalate"
            next_assignee = forced_next or "human"
            recovery_state = "escalated_to_human"
        elif forced_action == "retry":
            action = "retry"
            next_assignee = forced_next or next_assignee
            recovery_state = "recovery_scheduled"
        elif next_assignee == "human":
            action = "human"
            recovery_state = "human_handoff"
        else:
            action = "retry"
            recovery_state = "recovery_scheduled"

        cooldown_until_ts = now_unix + cooldown_sec if cooldown_sec > 0 else 0
        entries[key] = {
            "taskId": task_id,
            "reasonCode": reason,
            "attempt": attempt,
            "nextAssignee": next_assignee,
            "action": action,
            "recoveryState": recovery_state,
            "cooldownUntilTs": cooldown_until_ts,
            "cooldownUntil": ts_to_iso(cooldown_until_ts),
            "updatedAt": now_iso(),
            "maxAttempts": max_attempts,
            **classification_fields,
        }
        _save_recovery_state_unlocked(root, state)

        return {
            "reasonCode": reason,
            "attempt": attempt,
            "nextAssignee": next_assignee,
            "action": action,
            "recoveryState": recovery_state,
            "cooldownActive": False,
            "cooldownUntilTs": cooldown_until_ts,
            "cooldownUntil": ts_to_iso(cooldown_until_ts),
            "recoverable": action in {"retry", "human"},
            "maxAttempts": max_attempts,
            **classification_fields,
        }


def clear_task(root: str, task_id: str) -> Dict[str, Any]:
    task_key = str(task_id or "").strip()
    if not task_key:
        return {"taskId": "", "cleared": False, "removedKeys": []}

    with _recovery_state_guard(root, require_lock=True):
        state = _load_recovery_state_unlocked_strict(root, caller="clear_task")
        entries = state.setdefault("entries", {})
        removed_keys: List[str] = []

        for key, raw in list(entries.items()):
            row_task = str((raw or {}).get("taskId") or "").strip() if isinstance(raw, dict) else ""
            if row_task == task_key or str(key).startswith(f"{task_key}|"):
                entries.pop(key, None)
                removed_keys.append(str(key))

        if removed_keys:
            _save_recovery_state_unlocked(root, state)

    return {"taskId": task_key, "cleared": bool(removed_keys), "removedKeys": removed_keys}
