#!/usr/bin/env python3
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional


CONTROL_FILE = "governance.control.json"
AUDIT_FILE = "governance.audit.jsonl"
APPROVAL_STATUSES = {"pending", "approved", "rejected"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def control_state_path(root: str) -> str:
    return os.path.join(root, "state", CONTROL_FILE)


def audit_log_path(root: str) -> str:
    return os.path.join(root, "state", AUDIT_FILE)


def default_control_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "paused": False,
        "frozen": False,
        "aborts": {"global": 0, "autopilot": 0, "scheduler": 0, "tasks": {}},
        "approvals": {},
        "updatedAt": now_iso(),
    }


def _as_int(value: Any, default: int = 0) -> int:
    try:
        n = int(value)
    except Exception:
        n = int(default)
    return max(0, n)


def canonical_agent(agent: Any) -> str:
    return str(agent or "").strip().lower()


def _normalize_aborts(raw: Any) -> Dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    tasks_raw = data.get("tasks") if isinstance(data.get("tasks"), dict) else {}
    tasks: Dict[str, int] = {}
    for key, val in tasks_raw.items():
        task_id = str(key or "").strip().upper()
        if not task_id:
            continue
        count = _as_int(val, 0)
        if count > 0:
            tasks[task_id] = count
    return {
        "global": _as_int(data.get("global"), 0),
        "autopilot": _as_int(data.get("autopilot"), 0),
        "scheduler": _as_int(data.get("scheduler"), 0),
        "tasks": tasks,
    }


