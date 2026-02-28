#!/usr/bin/env python3
import argparse
import json
import os
import re
import shlex
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_GROUP_ID = "oc_041146c92a9ccb403a7f4f48fb59701d"
DEFAULT_ACCOUNT_ID = "orchestrator"
DEFAULT_ALLOWED_BROADCASTERS = {"orchestrator"}
OPTIONAL_BROADCASTER = "broadcaster"
CLARIFY_ROLES = {"coder", "invest-analyst", "debugger", "broadcaster"}
BOT_ROLES = set(CLARIFY_ROLES) | {"orchestrator"}
MILESTONE_PREFIXES = ("[TASK]", "[CLAIM]", "[DONE]", "[BLOCKED]", "[DIAG]", "[REVIEW]")
DONE_HINTS = ("[DONE]", " done", "completed", "finish", "完成", "已完成", "通过", "verified")
BLOCKED_HINTS = ("[BLOCKED]", "blocked", "failed", "error", "exception", "失败", "阻塞", "卡住", "无法")
EVIDENCE_HINTS = ("/", ".py", ".md", "http", "截图", "日志", "log", "输出", "result", "测试")
STAGE_ONLY_HINTS = ("接下来", "下一步", "准备", "我先", "随后", "稍后", "计划", "will", "next", "going to", "plan to")
BOT_OPENID_CONFIG_CANDIDATES = (
    os.path.join("config", "feishu-bot-openids.json"),
    os.path.join("state", "feishu-bot-openids.json"),
)
VISIBILITY_MODES = ("milestone_only", "handoff_visible", "full_visible")
ACCEPTANCE_POLICY_CONFIG_CANDIDATES = (
    os.path.join("config", "acceptance-policy.json"),
    os.path.join("state", "acceptance-policy.json"),
)
DEFAULT_ACCEPTANCE_POLICY: Dict[str, Any] = {
    "global": {
        "requireEvidence": True,
    },
    "roles": {
        "coder": {
            "requireAny": ["test", "pytest", "验证", "通过", "日志", "log", "输出", "result"],
        },
        "debugger": {
            "requireAny": ["日志", "log", "error", "异常", "复现", "stack", "trace"],
        },
        "invest-analyst": {
            "requireAny": ["来源", "source", "引用", "link", "数据", "report"],
        },
        "broadcaster": {
            "requireAny": ["公告", "发布", "summary", "broadcast", "同步"],
        },
    },
}
AUTO_PROGRESS_STATE_FILE = "user-friendly.autopilot.json"
AUTO_PROGRESS_DEFAULT_MAX_STEPS = 2
AUTO_PROGRESS_MAX_STEPS_LIMIT = 10
PROJECT_DOC_CANDIDATES = ("PRD.md", "prd.md", "README.md", "readme.md")
DEFAULT_PROJECT_BOOTSTRAP_TASKS = (
    "梳理目标与验收标准（来自项目文档）",
    "拆解可执行里程碑并标注负责人建议",
    "启动首个最小可交付任务并回传证据",
)
TASK_CONTEXT_STATE_FILE = "task-context-map.json"
DEFAULT_CODER_WORKSPACE = os.path.expanduser("~/.openclaw/agents/coder/workspace")
SCHEDULER_STATE_FILE = "scheduler.kernel.json"
SCHEDULER_DEFAULT_INTERVAL_SEC = 300
SCHEDULER_MIN_INTERVAL_SEC = 60
SCHEDULER_MAX_INTERVAL_SEC = 86400
SCHEDULER_DEFAULT_MAX_STEPS = 1
SCHEDULER_DAEMON_STATE_FILE = "scheduler.daemon.json"
SCHEDULER_DAEMON_DEFAULT_POLL_SEC = 5
SCHEDULER_DAEMON_MIN_POLL_SEC = 0
SCHEDULER_DAEMON_MAX_POLL_SEC = 3600


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clip(text: Optional[str], limit: int = 160) -> str:
    s = " ".join((text or "").split())
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "..."


def load_bot_mentions(root: str) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    script_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    search_roots = [root, script_root]

    for base in search_roots:
        for rel in BOT_OPENID_CONFIG_CANDIDATES:
            path = os.path.join(base, rel)
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception:
                continue

            entries: Dict[str, Any] = {}
            if isinstance(raw, dict):
                role_map = raw.get("byRole")
                acct_map = raw.get("byAccountId")
                if isinstance(role_map, dict):
                    entries.update(role_map)
                if isinstance(acct_map, dict):
                    for k, v in acct_map.items():
                        entries.setdefault(k, v)
                if not entries:
                    entries = raw

            for role, info in entries.items():
                if not isinstance(role, str) or not isinstance(info, dict):
                    continue
                open_id = str(info.get("open_id") or info.get("openId") or "").strip()
                name = str(info.get("name") or role).strip() or role
                if not open_id:
                    continue
                out[role] = {"open_id": open_id, "name": name}

            if out:
                return out

    return out


def mention_tag_for(role: str, mentions: Dict[str, Dict[str, str]], fallback: str = "") -> str:
    info = mentions.get(role)
    if not isinstance(info, dict):
        return fallback or f"@{role}"
    open_id = str(info.get("open_id") or "").strip()
    if not open_id:
        return fallback or f"@{role}"
    name = str(info.get("name") or role).strip() or role
    safe_name = name.replace("<", "").replace(">", "")
    return f'<at user_id="{open_id}">{safe_name}</at>'


def contains_mention(text: str, role: str, mentions: Dict[str, Dict[str, str]]) -> bool:
    if f"@{role}" in text.lower():
        return True

    info = mentions.get(role)
    if not isinstance(info, dict):
        return False

    open_id = str(info.get("open_id") or "").strip()
    if open_id:
        pat = rf'<at\b[^>]*\buser_id\s*=\s*["\']{re.escape(open_id)}["\']'
        if re.search(pat, text, flags=re.IGNORECASE):
            return True

    name = str(info.get("name") or role).strip()
    if name:
        name_pat = rf"<at\b[^>]*>\s*{re.escape(name)}\s*</at>"
        if re.search(name_pat, text, flags=re.IGNORECASE):
            return True

    return False


def parse_json_loose(text: str) -> Any:
    s = (text or "").strip()
    if not s:
        raise ValueError("empty output")
    try:
        return json.loads(s)
    except Exception:
        pass
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        return json.loads(s[start : end + 1])
    raise ValueError(f"no json object found in output: {clip(s, 200)}")


def ensure_state(root: str) -> Tuple[str, str]:
    state_dir = os.path.join(root, "state")
    locks_dir = os.path.join(state_dir, "locks")
    os.makedirs(locks_dir, exist_ok=True)
    jsonl = os.path.join(state_dir, "tasks.jsonl")
    snapshot = os.path.join(state_dir, "tasks.snapshot.json")
    if not os.path.exists(jsonl):
        with open(jsonl, "w", encoding="utf-8"):
            pass
    if not os.path.exists(snapshot):
        data = {"tasks": {}, "meta": {"version": 2, "updatedAt": now_iso()}}
        with open(snapshot, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=True, indent=2)
            f.write("\n")
    return jsonl, snapshot


