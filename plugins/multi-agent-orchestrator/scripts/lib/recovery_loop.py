#!/usr/bin/env python3
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List


RECOVERY_POLICY_CONFIG_CANDIDATES = (
    os.path.join("config", "recovery-policy.json"),
    os.path.join("state", "recovery-policy.json"),
)
RECOVERY_STATE_FILE = os.path.join("state", "recovery.state.json")
RECOVERY_REASON_CODES = {"spawn_failed", "incomplete_output", "blocked_signal"}
DEFAULT_RECOVERY_POLICY: Dict[str, Any] = {
    "recoveryChain": ["coder", "debugger", "invest-analyst", "human"],
    "default": {"maxAttempts": 2, "cooldownSec": 180},
    "reasonPolicies": {
        "spawn_failed": {"maxAttempts": 2, "cooldownSec": 180},
        "incomplete_output": {"maxAttempts": 2, "cooldownSec": 120},
        "blocked_signal": {"maxAttempts": 2, "cooldownSec": 180},
    },
}


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


def load_recovery_state(root: str) -> Dict[str, Any]:
    path = recovery_state_path(root)
    if not os.path.exists(path):
        return {"entries": {}, "updatedAt": ""}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"entries": {}, "updatedAt": ""}
    entries = data.get("entries") if isinstance(data.get("entries"), dict) else {}
    return {"entries": entries, "updatedAt": str(data.get("updatedAt") or "")}


def save_recovery_state(root: str, state: Dict[str, Any]) -> None:
    path = recovery_state_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "entries": state.get("entries") if isinstance(state.get("entries"), dict) else {},
        "updatedAt": now_iso(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=True, indent=2)
        f.write("\n")


def normalize_reason(reason_code: str) -> str:
    return str(reason_code or "").strip().lower()


def should_trigger_recovery(reason_code: str) -> bool:
    return normalize_reason(reason_code) in RECOVERY_REASON_CODES


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


def get_active_cooldown(root: str, task_id: str, now_ts: int = None) -> Dict[str, Any]:
    now_unix = int(now_ts if now_ts is not None else time.time())
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
        }
        if not best or cooldown_until_ts > int(best.get("cooldownUntilTs") or 0):
            best = candidate

    return best


def decide_recovery(root: str, task_id: str, current_assignee: str, reason_code: str, now_ts: int = None) -> Dict[str, Any]:
    reason = normalize_reason(reason_code)
    if not should_trigger_recovery(reason):
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
        }

    now_unix = int(now_ts if now_ts is not None else time.time())
    policy = load_recovery_policy(root)
    chain = normalize_chain(policy.get("recoveryChain"))
    reason_conf = (policy.get("reasonPolicies") or {}).get(reason) or {}
    max_attempts = max(1, safe_int(reason_conf.get("maxAttempts"), 2))
    cooldown_sec = max(0, safe_int(reason_conf.get("cooldownSec"), 180))

    state = load_recovery_state(root)
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
        }

    attempt = prev_attempt + 1
    next_assignee = next_assignee_for(chain, current_assignee)

    if attempt > max_attempts:
        action = "escalate"
        next_assignee = "human"
        recovery_state = "escalated_to_human"
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
    }
    save_recovery_state(root, state)

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
    }
