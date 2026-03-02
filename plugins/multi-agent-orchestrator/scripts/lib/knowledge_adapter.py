#!/usr/bin/env python3
import json
import os
import time
from typing import Any, Dict, List

KNOWLEDGE_FEEDBACK_CONFIG_CANDIDATES = (
    os.path.join("config", "knowledge-feedback.json"),
    os.path.join("state", "knowledge-feedback.config.json"),
)
DEFAULT_SOURCE_CANDIDATES = (
    os.path.join("state", "knowledge-feedback.json"),
    os.path.join("state", "lessons-learned.json"),
)
DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "readOnly": True,
    "timeoutMs": 300,
    "maxItems": 3,
    "sourceCandidates": list(DEFAULT_SOURCE_CANDIDATES),
}
MAX_HINT_ITEMS_LIMIT = 20


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"1", "true", "yes", "on"}:
            return True
        if s in {"0", "false", "no", "off"}:
            return False
    return bool(default)


def _normalize_source_candidates(raw: Any) -> List[str]:
    out: List[str] = []
    if isinstance(raw, list):
        for item in raw:
            path = _as_text(item)
            if not path or path in out:
                continue
            out.append(path)
    if out:
        return out
    return list(DEFAULT_SOURCE_CANDIDATES)


def _normalize_config(raw: Any) -> Dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    max_items = _safe_int(data.get("maxItems"), int(DEFAULT_CONFIG["maxItems"]))
    timeout_ms = _safe_int(data.get("timeoutMs"), int(DEFAULT_CONFIG["timeoutMs"]))
    return {
        "enabled": _coerce_bool(data.get("enabled"), bool(DEFAULT_CONFIG["enabled"])),
        "readOnly": _coerce_bool(data.get("readOnly"), bool(DEFAULT_CONFIG["readOnly"])),
        "timeoutMs": max(50, timeout_ms),
        "maxItems": min(MAX_HINT_ITEMS_LIMIT, max(1, max_items)),
        "sourceCandidates": _normalize_source_candidates(data.get("sourceCandidates")),
    }


def _merge_config(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key in ("enabled", "readOnly", "timeoutMs", "maxItems", "sourceCandidates"):
        if key in incoming:
            out[key] = incoming.get(key)
    return out


def load_config(root: str) -> Dict[str, Any]:
    script_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    search_roots = [script_root, root]
    merged: Dict[str, Any] = dict(DEFAULT_CONFIG)
    source_paths: List[str] = []

    for base in search_roots:
        for rel in KNOWLEDGE_FEEDBACK_CONFIG_CANDIDATES:
            path = os.path.join(base, rel)
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    merged = _merge_config(merged, loaded)
                    source_paths.append(path)
            except Exception:
                continue

    conf = _normalize_config(merged)
    conf["sourcePaths"] = source_paths
    return conf


def _result(
    *,
    enabled: bool,
    degraded: bool = False,
    degrade_reason: str = "",
    knowledge_tags: List[str] = None,
    hints: List[str] = None,
    source: str = "",
) -> Dict[str, Any]:
    return {
        "enabled": bool(enabled),
        "degraded": bool(degraded),
        "degradeReason": _as_text(degrade_reason),
        "knowledgeTags": list(knowledge_tags or []),
        "hints": list(hints or []),
        "source": _as_text(source),
    }


def _resolve_candidate_paths(root: str, raw_candidates: Any) -> List[str]:
    out: List[str] = []
    seen: set = set()
    for raw in raw_candidates if isinstance(raw_candidates, list) else []:
        candidate = _as_text(raw)
        if not candidate:
            continue
        path = candidate if os.path.isabs(candidate) else os.path.join(root, candidate)
        norm = os.path.normpath(path)
        if norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def _as_str_list(value: Any) -> List[str]:
    out: List[str] = []
    if isinstance(value, str):
        text = _as_text(value)
        if text:
            out.append(text)
        return out
    if isinstance(value, list):
        for item in value:
            text = _as_text(item)
            if text:
                out.append(text)
    return out


def _extract_hints(payload: Dict[str, Any], max_items: int) -> Dict[str, Any]:
    hints: List[str] = []
    tags: List[str] = []
    for tag in ("lessons", "mistakes", "patterns"):
        items = _as_str_list(payload.get(tag))
        used = False
        for item in items:
            if item in hints:
                continue
            hints.append(item)
            used = True
            if len(hints) >= max_items:
                break
        if used:
            tags.append(tag)
        if len(hints) >= max_items:
            break
    return {"hints": hints, "knowledgeTags": tags}


def _elapsed_ms(start_at: float) -> int:
    return max(0, int((time.monotonic() - start_at) * 1000))


def fetch_feedback(root: str, task_id: str = "", agent: str = "", objective: str = "") -> Dict[str, Any]:
    del task_id, agent, objective

    conf = load_config(root)
    enabled = bool(conf.get("enabled"))
    if not enabled:
        return _result(enabled=False)
    if not bool(conf.get("readOnly")):
        return _result(enabled=True, degraded=True, degrade_reason="knowledge adapter requires readOnly=true")

    start_at = time.monotonic()
    timeout_ms = _safe_int(conf.get("timeoutMs"), int(DEFAULT_CONFIG["timeoutMs"]))
    max_items = _safe_int(conf.get("maxItems"), int(DEFAULT_CONFIG["maxItems"]))
    max_items = min(MAX_HINT_ITEMS_LIMIT, max(1, max_items))
    candidates = _resolve_candidate_paths(root, conf.get("sourceCandidates"))

    for source_path in candidates:
        if _elapsed_ms(start_at) > timeout_ms:
            return _result(enabled=True, degraded=True, degrade_reason="knowledge adapter timeout")
        if not os.path.exists(source_path):
            continue
        try:
            with open(source_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception as err:
            return _result(
                enabled=True,
                degraded=True,
                degrade_reason=f"failed to read knowledge source {source_path}: {err}",
            )
        if not isinstance(loaded, dict):
            continue

        extracted = _extract_hints(loaded, max_items)
        hints = extracted.get("hints") or []
        if hints:
            return _result(
                enabled=True,
                degraded=False,
                knowledge_tags=list(extracted.get("knowledgeTags") or []),
                hints=list(hints),
                source=source_path,
            )

    return _result(enabled=True)