def load_snapshot(root: str) -> Dict[str, Any]:
    _, snapshot = ensure_state(root)
    with open(snapshot, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "tasks" not in data or not isinstance(data["tasks"], dict):
        raise ValueError("invalid snapshot format: tasks must be object")
    return data


def actor_allowed(actor: str, allow_broadcaster: bool) -> bool:
    allowed = set(DEFAULT_ALLOWED_BROADCASTERS)
    if allow_broadcaster:
        allowed.add(OPTIONAL_BROADCASTER)
    return actor in allowed


STATUS_ZH = {
    "pending": "待处理",
    "claimed": "已认领",
    "in_progress": "进行中",
    "review": "待复核",
    "done": "已完成",
    "blocked": "阻塞",
    "failed": "失败",
}
STATUS_DISPLAY_ORDER = ["pending", "claimed", "in_progress", "review", "done", "blocked", "failed"]
STATUS_PENDING_BUCKET = {"pending", "claimed", "in_progress", "review"}


def status_zh(status: str) -> str:
    s = (status or "").strip()
    return STATUS_ZH.get(s, s or "-")


def sort_tasks_for_status(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        tasks,
        key=lambda t: (
            str(t.get("updatedAt") or ""),
            str(t.get("taskId") or ""),
        ),
        reverse=True,
    )


def format_status_entry(task: Dict[str, Any], kind: str, title_limit: int, extra_limit: int) -> str:
    task_id = str(task.get("taskId") or "-")
    title = clip(task.get("title") or "未命名任务", title_limit)
    if kind == "blocked":
        reason = clip(task.get("blockedReason") or "未填原因", extra_limit)
        return f"{task_id} {title}（{reason}）"
    assignee = task.get("owner") or task.get("assigneeHint") or "未指派"
    return f"{task_id} {title}（{clip(str(assignee), extra_limit)}）"


def format_status_summary_message(tasks: Dict[str, Any], full: bool = False) -> Tuple[str, Dict[str, int]]:
    counts: Dict[str, int] = {}
    rows: List[Dict[str, Any]] = []
    for raw in tasks.values():
        if not isinstance(raw, dict):
            continue
        rows.append(raw)
        st = str(raw.get("status") or "pending")
        counts[st] = counts.get(st, 0) + 1

    total = len(rows)
    blocked_tasks = sort_tasks_for_status([t for t in rows if str(t.get("status") or "") == "blocked"])
    pending_tasks = sort_tasks_for_status(
        [t for t in rows if str(t.get("status") or "pending") in STATUS_PENDING_BUCKET]
    )

    top_n = 6 if full else 3
    title_limit = 28 if full else 18
    extra_limit = 20 if full else 12
    max_chars = 1200 if full else 500

    blocked_items = [format_status_entry(t, "blocked", title_limit, extra_limit) for t in blocked_tasks[:top_n]]
    pending_items = [format_status_entry(t, "pending", title_limit, extra_limit) for t in pending_tasks[:top_n]]

    ordered = [k for k in STATUS_DISPLAY_ORDER if counts.get(k)]
    tail = sorted([k for k in counts if k not in STATUS_DISPLAY_ORDER])
    counts_text = "、".join([f"{status_zh(k)}{counts[k]}" for k in ordered + tail]) or "暂无任务"

    header = f"[TASK] 看板汇总 | 总数{total} | {counts_text}"
    blocked_line = f"阻塞Top{top_n}: " + ("；".join(blocked_items) if blocked_items else "无")
    pending_line = f"待推进Top{top_n}: " + ("；".join(pending_items) if pending_items else "无")
    lines = [header, blocked_line, pending_line]

    while len("\n".join(lines)) > max_chars and (blocked_items or pending_items):
        if len(blocked_items) >= len(pending_items) and blocked_items:
            blocked_items.pop()
        elif pending_items:
            pending_items.pop()
        blocked_line = f"阻塞Top{top_n}: " + ("；".join(blocked_items) if blocked_items else "无")
        pending_line = f"待推进Top{top_n}: " + ("；".join(pending_items) if pending_items else "无")
        lines = [header, blocked_line, pending_line]

    msg = "\n".join(lines)
    if len(msg) > max_chars:
        msg = header

    return msg, counts


def build_three_line(prefix: str, task_id: str, status: str, owner_or_hint: str, key_line: str) -> str:
    line1 = f"{prefix} {task_id} | 状态={status_zh(status)} | {owner_or_hint}"
    return f"{line1}\n{key_line.strip()}"


def normalize_string_list(value: Any, limit: int = 6, item_limit: int = 180) -> List[str]:
    out: List[str] = []
    if isinstance(value, str):
        s = clip(value, item_limit)
        if s:
            out.append(s)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                s = clip(item, item_limit)
                if s:
                    out.append(s)
            elif isinstance(item, dict):
                text = clip(json.dumps(item, ensure_ascii=False), item_limit)
                if text:
                    out.append(text)
            else:
                s = clip(str(item), item_limit)
                if s:
                    out.append(s)
            if len(out) >= limit:
                break
    return out[:limit]


def compact_event_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return clip(str(payload), 120)
    compact: Dict[str, str] = {}
    for key in ("from", "to", "owner", "result", "blockedReason", "review", "relatedTo", "title"):
        if key in payload and payload.get(key) is not None:
            compact[key] = clip(str(payload.get(key)), 120)
    if compact:
        return compact
    return clip(json.dumps(payload, ensure_ascii=False), 120)


def read_recent_task_events(root: str, task_id: str, limit: int = 8) -> List[Dict[str, Any]]:
    jsonl, _ = ensure_state(root)
    rows: List[Dict[str, Any]] = []
    try:
        with open(jsonl, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except Exception:
                    continue
                if str(obj.get("taskId") or "") != task_id:
                    continue
                rows.append(
                    {
                        "at": str(obj.get("at") or ""),
                        "actor": str(obj.get("actor") or ""),
                        "type": str(obj.get("type") or ""),
                        "messageType": str(obj.get("messageType") or ""),
                        "payload": compact_event_payload(obj.get("payload")),
                    }
                )
    except Exception:
        return []
    return rows[-limit:]


def build_prompt_board_snapshot(root: str, focus_task_id: str, top_n: int = 3) -> Dict[str, Any]:
    data = load_snapshot(root)
    tasks = data.get("tasks", {})
    counts: Dict[str, int] = {}
    pending: List[Dict[str, str]] = []
    blocked: List[Dict[str, str]] = []

    for raw in tasks.values():
        if not isinstance(raw, dict):
            continue
        st = str(raw.get("status") or "pending")
        counts[st] = counts.get(st, 0) + 1
        rec = {
            "taskId": str(raw.get("taskId") or ""),
            "title": clip(str(raw.get("title") or ""), 80),
            "owner": str(raw.get("owner") or raw.get("assigneeHint") or ""),
            "status": st,
            "updatedAt": str(raw.get("updatedAt") or ""),
        }
        if st == "blocked":
            blocked.append(rec)
        if st in STATUS_PENDING_BUCKET:
            pending.append(rec)

    pending = sorted(pending, key=lambda x: (x.get("updatedAt") or "", x.get("taskId") or ""), reverse=True)[:top_n]
    blocked = sorted(blocked, key=lambda x: (x.get("updatedAt") or "", x.get("taskId") or ""), reverse=True)[:top_n]
    return {
        "counts": counts,
        "focusTaskId": focus_task_id,
        "pendingTop": pending,
        "blockedTop": blocked,
    }


def infer_task_kind(agent: str, title: str, dispatch_task: str) -> str:
    agent_norm = (agent or "").strip().lower()
    text = f"{title} {dispatch_task}".lower()
    if agent_norm == "debugger" or any(k in text for k in ("debug", "bug", "故障", "异常", "排查", "trace", "error")):
        return "debug"
    if agent_norm == "invest-analyst" or any(k in text for k in ("research", "分析", "调研", "source", "report")):
        return "research"
    if agent_norm == "broadcaster" or any(k in text for k in ("broadcast", "公告", "发布", "summary", "同步")):
        return "broadcast"
    return "coding"


def requirements_for_kind(kind: str) -> List[str]:
    if kind == "debug":
        return [
            "先定位根因，再给修复建议或修复结果。",
            "必须包含复现/日志/错误栈中的至少一项证据。",
            "若无法修复，给出明确阻塞原因和下一步建议。",
        ]
    if kind == "research":
        return [
            "先给结论，再给依据列表。",
            "证据至少包含来源链接、文档路径或数据摘要。",
            "输出需明确推荐方案与权衡。",
        ]
    if kind == "broadcast":
        return [
            "输出应面向群成员可直接转发或发布。",
            "明确对象、目的、发布时间或触发条件。",
            "如信息不足，返回 blocked 并给缺失字段清单。",
        ]
    return [
        "优先完成最小可交付改动，再补充验证。",
        "结果必须包含可验证证据（测试、日志、文件、命令输出）。",
        "如遇阻塞，返回 blocked 并说明根因与下一步。",
    ]


def build_structured_output_schema(task_id: str, agent: str) -> Dict[str, Any]:
    return {
        "taskId": task_id,
        "agent": agent,
        "status": "done|blocked|progress",
        "summary": "一句话结果摘要",
        "changes": [{"path": "文件路径", "summary": "改动说明"}],
        "evidence": ["日志/命令输出/截图路径/链接"],
        "risks": ["潜在风险或注意事项"],
        "nextActions": ["下一步建议（可为空）"],
    }


def build_agent_prompt(root: str, task: Dict[str, Any], agent: str, dispatch_task: str) -> str:
    task_id = str(task.get("taskId") or "")
    title = str(task.get("title") or "")
    project_path = lookup_task_project_path(root, task_id)
    task_kind = infer_task_kind(agent, title, dispatch_task)
    requirements = requirements_for_kind(task_kind)
    schema = build_structured_output_schema(task_id, agent)
    board_snapshot = build_prompt_board_snapshot(root, task_id)
    history = read_recent_task_events(root, task_id, limit=8)

    task_context = {
        "taskId": task_id,
        "title": clip(title, 120),
        "currentStatus": str(task.get("status") or ""),
        "owner": str(task.get("owner") or ""),
        "assigneeHint": str(task.get("assigneeHint") or ""),
        "projectId": str(task.get("projectId") or ""),
        "relatedTo": str(task.get("relatedTo") or ""),
        "objective": clip(dispatch_task, 320),
    }
    if project_path:
        task_context["projectPath"] = project_path

    lines = [
        "SYSTEM_ROLE: You are a specialist execution agent in a multi-agent project team.",
        "TASK_CONTEXT:",
        json.dumps(task_context, ensure_ascii=False, indent=2),
        "BOARD_SNAPSHOT:",
        json.dumps(board_snapshot, ensure_ascii=False, indent=2),
        "TASK_RECENT_HISTORY:",
        json.dumps(history, ensure_ascii=False, indent=2),
        "EXECUTION_REQUIREMENTS:",
    ]
    for idx, item in enumerate(requirements, start=1):
        lines.append(f"{idx}. {item}")
    lines.extend(
        [
            "OUTPUT_SCHEMA:",
            json.dumps(schema, ensure_ascii=False, indent=2),
            "OUTPUT_RULES:",
            "1. Return one valid JSON object only (no markdown fence, no extra text).",
            "2. Keep taskId and agent fields consistent with TASK_CONTEXT.",
            "3. status=done must include concrete evidence entries.",
            "4. If blocked, summary must state blocker cause clearly.",
        ]
    )
    prompt = "\n".join(lines)
    if len(prompt) <= 5000:
        return prompt
    return prompt[:4999] + "..."


def send_group_message(group_id: str, account_id: str, text: str, mode: str) -> Dict[str, Any]:
    payload = {
        "channel": "feishu",
        "accountId": account_id,
        "target": f"chat:{group_id}",
        "text": text,
        "mode": mode,
    }
    if mode == "dry-run":
        return {"ok": True, "dryRun": True, "payload": payload}
    cmd = [
        "openclaw",
        "message",
        "send",
        "--channel",
        "feishu",
        "--account",
        account_id,
        "--target",
        f"chat:{group_id}",
        "--message",
        text,
        "--json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=45)
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": f"send failed (exit={proc.returncode})",
            "stdout": clip(stdout, 500),
            "stderr": clip(stderr, 500),
            "payload": payload,
        }
    out = {"ok": True, "dryRun": False, "payload": payload}
    try:
        if stdout:
            out["result"] = parse_json_loose(stdout)
    except Exception:
        pass
    if stderr:
        out["stderr"] = clip(stderr, 500)
    return out


def send_group_card(group_id: str, account_id: str, card: Dict[str, Any], mode: str, fallback_text: str = "") -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "channel": "feishu",
        "accountId": account_id,
        "target": f"chat:{group_id}",
        "card": card,
        "mode": mode,
    }
    if fallback_text:
        payload["text"] = fallback_text

    if mode == "dry-run":
        return {"ok": True, "dryRun": True, "payload": payload}

    cmd = [
        "openclaw",
        "message",
        "send",
        "--channel",
        "feishu",
        "--account",
        account_id,
        "--target",
        f"chat:{group_id}",
        "--card",
        json.dumps(card, ensure_ascii=False),
        "--json",
    ]
    if fallback_text:
        cmd.extend(["--message", fallback_text])

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=45)
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": f"card send failed (exit={proc.returncode})",
            "stdout": clip(stdout, 500),
            "stderr": clip(stderr, 500),
            "payload": payload,
        }

    out = {"ok": True, "dryRun": False, "payload": payload}
    try:
        if stdout:
            out["result"] = parse_json_loose(stdout)
    except Exception:
        pass
    if stderr:
        out["stderr"] = clip(stderr, 500)
    return out


def board_apply(root: str, actor: str, text: str) -> Dict[str, Any]:
    script_dir = os.path.dirname(__file__)
    board_py = os.path.join(script_dir, "task_board.py")
    cmd = ["python3", board_py, "apply", "--root", root, "--actor", actor, "--text", text]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=45)
    obj = parse_json_loose(proc.stdout or "{}")
    if proc.returncode != 0 and obj.get("ok") is True:
        obj["ok"] = False
    return obj


def build_apply_messages(
    root: str, apply_obj: Dict[str, Any], include_escalate_blocked: bool
) -> List[Dict[str, str]]:
    data = load_snapshot(root)
    tasks: Dict[str, Any] = data.get("tasks", {})
    intent = apply_obj.get("intent")
    if not apply_obj.get("ok", False):
        return []

    def get_task(tid: Optional[str]) -> Optional[Dict[str, Any]]:
        if not tid:
            return None
        task = tasks.get(tid)
        if isinstance(task, dict):
            return task
        return None

    messages: List[Dict[str, str]] = []

    if intent == "create_task":
        tid = apply_obj.get("taskId")
        task = get_task(tid)
        if task:
            text = build_three_line(
                "[TASK]",
                tid,
                str(task.get("status", "pending")),
                f"建议负责人={task.get('assigneeHint') or '-'}",
                f"标题: {clip(task.get('title') or '未命名任务')}",
            )
            messages.append({"prefix": "[TASK]", "taskId": tid, "text": text})
        return messages

    if intent == "claim_task":
        tid = apply_obj.get("taskId")
        task = get_task(tid)
        if task:
            text = build_three_line(
                "[CLAIM]",
                tid,
                str(task.get("status", "claimed")),
                f"负责人={task.get('owner') or '-'}",
                f"标题: {clip(task.get('title') or '未命名任务')}",
            )
            messages.append({"prefix": "[CLAIM]", "taskId": tid, "text": text})
        return messages

    if intent == "mark_done":
        tid = apply_obj.get("taskId")
        task = get_task(tid)
        if task:
            text = build_three_line(
                "[DONE]",
                tid,
                str(task.get("status", "done")),
                f"负责人={task.get('owner') or '-'}",
                f"结果: {clip(task.get('result') or '完成')}",
            )
            messages.append({"prefix": "[DONE]", "taskId": tid, "text": text})
        return messages

    if intent == "block_task":
        tid = apply_obj.get("taskId")
        task = get_task(tid)
        if task:
            text = build_three_line(
                "[BLOCKED]",
                tid,
                str(task.get("status", "blocked")),
                f"负责人={task.get('owner') or '-'}",
                f"原因: {clip(task.get('blockedReason') or '未填写')}",
            )
            messages.append({"prefix": "[BLOCKED]", "taskId": tid, "text": text})
        return messages

    if intent == "escalate_task":
        blocked_tid = apply_obj.get("taskId")
        diag_tid = apply_obj.get("diagTaskId")
        blocked_task = get_task(blocked_tid)
        diag_task = get_task(diag_tid)
        if include_escalate_blocked and blocked_task:
            text = build_three_line(
                "[BLOCKED]",
                blocked_tid,
                str(blocked_task.get("status", "blocked")),
                f"负责人={blocked_task.get('owner') or '-'}",
                f"原因: {clip(blocked_task.get('blockedReason') or '未填写')}",
            )
            messages.append({"prefix": "[BLOCKED]", "taskId": blocked_tid, "text": text})
        if diag_task:
            detail = f"诊断内容: {clip(diag_task.get('title') or '诊断跟进')}"
            related = diag_task.get("relatedTo")
            if related:
                detail = f"{detail} | 关联={related}"
            text = build_three_line(
                "[DIAG]",
                diag_tid,
                str(diag_task.get("status", "pending")),
                f"指派={diag_task.get('assigneeHint') or 'debugger'}",
                detail,
            )
            messages.append({"prefix": "[DIAG]", "taskId": diag_tid, "text": text})
        return messages

    return messages


def publish_apply_result(
    root: str,
    actor: str,
    apply_obj: Dict[str, Any],
    group_id: str,
    account_id: str,
    mode: str,
    allow_broadcaster: bool,
) -> Dict[str, Any]:
    if mode == "off":
        return {"ok": True, "skipped": True, "reason": "mode=off"}
    if not actor_allowed(actor, allow_broadcaster):
        return {"ok": True, "skipped": True, "reason": f"actor not allowed to broadcast: {actor}"}

    messages = build_apply_messages(root, apply_obj, include_escalate_blocked=False)
    if not messages:
        return {"ok": True, "skipped": True, "reason": "no milestone message for intent"}

    results = []
    for msg in messages:
        sent = send_group_message(group_id, account_id, msg["text"], mode)
        results.append({"message": msg, "send": sent})
    ok = all(r["send"].get("ok") for r in results)
    return {"ok": ok, "count": len(results), "results": results}


def cmd_publish_apply(args: argparse.Namespace) -> int:
    try:
        apply_obj = parse_json_loose(args.apply_json)
    except Exception as err:
        print(json.dumps({"ok": False, "error": f"invalid apply json: {err}"}))
        return 1

    result = publish_apply_result(
        args.root,
        args.actor,
        apply_obj,
        args.group_id,
        args.account_id,
        args.mode,
        args.allow_broadcaster,
    )
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get("ok") else 1


def get_task(root: str, task_id: str) -> Optional[Dict[str, Any]]:
    snap = load_snapshot(root)
    task = snap.get("tasks", {}).get(task_id)
    return task if isinstance(task, dict) else None


def ensure_claimed(root: str, task_id: str, agent: str) -> Optional[Dict[str, Any]]:
    task = get_task(root, task_id)
    if not isinstance(task, dict):
        return None
    status = str(task.get("status") or "")
    if status in {"pending", "claimed"}:
        return board_apply(root, agent, f"@{agent} claim task {task_id}")
    return {"ok": True, "intent": "claim_task", "taskId": task_id, "status": status, "skipped": True}


