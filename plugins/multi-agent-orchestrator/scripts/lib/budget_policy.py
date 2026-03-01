#!/usr/bin/env python3
import copy
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List


BUDGET_POLICY_CONFIG_CANDIDATES = (
    os.path.join("config", "budget-policy.json"),
    os.path.join("state", "budget-policy.json"),
)
BUDGET_STATE_FILE = os.path.join("state", "budget.state.json")
DEFAULT_BUDGET_POLICY: Dict[str, Any] = {
    "global": {
        "maxTaskTokens": 12000,
        "maxTaskWallTimeSec": 1200,
        "maxTaskRetries": 3,
        "degradePolicy": ["reduced_context", "manual_handoff", "stop_run"],
        "onExceeded": "manual_handoff",
    },
    "agents": {
        "coder": {
            "maxTaskTokens": 8000,
            "maxTaskWallTimeSec": 900,
            "maxTaskRetries": 2,
            "degradePolicy": ["reduced_context", "manual_handoff", "stop_run"],
            "onExceeded": "manual_handoff",
        }
    },
}
DEGRADE_ACTIONS = {"reduced_context", "manual_handoff", "stop_run"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_int(value: Any, default: int = 0) -> int:
    try:
        out = int(value)
    except Exception:
        return default
    return out if out >= 0 else default


def normalize_degrade_policy(value: Any, fallback: List[str]) -> List[str]:
    out: List[str] = []
    if isinstance(value, list):
        for item in value:
            action = str(item or "").strip().lower()
            if action and action in DEGRADE_ACTIONS and action not in out:
                out.append(action)
    if out:
        return out
    fallback_out = [x for x in fallback if x in DEGRADE_ACTIONS]
    return fallback_out or ["manual_handoff"]


def normalize_on_exceeded(value: Any, degrade_policy: List[str]) -> str:
    action = str(value or "").strip().lower()
    if action in DEGRADE_ACTIONS:
        return action
    if degrade_policy:
        return degrade_policy[0]
    return "manual_handoff"


def normalize_policy_conf(raw: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
    degrade_policy = normalize_degrade_policy(raw.get("degradePolicy"), fallback.get("degradePolicy") or [])
    max_task_tokens = safe_int(raw.get("maxTaskTokens"), safe_int(fallback.get("maxTaskTokens"), 12000))
    max_task_wall_time_sec = safe_int(raw.get("maxTaskWallTimeSec"), safe_int(fallback.get("maxTaskWallTimeSec"), 1200))
    max_task_retries = safe_int(raw.get("maxTaskRetries"), safe_int(fallback.get("maxTaskRetries"), 3))

    return {
        "maxTaskTokens": max(1, max_task_tokens),
        "maxTaskWallTimeSec": max(1, max_task_wall_time_sec),
        "maxTaskRetries": max(1, max_task_retries),
        "degradePolicy": degrade_policy,
        "onExceeded": normalize_on_exceeded(raw.get("onExceeded"), degrade_policy),
    }


def merge_budget_policy(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {
        "global": dict(base.get("global") or {}),
        "agents": {
            k: dict(v)
            for k, v in (base.get("agents") or {}).items()
            if isinstance(k, str) and isinstance(v, dict)
        },
    }
    if not isinstance(override, dict):
        return merged

    global_override = override.get("global")
    if isinstance(global_override, dict):
        merged["global"].update(global_override)

    agents_override = override.get("agents")
    if isinstance(agents_override, dict):
        for agent, conf in agents_override.items():
            if not isinstance(agent, str) or not isinstance(conf, dict):
                continue
            key = agent.strip().lower()
            current = dict(merged["agents"].get(key) or {})
            current.update(conf)
            merged["agents"][key] = current

    return merged


def load_budget_policy(root: str) -> Dict[str, Any]:
    script_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    search_roots = [script_root, root]
    merged = copy.deepcopy(DEFAULT_BUDGET_POLICY)

    for base in search_roots:
        for rel in BUDGET_POLICY_CONFIG_CANDIDATES:
            path = os.path.join(base, rel)
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    merged = merge_budget_policy(merged, loaded)
            except Exception:
                continue

    global_conf = normalize_policy_conf(merged.get("global") if isinstance(merged.get("global"), dict) else {}, DEFAULT_BUDGET_POLICY["global"])
    agents_out: Dict[str, Dict[str, Any]] = {}
    for agent, conf in (merged.get("agents") or {}).items():
        if not isinstance(agent, str) or not isinstance(conf, dict):
            continue
        agents_out[agent.strip().lower()] = normalize_policy_conf(conf, global_conf)

    return {"global": global_conf, "agents": agents_out}


def budget_state_path(root: str) -> str:
    return os.path.join(root, BUDGET_STATE_FILE)


def load_budget_state(root: str) -> Dict[str, Any]:
    path = budget_state_path(root)
    if not os.path.exists(path):
        return {"entries": {}, "updatedAt": ""}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"entries": {}, "updatedAt": ""}
    entries = data.get("entries") if isinstance(data.get("entries"), dict) else {}
    return {"entries": entries, "updatedAt": str(data.get("updatedAt") or "")}


def save_budget_state(root: str, state: Dict[str, Any]) -> None:
    path = budget_state_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "entries": state.get("entries") if isinstance(state.get("entries"), dict) else {},
        "updatedAt": now_iso(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=True, indent=2)
        f.write("\n")


def budget_entry_key(task_id: str, agent: str) -> str:
    return f"{str(task_id or '').strip()}|{str(agent or '').strip().lower()}"


def resolve_limits(policy: Dict[str, Any], agent: str) -> Dict[str, Any]:
    key = str(agent or "").strip().lower()
    agents = policy.get("agents") if isinstance(policy.get("agents"), dict) else {}
    if isinstance(agents.get(key), dict):
        return dict(agents.get(key) or {})
    return dict(policy.get("global") or {})


def normalize_usage(entry: Dict[str, Any]) -> Dict[str, int]:
    return {
        "tokenUsage": safe_int(entry.get("tokenUsage"), 0),
        "elapsedMs": safe_int(entry.get("elapsedMs"), 0),
        "retryCount": safe_int(entry.get("retryCount"), 0),
    }


def build_budget_snapshot(task_id: str, agent: str, limits: Dict[str, Any], usage: Dict[str, int]) -> Dict[str, Any]:
    max_tokens = max(1, safe_int(limits.get("maxTaskTokens"), 1))
    max_time_ms = max(1, safe_int(limits.get("maxTaskWallTimeSec"), 1)) * 1000
    max_retries = max(1, safe_int(limits.get("maxTaskRetries"), 1))
    return {
        "taskId": str(task_id or "").strip(),
        "agent": str(agent or "").strip().lower(),
        "limits": {
            "maxTaskTokens": max_tokens,
            "maxTaskWallTimeSec": max(1, safe_int(limits.get("maxTaskWallTimeSec"), 1)),
            "maxTaskRetries": max_retries,
        },
        "usage": {
            "tokenUsage": safe_int(usage.get("tokenUsage"), 0),
            "elapsedMs": safe_int(usage.get("elapsedMs"), 0),
            "retryCount": safe_int(usage.get("retryCount"), 0),
        },
        "remaining": {
            "tokens": max_tokens - safe_int(usage.get("tokenUsage"), 0),
            "wallTimeMs": max_time_ms - safe_int(usage.get("elapsedMs"), 0),
            "retries": max_retries - safe_int(usage.get("retryCount"), 0),
        },
    }


def evaluate_precheck_exceeded(limits: Dict[str, Any], usage: Dict[str, int]) -> List[str]:
    exceeded: List[str] = []
    if safe_int(usage.get("tokenUsage"), 0) >= max(1, safe_int(limits.get("maxTaskTokens"), 1)):
        exceeded.append("maxTaskTokens")
    if safe_int(usage.get("elapsedMs"), 0) >= max(1, safe_int(limits.get("maxTaskWallTimeSec"), 1)) * 1000:
        exceeded.append("maxTaskWallTimeSec")
    if safe_int(usage.get("retryCount"), 0) >= max(1, safe_int(limits.get("maxTaskRetries"), 1)):
        exceeded.append("maxTaskRetries")
    return exceeded


def evaluate_postcheck_exceeded(limits: Dict[str, Any], usage: Dict[str, int]) -> List[str]:
    exceeded: List[str] = []
    if safe_int(usage.get("tokenUsage"), 0) > max(1, safe_int(limits.get("maxTaskTokens"), 1)):
        exceeded.append("maxTaskTokens")
    if safe_int(usage.get("elapsedMs"), 0) > max(1, safe_int(limits.get("maxTaskWallTimeSec"), 1)) * 1000:
        exceeded.append("maxTaskWallTimeSec")
    if safe_int(usage.get("retryCount"), 0) > max(1, safe_int(limits.get("maxTaskRetries"), 1)):
        exceeded.append("maxTaskRetries")
    return exceeded


def make_budget_decision(task_id: str, agent: str, limits: Dict[str, Any], usage: Dict[str, int], exceeded_keys: List[str]) -> Dict[str, Any]:
    degrade_action = ""
    next_assignee = ""
    allowed = len(exceeded_keys) == 0
    if not allowed:
        degrade_action = normalize_on_exceeded(limits.get("onExceeded"), normalize_degrade_policy(limits.get("degradePolicy"), []))
        next_assignee = "human"

    return {
        "allowed": allowed,
        "reasonCode": "" if allowed else "budget_exceeded",
        "exceededKeys": exceeded_keys,
        "degradeAction": degrade_action,
        "nextAssignee": next_assignee,
        "budgetSnapshot": build_budget_snapshot(task_id, agent, limits, usage),
    }


def precheck_budget(root: str, task_id: str, agent: str) -> Dict[str, Any]:
    policy = load_budget_policy(root)
    limits = resolve_limits(policy, agent)
    state = load_budget_state(root)
    entries = state.get("entries") if isinstance(state.get("entries"), dict) else {}
    key = budget_entry_key(task_id, agent)
    usage = normalize_usage(entries.get(key) if isinstance(entries.get(key), dict) else {})
    exceeded = evaluate_precheck_exceeded(limits, usage)
    return make_budget_decision(task_id, agent, limits, usage, exceeded)


def record_and_check_budget(
    root: str,
    task_id: str,
    agent: str,
    token_usage: Any,
    elapsed_ms: Any,
    retry_increment: Any,
) -> Dict[str, Any]:
    policy = load_budget_policy(root)
    limits = resolve_limits(policy, agent)
    state = load_budget_state(root)
    entries = state.get("entries") if isinstance(state.get("entries"), dict) else {}
    key = budget_entry_key(task_id, agent)
    current = entries.get(key) if isinstance(entries.get(key), dict) else {}
    usage = normalize_usage(current)

    usage["tokenUsage"] += safe_int(token_usage, 0)
    usage["elapsedMs"] += safe_int(elapsed_ms, 0)
    usage["retryCount"] += safe_int(retry_increment, 0)

    entries[key] = {
        "taskId": str(task_id or "").strip(),
        "agent": str(agent or "").strip().lower(),
        "tokenUsage": usage["tokenUsage"],
        "elapsedMs": usage["elapsedMs"],
        "retryCount": usage["retryCount"],
        "updatedAt": now_iso(),
    }
    state["entries"] = entries
    save_budget_state(root, state)

    exceeded = evaluate_postcheck_exceeded(limits, usage)
    return make_budget_decision(task_id, agent, limits, usage, exceeded)
