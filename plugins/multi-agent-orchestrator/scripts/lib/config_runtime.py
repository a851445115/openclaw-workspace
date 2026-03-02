#!/usr/bin/env python3
import copy
import json
import os
from typing import Any, Dict, List, Optional, Tuple

RUNTIME_POLICY_CONFIG_CANDIDATES = (
    os.path.join("config", "runtime-policy.json"),
    os.path.join("state", "runtime-policy.json"),
)
LEGACY_BUDGET_CONFIG_CANDIDATES = (
    os.path.join("config", "budget-policy.json"),
    os.path.join("state", "budget-policy.json"),
)
BACKOFF_MODES = {"fixed", "linear", "exponential"}
DEFAULT_RUNTIME_CONFIG: Dict[str, Any] = {
    "agents": [],
    "orchestrator": {
        "maxConcurrentSpawns": 3,
        "retryPolicy": {
            "maxAttempts": 2,
            "backoff": {
                "mode": "exponential",
                "baseMs": 500,
                "maxMs": 8000,
                "multiplier": 2.0,
                "jitterPct": 20,
            },
        },
        "budgetPolicy": {
            "guardrails": {
                "maxTaskTokens": 12000,
                "maxTaskWallTimeSec": 1200,
                "maxTaskRetries": 3,
            }
        },
    },
}


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _deep_merge(base: Any, override: Any) -> Any:
    if not isinstance(base, dict) or not isinstance(override, dict):
        return copy.deepcopy(override)
    out = copy.deepcopy(base)
    for key, value in override.items():
        if key in out and isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = _deep_merge(out.get(key), value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _load_json_if_exists(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else None
    except Exception:
        return None


def _normalize_capabilities(raw: Any) -> List[str]:
    out: List[str] = []
    if isinstance(raw, str):
        value = _as_text(raw)
        if value:
            out.append(value)
        return out
    if not isinstance(raw, list):
        return out
    for item in raw:
        value = _as_text(item)
        if not value or value in out:
            continue
        out.append(value)
    return out


def _normalize_agents(raw: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen_ids: set = set()

    if isinstance(raw, dict):
        iterable = []
        for key, value in raw.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("id", key)
            else:
                row = {"id": key, "capabilities": value}
            iterable.append(row)
    else:
        iterable = raw if isinstance(raw, list) else []

    for item in iterable:
        if isinstance(item, str):
            agent_id = _as_text(item)
            capabilities: List[str] = []
        elif isinstance(item, dict):
            agent_id = _as_text(item.get("id") or item.get("name"))
            capabilities = _normalize_capabilities(item.get("capabilities"))
        else:
            continue

        if not agent_id or agent_id in seen_ids:
            continue
        seen_ids.add(agent_id)
        out.append({"id": agent_id, "capabilities": capabilities})

    return out


def _normalize_backoff(raw: Any) -> Dict[str, Any]:
    defaults = copy.deepcopy(DEFAULT_RUNTIME_CONFIG["orchestrator"]["retryPolicy"]["backoff"])
    data = raw if isinstance(raw, dict) else {}

    mode = _as_text(data.get("mode") or defaults["mode"]).lower()
    if mode not in BACKOFF_MODES:
        mode = str(defaults["mode"])

    base_ms = _safe_int(data.get("baseMs", data.get("baseMsec", defaults["baseMs"])), int(defaults["baseMs"]))
    max_ms = _safe_int(data.get("maxMs", data.get("maxMsec", defaults["maxMs"])), int(defaults["maxMs"]))
    multiplier = _safe_float(data.get("multiplier", defaults["multiplier"]), float(defaults["multiplier"]))
    jitter_pct = _safe_int(data.get("jitterPct", defaults["jitterPct"]), int(defaults["jitterPct"]))

    base_ms = max(1, base_ms)
    max_ms = max(base_ms, max_ms)
    multiplier = max(1.0, multiplier)
    jitter_pct = min(100, max(0, jitter_pct))

    return {
        "mode": mode,
        "baseMs": base_ms,
        "maxMs": max_ms,
        "multiplier": multiplier,
        "jitterPct": jitter_pct,
    }


def _normalize_retry_policy(raw: Any) -> Dict[str, Any]:
    defaults = copy.deepcopy(DEFAULT_RUNTIME_CONFIG["orchestrator"]["retryPolicy"])
    data = raw if isinstance(raw, dict) else {}

    max_attempts = _safe_int(
        data.get("maxAttempts", data.get("maxRetries", defaults["maxAttempts"])),
        int(defaults["maxAttempts"]),
    )
    max_attempts = max(1, max_attempts)
    backoff = _normalize_backoff(data.get("backoff"))
    return {"maxAttempts": max_attempts, "backoff": backoff}


def _normalize_guardrails(raw: Any) -> Dict[str, Any]:
    defaults = copy.deepcopy(DEFAULT_RUNTIME_CONFIG["orchestrator"]["budgetPolicy"]["guardrails"])
    data = raw if isinstance(raw, dict) else {}
    return {
        "maxTaskTokens": max(1, _safe_int(data.get("maxTaskTokens", defaults["maxTaskTokens"]), int(defaults["maxTaskTokens"]))),
        "maxTaskWallTimeSec": max(
            1,
            _safe_int(data.get("maxTaskWallTimeSec", defaults["maxTaskWallTimeSec"]), int(defaults["maxTaskWallTimeSec"])),
        ),
        "maxTaskRetries": max(1, _safe_int(data.get("maxTaskRetries", defaults["maxTaskRetries"]), int(defaults["maxTaskRetries"]))),
    }


def _normalize_budget_policy(raw: Any, fallback_guardrails: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    if isinstance(data.get("guardrails"), dict):
        guardrails_raw = data.get("guardrails")
    elif data:
        guardrails_raw = data
    elif isinstance(fallback_guardrails, dict):
        guardrails_raw = fallback_guardrails
    else:
        guardrails_raw = {}
    return {"guardrails": _normalize_guardrails(guardrails_raw)}


def _load_legacy_budget_guardrails(root: str) -> Tuple[Dict[str, Any], List[str]]:
    script_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    search_roots = [script_root, root]
    source_paths: List[str] = []
    legacy: Dict[str, Any] = {}

    for base in search_roots:
        for rel in LEGACY_BUDGET_CONFIG_CANDIDATES:
            path = os.path.join(base, rel)
            loaded = _load_json_if_exists(path)
            if not isinstance(loaded, dict):
                continue
            source_paths.append(path)
            legacy = _deep_merge(legacy, loaded)

    global_block = legacy.get("global") if isinstance(legacy.get("global"), dict) else legacy
    fallback = {}
    if isinstance(global_block, dict):
        for key in ("maxTaskTokens", "maxTaskWallTimeSec", "maxTaskRetries"):
            if key in global_block:
                fallback[key] = global_block.get(key)
    return fallback, source_paths


def _normalize_orchestrator(raw: Any, fallback_guardrails: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    defaults = copy.deepcopy(DEFAULT_RUNTIME_CONFIG["orchestrator"])
    data = raw if isinstance(raw, dict) else {}

    max_concurrent_spawns = _safe_int(data.get("maxConcurrentSpawns"), int(defaults["maxConcurrentSpawns"]))
    max_concurrent_spawns = max(1, max_concurrent_spawns)

    retry_raw: Dict[str, Any] = {}
    retry_policy = data.get("retryPolicy")
    if isinstance(retry_policy, dict):
        retry_raw = retry_policy
    else:
        for key in ("maxAttempts", "maxRetries", "backoff"):
            if key in data:
                retry_raw[key] = data.get(key)

    budget_policy_raw = data.get("budgetPolicy")
    budget_policy = _normalize_budget_policy(budget_policy_raw, fallback_guardrails=fallback_guardrails)

    return {
        "maxConcurrentSpawns": max_concurrent_spawns,
        "retryPolicy": _normalize_retry_policy(retry_raw),
        "budgetPolicy": budget_policy,
    }


def load_runtime_config(root: str, override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    script_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    search_roots = [script_root, root]

    merged: Dict[str, Any] = copy.deepcopy(DEFAULT_RUNTIME_CONFIG)
    sources: List[str] = []

    for base in search_roots:
        for rel in RUNTIME_POLICY_CONFIG_CANDIDATES:
            path = os.path.join(base, rel)
            loaded = _load_json_if_exists(path)
            if not isinstance(loaded, dict):
                continue
            merged = _deep_merge(merged, loaded)
            sources.append(path)

    if isinstance(override, dict):
        merged = _deep_merge(merged, override)

    fallback_guardrails, legacy_sources = _load_legacy_budget_guardrails(root)
    normalized = {
        "agents": _normalize_agents(merged.get("agents")),
        "orchestrator": _normalize_orchestrator(merged.get("orchestrator"), fallback_guardrails=fallback_guardrails),
        "meta": {
            "sources": sources,
            "legacyBudgetSources": legacy_sources,
        },
    }
    return normalized