def extract_text_for_judgement(obj: Any) -> str:
    chunks: List[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, str):
            if v.strip():
                chunks.append(v.strip())
            return
        if isinstance(v, dict):
            for key in ("text", "message", "content", "output", "reply", "final", "result"):
                if key in v:
                    walk(v.get(key))
            for item in v.values():
                if isinstance(item, (dict, list)):
                    walk(item)
            return
        if isinstance(v, list):
            for item in v:
                walk(item)

    walk(obj)
    return "\n".join(chunks)


def normalize_spawn_report(task_id: str, role: str, spawn_obj: Dict[str, Any], fallback_text: str = "") -> Dict[str, Any]:
    base = spawn_obj
    if isinstance(spawn_obj.get("report"), dict):
        base = spawn_obj.get("report")

    status_hint = str(base.get("status") or spawn_obj.get("status") or base.get("taskStatus") or "").strip().lower()
    summary = clip(
        str(base.get("summary") or base.get("message") or base.get("result") or base.get("output") or ""),
        260,
    )
    evidence = normalize_string_list(base.get("evidence"))
    risks = normalize_string_list(base.get("risks"))
    next_actions = normalize_string_list(base.get("nextActions") or base.get("next"))

    changes_raw = base.get("changes")
    changes: List[Dict[str, str]] = []
    if isinstance(changes_raw, list):
        for item in changes_raw[:8]:
            if isinstance(item, dict):
                changes.append(
                    {
                        "path": clip(str(item.get("path") or item.get("file") or ""), 140),
                        "summary": clip(str(item.get("summary") or item.get("change") or ""), 180),
                    }
                )
            elif isinstance(item, str):
                changes.append({"path": "", "summary": clip(item, 180)})

    text = (fallback_text or extract_text_for_judgement(spawn_obj) or "").strip()
    if not summary:
        summary = clip(text, 260)
    if not evidence and has_evidence(text):
        evidence = [clip(text, 200)]
    if not status_hint and parse_wakeup_kind(text) == "done":
        status_hint = "done"

    acceptance_chunks = [summary, text]
    acceptance_chunks.extend([f"{c.get('path')}: {c.get('summary')}" for c in changes if c.get("path") or c.get("summary")])
    acceptance_chunks.extend(evidence)
    acceptance_text = "\n".join([c for c in acceptance_chunks if c]).strip()

    detail_parts: List[str] = []
    if summary:
        detail_parts.append(summary)
    if evidence:
        detail_parts.append("证据: " + "; ".join(evidence[:2]))
    if changes:
        first_changes = [c for c in changes[:2] if c.get("path") or c.get("summary")]
        if first_changes:
            rendered = "; ".join([f"{c.get('path') or '-'} {c.get('summary') or ''}".strip() for c in first_changes])
            detail_parts.append("变更: " + rendered)
    detail = clip(" | ".join(detail_parts) or acceptance_text or f"{task_id} 子代理未返回有效内容", 220)

    structured = bool(
        isinstance(base, dict)
        and any(k in base for k in ("summary", "evidence", "changes", "nextActions", "risks", "status"))
    )
    return {
        "taskId": task_id,
        "agent": role,
        "status": status_hint,
        "summary": summary,
        "evidence": evidence,
        "changes": changes,
        "risks": risks,
        "nextActions": next_actions,
        "acceptanceText": acceptance_text,
        "detail": detail,
        "structured": structured,
    }


def classify_spawn_result(root: str, task_id: str, role: str, spawn_obj: Dict[str, Any], fallback_text: str = "") -> Dict[str, Any]:
    status_hint = str(spawn_obj.get("status") or spawn_obj.get("taskStatus") or "").strip().lower()
    ok_flag = spawn_obj.get("ok")
    report = normalize_spawn_report(task_id, role, spawn_obj, fallback_text=fallback_text)
    text = str(report.get("acceptanceText") or "").strip()
    detail = str(report.get("detail") or "").strip()
    kind = parse_wakeup_kind(text or detail)

    if status_hint in {"blocked", "failed", "error", "timeout", "cancelled"}:
        return {
            "decision": "blocked",
            "detail": clip(detail or text or f"{task_id} 子代理执行失败", 200),
            "reasonCode": "spawn_failed",
            "report": report,
        }

    if ok_flag is False:
        return {
            "decision": "blocked",
            "detail": clip(detail or text or f"{task_id} 子代理执行失败", 200),
            "reasonCode": "spawn_failed",
            "report": report,
        }

    maybe_done = status_hint in {"done", "completed", "success", "succeeded"} or str(report.get("status") or "") in {
        "done",
        "completed",
        "success",
        "succeeded",
    } or kind == "done"
    if maybe_done:
        accepted = evaluate_acceptance(root, role, text or detail)
        if accepted.get("ok"):
            return {
                "decision": "done",
                "detail": clip(detail or text or f"{task_id} 子代理返回完成", 200),
                "reasonCode": "done_with_evidence",
                "report": report,
            }
        return {
            "decision": "blocked",
            "detail": clip(
                f"{detail or text or f'{task_id} 子代理结果未通过验收'} | {accepted.get('reason') or '未通过验收策略'}",
                200,
            ),
            "reasonCode": "incomplete_output",
            "report": report,
        }

    if str(report.get("status") or "") in {"blocked", "failed", "error"} or kind == "blocked":
        return {
            "decision": "blocked",
            "detail": clip(detail or text or f"{task_id} 子代理返回阻塞", 200),
            "reasonCode": "blocked_signal",
            "report": report,
        }

    return {
        "decision": "blocked",
        "detail": clip(detail or text or f"{task_id} 子代理未给出完成信号", 200),
        "reasonCode": "no_completion_signal",
        "report": report,
    }


def run_dispatch_spawn(args: argparse.Namespace, task_prompt: str) -> Dict[str, Any]:
    plan = resolve_spawn_plan(args, task_prompt)
    executor = str(plan.get("executor") or "openclaw_agent")
    planned_cmd = list(plan.get("command") or [])

    if args.mode == "dry-run" and not args.spawn_output:
        return {
            "ok": True,
            "skipped": True,
            "reason": "dry-run without spawn output",
            "stdout": "",
            "stderr": "",
            "command": [],
            "executor": executor,
            "plannedCommand": planned_cmd,
            "decision": "",
            "detail": "",
        }

    if args.spawn_output:
        try:
            obj = parse_json_loose(args.spawn_output)
            if not isinstance(obj, dict):
                obj = {"raw": args.spawn_output}
            decision = classify_spawn_result(args.root, args.task_id, args.agent, obj, fallback_text=args.spawn_output)
            return {
                "ok": True,
                "simulated": True,
                "stdout": args.spawn_output,
                "stderr": "",
                "command": ["--spawn-output"],
                "executor": executor,
                "plannedCommand": planned_cmd,
                "spawnResult": obj,
                "decision": decision["decision"],
                "detail": decision["detail"],
                "reasonCode": decision.get("reasonCode", "classified"),
                "normalizedReport": decision.get("report"),
            }
        except Exception as err:
            return {
                "ok": False,
                "error": f"invalid --spawn-output: {err}",
                "stdout": args.spawn_output,
                "stderr": "",
                "command": ["--spawn-output"],
                "executor": executor,
                "plannedCommand": planned_cmd,
                "decision": "blocked",
                "detail": clip(str(err), 200),
                "reasonCode": "invalid_spawn_output",
            }

    cmd = planned_cmd
    if not cmd:
        return {
            "ok": False,
            "error": "spawn plan resolved to empty command",
            "stdout": "",
            "stderr": "",
            "command": [],
            "executor": executor,
            "plannedCommand": planned_cmd,
            "decision": "blocked",
            "detail": "spawn command is empty",
            "reasonCode": "spawn_command_empty",
        }

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=max(10, args.timeout_sec + 5))
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    parsed: Dict[str, Any] = {}
    if stdout:
        try:
            obj = parse_json_loose(stdout)
            if isinstance(obj, dict):
                parsed = obj
            else:
                parsed = {"output": obj}
        except Exception:
            parsed = {"output": stdout}

    if proc.returncode != 0:
        detail = clip(stderr or stdout or f"spawn exit={proc.returncode}", 200)
        return {
            "ok": False,
            "error": f"spawn failed (exit={proc.returncode})",
            "stdout": stdout,
            "stderr": stderr,
            "command": cmd,
            "executor": executor,
            "plannedCommand": planned_cmd,
            "spawnResult": parsed,
            "decision": "blocked",
            "detail": detail,
            "reasonCode": "spawn_failed",
        }

    decision = classify_spawn_result(args.root, args.task_id, args.agent, parsed or {"output": stdout}, fallback_text=stdout)
    return {
        "ok": True,
        "stdout": stdout,
        "stderr": stderr,
        "command": cmd,
        "executor": executor,
        "plannedCommand": planned_cmd,
        "spawnResult": parsed,
        "decision": decision["decision"],
        "detail": decision["detail"],
        "reasonCode": decision.get("reasonCode", "classified"),
        "normalizedReport": decision.get("report"),
    }


def dispatch_once(args: argparse.Namespace) -> Dict[str, Any]:
    visibility_mode = str(getattr(args, "visibility_mode", VISIBILITY_MODES[0]) or VISIBILITY_MODES[0])
    if visibility_mode not in VISIBILITY_MODES:
        visibility_mode = VISIBILITY_MODES[0]

    if args.actor != "orchestrator":
        return {"ok": False, "error": "dispatch is restricted to actor=orchestrator"}

    task = get_task(args.root, args.task_id)
    if not isinstance(task, dict):
        return {"ok": False, "error": f"task not found: {args.task_id}"}

    claimed = ensure_claimed(args.root, args.task_id, args.agent)
    if not isinstance(claimed, dict) or not claimed.get("ok"):
        return {
            "ok": False,
            "error": f"failed to claim task: {args.task_id}",
            "claim": claimed,
        }

    task = get_task(args.root, args.task_id) or task
    status = str(task.get("status") or "")
    title = clip(task.get("title") or "未命名任务")
    dispatch_task = clip(args.task or f"{args.task_id}: {task.get('title') or 'untitled'}", 300)
    agent_prompt = build_agent_prompt(args.root, task, args.agent, dispatch_task)

    dispatch_mode_line = "派发模式: 手动协作（等待回报）" if not args.spawn else "派发模式: 自动执行闭环（spawn并回写看板）"

    claim_text = "\n".join(
        [
            f"[CLAIM] {args.task_id} | 状态={status_zh(status or '-')} | 指派={args.agent}",
            f"标题: {title}",
            dispatch_mode_line,
        ]
    )
    claim_send = send_group_message(args.group_id, args.account_id, claim_text, args.mode)

    mentions = load_bot_mentions(args.root)
    orchestrator_mention = mention_tag_for("orchestrator", mentions, fallback="@orchestrator")
    assignee_mention = mention_tag_for(args.agent, mentions, fallback=f"@{args.agent}")
    report_template = f"{orchestrator_mention} {args.task_id} 已完成，证据: 日志/截图/链接"
    task_text = "\n".join(
        [
            f"[TASK] {args.task_id} | 负责人={args.agent}",
            f"任务: {dispatch_task}",
            f"请 {assignee_mention} 执行，完成后按模板回报：{report_template}。",
        ]
    )
    task_send = send_group_message(args.group_id, args.account_id, task_text, args.mode)

    spawn = {
        "ok": True,
        "skipped": True,
        "reason": "spawn disabled",
        "decision": "",
        "detail": "",
        "command": [],
        "stdout": "",
        "stderr": "",
    }
    close_apply: Dict[str, Any] = {"ok": True, "skipped": True, "reason": "spawn disabled"}
    close_publish: Dict[str, Any] = {"ok": True, "skipped": True, "reason": "spawn disabled"}
    worker_report: Dict[str, Any] = {"ok": True, "skipped": True, "reason": "visibility mode not enabled"}

    if args.spawn:
        spawn = run_dispatch_spawn(args, agent_prompt)
        if (
            not spawn.get("skipped")
            and not args.spawn_output
            and spawn.get("decision") == "blocked"
            and str(spawn.get("reasonCode") or "") in {"incomplete_output", "missing_evidence", "stage_only", "role_policy_missing_keyword"}
        ):
            retry_prompt = clip(
                agent_prompt
                + "\n\n交付硬性要求：请直接给出最终可验证结果（改动文件/命令输出/commit哈希/验证结论），不要只给阶段性进度。",
                5000,
            )
            retry_spawn = run_dispatch_spawn(args, retry_prompt)
            spawn["retried"] = True
            spawn["retry"] = retry_spawn
            if retry_spawn.get("decision") == "done":
                spawn = retry_spawn

        if spawn.get("skipped"):
            close_apply = {"ok": True, "skipped": True, "reason": spawn.get("reason", "spawn skipped")}
            close_publish = {"ok": True, "skipped": True, "reason": "spawn skipped"}
        else:
            decision = spawn.get("decision") or "blocked"
            detail = clip(spawn.get("detail") or f"{args.task_id} 子代理执行结果未明确", 200)
            if decision == "done":
                close_apply = board_apply(args.root, "orchestrator", f"mark done {args.task_id}: {detail}")
            else:
                close_apply = board_apply(args.root, "orchestrator", f"block task {args.task_id}: {detail}")
            close_publish = publish_apply_result(
                args.root,
                "orchestrator",
                close_apply,
                args.group_id,
                args.account_id,
                args.mode,
                allow_broadcaster=False,
            )

            if decision == "done" and visibility_mode in {"handoff_visible", "full_visible"}:
                handoff_line = f"{orchestrator_mention} {args.task_id} 已完成，结果: {detail}"
                worker_text = "\n".join(
                    [
                        f"[TASK] {args.task_id} | 交接人={args.agent}",
                        handoff_line,
                    ]
                )
                worker_send = send_group_message(args.group_id, args.agent, worker_text, args.mode)
                worker_report = {
                    "ok": bool(worker_send.get("ok")),
                    "skipped": False,
                    "visibilityMode": visibility_mode,
                    "send": worker_send,
                }
            else:
                worker_report = {"ok": True, "skipped": True, "reason": "spawn not done or visibility hidden"}

    auto_close = bool(args.spawn and not spawn.get("skipped"))
    ok = (
        bool(claimed.get("ok"))
        and bool(claim_send.get("ok"))
        and bool(task_send.get("ok"))
        and bool(close_apply.get("ok"))
        and bool(close_publish.get("ok"))
        and bool(worker_report.get("ok"))
    )
    return {
        "ok": ok,
        "handled": True,
        "intent": "dispatch",
        "taskId": args.task_id,
        "agent": args.agent,
        "dispatchMode": "spawn" if auto_close else "manual",
        "visibilityMode": visibility_mode,
        "claim": claimed,
        "claimSend": claim_send,
        "taskSend": task_send,
        "spawn": spawn,
        "closeApply": close_apply,
        "closePublish": close_publish,
        "workerReport": worker_report,
        "waitForReport": not auto_close,
        "autoClose": auto_close,
        "reportTemplate": report_template,
        "agentPrompt": agent_prompt,
    }


