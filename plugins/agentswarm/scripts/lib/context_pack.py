#!/usr/bin/env python3
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List


RETRY_CONTEXT_STATE_FILE = os.path.join("state", "retry-context.json")
MAX_ARTIFACTS = 10
MAX_CHECKLIST = 10
MAX_RECENT_DECISIONS = 8
LOGGER = logging.getLogger(__name__)
_STATE_LOCK = threading.RLock()

URL_RE = re.compile(r"https?://[^\s\"'<>]+")
PATH_RE = re.compile(r"(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clip(text: Any, limit: int = 220) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "..."


def _normalize_list(value: Any, limit: int, item_limit: int = 220) -> List[str]:
    out: List[str] = []
    if isinstance(value, str):
        item = clip(value, item_limit)
        if item:
            out.append(item)
    elif isinstance(value, list):
        for raw in value:
            item = clip(raw, item_limit)
            if item and item not in out:
                out.append(item)
            if len(out) >= limit:
                break
    return out[:limit]


def _extract_artifacts_from_text(text: str, limit: int = MAX_ARTIFACTS) -> List[str]:
    out: List[str] = []
    source = str(text or "")
    for match in URL_RE.finditer(source):
        token = clip(match.group(0), 220)
        if token and token not in out:
            out.append(token)
        if len(out) >= limit:
            return out
    for match in PATH_RE.finditer(source):
        token = clip(match.group(0), 220)
        if token and token not in out:
            out.append(token)
        if len(out) >= limit:
            return out
    return out


def digest_text(text: str) -> str:
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def context_state_path(root: str) -> str:
    return os.path.join(root, RETRY_CONTEXT_STATE_FILE)


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
        LOGGER.exception("failed to persist retry context atomically: path=%s", path)
        raise
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def load_state(root: str) -> Dict[str, Any]:
    path = context_state_path(root)
    if not os.path.exists(path):
        return {"tasks": {}, "updatedAt": ""}
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except Exception:
        LOGGER.warning("failed to load retry context state: path=%s", path, exc_info=True)
        return {"tasks": {}, "updatedAt": ""}
    tasks = loaded.get("tasks") if isinstance(loaded.get("tasks"), dict) else {}
    return {"tasks": tasks, "updatedAt": str(loaded.get("updatedAt") or "")}


def save_state(root: str, state: Dict[str, Any]) -> None:
    with _STATE_LOCK:
        path = context_state_path(root)
        payload = {
            "tasks": state.get("tasks") if isinstance(state.get("tasks"), dict) else {},
            "updatedAt": now_iso(),
        }
        _write_json_atomic(path, payload)


def _normalize_decisions(value: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not isinstance(value, list):
        return out
    for raw in value:
        if not isinstance(raw, dict):
            continue
        row = {
            "at": str(raw.get("at") or ""),
            "decision": clip(raw.get("decision"), 40),
            "reasonCode": clip(raw.get("reasonCode"), 80),
            "agent": clip(raw.get("agent"), 80),
            "executor": clip(raw.get("executor"), 80),
        }
        if row["decision"] or row["reasonCode"]:
            out.append(row)
        if len(out) >= MAX_RECENT_DECISIONS:
            break
    return out


def record_failure(
    root: str,
    task_id: str,
    agent: str,
    executor: str,
    prompt_text: str = "",
    output_text: str = "",
    blocked_reason: str = "",
    artifact_index: Any = None,
    unfinished_checklist: Any = None,
    decision: str = "blocked",
    reason_code: str = "",
) -> Dict[str, Any]:
    task_key = str(task_id or "").strip()
    if not task_key:
        return {}
    with _STATE_LOCK:
        state = load_state(root)
        tasks = state.setdefault("tasks", {})
        existing = tasks.get(task_key) if isinstance(tasks.get(task_key), dict) else {}
        now = now_iso()

        reason = clip(blocked_reason or reason_code or "blocked", 120)
        artifacts = _normalize_list(artifact_index, MAX_ARTIFACTS)
        if not artifacts:
            artifacts = _extract_artifacts_from_text(output_text, limit=MAX_ARTIFACTS)
        if not artifacts and isinstance(existing.get("artifactIndex"), list):
            artifacts = _normalize_list(existing.get("artifactIndex"), MAX_ARTIFACTS)

        checklist = _normalize_list(unfinished_checklist, MAX_CHECKLIST)
        if not checklist and isinstance(existing.get("unfinishedChecklist"), list):
            checklist = _normalize_list(existing.get("unfinishedChecklist"), MAX_CHECKLIST)

        recent = _normalize_decisions(existing.get("recentDecisions"))
        recent.append(
            {
                "at": now,
                "decision": clip(decision or "blocked", 40),
                "reasonCode": clip(reason_code or reason, 80),
                "agent": clip(agent, 80),
                "executor": clip(executor, 80),
            }
        )
        if len(recent) > MAX_RECENT_DECISIONS:
            recent = recent[-MAX_RECENT_DECISIONS:]

        entry = {
            "taskId": task_key,
            "agent": clip(agent, 80),
            "executor": clip(executor, 80),
            "updatedAt": now,
            "lastPromptDigest": digest_text(prompt_text),
            "lastOutputDigest": digest_text(output_text),
            "blockedReason": reason,
            "artifactIndex": artifacts,
            "unfinishedChecklist": checklist,
            "recentDecisions": recent,
        }
        tasks[task_key] = entry
        save_state(root, state)
        return dict(entry)


def clear_task(root: str, task_id: str) -> Dict[str, Any]:
    task_key = str(task_id or "").strip()
    if not task_key:
        return {"cleared": False}
    with _STATE_LOCK:
        state = load_state(root)
        tasks = state.setdefault("tasks", {})
        existed = task_key in tasks
        tasks.pop(task_key, None)
        save_state(root, state)
        return {"cleared": existed, "taskId": task_key}


def build_retry_context(root: str, task_id: str) -> Dict[str, Any]:
    task_key = str(task_id or "").strip()
    if not task_key:
        return {}
    with _STATE_LOCK:
        state = load_state(root)
        tasks = state.get("tasks") if isinstance(state.get("tasks"), dict) else {}
        raw = tasks.get(task_key)
        if not isinstance(raw, dict):
            return {}

        decisions = _normalize_decisions(raw.get("recentDecisions"))
        return {
            "taskId": task_key,
            "agent": clip(raw.get("agent"), 80),
            "executor": clip(raw.get("executor"), 80),
            "updatedAt": str(raw.get("updatedAt") or ""),
            "lastPromptDigest": str(raw.get("lastPromptDigest") or ""),
            "lastOutputDigest": str(raw.get("lastOutputDigest") or ""),
            "blockedReason": str(raw.get("blockedReason") or ""),
            "artifactIndex": _normalize_list(raw.get("artifactIndex"), MAX_ARTIFACTS),
            "unfinishedChecklist": _normalize_list(raw.get("unfinishedChecklist"), MAX_CHECKLIST),
            "recentDecisions": decisions,
        }