def _normalize_approvals(raw: Any) -> Dict[str, Dict[str, Any]]:
    src = raw if isinstance(raw, dict) else {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, obj in src.items():
        record = obj if isinstance(obj, dict) else {}
        approval_id = str(record.get("id") or key or "").strip()
        if not approval_id:
            continue
        status = str(record.get("status") or "pending").strip().lower()
        if status not in APPROVAL_STATUSES:
            status = "pending"
        target = record.get("target") if isinstance(record.get("target"), dict) else {}
        out[approval_id] = {
            "id": approval_id,
            "status": status,
            "target": target,
            "reason": str(record.get("reason") or ""),
            "createdAt": str(record.get("createdAt") or ""),
            "updatedAt": str(record.get("updatedAt") or ""),
            "decidedAt": str(record.get("decidedAt") or ""),
            "decidedBy": str(record.get("decidedBy") or ""),
        }
    return out


def normalize_control_state(raw: Any) -> Dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    return {
        "version": int(data.get("version") or 1),
        "paused": bool(data.get("paused")),
        "frozen": bool(data.get("frozen")),
        "aborts": _normalize_aborts(data.get("aborts")),
        "approvals": _normalize_approvals(data.get("approvals")),
        "updatedAt": str(data.get("updatedAt") or ""),
    }


def _ensure_runtime_files(root: str) -> None:
    state_dir = os.path.join(root, "state")
    os.makedirs(state_dir, exist_ok=True)
    control_path = control_state_path(root)
    if not os.path.exists(control_path):
        with open(control_path, "w", encoding="utf-8") as f:
            json.dump(default_control_state(), f, ensure_ascii=True, indent=2)
            f.write("\n")
    audit_path = audit_log_path(root)
    if not os.path.exists(audit_path):
        with open(audit_path, "w", encoding="utf-8"):
            pass


def load_control_state(root: str) -> Dict[str, Any]:
    _ensure_runtime_files(root)
    path = control_state_path(root)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = default_control_state()
    return normalize_control_state(raw)


def save_control_state(root: str, state: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_runtime_files(root)
    normalized = normalize_control_state(state)
    normalized["updatedAt"] = now_iso()
    path = control_state_path(root)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=True, indent=2)
        f.write("\n")
    return normalized


def _read_last_hash(path: str) -> str:
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        return ""
    for line in reversed(lines):
        s = line.strip()
        if not s:
            continue
        try:
            row = json.loads(s)
            h = str(row.get("hash") or "").strip()
            if h:
                return h
        except Exception:
            continue
    return ""


def append_audit(root: str, actor: str, action: str, target: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_runtime_files(root)
    path = audit_log_path(root)
    prev_hash = _read_last_hash(path)
    core = {
        "at": now_iso(),
        "actor": str(actor or "unknown"),
        "action": str(action or ""),
        "target": target if isinstance(target, dict) else {"value": str(target or "")},
        "result": result if isinstance(result, dict) else {"value": str(result or "")},
        "prevHash": prev_hash,
    }
    digest = hashlib.sha256(
        json.dumps(core, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    row = dict(core)
    row["hash"] = digest
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=True) + "\n")
    return row


def summarize_state(state: Dict[str, Any]) -> Dict[str, Any]:
    approvals = state.get("approvals") if isinstance(state.get("approvals"), dict) else {}
    counts = {"pending": 0, "approved": 0, "rejected": 0}
    for item in approvals.values():
        status = str((item or {}).get("status") or "pending")
        if status in counts:
            counts[status] += 1
    aborts = state.get("aborts") if isinstance(state.get("aborts"), dict) else {}
    task_aborts = aborts.get("tasks") if isinstance(aborts.get("tasks"), dict) else {}
    return {
        "paused": bool(state.get("paused")),
        "frozen": bool(state.get("frozen")),
        "aborts": {
            "global": _as_int(aborts.get("global"), 0),
            "autopilot": _as_int(aborts.get("autopilot"), 0),
            "scheduler": _as_int(aborts.get("scheduler"), 0),
            "tasks": {k: _as_int(v, 0) for k, v in task_aborts.items() if _as_int(v, 0) > 0},
        },
        "approvalCounts": counts,
        "updatedAt": str(state.get("updatedAt") or ""),
    }


def _consume_abort_counter(aborts: Dict[str, Any], key: str) -> bool:
    count = _as_int(aborts.get(key), 0)
    if count <= 0:
        return False
    next_count = count - 1
    if next_count > 0:
        aborts[key] = next_count
    else:
        aborts[key] = 0
    return True


def _consume_task_abort(aborts: Dict[str, Any], task_id: str) -> bool:
    tasks = aborts.get("tasks") if isinstance(aborts.get("tasks"), dict) else {}
    key = str(task_id or "").strip().upper()
    if not key:
        return False
    count = _as_int(tasks.get(key), 0)
    if count <= 0:
        return False
    next_count = count - 1
    if next_count > 0:
        tasks[key] = next_count
    else:
        tasks.pop(key, None)
    aborts["tasks"] = tasks
    return True


def checkpoint_dispatch(root: str, actor: str, task_id: str, agent: str) -> Dict[str, Any]:
    state = load_control_state(root)
    if bool(state.get("frozen")):
        return {"allowed": False, "reason": "governance_frozen", "state": summarize_state(state)}

    aborts = state.get("aborts") if isinstance(state.get("aborts"), dict) else {}
    consumed: Optional[Dict[str, Any]] = None
    task_norm = str(task_id or "").strip().upper()
    agent_norm = canonical_agent(agent)
    if _consume_task_abort(aborts, task_norm):
        consumed = {"scope": "task", "taskId": task_norm}
    elif _consume_abort_counter(aborts, "global"):
        consumed = {"scope": "global"}
    if consumed:
        state["aborts"] = aborts
        state = save_control_state(root, state)
        append_audit(
            root,
            actor,
            "checkpoint.dispatch",
            {"taskId": task_norm, "agent": agent_norm},
            {"allowed": False, "reason": "governance_aborted", "consumed": consumed},
        )
        return {
            "allowed": False,
            "reason": "governance_aborted",
            "consumed": consumed,
            "state": summarize_state(state),
        }

    approvals = state.get("approvals") if isinstance(state.get("approvals"), dict) else {}
    for approval_id in sorted(approvals.keys()):
        item = approvals.get(approval_id) or {}
        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        if str(target.get("type") or "").strip().lower() != "dispatch":
            continue
        target_task = str(target.get("taskId") or "").strip().upper()
        if target_task and target_task != task_norm:
            continue
        target_agent = canonical_agent(target.get("agent"))
        if target_agent and target_agent != agent_norm:
            continue
        status = str(item.get("status") or "pending").strip().lower()
        if status == "pending":
            return {
                "allowed": False,
                "reason": "approval_required",
                "approvalId": approval_id,
                "state": summarize_state(state),
            }
        if status == "rejected":
            return {
                "allowed": False,
                "reason": "approval_rejected",
                "approvalId": approval_id,
                "state": summarize_state(state),
            }
    return {"allowed": True, "reason": "allowed", "state": summarize_state(state)}


def checkpoint_autopilot(root: str, actor: str) -> Dict[str, Any]:
    state = load_control_state(root)
    if bool(state.get("frozen")):
        return {"allowed": False, "reason": "governance_frozen", "state": summarize_state(state)}
    if bool(state.get("paused")):
        return {"allowed": False, "reason": "governance_paused", "state": summarize_state(state)}

    aborts = state.get("aborts") if isinstance(state.get("aborts"), dict) else {}
    consumed: Optional[Dict[str, Any]] = None
    if _consume_abort_counter(aborts, "autopilot"):
        consumed = {"scope": "autopilot"}
    elif _consume_abort_counter(aborts, "global"):
        consumed = {"scope": "global"}
    if consumed:
        state["aborts"] = aborts
        state = save_control_state(root, state)
        append_audit(
            root,
            actor,
            "checkpoint.autopilot",
            {},
            {"allowed": False, "reason": "governance_aborted", "consumed": consumed},
        )
        return {
            "allowed": False,
            "reason": "governance_aborted",
            "consumed": consumed,
            "state": summarize_state(state),
        }
    return {"allowed": True, "reason": "allowed", "state": summarize_state(state)}


def checkpoint_scheduler(root: str, actor: str) -> Dict[str, Any]:
    state = load_control_state(root)
    if bool(state.get("frozen")):
        return {"allowed": False, "reason": "governance_frozen", "state": summarize_state(state)}
    if bool(state.get("paused")):
        return {"allowed": False, "reason": "governance_paused", "state": summarize_state(state)}

    aborts = state.get("aborts") if isinstance(state.get("aborts"), dict) else {}
    consumed: Optional[Dict[str, Any]] = None
    if _consume_abort_counter(aborts, "scheduler"):
        consumed = {"scope": "scheduler"}
    elif _consume_abort_counter(aborts, "global"):
        consumed = {"scope": "global"}
    if consumed:
        state["aborts"] = aborts
        state = save_control_state(root, state)
        append_audit(
            root,
            actor,
            "checkpoint.scheduler",
            {},
            {"allowed": False, "reason": "governance_aborted", "consumed": consumed},
        )
        return {
            "allowed": False,
            "reason": "governance_aborted",
            "consumed": consumed,
            "state": summarize_state(state),
        }
    return {"allowed": True, "reason": "allowed", "state": summarize_state(state)}


def parse_governance_command(text: str) -> Optional[Dict[str, Any]]:
    s = re.sub(r"\s+", " ", str(text or "").strip())
    m = re.match(r"^治理(?:\s+(.*))?$", s)
    if not m:
        return None
    body = str(m.group(1) or "").strip()
    if not body or body == "状态":
        return {"action": "status"}
    if body == "暂停":
        return {"action": "pause"}
    if body == "恢复":
        return {"action": "resume"}
    if body == "冻结":
        return {"action": "freeze"}
    if body == "解冻":
        return {"action": "unfreeze"}

    m_abort = re.match(r"^中止\s+(.+)$", body)
    if m_abort:
        raw_target = str(m_abort.group(1) or "").strip()
        if raw_target == "全部":
            target = "all"
        elif raw_target == "调度":
            target = "scheduler"
        elif raw_target == "自动推进":
            target = "autopilot"
        else:
            target = str(raw_target).upper()
        return {"action": "abort", "target": target}

    m_approval = re.match(r"^审批\s+(通过|拒绝)\s+([A-Za-z0-9_.:-]+)$", body)
    if m_approval:
        verdict = str(m_approval.group(1))
        approval_id = str(m_approval.group(2))
        if verdict == "通过":
            return {"action": "approve", "approvalId": approval_id}
        return {"action": "reject", "approvalId": approval_id}

    return {"action": "invalid", "error": f"unsupported governance command: {body}"}


def _save_and_audit(root: str, actor: str, action: str, target: Dict[str, Any], state: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    saved = save_control_state(root, state)
    append_audit(root, actor, action, target, result)
    return saved


def execute_governance_command(root: str, actor: str, command: Dict[str, Any]) -> Dict[str, Any]:
    action = str((command or {}).get("action") or "").strip()
    if action == "invalid":
        state = load_control_state(root)
        result = {"ok": False, "action": action, "error": str(command.get("error") or "invalid governance command"), "state": summarize_state(state)}
        append_audit(root, actor, action, {}, {"ok": False, "error": result["error"]})
        return result

    state = load_control_state(root)
    if action == "status":
        summary = summarize_state(state)
        append_audit(root, actor, action, {}, {"ok": True, "state": summary})
        return {"ok": True, "action": action, "state": summary}

    if action == "pause":
        state["paused"] = True
        saved = _save_and_audit(root, actor, action, {"scope": "runtime"}, state, {"ok": True, "paused": True})
        return {"ok": True, "action": action, "state": summarize_state(saved)}

    if action == "resume":
        state["paused"] = False
        saved = _save_and_audit(root, actor, action, {"scope": "runtime"}, state, {"ok": True, "paused": False})
        return {"ok": True, "action": action, "state": summarize_state(saved)}

    if action == "freeze":
        state["frozen"] = True
        saved = _save_and_audit(
            root,
            actor,
            action,
            {"scope": "dispatch/autopilot/scheduler"},
            state,
            {"ok": True, "frozen": True},
        )
        return {"ok": True, "action": action, "state": summarize_state(saved)}

    if action == "unfreeze":
        state["frozen"] = False
        saved = _save_and_audit(
            root,
            actor,
            action,
            {"scope": "dispatch/autopilot/scheduler"},
            state,
            {"ok": True, "frozen": False},
        )
        return {"ok": True, "action": action, "state": summarize_state(saved)}

    if action == "abort":
        target = str((command or {}).get("target") or "").strip()
        aborts = state.get("aborts") if isinstance(state.get("aborts"), dict) else _normalize_aborts({})
        normalized = target
        if target in {"all", "global"}:
            aborts["global"] = _as_int(aborts.get("global"), 0) + 1
            normalized = "all"
        elif target == "autopilot":
            aborts["autopilot"] = _as_int(aborts.get("autopilot"), 0) + 1
            normalized = "autopilot"
        elif target == "scheduler":
            aborts["scheduler"] = _as_int(aborts.get("scheduler"), 0) + 1
            normalized = "scheduler"
        else:
            task_id = str(target).strip().upper()
            if not task_id:
                return {"ok": False, "action": action, "error": "abort target is required", "state": summarize_state(state)}
            tasks = aborts.get("tasks") if isinstance(aborts.get("tasks"), dict) else {}
            tasks[task_id] = _as_int(tasks.get(task_id), 0) + 1
            aborts["tasks"] = tasks
            normalized = task_id
        state["aborts"] = aborts
        saved = _save_and_audit(root, actor, action, {"target": normalized}, state, {"ok": True, "target": normalized})
        return {"ok": True, "action": action, "target": normalized, "state": summarize_state(saved)}

    if action in {"approve", "reject"}:
        approval_id = str((command or {}).get("approvalId") or "").strip()
        if not approval_id:
            return {"ok": False, "action": action, "error": "approvalId is required", "state": summarize_state(state)}
        approvals = state.get("approvals") if isinstance(state.get("approvals"), dict) else {}
        item = approvals.get(approval_id)
        if not isinstance(item, dict):
            return {"ok": False, "action": action, "error": f"approval not found: {approval_id}", "approvalId": approval_id, "state": summarize_state(state)}
        final_status = "approved" if action == "approve" else "rejected"
        item["status"] = final_status
        item["decidedAt"] = now_iso()
        item["decidedBy"] = str(actor or "")
        item["updatedAt"] = now_iso()
        approvals[approval_id] = item
        state["approvals"] = approvals
        saved = _save_and_audit(
            root,
            actor,
            action,
            {"approvalId": approval_id},
            state,
            {"ok": True, "approvalId": approval_id, "status": final_status},
        )
        return {
            "ok": True,
            "action": action,
            "approvalId": approval_id,
            "status": final_status,
            "state": summarize_state(saved),
        }

    result = {"ok": False, "action": action, "error": f"unsupported action: {action}", "state": summarize_state(state)}
    append_audit(root, actor, action, {}, {"ok": False, "error": result["error"]})
    return result


def format_governance_message(result: Dict[str, Any]) -> str:
    action = str((result or {}).get("action") or "")
    if not bool((result or {}).get("ok")):
        return f"[BLOCKED] 治理命令失败: {result.get('error') or 'unknown error'}"
    state = (result or {}).get("state") if isinstance((result or {}).get("state"), dict) else {}
    if action == "status":
        approval_counts = state.get("approvalCounts") if isinstance(state.get("approvalCounts"), dict) else {}
        aborts = state.get("aborts") if isinstance(state.get("aborts"), dict) else {}
        task_abort_count = len(aborts.get("tasks") or {})
        return (
            f"[TASK] 治理状态 | 暂停={'是' if state.get('paused') else '否'} | 冻结={'是' if state.get('frozen') else '否'} | "
            f"中止(global={aborts.get('global', 0)},autopilot={aborts.get('autopilot', 0)},scheduler={aborts.get('scheduler', 0)},tasks={task_abort_count}) | "
            f"审批(pending={approval_counts.get('pending', 0)},approved={approval_counts.get('approved', 0)},rejected={approval_counts.get('rejected', 0)})"
        )
    if action == "pause":
        return "[TASK] 治理已暂停：自动推进与调度推进已阻断。"
    if action == "resume":
        return "[TASK] 治理已恢复：自动推进与调度推进可继续。"
    if action == "freeze":
        return "[TASK] 治理已冻结：dispatch、自动推进与调度已阻断。"
    if action == "unfreeze":
        return "[TASK] 治理已解冻：dispatch、自动推进与调度可继续。"
    if action == "abort":
        return f"[TASK] 已登记中止标记：{result.get('target') or '-'}（命中后一次性消费）。"
    if action == "approve":
        return f"[TASK] 审批已通过：{result.get('approvalId') or '-'}"
    if action == "reject":
        return f"[TASK] 审批已拒绝：{result.get('approvalId') or '-'}"
    return f"[TASK] 治理命令已处理: {action}"