def cmd_dispatch(args: argparse.Namespace) -> int:
    result = dispatch_once(args)
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get("ok") else 1


def autopilot_once(args: argparse.Namespace) -> Dict[str, Any]:
    if args.actor != "orchestrator":
        return {"ok": False, "error": "autopilot is restricted to actor=orchestrator"}
    max_steps = max(1, int(args.max_steps))
    steps: List[Dict[str, Any]] = []
    summary = {"done": 0, "blocked": 0, "manual": 0}
    stop_reason = "no_runnable_task"
    ok = True

    for idx in range(max_steps):
        task = choose_task_for_run(args.root, "")
        if not isinstance(task, dict):
            stop_reason = "no_runnable_task"
            break
        task_id = str(task.get("taskId") or "").strip()
        if not task_id:
            stop_reason = "invalid_task"
            ok = False
            break

        agent = str(task.get("owner") or task.get("assigneeHint") or "coder")
        if agent not in BOT_ROLES:
            agent = suggest_agent_from_title(str(task.get("title") or ""))

        d_args = argparse.Namespace(
            root=args.root,
            task_id=task_id,
            agent=agent,
            task=f"{task_id}: {task.get('title') or 'untitled'}",
            actor="orchestrator",
            session_id=args.session_id,
            group_id=args.group_id,
            account_id=args.account_id,
            mode=args.mode,
            timeout_sec=args.timeout_sec,
            spawn=args.spawn,
            spawn_cmd=args.spawn_cmd,
            spawn_output=args.spawn_output,
            visibility_mode=args.visibility_mode,
        )
        dispatch_result = dispatch_once(d_args)
        steps.append({"index": idx + 1, "taskId": task_id, "agent": agent, "dispatch": dispatch_result})

        if not dispatch_result.get("ok"):
            ok = False
            stop_reason = "dispatch_failed"
            break

        if dispatch_result.get("autoClose"):
            if str((dispatch_result.get("spawn") or {}).get("decision") or "") == "done":
                summary["done"] += 1
            else:
                summary["blocked"] += 1
        else:
            summary["manual"] += 1
        stop_reason = "max_steps_reached"

    result = {
        "ok": ok,
        "handled": True,
        "intent": "autopilot",
        "maxSteps": max_steps,
        "stepsRun": len(steps),
        "summary": summary,
        "stopReason": stop_reason,
        "visibilityMode": str(args.visibility_mode),
        "steps": steps,
    }
    return result


def cmd_autopilot(args: argparse.Namespace) -> int:
    result = autopilot_once(args)
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get("ok") else 1


def scheduler_run_once(args: argparse.Namespace) -> Dict[str, Any]:
    if args.actor != "orchestrator":
        return {"ok": False, "error": "scheduler-run is restricted to actor=orchestrator"}

    action = str(getattr(args, "action", "tick") or "tick").strip().lower()
    if action not in {"enable", "disable", "status", "tick"}:
        action = "tick"

    state = load_scheduler_state(args.root)
    now_ts = int(time.time())

    interval_arg = getattr(args, "interval_sec", None)
    max_steps_arg = getattr(args, "max_steps", None)
    has_interval_update = interval_arg not in (None, "", 0, "0")
    has_max_steps_update = max_steps_arg not in (None, "", 0, "0")

    if action == "enable":
        interval_sec = normalize_interval_sec(
            interval_arg if has_interval_update else state.get("intervalSec", SCHEDULER_DEFAULT_INTERVAL_SEC),
            SCHEDULER_DEFAULT_INTERVAL_SEC,
        )
        state["enabled"] = True
        state["intervalSec"] = interval_sec
        if has_max_steps_update:
            state["maxSteps"] = normalize_steps(max_steps_arg, state.get("maxSteps", SCHEDULER_DEFAULT_MAX_STEPS))
    elif action == "disable":
        state["enabled"] = False
        state["nextDueTs"] = 0
    else:
        if has_interval_update:
            state["intervalSec"] = normalize_interval_sec(interval_arg, state.get("intervalSec", SCHEDULER_DEFAULT_INTERVAL_SEC))
        if has_max_steps_update:
            state["maxSteps"] = normalize_steps(max_steps_arg, state.get("maxSteps", SCHEDULER_DEFAULT_MAX_STEPS))

    force = bool(getattr(args, "force", False))
    should_tick = action in {"enable", "tick"}

    run_result: Dict[str, Any] = {"ok": True, "skipped": True, "reason": "status_only"}
    if should_tick:
        if not state.get("enabled") and not force:
            run_result = {"ok": True, "skipped": True, "reason": "disabled"}
        elif not force and int(state.get("nextDueTs") or 0) > now_ts:
            run_result = {"ok": True, "skipped": True, "reason": "not_due"}
        else:
            a_args = argparse.Namespace(
                root=args.root,
                actor="orchestrator",
                session_id=getattr(args, "session_id", ""),
                group_id=args.group_id,
                account_id=args.account_id,
                mode=args.mode,
                timeout_sec=args.timeout_sec,
                spawn=args.spawn,
                spawn_cmd=args.spawn_cmd,
                spawn_output=args.spawn_output,
                max_steps=int(state.get("maxSteps") or SCHEDULER_DEFAULT_MAX_STEPS),
                visibility_mode=args.visibility_mode,
            )
            auto = autopilot_once(a_args)
            run_result = dict(auto)
            run_result["skipped"] = False
            if auto.get("ok"):
                state["lastRunTs"] = now_ts
                state["lastRunAt"] = now_iso()
                state["nextDueTs"] = now_ts + int(state.get("intervalSec") or SCHEDULER_DEFAULT_INTERVAL_SEC)

    state = save_scheduler_state(args.root, state)
    ok = bool(run_result.get("ok"))
    return {
        "ok": ok,
        "handled": True,
        "intent": "scheduler_run",
        "action": action,
        "state": state,
        "run": run_result,
        "skipped": bool(run_result.get("skipped")),
        "reason": str(run_result.get("reason") or ""),
    }


def cmd_scheduler_run(args: argparse.Namespace) -> int:
    result = scheduler_run_once(args)
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get("ok") else 1


def scheduler_daemon_state_path(root: str) -> str:
    return os.path.join(root, "state", SCHEDULER_DAEMON_STATE_FILE)


def normalize_poll_sec(value: Any, default_poll_sec: float = SCHEDULER_DAEMON_DEFAULT_POLL_SEC) -> float:
    try:
        n = float(value)
    except Exception:
        n = float(default_poll_sec)
    if n < SCHEDULER_DAEMON_MIN_POLL_SEC:
        n = float(SCHEDULER_DAEMON_MIN_POLL_SEC)
    if n > SCHEDULER_DAEMON_MAX_POLL_SEC:
        n = float(SCHEDULER_DAEMON_MAX_POLL_SEC)
    return n


def load_scheduler_daemon_state(root: str) -> Dict[str, Any]:
    data = load_json_file(
        scheduler_daemon_state_path(root),
        {
            "running": False,
            "pid": 0,
            "pollSec": SCHEDULER_DAEMON_DEFAULT_POLL_SEC,
            "maxLoops": 0,
            "loops": 0,
            "runs": 0,
            "skips": 0,
            "errors": 0,
            "stopReason": "",
            "startedAt": "",
            "endedAt": "",
            "lastTickAt": "",
            "lastResult": {},
            "updatedAt": "",
        },
    )
    return {
        "running": bool(data.get("running")),
        "pid": int(data.get("pid") or 0),
        "pollSec": normalize_poll_sec(data.get("pollSec"), SCHEDULER_DAEMON_DEFAULT_POLL_SEC),
        "maxLoops": int(data.get("maxLoops") or 0),
        "loops": int(data.get("loops") or 0),
        "runs": int(data.get("runs") or 0),
        "skips": int(data.get("skips") or 0),
        "errors": int(data.get("errors") or 0),
        "stopReason": str(data.get("stopReason") or ""),
        "startedAt": str(data.get("startedAt") or ""),
        "endedAt": str(data.get("endedAt") or ""),
        "lastTickAt": str(data.get("lastTickAt") or ""),
        "lastResult": data.get("lastResult") if isinstance(data.get("lastResult"), dict) else {},
        "updatedAt": str(data.get("updatedAt") or ""),
    }


def save_scheduler_daemon_state(root: str, state: Dict[str, Any]) -> Dict[str, Any]:
    normalized = {
        "running": bool(state.get("running")),
        "pid": int(state.get("pid") or 0),
        "pollSec": normalize_poll_sec(state.get("pollSec"), SCHEDULER_DAEMON_DEFAULT_POLL_SEC),
        "maxLoops": int(state.get("maxLoops") or 0),
        "loops": int(state.get("loops") or 0),
        "runs": int(state.get("runs") or 0),
        "skips": int(state.get("skips") or 0),
        "errors": int(state.get("errors") or 0),
        "stopReason": str(state.get("stopReason") or ""),
        "startedAt": str(state.get("startedAt") or ""),
        "endedAt": str(state.get("endedAt") or ""),
        "lastTickAt": str(state.get("lastTickAt") or ""),
        "lastResult": state.get("lastResult") if isinstance(state.get("lastResult"), dict) else {},
        "updatedAt": now_iso(),
    }
    save_json_file(scheduler_daemon_state_path(root), normalized)
    return normalized


def cmd_scheduler_daemon(args: argparse.Namespace) -> int:
    if args.actor != "orchestrator":
        print(json.dumps({"ok": False, "error": "scheduler-daemon is restricted to actor=orchestrator"}, ensure_ascii=True))
        return 1

    poll_sec = normalize_poll_sec(getattr(args, "poll_sec", SCHEDULER_DAEMON_DEFAULT_POLL_SEC), SCHEDULER_DAEMON_DEFAULT_POLL_SEC)
    max_loops = int(getattr(args, "max_loops", 0) or 0)
    if max_loops < 0:
        max_loops = 0

    loops = 0
    runs = 0
    skips = 0
    errors = 0
    stop_reason = ""

    daemon_state = {
        "running": True,
        "pid": os.getpid(),
        "pollSec": poll_sec,
        "maxLoops": max_loops,
        "loops": 0,
        "runs": 0,
        "skips": 0,
        "errors": 0,
        "stopReason": "",
        "startedAt": now_iso(),
        "endedAt": "",
        "lastTickAt": "",
        "lastResult": {},
    }
    save_scheduler_daemon_state(args.root, daemon_state)

    try:
        while True:
            loops += 1
            s_args = argparse.Namespace(
                root=args.root,
                actor="orchestrator",
                action="tick",
                interval_sec=getattr(args, "interval_sec", None),
                max_steps=getattr(args, "max_steps", None),
                force=bool(getattr(args, "force", False)),
                group_id=args.group_id,
                account_id=args.account_id,
                mode=args.mode,
                session_id=args.session_id,
                timeout_sec=args.timeout_sec,
                spawn=args.spawn,
                spawn_cmd=args.spawn_cmd,
                spawn_output=args.spawn_output,
                visibility_mode=args.visibility_mode,
            )
            tick = scheduler_run_once(s_args)
            if tick.get("ok"):
                if tick.get("skipped"):
                    skips += 1
                else:
                    runs += 1
            else:
                errors += 1

            daemon_state.update(
                {
                    "loops": loops,
                    "runs": runs,
                    "skips": skips,
                    "errors": errors,
                    "lastTickAt": now_iso(),
                    "lastResult": tick,
                }
            )
            save_scheduler_daemon_state(args.root, daemon_state)

            if max_loops > 0 and loops >= max_loops:
                stop_reason = "max_loops_reached"
                break

            if bool(getattr(args, "exit_when_disabled", False)):
                sched_state = tick.get("state") if isinstance(tick, dict) else {}
                if not bool((sched_state or {}).get("enabled")):
                    stop_reason = "scheduler_disabled"
                    break

            if poll_sec > 0:
                time.sleep(poll_sec)
    except KeyboardInterrupt:
        stop_reason = "interrupted"
    except Exception as err:
        errors += 1
        stop_reason = f"exception:{clip(str(err), 120)}"
    finally:
        daemon_state.update(
            {
                "running": False,
                "loops": loops,
                "runs": runs,
                "skips": skips,
                "errors": errors,
                "stopReason": stop_reason,
                "endedAt": now_iso(),
            }
        )
        final_state = save_scheduler_daemon_state(args.root, daemon_state)

    ok = errors == 0
    result = {
        "ok": ok,
        "handled": True,
        "intent": "scheduler_daemon",
        "pollSec": poll_sec,
        "maxLoops": max_loops,
        "loops": loops,
        "runs": runs,
        "skips": skips,
        "errors": errors,
        "stopReason": stop_reason or "completed",
        "state": final_state,
    }
    print(json.dumps(result, ensure_ascii=True))
    return 0 if ok else 1


