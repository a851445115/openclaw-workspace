#!/usr/bin/env python3
import json
import math
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


RUNNABLE_STATUSES = {"pending", "claimed", "in_progress", "review"}
STATUS_BONUS = {
    "pending": 0.0,
    "claimed": 2.0,
    "in_progress": 3.0,
    "review": 1.0,
}
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$", flags=re.IGNORECASE)


def _to_number(value: Any, default: float = 0.0) -> float:
    fallback = float(default)
    if value is None:
        return fallback
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        num = float(value)
        return num if math.isfinite(num) else fallback
    text = str(value).strip()
    if not text:
        return fallback
    try:
        num = float(text)
        return num if math.isfinite(num) else fallback
    except Exception:
        return fallback


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _dedupe_keep_order(items: Sequence[str]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for raw in items:
        item = _as_text(raw)
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _split_tokens(text: str) -> List[str]:
    # Accept simple CSV/space separated forms for backwards compatibility.
    if not text:
        return []
    tokens = re.split(r"[\s,;]+", text)
    return [tok.strip() for tok in tokens if tok.strip()]


def _normalize_refs(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return _dedupe_keep_order([_as_text(item) for item in raw])
    if isinstance(raw, dict):
        refs: List[str] = []
        for key in ("taskId", "id", "ref", "value"):
            if key in raw:
                refs.extend(_normalize_refs(raw.get(key)))
        return _dedupe_keep_order(refs)
    text = _as_text(raw)
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
            return _normalize_refs(parsed)
        except Exception:
            pass
    return _dedupe_keep_order(_split_tokens(text))


def _normalize_task_id(raw: Any) -> str:
    task_id = _as_text(raw).upper()
    return task_id


def _status_of(task: Any) -> str:
    if not isinstance(task, dict):
        return ""
    return _as_text(task.get("status")).lower()


def _is_task_id(text: str) -> bool:
    return bool(TASK_ID_PATTERN.match(_normalize_task_id(text)))


def _dependency_blockers(depends_on: Sequence[str], tasks: Dict[str, Dict[str, Any]]) -> List[str]:
    unresolved: List[str] = []
    for dep in depends_on:
        dep_id = _normalize_task_id(dep)
        dep_task = tasks.get(dep_id)
        dep_status = _status_of(dep_task)
        if dep_task is None:
            unresolved.append(f"{dep_id}(missing)")
            continue
        if dep_status != "done":
            unresolved.append(f"{dep_id}({dep_status or 'unknown'})")
    return unresolved


def _blocked_by_reasons(blocked_by: Sequence[str], tasks: Dict[str, Dict[str, Any]]) -> List[str]:
    unresolved: List[str] = []
    for item in blocked_by:
        token = _as_text(item)
        if not token:
            continue
        normalized = _normalize_task_id(token)
        if _is_task_id(normalized):
            ref_task = tasks.get(normalized)
            if ref_task is None:
                # Keep text blocker semantics for unknown refs while still allowing
                # non T-### task IDs to resolve against existing tasks.
                unresolved.append(token)
                continue
            ref_status = _status_of(ref_task)
            if ref_status != "done":
                unresolved.append(f"{normalized}({ref_status or 'unknown'})")
            continue
        # Non-task blockers are treated as unresolved text blockers.
        unresolved.append(token)
    return unresolved


def evaluate_task(task: Dict[str, Any], all_tasks: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    task_id = _normalize_task_id(task.get("taskId"))
    status = _status_of(task)
    depends_on = _normalize_refs(task.get("dependsOn"))
    blocked_by = _normalize_refs(task.get("blockedBy"))

    if status not in RUNNABLE_STATUSES:
        return {
            "taskId": task_id,
            "runnable": False,
            "ready": False,
            "status": status,
            "score": None,
            "priority": _to_number(task.get("priority"), 0.0),
            "impact": _to_number(task.get("impact"), 0.0),
            "dependsOn": depends_on,
            "blockedBy": blocked_by,
            "reasonCode": "status_not_runnable",
            "reason": f"status={status or '-'} not runnable",
        }

    missing_deps = _dependency_blockers(depends_on, all_tasks)
    blockers = _blocked_by_reasons(blocked_by, all_tasks)
    ready = not missing_deps and not blockers
    priority = _to_number(task.get("priority"), 0.0)
    impact = _to_number(task.get("impact"), 0.0)
    score = round((priority * 10.0) + (impact * 5.0) + STATUS_BONUS.get(status, 0.0), 6) if ready else None

    if not ready:
        pieces: List[str] = []
        if missing_deps:
            pieces.append("dependsOn unresolved: " + ", ".join(missing_deps))
        if blockers:
            pieces.append("blockedBy unresolved: " + ", ".join(blockers))
        return {
            "taskId": task_id,
            "runnable": True,
            "ready": False,
            "status": status,
            "score": None,
            "priority": priority,
            "impact": impact,
            "dependsOn": depends_on,
            "blockedBy": blocked_by,
            "reasonCode": "dependencies_unmet",
            "reason": " | ".join(pieces),
        }

    reason = (
        f"ready; score={score:.3f} "
        f"(priority={priority:g}, impact={impact:g}, status={status}, "
        f"dependsOn={len(depends_on)}, blockedBy={len(blocked_by)})"
    )
    return {
        "taskId": task_id,
        "runnable": True,
        "ready": True,
        "status": status,
        "score": score,
        "priority": priority,
        "impact": impact,
        "dependsOn": depends_on,
        "blockedBy": blocked_by,
        "reasonCode": "ready_scored",
        "reason": reason,
    }


def _task_sort_key_for_ready(item: Dict[str, Any]) -> Tuple[float, str]:
    # Stable deterministic selection: highest score first, then taskId ascending.
    return (-_to_number(item.get("score"), 0.0), _normalize_task_id(item.get("taskId")))


def select_task(
    tasks: Dict[str, Any],
    requested_task_id: str = "",
    excluded_task_ids: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    excluded = {_normalize_task_id(x) for x in (excluded_task_ids or set()) if _as_text(x)}

    normalized_tasks: Dict[str, Dict[str, Any]] = {}
    for _, raw in (tasks or {}).items():
        if not isinstance(raw, dict):
            continue
        task_id = _normalize_task_id(raw.get("taskId"))
        if not task_id:
            continue
        normalized_tasks[task_id] = raw

    if requested_task_id:
        req_id = _normalize_task_id(requested_task_id)
        req_task = normalized_tasks.get(req_id)
        if req_task is None or req_id in excluded:
            return {
                "selectedTaskId": "",
                "selectedTask": None,
                "selectedScore": None,
                "reasonCode": "requested_not_found_or_excluded",
                "reason": f"requested task unavailable: {req_id}",
                "readyQueue": [],
                "evaluations": {},
                "selection": {
                    "taskId": req_id,
                    "score": None,
                    "reasonCode": "requested_not_found_or_excluded",
                    "reason": f"requested task unavailable: {req_id}",
                },
            }
        eval_obj = evaluate_task(req_task, normalized_tasks)
        if not bool(eval_obj.get("ready")):
            reason = str(eval_obj.get("reason") or "requested task is not ready")
            return {
                "selectedTaskId": "",
                "selectedTask": None,
                "selectedScore": None,
                "reasonCode": "requested_task_not_ready",
                "reason": reason,
                "readyQueue": [],
                "evaluations": {req_id: eval_obj},
                "selection": {
                    "taskId": req_id,
                    "score": None,
                    "reasonCode": "requested_task_not_ready",
                    "reason": reason,
                },
            }
        selection = {
            "taskId": req_id,
            "score": _to_number(eval_obj.get("score"), 0.0),
            "reasonCode": "requested_task_selected",
            "reason": "requested task selected from ready state",
        }
        return {
            "selectedTaskId": req_id,
            "selectedTask": req_task,
            "selectedScore": selection["score"],
            "reasonCode": "requested_task_selected",
            "reason": "requested task selected from ready state",
            "readyQueue": [],
            "evaluations": {req_id: eval_obj},
            "selection": selection,
        }

    evaluations: Dict[str, Dict[str, Any]] = {}
    ready_rows: List[Dict[str, Any]] = []

    for task_id in sorted(normalized_tasks.keys()):
        if task_id in excluded:
            continue
        task = normalized_tasks[task_id]
        eval_obj = evaluate_task(task, normalized_tasks)
        evaluations[task_id] = eval_obj
        if eval_obj.get("ready"):
            ready_rows.append(
                {
                    "taskId": task_id,
                    "score": _to_number(eval_obj.get("score"), 0.0),
                    "reason": str(eval_obj.get("reason") or ""),
                    "reasonCode": str(eval_obj.get("reasonCode") or ""),
                }
            )

    ready_rows.sort(key=_task_sort_key_for_ready)

    if not ready_rows:
        return {
            "selectedTaskId": "",
            "selectedTask": None,
            "selectedScore": None,
            "reasonCode": "no_ready_task",
            "reason": "no task in ready queue",
            "readyQueue": ready_rows,
            "evaluations": evaluations,
            "selection": {
                "taskId": "",
                "score": None,
                "reasonCode": "no_ready_task",
                "reason": "no task in ready queue",
            },
        }

    top = ready_rows[0]
    selected_id = _normalize_task_id(top.get("taskId"))
    selected_task = normalized_tasks.get(selected_id)
    selection = {
        "taskId": selected_id,
        "score": _to_number(top.get("score"), 0.0),
        "reasonCode": str(top.get("reasonCode") or "ready_scored"),
        "reason": str(top.get("reason") or ""),
    }
    return {
        "selectedTaskId": selected_id,
        "selectedTask": selected_task,
        "selectedScore": selection["score"],
        "reasonCode": "selected_from_ready_queue",
        "reason": f"selected {selected_id} from ready queue",
        "readyQueue": ready_rows,
        "evaluations": evaluations,
        "selection": selection,
    }
