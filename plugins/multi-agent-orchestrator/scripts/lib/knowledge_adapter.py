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
    "maxRetries": 1,
    "sourceCandidates": list(DEFAULT_SOURCE_CANDIDATES),
}
MAX_HINT_ITEMS_LIMIT = 20
MAX_RETRY_LIMIT = 8
BACKFILL_ENTRY_CHAR_LIMIT = 220
BACKFILL_LIST_LIMIT = 100
BACKFILL_TAG_LIMIT = 30
BACKFILL_STATE_PATH = os.path.join("state", "knowledge-feedback.json")
FAILURE_PATTERN_BY_REASON: Dict[str, str] = {
    "incomplete_output": "先补充最终可验证证据再回报，避免只给阶段性进度。",
    "missing_evidence": "done 结论必须附测试/日志/命令输出等硬证据。",
    "blocked_signal": "阻塞时给出根因、影响范围和下一步负责人。",
    "spawn_failed": "执行失败后先修复运行环境或命令，再进行重试。",
    "budget_exceeded": "预算超限时缩小任务范围或切换人工接管。",
}


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
    max_retries = _safe_int(data.get("maxRetries"), int(DEFAULT_CONFIG["maxRetries"]))
    return {
        "enabled": _coerce_bool(data.get("enabled"), bool(DEFAULT_CONFIG["enabled"])),
        "readOnly": _coerce_bool(data.get("readOnly"), bool(DEFAULT_CONFIG["readOnly"])),
        "timeoutMs": max(50, timeout_ms),
        "maxItems": min(MAX_HINT_ITEMS_LIMIT, max(1, max_items)),
        "maxRetries": min(MAX_RETRY_LIMIT, max(0, max_retries)),
        "sourceCandidates": _normalize_source_candidates(data.get("sourceCandidates")),
    }


def _merge_config(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key in ("enabled", "readOnly", "timeoutMs", "maxItems", "maxRetries", "sourceCandidates"):
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


def _resolve_candidate_paths(root: str, raw_candidates: Any) -> Dict[str, Any]:
    root_real = os.path.realpath(root)
    out: List[str] = []
    seen: set = set()
    rejected: List[str] = []
    for raw in raw_candidates if isinstance(raw_candidates, list) else []:
        candidate = _as_text(raw)
        if not candidate:
            continue
        candidate_abs = os.path.realpath(candidate) if os.path.isabs(candidate) else os.path.realpath(os.path.join(root_real, candidate))
        try:
            in_root = os.path.commonpath([root_real, candidate_abs]) == root_real
        except Exception:
            in_root = False
        if not in_root:
            rejected.append(candidate)
            continue
        if candidate_abs in seen:
            continue
        seen.add(candidate_abs)
        out.append(candidate_abs)
    return {"paths": out, "rejectedCandidates": rejected}


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


def _clip_entry(text: Any, limit: int = BACKFILL_ENTRY_CHAR_LIMIT) -> str:
    value = _as_text(text)
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "..."


def _prepend_unique(items: List[str], entry: str, limit: int) -> bool:
    value = _as_text(entry)
    if not value or value in items:
        return False
    items.insert(0, value)
    del items[limit:]
    return True


def _append_unique(items: List[str], entry: str, limit: int) -> bool:
    value = _as_text(entry)
    if not value or value in items:
        return False
    items.append(value)
    if len(items) > limit:
        del items[: len(items) - limit]
    return True


def _build_failure_mistake(task_id: str, agent: str, reason_code: str, detail: str) -> str:
    head = "/".join([x for x in [task_id, agent, reason_code] if _as_text(x)])
    tail = _clip_entry(detail, 160)
    if head and tail:
        return f"{head}: {tail}"
    if head:
        return head
    return tail


def _build_failure_pattern(reason_code: str) -> str:
    key = _as_text(reason_code)
    if key in FAILURE_PATTERN_BY_REASON:
        return FAILURE_PATTERN_BY_REASON[key]
    if key:
        return f"reason={key} 时，优先记录根因与可复现证据，再执行下一步。"
    return "失败后先定位根因并补充可验证证据，再继续推进。"


def _load_backfill_payload(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        loaded = json.load(f)
    return loaded if isinstance(loaded, dict) else {}


def backfill_failure_feedback(
    root: str,
    task_id: str = "",
    agent: str = "",
    reason_code: str = "",
    detail: str = "",
) -> Dict[str, Any]:
    conf = load_config(root)
    if not bool(conf.get("enabled")):
        return {"ok": True, "skipped": True, "reason": "disabled"}

    path = os.path.join(root, BACKFILL_STATE_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    try:
        payload = _load_backfill_payload(path)
    except Exception:
        payload = {}

    mistakes = _as_str_list(payload.get("mistakes"))
    patterns = _as_str_list(payload.get("patterns"))
    tags = _as_str_list(payload.get("tags"))

    added = {"mistakes": [], "patterns": [], "tags": []}
    mistake_entry = _build_failure_mistake(task_id, agent, reason_code, detail)
    if _prepend_unique(mistakes, mistake_entry, BACKFILL_LIST_LIMIT):
        added["mistakes"].append(mistake_entry)

    pattern_entry = _build_failure_pattern(reason_code)
    if _prepend_unique(patterns, pattern_entry, BACKFILL_LIST_LIMIT):
        added["patterns"].append(pattern_entry)

    reason_tag = f"reason:{_as_text(reason_code) or 'unknown'}"
    for tag in ("dispatch_failure", reason_tag):
        if _append_unique(tags, tag, BACKFILL_TAG_LIMIT):
            added["tags"].append(tag)

    payload["mistakes"] = mistakes
    payload["patterns"] = patterns
    payload["tags"] = tags
    payload["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    temp_path = path + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temp_path, path)
        return {"ok": True, "skipped": False, "path": path, "added": added}
    except Exception as err:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        return {"ok": False, "skipped": True, "error": _clip_entry(str(err), 160)}


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
    max_retries = _safe_int(conf.get("maxRetries"), int(DEFAULT_CONFIG["maxRetries"]))
    max_items = min(MAX_HINT_ITEMS_LIMIT, max(1, max_items))
    max_retries = min(MAX_RETRY_LIMIT, max(0, max_retries))
    total_attempts = 1 + max_retries

    resolved = _resolve_candidate_paths(root, conf.get("sourceCandidates"))
    candidates = list(resolved.get("paths") or [])
    rejected = [str(x) for x in (resolved.get("rejectedCandidates") or []) if _as_text(x)]
    if not candidates and rejected:
        return _result(
            enabled=True,
            degraded=True,
            degrade_reason=f"rejected out-of-root sourceCandidates: {', '.join(rejected[:3])}",
        )

    last_error = ""
    had_read_error = False
    for source_path in candidates:
        for _ in range(total_attempts):
            if _elapsed_ms(start_at) > timeout_ms:
                return _result(enabled=True, degraded=True, degrade_reason="knowledge adapter timeout")
            if not os.path.exists(source_path):
                break
            try:
                with open(source_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
            except Exception as err:
                had_read_error = True
                last_error = _clip_entry(f"failed to read knowledge source {source_path}: {err}")
                continue

            if not isinstance(loaded, dict):
                had_read_error = True
                last_error = _clip_entry(f"knowledge source {source_path} must be a JSON object")
                break

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
            break

    if had_read_error:
        return _result(enabled=True, degraded=True, degrade_reason=last_error or "knowledge adapter read failed")
    return _result(enabled=True)