def load_json_file(path: str, default_obj: Dict[str, Any]) -> Dict[str, Any]:
    if not os.path.exists(path):
        return default_obj
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json_file(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=True, indent=2)
        f.write("\n")


def cmd_clarify(args: argparse.Namespace) -> int:
    if args.actor != "orchestrator":
        print(json.dumps({"ok": False, "error": "clarify is restricted to actor=orchestrator"}))
        return 1
    if args.role not in CLARIFY_ROLES:
        print(json.dumps({"ok": False, "error": f"unsupported role: {args.role}"}))
        return 1
    q = clip(args.question, 140)
    if not q:
        print(json.dumps({"ok": False, "error": "question cannot be empty"}))
        return 1

    state_file = args.state_file or os.path.join(args.root, "state", "clarify.cooldown.json")
    state = load_json_file(state_file, {"entries": {}})
    entries = state.setdefault("entries", {})
    key = f"{args.group_id}:{args.role}"
    global_key = f"{args.group_id}:*"
    now_ts = int(time.time())

    last = entries.get(key, {})
    last_ts = int(last.get("ts", 0)) if isinstance(last, dict) else 0
    wait = args.cooldown_sec - (now_ts - last_ts)

    global_last = entries.get(global_key, {})
    global_last_ts = int(global_last.get("ts", 0)) if isinstance(global_last, dict) else 0
    global_wait = args.cooldown_sec - (now_ts - global_last_ts)

    retry_after = max(wait, global_wait)
    if retry_after > 0 and not args.force:
        print(
            json.dumps(
                {
                    "ok": False,
                    "throttled": True,
                    "retryAfterSec": retry_after,
                    "lastAt": last.get("at") if isinstance(last, dict) else None,
                    "globalLastAt": global_last.get("at") if isinstance(global_last, dict) else None,
                }
            )
        )
        return 1

    text = "\n".join(
        [
            f"[TASK] {args.task_id} | 状态=澄清 | 目标角色={args.role}",
            f"问题: {q}",
        ]
    )
    sent = send_group_message(args.group_id, args.account_id, text, args.mode)
    if sent.get("ok") and args.mode == "send":
        stamp = {"ts": now_ts, "at": now_iso(), "taskId": args.task_id, "by": args.actor}
        entries[key] = stamp
        entries[global_key] = stamp
        save_json_file(state_file, state)
    print(json.dumps({"ok": bool(sent.get("ok")), "send": sent, "throttleKey": key, "globalThrottleKey": global_key}, ensure_ascii=True))
    return 0 if sent.get("ok") else 1




def suggest_agent_from_title(title: str) -> str:
    s = (title or "").lower()
    if any(k in s for k in ["debug", "bug", "故障", "排查", "异常"]):
        return "debugger"
    if any(k in s for k in ["调研", "分析", "research", "invest"]):
        return "invest-analyst"
    if any(k in s for k in ["发布", "播报", "公告", "broadcast", "summary", "总结"]):
        return "broadcaster"
    return "coder"

def parse_project_tasks(payload: str) -> Tuple[str, List[str]]:
    content = payload.strip()
    if not content:
        return "未命名项目", []
    if ":" in content:
        project_name, items = content.split(":", 1)
    else:
        project_name, items = content, ""
    project_name = clip(project_name.strip() or "未命名项目", 80)
    parts = [p.strip(" -") for p in re.split(r"[;\n]+", items) if p.strip()]
    if not parts and items.strip():
        parts = [items.strip()]
    if not parts:
        parts = [f"项目启动: {project_name}"]
    return project_name, parts


def normalize_project_path(raw: str) -> str:
    s = (raw or "").strip().strip("'").strip('"')
    if not s:
        return ""
    return os.path.abspath(os.path.expanduser(s))


def read_project_doc(project_path: str) -> Tuple[str, str]:
    for filename in PROJECT_DOC_CANDIDATES:
        path = os.path.join(project_path, filename)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                return path, f.read()
        except Exception:
            continue
    return "", ""


def infer_project_name(project_path: str, doc_text: str) -> str:
    for line in (doc_text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            name = stripped.lstrip("#").strip()
            if name:
                return clip(name, 80)
    base = os.path.basename(project_path.rstrip(os.sep))
    return clip(base or "未命名项目", 80)


def extract_project_bootstrap_tasks(doc_text: str) -> List[str]:
    lines = (doc_text or "").splitlines()
    if not lines:
        return []

    tasks: List[str] = []
    in_milestone = False
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            in_milestone = bool(re.search(r"里程碑|milestone", line, flags=re.IGNORECASE))
            continue

        m = re.match(r"^[-*]\s*(?:M|m)\d+\s*[：:]\s*(.+)$", line)
        if m:
            candidate = clip(m.group(1).strip(), 120)
            if candidate:
                tasks.append(candidate)
            continue

        if in_milestone:
            m2 = re.match(r"^[-*]\s*(.+)$", line)
            if m2:
                candidate = clip(m2.group(1).strip(), 120)
                if candidate:
                    tasks.append(candidate)

    deduped: List[str] = []
    seen = set()
    for task in tasks:
        key = task.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(task)
        if len(deduped) >= 8:
            break
    return deduped


def build_project_bootstrap(project_path: str) -> Dict[str, Any]:
    doc_path, doc_text = read_project_doc(project_path)
    project_name = infer_project_name(project_path, doc_text)
    tasks = extract_project_bootstrap_tasks(doc_text)
    if not tasks:
        tasks = list(DEFAULT_PROJECT_BOOTSTRAP_TASKS)
    return {
        "projectPath": project_path,
        "projectName": project_name,
        "docPath": doc_path,
        "tasks": tasks,
    }


def auto_progress_state_path(root: str) -> str:
    return os.path.join(root, "state", AUTO_PROGRESS_STATE_FILE)


def normalize_steps(value: Any, default_steps: int = AUTO_PROGRESS_DEFAULT_MAX_STEPS) -> int:
    try:
        n = int(value)
    except Exception:
        n = int(default_steps)
    n = max(1, n)
    return min(AUTO_PROGRESS_MAX_STEPS_LIMIT, n)


def load_auto_progress_state(root: str) -> Dict[str, Any]:
    path = auto_progress_state_path(root)
    data = load_json_file(path, {"enabled": False, "maxSteps": AUTO_PROGRESS_DEFAULT_MAX_STEPS, "updatedAt": ""})
    enabled = bool(data.get("enabled"))
    max_steps = normalize_steps(data.get("maxSteps"), AUTO_PROGRESS_DEFAULT_MAX_STEPS)
    return {"enabled": enabled, "maxSteps": max_steps, "updatedAt": str(data.get("updatedAt") or "")}


def save_auto_progress_state(root: str, enabled: bool, max_steps: int) -> Dict[str, Any]:
    state = {
        "enabled": bool(enabled),
        "maxSteps": normalize_steps(max_steps, AUTO_PROGRESS_DEFAULT_MAX_STEPS),
        "updatedAt": now_iso(),
    }
    save_json_file(auto_progress_state_path(root), state)
    return state


def scheduler_state_path(root: str) -> str:
    return os.path.join(root, "state", SCHEDULER_STATE_FILE)


def normalize_interval_sec(value: Any, default_interval: int = SCHEDULER_DEFAULT_INTERVAL_SEC) -> int:
    try:
        n = int(value)
    except Exception:
        n = int(default_interval)
    n = max(SCHEDULER_MIN_INTERVAL_SEC, n)
    return min(SCHEDULER_MAX_INTERVAL_SEC, n)


def load_scheduler_state(root: str) -> Dict[str, Any]:
    path = scheduler_state_path(root)
    data = load_json_file(
        path,
        {
            "enabled": False,
            "intervalSec": SCHEDULER_DEFAULT_INTERVAL_SEC,
            "maxSteps": SCHEDULER_DEFAULT_MAX_STEPS,
            "lastRunTs": 0,
            "lastRunAt": "",
            "nextDueTs": 0,
            "updatedAt": "",
        },
    )
    return {
        "enabled": bool(data.get("enabled")),
        "intervalSec": normalize_interval_sec(data.get("intervalSec"), SCHEDULER_DEFAULT_INTERVAL_SEC),
        "maxSteps": normalize_steps(data.get("maxSteps"), SCHEDULER_DEFAULT_MAX_STEPS),
        "lastRunTs": int(data.get("lastRunTs") or 0),
        "lastRunAt": str(data.get("lastRunAt") or ""),
        "nextDueTs": int(data.get("nextDueTs") or 0),
        "updatedAt": str(data.get("updatedAt") or ""),
    }


def save_scheduler_state(root: str, state: Dict[str, Any]) -> Dict[str, Any]:
    normalized = {
        "enabled": bool(state.get("enabled")),
        "intervalSec": normalize_interval_sec(state.get("intervalSec"), SCHEDULER_DEFAULT_INTERVAL_SEC),
        "maxSteps": normalize_steps(state.get("maxSteps"), SCHEDULER_DEFAULT_MAX_STEPS),
        "lastRunTs": int(state.get("lastRunTs") or 0),
        "lastRunAt": str(state.get("lastRunAt") or ""),
        "nextDueTs": int(state.get("nextDueTs") or 0),
        "updatedAt": now_iso(),
    }
    save_json_file(scheduler_state_path(root), normalized)
    return normalized


def build_user_help_message() -> str:
    lines = [
        "[TASK] Orchestrator 快速入口",
        "1) @orchestrator 开始项目 /absolute/path/to/project",
        "2) @orchestrator 项目状态",
        "3) @orchestrator 自动推进 开 [N] | 关 | 状态",
        "4) @orchestrator run / autopilot / dispatch 仍可继续使用",
    ]
    return "\n".join(lines)


def build_control_panel_card(root: str, state: Dict[str, Any]) -> Dict[str, Any]:
    enabled = bool((state or {}).get("enabled"))
    max_steps = int((state or {}).get("maxSteps") or AUTO_PROGRESS_DEFAULT_MAX_STEPS)
    status_text = "已开启" if enabled else "已关闭"
    snapshot = load_snapshot(root)
    tasks = snapshot.get("tasks", {}) if isinstance(snapshot, dict) else {}
    blocked = len([1 for t in tasks.values() if isinstance(t, dict) and str(t.get("status") or "") == "blocked"])
    pending_like = len(
        [1 for t in tasks.values() if isinstance(t, dict) and str(t.get("status") or "") in {"pending", "claimed", "in_progress", "review"}]
    )
    return {
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "weight": "Bolder", "size": "Medium", "text": "Orchestrator 控制台"},
            {"type": "TextBlock", "wrap": True, "text": f"自动推进: {status_text}（maxSteps={max_steps}）"},
            {"type": "TextBlock", "wrap": True, "text": f"看板: 进行中={pending_like} | 阻塞={blocked}"},
            {"type": "TextBlock", "wrap": True, "text": "常用命令："},
            {"type": "TextBlock", "wrap": True, "text": "• 开始项目请替换绝对路径"},
            {"type": "TextBlock", "wrap": True, "text": "• @orchestrator 项目状态"},
            {"type": "TextBlock", "wrap": True, "text": "• @orchestrator 推进一次"},
            {"type": "TextBlock", "wrap": True, "text": "• @orchestrator 自动推进 开 2 / 关"},
        ],
        "actions": [
            {"type": "Action.Submit", "title": "开始项目", "data": {"command": "@orchestrator 开始项目 /absolute/path/to/project"}},
            {"type": "Action.Submit", "title": "推进一次", "data": {"command": "@orchestrator 推进一次"}},
            {"type": "Action.Submit", "title": "自动推进开", "data": {"command": "@orchestrator 自动推进 开 2"}},
            {"type": "Action.Submit", "title": "自动推进关", "data": {"command": "@orchestrator 自动推进 关"}},
            {"type": "Action.Submit", "title": "查看阻塞", "data": {"command": "@orchestrator status"}},
            {"type": "Action.Submit", "title": "验收摘要", "data": {"command": "@orchestrator synthesize"}},
        ],
    }


def task_context_state_path(root: str) -> str:
    return os.path.join(root, "state", TASK_CONTEXT_STATE_FILE)


