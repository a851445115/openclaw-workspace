#!/usr/bin/env python3
import hashlib
import json
import os
from typing import Any, Dict


ROLE_STRATEGY_CONFIG_CANDIDATES = (
    os.path.join("config", "role-strategies.json"),
    os.path.join("state", "role-strategies.json"),
)


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _norm_key(value: Any) -> str:
    return _as_text(value).lower()


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _normalize_rollout_percent(value: Any, default: int = 100) -> int:
    n = _safe_int(value, default)
    if n < 0:
        return 0
    if n > 100:
        return 100
    return n


def _normalize_strategy_entry(raw: Any, fallback_id: str, source: str) -> Dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    strategy_id = _as_text(data.get("strategyId") or data.get("id") or fallback_id)
    content = _as_text(data.get("content") or data.get("prompt"))
    enabled = bool(data.get("enabled", True))
    rollout_percent = _normalize_rollout_percent(data.get("rolloutPercent"), 100)
    return {
        "strategyId": strategy_id,
        "content": content,
        "enabled": enabled,
        "rolloutPercent": rollout_percent,
        "source": source,
    }


def _iter_task_kind_entries(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}

    entries: Dict[str, Any] = {}
    agents = raw.get("agents")
    if isinstance(agents, dict):
        for agent, entry in agents.items():
            key = _norm_key(agent)
            if key:
                entries[key] = entry

    if "default" in raw:
        entries["default"] = raw.get("default")

    for key_raw, entry in raw.items():
        key = _norm_key(key_raw)
        if not key or key in {"agents"}:
            continue
        if key == "default" and "default" in entries:
            continue
        entries[key] = entry

    return entries


def _merge_library(base: Dict[str, Any], loaded: Dict[str, Any], source: str) -> Dict[str, Any]:
    task_kinds = loaded.get("taskKinds")
    if isinstance(task_kinds, dict):
        for task_kind, task_kind_block in task_kinds.items():
            task_kind_key = _norm_key(task_kind)
            if not task_kind_key:
                continue
            out_block = base["taskKinds"].setdefault(task_kind_key, {})
            for agent_key, entry in _iter_task_kind_entries(task_kind_block).items():
                key = _norm_key(agent_key)
                if not key:
                    continue
                fallback_id = f"{task_kind_key}:{key}"
                out_block[key] = _normalize_strategy_entry(entry, fallback_id, source)

    agents = loaded.get("agents")
    if isinstance(agents, dict):
        for agent, conf in agents.items():
            agent_key = _norm_key(agent)
            if not agent_key:
                continue
            entry_raw = conf.get("default") if isinstance(conf, dict) and "default" in conf else conf
            fallback_id = f"{agent_key}:default"
            base["agents"][agent_key] = {"default": _normalize_strategy_entry(entry_raw, fallback_id, source)}

    if "default" in loaded:
        base["default"] = _normalize_strategy_entry(loaded.get("default"), "global:default", source)

    return base


def load_strategy_library(root: str) -> Dict[str, Any]:
    script_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    search_roots = [script_root, root]
    out: Dict[str, Any] = {
        "taskKinds": {},
        "agents": {},
        "default": {},
        "sourcePaths": [],
    }

    for base in search_roots:
        for rel in ROLE_STRATEGY_CONFIG_CANDIDATES:
            path = os.path.join(base, rel)
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    out = _merge_library(out, loaded, path)
                    out["sourcePaths"].append(path)
            except Exception:
                continue

    return out


def _rollout_hit(task_id: str, rollout_percent: int) -> bool:
    if rollout_percent <= 0:
        return False
    if rollout_percent >= 100:
        return True
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 100
    return bucket < rollout_percent


def _resolve_from_template(template: Dict[str, Any], matched_by: str, task_id: str) -> Dict[str, Any]:
    enabled_by_switch = bool(template.get("enabled", True))
    rollout_percent = _normalize_rollout_percent(template.get("rolloutPercent"), 100)
    enabled = enabled_by_switch and _rollout_hit(task_id, rollout_percent)
    return {
        "strategyId": _as_text(template.get("strategyId")),
        "content": _as_text(template.get("content")),
        "source": _as_text(template.get("source")),
        "matchedBy": matched_by,
        "enabled": enabled,
    }


def resolve_strategy(
    library: Dict[str, Any],
    agent: str,
    task_kind: str,
    task_id: str = "",
) -> Dict[str, Any]:
    lib = library if isinstance(library, dict) else {}
    task_kind_key = _norm_key(task_kind)
    agent_key = _norm_key(agent)
    stable_task_id = _as_text(task_id)

    task_kind_map = lib.get("taskKinds") if isinstance(lib.get("taskKinds"), dict) else {}
    task_kind_conf = task_kind_map.get(task_kind_key) if isinstance(task_kind_map.get(task_kind_key), dict) else {}

    agents_map = lib.get("agents") if isinstance(lib.get("agents"), dict) else {}
    agent_conf = agents_map.get(agent_key) if isinstance(agents_map.get(agent_key), dict) else {}

    candidates = [
        (task_kind_conf.get(agent_key), "taskKind+agent"),
        (task_kind_conf.get("default"), "taskKind default"),
        (agent_conf.get("default"), "agent default"),
        (lib.get("default"), "global default"),
    ]

    for raw, matched_by in candidates:
        if not isinstance(raw, dict):
            continue
        if not _as_text(raw.get("strategyId")) and not _as_text(raw.get("content")):
            continue
        return _resolve_from_template(raw, matched_by, stable_task_id)

    return {
        "strategyId": "",
        "content": "",
        "source": "",
        "matchedBy": "none",
        "enabled": False,
    }