def load_task_context_state(root: str) -> Dict[str, Any]:
    state = load_json_file(task_context_state_path(root), {"tasks": {}})
    tasks = state.get("tasks")
    if not isinstance(tasks, dict):
        tasks = {}
    return {"tasks": tasks}


def save_task_context_state(root: str, state: Dict[str, Any]) -> None:
    tasks = state.get("tasks")
    if not isinstance(tasks, dict):
        tasks = {}
    save_json_file(task_context_state_path(root), {"tasks": tasks})


def bind_task_project_context(root: str, task_id: str, project_path: str, project_name: str) -> None:
    if not task_id:
        return
    state = load_task_context_state(root)
    tasks = state.setdefault("tasks", {})
    tasks[task_id] = {
        "projectPath": project_path,
        "projectName": project_name,
        "updatedAt": now_iso(),
    }
    save_task_context_state(root, state)


def lookup_task_project_path(root: str, task_id: str) -> str:
    if not task_id:
        return ""
    state = load_task_context_state(root)
    tasks = state.get("tasks", {})
    entry = tasks.get(task_id)
    if not isinstance(entry, dict):
        return ""
    path = normalize_project_path(str(entry.get("projectPath") or ""))
    if not path or not os.path.isdir(path):
        return ""
    return path


def render_spawn_template(template: str, values: Dict[str, Any]) -> List[str]:
    rendered = template
    for key, raw in values.items():
        rendered = rendered.replace("{" + key + "}", shlex.quote(str(raw)))
    return shlex.split(rendered)


def resolve_spawn_plan(args: argparse.Namespace, task_prompt: str) -> Dict[str, Any]:
    values = {
        "root": args.root,
        "task_id": args.task_id,
        "agent": args.agent,
        "task": task_prompt,
        "timeout_sec": args.timeout_sec,
        "bridge": os.path.join(os.path.dirname(__file__), "codex_worker_bridge.py"),
    }

    raw_spawn_cmd = str(getattr(args, "spawn_cmd", "") or "").strip()
    if raw_spawn_cmd:
        return {
            "executor": "custom",
            "command": render_spawn_template(raw_spawn_cmd, values),
            "template": raw_spawn_cmd,
        }

    if str(args.agent or "").strip().lower() == "coder":
        template = "python3 {bridge} --root {root} --task-id {task_id} --agent {agent} --task {task} --timeout-sec {timeout_sec}"
        return {
            "executor": "codex_cli",
            "command": render_spawn_template(template, values),
            "template": template,
        }

    command = [
        "openclaw",
        "agent",
        "--agent",
        args.agent,
        "--message",
        task_prompt,
        "--json",
        "--timeout",
        str(args.timeout_sec),
    ]
    return {
        "executor": "openclaw_agent",
        "command": command,
        "template": "",
    }


def choose_task_for_run(root: str, requested: str) -> Optional[Dict[str, Any]]:
    data = load_snapshot(root)
    tasks = data.get("tasks", {})
    if requested:
        t = tasks.get(requested)
        if isinstance(t, dict):
            return t
        return None
    candidates = []
    for t in tasks.values():
        if not isinstance(t, dict):
            continue
        if t.get("status") in {"pending", "claimed", "in_progress", "review"}:
            candidates.append(t)
    if not candidates:
        return None
    candidates.sort(key=lambda x: x.get("taskId") or "")
    return candidates[0]


def has_evidence(text: str) -> bool:
    lower = (text or "").lower()
    return any(h.lower() in lower for h in EVIDENCE_HINTS)


def looks_stage_only(text: str) -> bool:
    lower = (text or "").lower()
    has_stage = any(h.lower() in lower for h in STAGE_ONLY_HINTS)
    return has_stage and not has_evidence(text)


def parse_wakeup_kind(text: str) -> str:
    lower = text.lower()
    if any(h.lower() in lower for h in BLOCKED_HINTS):
        return "blocked"
    if any(h.lower() in lower for h in DONE_HINTS):
        return "done"
    return "progress"


def merge_acceptance_policy(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {
        "global": dict(base.get("global") or {}),
        "roles": {k: dict(v) for k, v in (base.get("roles") or {}).items() if isinstance(v, dict)},
    }
    if not isinstance(override, dict):
        return merged

    glob = override.get("global")
    if isinstance(glob, dict):
        merged["global"].update(glob)

    roles = override.get("roles")
    if isinstance(roles, dict):
        for role, conf in roles.items():
            if not isinstance(role, str) or not isinstance(conf, dict):
                continue
            role_conf = dict(merged["roles"].get(role) or {})
            role_conf.update(conf)
            merged["roles"][role] = role_conf

    return merged


def load_acceptance_policy(root: str) -> Dict[str, Any]:
    script_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    search_roots = [root, script_root]
    policy = DEFAULT_ACCEPTANCE_POLICY
    for base in search_roots:
        for rel in ACCEPTANCE_POLICY_CONFIG_CANDIDATES:
            path = os.path.join(base, rel)
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    policy = merge_acceptance_policy(policy, loaded)
            except Exception:
                continue
    return policy


def evaluate_acceptance(root: str, role: str, text: str) -> Dict[str, Any]:
    note = (text or "").strip()
    policy = load_acceptance_policy(root)
    global_conf = policy.get("global") if isinstance(policy, dict) else {}
    role_conf = (policy.get("roles") or {}).get(role) if isinstance(policy, dict) else {}
    if not isinstance(global_conf, dict):
        global_conf = {}
    if not isinstance(role_conf, dict):
        role_conf = {}

    require_evidence = bool(global_conf.get("requireEvidence", True))
    if require_evidence and not has_evidence(note):
        return {
            "ok": False,
            "reasonCode": "missing_evidence",
            "reason": "缺少可验证证据（文件/日志/链接/命令输出）。",
        }

    if looks_stage_only(note):
        return {
            "ok": False,
            "reasonCode": "stage_only",
            "reason": "仅包含阶段性描述，未给出最终验收结果。",
        }

    required_any = role_conf.get("requireAny")
    if isinstance(required_any, list) and required_any:
        lower = note.lower()
        wanted = [str(x).strip() for x in required_any if str(x).strip()]
        matched = [kw for kw in wanted if kw.lower() in lower]
        if not matched:
            return {
                "ok": False,
                "reasonCode": "role_policy_missing_keyword",
                "reason": f"{role} 交付缺少验收关键词（至少包含其一：{', '.join(wanted[:6])}）。",
            }

    return {"ok": True, "reasonCode": "accepted", "reason": "通过验收策略"}


def find_task_id(text: str) -> str:
    m = re.search(r"\bT-\d+\b", text, flags=re.IGNORECASE)
    return m.group(0).upper() if m else ""


def maybe_normalize_board_command(cmd_body: str) -> str:
    s = cmd_body.strip()
    if not s:
        return ""

    m = re.match(r"^claim(?:\s+task)?\s+([A-Za-z0-9_-]+)$", s, flags=re.IGNORECASE)
    if m:
        return f"claim task {m.group(1)}"

    m = re.match(r"^(?:mark\s+)?done\s+([A-Za-z0-9_-]+)(?:\s*:?\s*(.*))?$", s, flags=re.IGNORECASE)
    if m:
        detail = (m.group(2) or "")
        return f"mark done {m.group(1)}: {detail}" if detail else f"mark done {m.group(1)}"

    m = re.match(r"^(?:block|blocked)(?:\s+task)?\s+([A-Za-z0-9_-]+)(?:\s*:?\s*(.*))?$", s, flags=re.IGNORECASE)
    if m:
        detail = (m.group(2) or "")
        return f"block task {m.group(1)}: {detail}" if detail else f"block task {m.group(1)}"

    m = re.match(r"^escalate(?:\s+task)?\s+([A-Za-z0-9_-]+)(?:\s*:?\s*(.*))?$", s, flags=re.IGNORECASE)
    if m:
        detail = (m.group(2) or "")
        return f"escalate task {m.group(1)}: {detail}" if detail else f"escalate task {m.group(1)}"

    m = re.match(r"^synthesize(?:\s+([A-Za-z0-9_-]+))?$", s, flags=re.IGNORECASE)
    if m:
        tid = (m.group(1) or "").strip()
        return f"synthesize {tid}".strip()

    m = re.match(r"^create\s+task\b(.+)$", s, flags=re.IGNORECASE)
    if m:
        return f"create task{m.group(1)}"

    return ""


def should_ignore_bot_loop(actor: str, text: str) -> bool:
    actor_norm = (actor or "").strip().lower()
    if actor_norm not in BOT_ROLES:
        return False
    stripped = text.strip()
    return any(stripped.startswith(prefix) for prefix in MILESTONE_PREFIXES)


def cmd_feishu_router(args: argparse.Namespace) -> int:
    text = (args.text or "").strip()
    norm = text.replace("＠", "@").strip()
    if not norm:
        print(json.dumps({"ok": False, "handled": False, "error": "empty text"}))
        return 1

    if should_ignore_bot_loop(args.actor, norm):
        print(json.dumps({"ok": True, "handled": True, "intent": "ignored_loop", "reason": "bot milestone echo"}))
        return 0

    # A+1 default: do NOT spawn subagents on dispatch/run/verify unless explicitly enabled.
    dispatch_spawn = bool(getattr(args, "dispatch_spawn", False))
    # Back-compat: --dispatch-manual existed previously; manual is now the default.
    if bool(getattr(args, "dispatch_manual", False)):
        dispatch_spawn = False
    visibility_mode = str(getattr(args, "visibility_mode", VISIBILITY_MODES[0]) or VISIBILITY_MODES[0])
    if visibility_mode not in VISIBILITY_MODES:
        visibility_mode = VISIBILITY_MODES[0]

    cmd_body = norm
    if norm.lower().startswith("@orchestrator"):
        cmd_body = norm[len("@orchestrator") :].strip()

    # Command: @orchestrator 帮助
    if re.match(r"^(?:帮助|help|\?)$", cmd_body, flags=re.IGNORECASE):
        msg = build_user_help_message()
        out = send_group_message(args.group_id, args.account_id, msg, args.mode)
        print(json.dumps({"ok": bool(out.get("ok")), "handled": True, "intent": "help", "send": out}))
        return 0 if out.get("ok") else 1

    # Command: @orchestrator 控制台
    if re.match(r"^(?:控制台|console|panel)$", cmd_body, flags=re.IGNORECASE):
        auto_state = load_auto_progress_state(args.root)
        card = build_control_panel_card(args.root, auto_state)
        fallback = "[TASK] 控制台已更新，可点击：开始项目 / 推进一次 / 自动推进开关 / 查看阻塞 / 验收摘要。"
        out = send_group_card(args.group_id, args.account_id, card, args.mode, fallback_text=fallback)
        print(
            json.dumps(
                {
                    "ok": bool(out.get("ok")),
                    "handled": True,
                    "intent": "control_panel",
                    "state": auto_state,
                    "send": out,
                },
                ensure_ascii=True,
            )
        )
        return 0 if out.get("ok") else 1

    # Command: @orchestrator 自动推进 开 [N] | 关 | 状态
    m = re.match(
        r"^(?:自动推进|auto[\s_-]*progress)(?:\s+(开|关|状态|on|off|status)(?:\s+(\d+))?)?$",
        cmd_body,
        flags=re.IGNORECASE,
    )
    if m:
        raw_action = (m.group(1) or "状态").strip().lower()
        raw_steps = (m.group(2) or "").strip()
        if raw_action in {"on", "开"}:
            action = "on"
        elif raw_action in {"off", "关"}:
            action = "off"
        else:
            action = "status"

        state = load_auto_progress_state(args.root)
        if action == "on":
            steps = normalize_steps(raw_steps or state.get("maxSteps", AUTO_PROGRESS_DEFAULT_MAX_STEPS))
            state = save_auto_progress_state(args.root, True, steps)
        elif action == "off":
            state = save_auto_progress_state(args.root, False, normalize_steps(state.get("maxSteps", AUTO_PROGRESS_DEFAULT_MAX_STEPS)))

        spawn_enabled = True
        if bool(getattr(args, "dispatch_manual", False)):
            spawn_enabled = False
        if bool(getattr(args, "dispatch_spawn", False)):
            spawn_enabled = True

        kick = {"ok": True, "skipped": True, "reason": "autopilot not requested"}
        if action == "on":
            a_args = argparse.Namespace(
                root=args.root,
                actor="orchestrator",
                session_id=args.session_id,
                group_id=args.group_id,
                account_id=args.account_id,
                mode=args.mode,
                timeout_sec=args.timeout_sec,
                spawn=spawn_enabled,
                spawn_cmd=args.spawn_cmd,
                spawn_output=args.spawn_output,
                max_steps=state.get("maxSteps", AUTO_PROGRESS_DEFAULT_MAX_STEPS),
                visibility_mode=visibility_mode,
            )
            kick = autopilot_once(a_args)

        status_text = "开启" if state.get("enabled") else "关闭"
        msg = f"[TASK] 自动推进已{status_text} | maxSteps={state.get('maxSteps')}"
        out = send_group_message(args.group_id, args.account_id, msg, args.mode)
        ok = bool(out.get("ok")) and bool(kick.get("ok"))
        print(
            json.dumps(
                {
                    "ok": ok,
                    "handled": True,
                    "intent": "auto_progress",
                    "action": action,
                    "state": state,
                    "kickoff": kick,
                    "send": out,
                },
                ensure_ascii=True,
            )
        )
        return 0 if ok else 1

    # Command: @orchestrator 调度 开 [分钟] | 关 | 状态
    m = re.match(
        r"^(?:调度|scheduler)(?:\s+(开|关|状态|on|off|status)(?:\s+(\d+))?)?$",
        cmd_body,
        flags=re.IGNORECASE,
    )
    if m:
        raw_action = (m.group(1) or "状态").strip().lower()
        raw_interval_minutes = (m.group(2) or "").strip()
        if raw_action in {"on", "开"}:
            action = "enable"
        elif raw_action in {"off", "关"}:
            action = "disable"
        else:
            action = "status"

        interval_sec = 0
        if raw_interval_minutes:
            try:
                interval_sec = int(raw_interval_minutes) * 60
            except Exception:
                interval_sec = 0

        spawn_enabled = True
        if bool(getattr(args, "dispatch_manual", False)):
            spawn_enabled = False
        if bool(getattr(args, "dispatch_spawn", False)):
            spawn_enabled = True

        s_args = argparse.Namespace(
            root=args.root,
            actor="orchestrator",
            action=action,
            interval_sec=interval_sec,
            max_steps=None,
            force=False,
            group_id=args.group_id,
            account_id=args.account_id,
            mode=args.mode,
            session_id=args.session_id,
            timeout_sec=args.timeout_sec,
            spawn=spawn_enabled,
            spawn_cmd=args.spawn_cmd,
            spawn_output=args.spawn_output,
            visibility_mode=visibility_mode,
        )
        scheduled = scheduler_run_once(s_args)
        state = scheduled.get("state") or {}
        status_text = "开启" if state.get("enabled") else "关闭"
        msg = (
            f"[TASK] 调度已{status_text} | intervalSec={state.get('intervalSec')} | "
            f"maxSteps={state.get('maxSteps')} | reason={scheduled.get('reason') or '-'}"
        )
        out = send_group_message(args.group_id, args.account_id, msg, args.mode)
        ok = bool(scheduled.get("ok")) and bool(out.get("ok"))
        print(
            json.dumps(
                {
                    "ok": ok,
                    "handled": True,
                    "intent": "scheduler_control",
                    "action": action,
                    "state": state,
                    "run": scheduled.get("run"),
                    "skipped": bool(scheduled.get("skipped")),
                    "reason": str(scheduled.get("reason") or ""),
                    "send": out,
                },
                ensure_ascii=True,
            )
        )
        return 0 if ok else 1

    # Command: @orchestrator 开始项目 /path/to/project
    m = re.match(r"^(?:开始项目|启动项目|start\s+project)\s+(.+)$", cmd_body, flags=re.IGNORECASE)
    if m:
        project_path = normalize_project_path(m.group(1))
        if not project_path or not os.path.isdir(project_path):
            out = send_group_message(args.group_id, args.account_id, f"[TASK] 项目路径不可用: {clip(project_path or m.group(1), 160)}", args.mode)
            print(json.dumps({"ok": bool(out.get("ok")), "handled": True, "intent": "start_project", "send": out}))
            return 0 if out.get("ok") else 1

        bootstrap = build_project_bootstrap(project_path)
        project_name = str(bootstrap.get("projectName") or "未命名项目")
        created: List[Dict[str, Any]] = []
        created_ids: List[str] = []
        for item in bootstrap.get("tasks", []):
            assignee = suggest_agent_from_title(str(item))
            apply_obj = board_apply(args.root, "orchestrator", f"@{assignee} create task: [{project_name}] {clip(str(item), 120)}")
            if isinstance(apply_obj, dict) and apply_obj.get("ok"):
                tid = str(apply_obj.get("taskId") or "")
                if tid:
                    created_ids.append(tid)
                    bind_task_project_context(args.root, tid, project_path, project_name)
            publish = publish_apply_result(
                args.root,
                "orchestrator",
                apply_obj,
                args.group_id,
                args.account_id,
                args.mode,
                allow_broadcaster=False,
            )
            created.append({"apply": apply_obj, "publish": publish})

        spawn_enabled = True
        if bool(getattr(args, "dispatch_manual", False)):
            spawn_enabled = False
        if bool(getattr(args, "dispatch_spawn", False)):
            spawn_enabled = True

        kickoff: Dict[str, Any] = {"ok": True, "skipped": True, "reason": "no task created"}
        first_task = next((tid for tid in created_ids if tid), "")
        if first_task:
            first_obj = get_task(args.root, first_task) or {}
            d_args = argparse.Namespace(
                root=args.root,
                task_id=first_task,
                agent=str(first_obj.get("assigneeHint") or "coder"),
                task=f"{first_task}: {first_obj.get('title') or 'untitled'}",
                actor="orchestrator",
                session_id=args.session_id,
                group_id=args.group_id,
                account_id=args.account_id,
                mode=args.mode,
                timeout_sec=args.timeout_sec,
                spawn=spawn_enabled,
                spawn_cmd=args.spawn_cmd,
                spawn_output=args.spawn_output,
                visibility_mode=visibility_mode,
            )
            kickoff = dispatch_once(d_args)

        doc_path = str(bootstrap.get("docPath") or "")
        doc_hint = f"文档={doc_path}" if doc_path else "文档=未找到PRD/README（使用默认任务模板）"
        msg = f"[TASK] 项目启动完成: {project_name} | 新建任务={len(created_ids)}\n{doc_hint}\n可用命令: @orchestrator 项目状态"
        ack = send_group_message(args.group_id, args.account_id, msg, args.mode)
        ok = all(c.get("apply", {}).get("ok") and c.get("publish", {}).get("ok") for c in created) and bool(ack.get("ok")) and bool(kickoff.get("ok"))
        print(
            json.dumps(
                {
                    "ok": ok,
                    "handled": True,
                    "intent": "start_project",
                    "projectPath": project_path,
                    "projectName": project_name,
                    "projectDoc": doc_path,
                    "createdCount": len(created_ids),
                    "createdTaskIds": created_ids,
                    "created": created,
                    "bootstrap": kickoff,
                    "ack": ack,
                },
                ensure_ascii=True,
            )
        )
        return 0 if ok else 1

    if re.match(r"^(?:项目状态|project\s+status)$", cmd_body, flags=re.IGNORECASE):
        cmd_body = "status"

    # Command: @orchestrator create project <name>: task1; task2
    m = re.match(r"^create\s+project\s+(.+)$", cmd_body, flags=re.IGNORECASE)
    if m:
        project_name, items = parse_project_tasks(m.group(1))
        created = []
        for item in items:
            assignee = suggest_agent_from_title(item)
            apply_obj = board_apply(args.root, "orchestrator", f"@{assignee} create task: [{project_name}] {item}")
            publish = publish_apply_result(
                args.root,
                "orchestrator",
                apply_obj,
                args.group_id,
                args.account_id,
                args.mode,
                allow_broadcaster=False,
            )
            created.append({"apply": apply_obj, "publish": publish})
        msg = f"[TASK] 项目已创建: {project_name}，共 {len(created)} 个任务。"
        ack = send_group_message(args.group_id, args.account_id, msg, args.mode)
        ok = all(c["apply"].get("ok") for c in created) and ack.get("ok")
        print(json.dumps({"ok": ok, "handled": True, "intent": "create_project", "created": created, "ack": ack}))
        return 0 if ok else 1

    # Command: @orchestrator run [T-xxx]
    m = re.match(r"^run(?:\s+([A-Za-z0-9_-]+))?$", cmd_body, flags=re.IGNORECASE)
    if m:
        requested = (m.group(1) or "").strip()
        if requested:
            requested_task = get_task(args.root, requested)
            if isinstance(requested_task, dict) and str(requested_task.get("status") or "") == "done":
                text_done = f"[DONE] {requested} 已完成，无需重复执行"
                sent = send_group_message(args.group_id, args.account_id, text_done, args.mode)
                print(
                    json.dumps(
                        {
                            "ok": bool(sent.get("ok")),
                            "handled": True,
                            "intent": "run",
                            "taskId": requested,
                            "idempotent": True,
                            "send": sent,
                        }
                    )
                )
                return 0 if sent.get("ok") else 1

        task = choose_task_for_run(args.root, requested)
        if not task:
            sent = send_group_message(args.group_id, args.account_id, "[TASK] 当前没有可执行任务。", args.mode)
            print(json.dumps({"ok": bool(sent.get("ok")), "handled": True, "intent": "run", "send": sent}))
            return 0 if sent.get("ok") else 1
        task_id = str(task.get("taskId"))
        agent = str(task.get("assigneeHint") or "coder")
        d_args = argparse.Namespace(
            root=args.root,
            task_id=task_id,
            agent=agent,
            task=f"{task_id}: {task.get('title') or 'untitled'}",
            actor="orchestrator",
            session_id=args.session_id,
            group_id=args.group_id,
            account_id=args.account_id,
            mode=args.mode,
            timeout_sec=args.timeout_sec,
            spawn=dispatch_spawn,
            spawn_cmd=args.spawn_cmd,
            spawn_output=args.spawn_output,
            visibility_mode=visibility_mode,
        )
        rc = cmd_dispatch(d_args)
        return rc

    # Command: @orchestrator autopilot [N]
    m = re.match(r"^autopilot(?:\s+(\d+))?$", cmd_body, flags=re.IGNORECASE)
    if m:
        max_steps = int(m.group(1) or getattr(args, "autopilot_max_steps", 3))
        spawn_enabled = True
        if bool(getattr(args, "dispatch_manual", False)):
            spawn_enabled = False
        if bool(getattr(args, "dispatch_spawn", False)):
            spawn_enabled = True
        a_args = argparse.Namespace(
            root=args.root,
            actor="orchestrator",
            session_id=args.session_id,
            group_id=args.group_id,
            account_id=args.account_id,
            mode=args.mode,
            timeout_sec=args.timeout_sec,
            spawn=spawn_enabled,
            spawn_cmd=args.spawn_cmd,
            spawn_output=args.spawn_output,
            max_steps=max_steps,
            visibility_mode=visibility_mode,
        )
        return cmd_autopilot(a_args)

    # Command: @orchestrator status [taskId|all|full]
    m = re.match(r"^status(?:\s+([A-Za-z0-9_-]+))?$", cmd_body, flags=re.IGNORECASE)
    if m:
        status_arg = (m.group(1) or "").strip()
        data = load_snapshot(args.root)
        tasks = data.get("tasks", {})
        full_mode = status_arg.lower() in {"all", "full"}
        if status_arg and not full_mode:
            task = tasks.get(status_arg)
            if not isinstance(task, dict):
                out = send_group_message(args.group_id, args.account_id, f"[TASK] 未找到任务 {status_arg}", args.mode)
                print(json.dumps({"ok": bool(out.get("ok")), "handled": True, "intent": "status", "send": out}))
                return 0 if out.get("ok") else 1
            msg = "\n".join(
                [
                    f"[TASK] {status_arg} | 状态={status_zh(str(task.get('status') or '-'))}",
                    f"负责人: {task.get('owner') or task.get('assigneeHint') or '-'}",
                    f"标题: {clip(task.get('title') or '未命名任务')}",
                ]
            )
            out = send_group_message(args.group_id, args.account_id, msg, args.mode)
            print(json.dumps({"ok": bool(out.get("ok")), "handled": True, "intent": "status", "send": out}))
            return 0 if out.get("ok") else 1

        msg, counts = format_status_summary_message(tasks, full=full_mode)
        out = send_group_message(args.group_id, args.account_id, msg, args.mode)
        print(
            json.dumps(
                {
                    "ok": bool(out.get("ok")),
                    "handled": True,
                    "intent": "status",
                    "full": full_mode,
                    "counts": counts,
                    "send": out,
                }
            )
        )
        return 0 if out.get("ok") else 1

    # Command: @orchestrator dispatch T-xxx role: task...
    m = re.match(r"^dispatch\s+([A-Za-z0-9_-]+)\s+([A-Za-z0-9_.-]+)(?:\s*:\s*(.*))?$", cmd_body, flags=re.IGNORECASE)
    if m:
        d_args = argparse.Namespace(
            root=args.root,
            task_id=m.group(1),
            agent=m.group(2),
            task=(m.group(3) or "").strip(),
            actor="orchestrator",
            session_id=args.session_id,
            group_id=args.group_id,
            account_id=args.account_id,
            mode=args.mode,
            timeout_sec=args.timeout_sec,
            spawn=dispatch_spawn,
            spawn_cmd=args.spawn_cmd,
            spawn_output=args.spawn_output,
            visibility_mode=visibility_mode,
        )
        return cmd_dispatch(d_args)

    # Command: @orchestrator clarify T-xxx role: question...
    m = re.match(r"^clarify\s+([A-Za-z0-9_-]+)\s+([A-Za-z0-9_.-]+)\s*:\s*(.+)$", cmd_body, flags=re.IGNORECASE)
    if m:
        c_args = argparse.Namespace(
            root=args.root,
            task_id=m.group(1),
            role=m.group(2),
            question=m.group(3),
            actor="orchestrator",
            group_id=args.group_id,
            account_id=args.account_id,
            cooldown_sec=args.clarify_cooldown_sec,
            state_file=args.clarify_state_file,
            mode=args.mode,
            force=False,
        )
        return cmd_clarify(c_args)

    # Explicit board commands via orchestrator entrance.
    normalized = maybe_normalize_board_command(cmd_body)
    if normalized:
        acceptance: Optional[Dict[str, Any]] = None
        m_done = re.match(r"^mark done\s+([A-Za-z0-9_-]+)(?:\s*:\s*(.*))?$", normalized, flags=re.IGNORECASE)
        if m_done:
            done_task_id = str(m_done.group(1))
            done_detail = str(m_done.group(2) or "")
            acceptance = evaluate_acceptance(args.root, args.actor, done_detail)
            if not acceptance.get("ok"):
                blocked_reason = clip(
                    f"{done_detail or '未提供交付说明'} | {acceptance.get('reason') or '未通过验收策略'}",
                    120,
                )
                normalized = f"block task {done_task_id}: {blocked_reason}"

        apply_actor = args.actor
        if args.actor == "orchestrator" and normalized.startswith("claim task"):
            apply_actor = "orchestrator"
        apply_obj = board_apply(args.root, apply_actor, normalized)

        if normalized.startswith("synthesize") and apply_obj.get("ok"):
            report = clip(str(apply_obj.get("report") or "暂无综合结果"), 1200)
            out = send_group_message(args.group_id, args.account_id, report, args.mode)
            ok = bool(out.get("ok"))
            print(json.dumps({"ok": ok, "handled": True, "intent": "synthesize", "apply": apply_obj, "send": out}))
            return 0 if ok else 1

        publish = publish_apply_result(
            args.root,
            "orchestrator",
            apply_obj,
            args.group_id,
            args.account_id,
            args.mode,
            allow_broadcaster=False,
        )
        ok = bool(apply_obj.get("ok")) and bool(publish.get("ok"))
        print(
            json.dumps(
                {
                    "ok": ok,
                    "handled": True,
                    "intent": "board_cmd",
                    "acceptance": acceptance,
                    "apply": apply_obj,
                    "publish": publish,
                }
            )
        )
        return 0 if ok else 1

    # Simple Wake-up v1: team member reports with @orchestrator or Feishu <at ...> mention.
    mentions = load_bot_mentions(args.root)
    if args.actor != "orchestrator" and contains_mention(norm, "orchestrator", mentions):
        task_id = find_task_id(norm)
        if not task_id:
            sent = send_group_message(args.group_id, args.account_id, "[TASK] 收到汇报，但未识别到任务ID（例如 T-001）。", args.mode)
            print(json.dumps({"ok": bool(sent.get("ok")), "handled": True, "intent": "wakeup", "send": sent}))
            return 0 if sent.get("ok") else 1

        kind = parse_wakeup_kind(norm)
        if kind == "blocked":
            apply_obj = board_apply(args.root, "orchestrator", f"block task {task_id}: {clip(norm, 120)}")
            publish = publish_apply_result(
                args.root,
                "orchestrator",
                apply_obj,
                args.group_id,
                args.account_id,
                args.mode,
                allow_broadcaster=False,
            )
            ok = bool(apply_obj.get("ok")) and bool(publish.get("ok"))
            print(json.dumps({"ok": ok, "handled": True, "intent": "wakeup", "kind": kind, "apply": apply_obj, "publish": publish}))
            return 0 if ok else 1

        if kind == "done":
            accepted = evaluate_acceptance(args.root, args.actor, norm)
            if accepted.get("ok"):
                apply_obj = board_apply(args.root, "orchestrator", f"mark done {task_id}: {clip(norm, 120)}")
            else:
                detail = clip(f"{norm} | {accepted.get('reason') or '未通过验收策略'}", 120)
                apply_obj = board_apply(args.root, "orchestrator", f"block task {task_id}: {detail}")
            publish = publish_apply_result(
                args.root,
                "orchestrator",
                apply_obj,
                args.group_id,
                args.account_id,
                args.mode,
                allow_broadcaster=False,
            )
            ok = bool(apply_obj.get("ok")) and bool(publish.get("ok"))
            print(
                json.dumps(
                    {
                        "ok": ok,
                        "handled": True,
                        "intent": "wakeup",
                        "kind": kind,
                        "verify": "acceptance-policy",
                        "acceptance": accepted,
                        "apply": apply_obj,
                        "publish": publish,
                    }
                )
            )
            return 0 if ok else 1

        verify_prompt = clip(f"verify {task_id} report from {args.actor}: {norm}", 300)
        d_args = argparse.Namespace(
            root=args.root,
            task_id=task_id,
            agent="debugger",
            task=verify_prompt,
            actor="orchestrator",
            session_id=args.session_id,
            group_id=args.group_id,
            account_id=args.account_id,
            mode=args.mode,
            timeout_sec=args.timeout_sec,
            spawn=dispatch_spawn,
            spawn_cmd=args.spawn_cmd,
            spawn_output=args.spawn_output,
            visibility_mode=visibility_mode,
        )
        rc = cmd_dispatch(d_args)
        return rc

    print(json.dumps({"ok": True, "handled": False, "intent": "pass-through"}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pub = sub.add_parser("publish-apply")
    p_pub.add_argument("--root", required=True)
    p_pub.add_argument("--actor", required=True)
    p_pub.add_argument("--apply-json", required=True)
    p_pub.add_argument("--group-id", default=DEFAULT_GROUP_ID)
    p_pub.add_argument("--account-id", default=DEFAULT_ACCOUNT_ID)
    p_pub.add_argument("--mode", choices=["send", "dry-run", "off"], default="send")
    p_pub.add_argument("--allow-broadcaster", action="store_true")
    p_pub.set_defaults(func=cmd_publish_apply)

    p_dispatch = sub.add_parser("dispatch")
    p_dispatch.add_argument("--root", required=True)
    p_dispatch.add_argument("--task-id", required=True)
    p_dispatch.add_argument("--agent", required=True)
    p_dispatch.add_argument("--task", default="")
    p_dispatch.add_argument("--actor", default="orchestrator")
    p_dispatch.add_argument("--session-id", default="")
    p_dispatch.add_argument("--group-id", default=DEFAULT_GROUP_ID)
    p_dispatch.add_argument("--account-id", default=DEFAULT_ACCOUNT_ID)
    p_dispatch.add_argument("--mode", choices=["send", "dry-run"], default="send")
    p_dispatch.add_argument("--timeout-sec", type=int, default=120)
    p_dispatch.add_argument("--visibility-mode", choices=list(VISIBILITY_MODES), default=VISIBILITY_MODES[0])
    # A+1 default: manual dispatch (send [CLAIM]/[TASK]) and wait for report.
    # Enable spawn only when explicitly requested.
    p_dispatch.add_argument("--spawn", dest="spawn", action="store_true", default=False)
    p_dispatch.add_argument("--no-spawn", dest="spawn", action="store_false")
    p_dispatch.add_argument("--spawn-cmd", default="")
    p_dispatch.add_argument("--spawn-output", default="")
    p_dispatch.set_defaults(func=cmd_dispatch)

    p_autopilot = sub.add_parser("autopilot")
    p_autopilot.add_argument("--root", required=True)
    p_autopilot.add_argument("--actor", default="orchestrator")
    p_autopilot.add_argument("--group-id", default=DEFAULT_GROUP_ID)
    p_autopilot.add_argument("--account-id", default=DEFAULT_ACCOUNT_ID)
    p_autopilot.add_argument("--mode", choices=["send", "dry-run"], default="send")
    p_autopilot.add_argument("--session-id", default="")
    p_autopilot.add_argument("--timeout-sec", type=int, default=120)
    p_autopilot.add_argument("--spawn", dest="spawn", action="store_true", default=True)
    p_autopilot.add_argument("--no-spawn", dest="spawn", action="store_false")
    p_autopilot.add_argument("--spawn-cmd", default="")
    p_autopilot.add_argument("--spawn-output", default="")
    p_autopilot.add_argument("--max-steps", type=int, default=3)
    p_autopilot.add_argument("--visibility-mode", choices=list(VISIBILITY_MODES), default=VISIBILITY_MODES[0])
    p_autopilot.set_defaults(func=cmd_autopilot)

    p_scheduler = sub.add_parser("scheduler-run")
    p_scheduler.add_argument("--root", required=True)
    p_scheduler.add_argument("--actor", default="orchestrator")
    p_scheduler.add_argument("--action", choices=["enable", "disable", "status", "tick"], default="tick")
    p_scheduler.add_argument("--interval-sec", type=int, default=None)
    p_scheduler.add_argument("--max-steps", type=int, default=None)
    p_scheduler.add_argument("--force", action="store_true")
    p_scheduler.add_argument("--group-id", default=DEFAULT_GROUP_ID)
    p_scheduler.add_argument("--account-id", default=DEFAULT_ACCOUNT_ID)
    p_scheduler.add_argument("--mode", choices=["send", "dry-run"], default="send")
    p_scheduler.add_argument("--session-id", default="")
    p_scheduler.add_argument("--timeout-sec", type=int, default=120)
    p_scheduler.add_argument("--spawn", dest="spawn", action="store_true", default=True)
    p_scheduler.add_argument("--no-spawn", dest="spawn", action="store_false")
    p_scheduler.add_argument("--spawn-cmd", default="")
    p_scheduler.add_argument("--spawn-output", default="")
    p_scheduler.add_argument("--visibility-mode", choices=list(VISIBILITY_MODES), default=VISIBILITY_MODES[0])
    p_scheduler.set_defaults(func=cmd_scheduler_run)

    p_scheduler_daemon = sub.add_parser("scheduler-daemon")
    p_scheduler_daemon.add_argument("--root", required=True)
    p_scheduler_daemon.add_argument("--actor", default="orchestrator")
    p_scheduler_daemon.add_argument("--poll-sec", type=float, default=SCHEDULER_DAEMON_DEFAULT_POLL_SEC)
    p_scheduler_daemon.add_argument("--max-loops", type=int, default=0)
    p_scheduler_daemon.add_argument("--exit-when-disabled", action="store_true")
    p_scheduler_daemon.add_argument("--interval-sec", type=int, default=None)
    p_scheduler_daemon.add_argument("--max-steps", type=int, default=None)
    p_scheduler_daemon.add_argument("--force", action="store_true")
    p_scheduler_daemon.add_argument("--group-id", default=DEFAULT_GROUP_ID)
    p_scheduler_daemon.add_argument("--account-id", default=DEFAULT_ACCOUNT_ID)
    p_scheduler_daemon.add_argument("--mode", choices=["send", "dry-run"], default="send")
    p_scheduler_daemon.add_argument("--session-id", default="")
    p_scheduler_daemon.add_argument("--timeout-sec", type=int, default=120)
    p_scheduler_daemon.add_argument("--spawn", dest="spawn", action="store_true", default=True)
    p_scheduler_daemon.add_argument("--no-spawn", dest="spawn", action="store_false")
    p_scheduler_daemon.add_argument("--spawn-cmd", default="")
    p_scheduler_daemon.add_argument("--spawn-output", default="")
    p_scheduler_daemon.add_argument("--visibility-mode", choices=list(VISIBILITY_MODES), default=VISIBILITY_MODES[0])
    p_scheduler_daemon.set_defaults(func=cmd_scheduler_daemon)

    p_clarify = sub.add_parser("clarify")
    p_clarify.add_argument("--root", required=True)
    p_clarify.add_argument("--task-id", required=True)
    p_clarify.add_argument("--role", required=True)
    p_clarify.add_argument("--question", required=True)
    p_clarify.add_argument("--actor", default="orchestrator")
    p_clarify.add_argument("--group-id", default=DEFAULT_GROUP_ID)
    p_clarify.add_argument("--account-id", default=DEFAULT_ACCOUNT_ID)
    p_clarify.add_argument("--cooldown-sec", type=int, default=300)
    p_clarify.add_argument("--state-file", default="")
    p_clarify.add_argument("--mode", choices=["send", "dry-run"], default="send")
    p_clarify.add_argument("--force", action="store_true")
    p_clarify.set_defaults(func=cmd_clarify)

    p_feishu = sub.add_parser("feishu-router")
    p_feishu.add_argument("--root", required=True)
    p_feishu.add_argument("--actor", required=True)
    p_feishu.add_argument("--text", required=True)
    p_feishu.add_argument("--group-id", default=DEFAULT_GROUP_ID)
    p_feishu.add_argument("--account-id", default=DEFAULT_ACCOUNT_ID)
    p_feishu.add_argument("--mode", choices=["send", "dry-run", "off"], default="send")
    p_feishu.add_argument("--session-id", default="")
    p_feishu.add_argument("--timeout-sec", type=int, default=120)
    p_feishu.add_argument("--dispatch-spawn", action="store_true")
    p_feishu.add_argument("--dispatch-manual", action="store_true")
    p_feishu.add_argument("--visibility-mode", choices=list(VISIBILITY_MODES), default=VISIBILITY_MODES[0])
    p_feishu.add_argument("--autopilot-max-steps", type=int, default=3)
    p_feishu.add_argument("--spawn-cmd", default="")
    p_feishu.add_argument("--spawn-output", default="")
    p_feishu.add_argument("--clarify-cooldown-sec", type=int, default=300)
    p_feishu.add_argument("--clarify-state-file", default="")
    p_feishu.set_defaults(func=cmd_feishu_router)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
