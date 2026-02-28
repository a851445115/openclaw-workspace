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
HARD_EVIDENCE_PATTERNS = (
    r"https?://",
    r"\b[\w./-]+\.(?:py|md|json|yaml|yml|log|txt|csv|png|jpg|jpeg|webp)\b",
    r"\blogs?/[\w./-]+\b",
    r"\bpytest\b.*\b(pass|passed|failed)\b",
    r"\btest(?:s)?\b.*\b(pass|passed|failed)\b",
)
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
DEFAULT_GOVERNANCE_STATE: Dict[str, Any] = {
    "paused": False,
    "pauseReason": "",
    "pausedAt": "",
    "frozenTaskIds": [],
}
RECOVERY_AGENT_CHAIN = ("debugger", "invest-analyst", "coder")


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


def looks_like_hard_evidence(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    lower = s.lower()
    for pat in HARD_EVIDENCE_PATTERNS:
        if re.search(pat, lower, flags=re.IGNORECASE):
            return True
    return False


def count_hard_evidence(items: List[str]) -> int:
    return sum(1 for item in items if looks_like_hard_evidence(item))


def normalize_verify_commands(value: Any, limit: int = 6) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(value, str):
        cmd = clip(value, 500)
        if cmd:
            out.append({"cmd": cmd, "expectedExit": 0, "cwd": "", "timeoutSec": 45})
        return out
    if not isinstance(value, list):
        return out
    for item in value:
        if isinstance(item, str):
            cmd = clip(item, 500)
            if cmd:
                out.append({"cmd": cmd, "expectedExit": 0, "cwd": "", "timeoutSec": 45})
        elif isinstance(item, dict):
            cmd = clip(str(item.get("cmd") or item.get("command") or ""), 500)
            if not cmd:
                continue
            out.append(
                {
                    "cmd": cmd,
                    "expectedExit": parse_int(item.get("expectedExit", 0), 0),
                    "cwd": clip(str(item.get("cwd") or ""), 200),
                    "timeoutSec": max(1, min(120, parse_int(item.get("timeoutSec", 45), 45))),
                }
            )
        if len(out) >= limit:
            break
    return out


def run_verify_commands(root: str, commands: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for item in commands:
        cmd_text = str(item.get("cmd") or "").strip()
        expected_exit = parse_int(item.get("expectedExit", 0), 0)
        cwd_hint = str(item.get("cwd") or "").strip()
        timeout_sec = max(1, min(120, parse_int(item.get("timeoutSec", 45), 45)))
        if not cmd_text:
            continue
        cwd = root
        if cwd_hint:
            candidate = cwd_hint if os.path.isabs(cwd_hint) else os.path.join(root, cwd_hint)
            if os.path.isdir(candidate):
                cwd = candidate
        try:
            proc = subprocess.run(
                shlex.split(cmd_text),
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_sec,
            )
            exit_code = int(proc.returncode)
            ok = exit_code == expected_exit
            results.append(
                {
                    "cmd": cmd_text,
                    "cwd": cwd,
                    "expectedExit": expected_exit,
                    "exitCode": exit_code,
                    "ok": ok,
                    "stdout": clip(proc.stdout or "", 260),
                    "stderr": clip(proc.stderr or "", 260),
                }
            )
        except Exception as err:
            results.append(
                {
                    "cmd": cmd_text,
                    "cwd": cwd,
                    "expectedExit": expected_exit,
                    "exitCode": -1,
                    "ok": False,
                    "stdout": "",
                    "stderr": clip(str(err), 260),
                }
            )
    return results


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
        "verifyCommands": [{"cmd": "pytest -q", "expectedExit": 0, "cwd": ".", "timeoutSec": 60}],
        "risks": ["潜在风险或注意事项"],
        "nextActions": ["下一步建议（可为空）"],
    }


def build_agent_prompt(root: str, task: Dict[str, Any], agent: str, dispatch_task: str) -> str:
    task_id = str(task.get("taskId") or "")
    title = str(task.get("title") or "")
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
            "4. verifyCommands is optional; if provided, each command must pass expectedExit.",
            "5. If blocked, summary must state blocker cause clearly.",
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

    source_task_id = str(base.get("taskId") or spawn_obj.get("taskId") or "").strip()
    source_agent = str(base.get("agent") or spawn_obj.get("agent") or "").strip()
    status_hint = str(base.get("status") or spawn_obj.get("status") or base.get("taskStatus") or "").strip().lower()
    summary = clip(
        str(base.get("summary") or base.get("message") or base.get("result") or base.get("output") or ""),
        260,
    )
    evidence = normalize_string_list(base.get("evidence"))
    verify_commands = normalize_verify_commands(base.get("verifyCommands") or base.get("verify"))
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
        and any(k in base for k in ("summary", "evidence", "changes", "nextActions", "risks", "status", "verifyCommands"))
    )
    return {
        "taskId": task_id,
        "agent": role,
        "sourceTaskId": source_task_id,
        "sourceAgent": source_agent,
        "status": status_hint,
        "summary": summary,
        "evidence": evidence,
        "verifyCommands": verify_commands,
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
        accepted = evaluate_acceptance(root, role, text or detail, report=report, task_id=task_id)
        if accepted.get("ok"):
            return {
                "decision": "done",
                "detail": clip(detail or text or f"{task_id} 子代理返回完成", 200),
                "reasonCode": "done_with_evidence",
                "report": report,
            }
        reject_code = str(accepted.get("reasonCode") or "incomplete_output")
        return {
            "decision": "blocked",
            "detail": clip(
                f"{detail or text or f'{task_id} 子代理结果未通过验收'} | {accepted.get('reason') or '未通过验收策略'}",
                200,
            ),
            "reasonCode": reject_code,
            "acceptance": accepted,
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
    simulated_output = str(getattr(args, "spawn_output", "") or "").strip()
    raw_seq = str(getattr(args, "spawn_output_seq", "") or "").strip()
    if raw_seq:
        try:
            seq_output = next_spawn_output(args)
        except Exception as err:
            return {
                "ok": False,
                "error": f"invalid --spawn-output-seq: {err}",
                "stdout": raw_seq,
                "stderr": "",
                "command": ["--spawn-output-seq"],
                "decision": "blocked",
                "detail": clip(str(err), 200),
                "reasonCode": "invalid_spawn_output_seq",
            }
        if seq_output:
            simulated_output = seq_output

    if args.mode == "dry-run" and not simulated_output:
        return {
            "ok": True,
            "skipped": True,
            "reason": "dry-run without spawn output",
            "stdout": "",
            "stderr": "",
            "command": [],
            "decision": "",
            "detail": "",
        }

    if simulated_output:
        try:
            obj = parse_json_loose(simulated_output)
            if not isinstance(obj, dict):
                obj = {"raw": simulated_output}
            decision = classify_spawn_result(args.root, args.task_id, args.agent, obj, fallback_text=simulated_output)
            return {
                "ok": True,
                "simulated": True,
                "stdout": simulated_output,
                "stderr": "",
                "command": ["--spawn-output-seq"] if raw_seq else ["--spawn-output"],
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
                "stdout": simulated_output,
                "stderr": "",
                "command": ["--spawn-output-seq"] if raw_seq else ["--spawn-output"],
                "decision": "blocked",
                "detail": clip(str(err), 200),
                "reasonCode": "invalid_spawn_output",
            }

    if args.spawn_cmd:
        rendered = (
            args.spawn_cmd.replace("{agent}", args.agent)
            .replace("{task_id}", args.task_id)
            .replace("{task}", task_prompt)
        )
        cmd = shlex.split(rendered)
    else:
        cmd = [
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
    recovery: Dict[str, Any] = {"enabled": bool(getattr(args, "auto_recover", False)), "applied": False, "attempts": []}

    if args.spawn:
        spawn = run_dispatch_spawn(args, agent_prompt)
        if (
            not spawn.get("skipped")
            and not str(getattr(args, "spawn_output", "") or "").strip()
            and not str(getattr(args, "spawn_output_seq", "") or "").strip()
            and spawn.get("decision") == "blocked"
            and str(spawn.get("reasonCode") or "")
            in {
                "incomplete_output",
                "missing_evidence",
                "missing_hard_evidence",
                "stage_only",
                "role_policy_missing_keyword",
                "schema_task_mismatch",
                "schema_agent_mismatch",
                "schema_status_invalid",
                "schema_missing_summary",
            }
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

        if (
            not spawn.get("skipped")
            and bool(getattr(args, "auto_recover", False))
            and str(spawn.get("decision") or "") == "blocked"
        ):
            max_attempts = max(1, parse_int(getattr(args, "recovery_max_attempts", 2), 2))
            for attempt_idx in range(max_attempts):
                recovery_agent = choose_recovery_agent(args.agent, attempt_idx)
                recovery_prompt = clip(
                    "\n".join(
                        [
                            agent_prompt,
                            "",
                            f"恢复模式: 请由 @{recovery_agent} 接管此任务，优先定位阻塞并给出可验收结果。",
                            f"上次失败原因: {spawn.get('reasonCode') or 'unknown'}",
                            f"上次失败详情: {clip(spawn.get('detail') or '', 260)}",
                        ]
                    ),
                    5000,
                )
                r_args = argparse.Namespace(**vars(args))
                r_args.agent = recovery_agent
                if hasattr(args, "_spawn_output_seq_queue"):
                    r_args._spawn_output_seq_queue = getattr(args, "_spawn_output_seq_queue")
                recovery_spawn = run_dispatch_spawn(r_args, recovery_prompt)
                recovery_entry = {
                    "attempt": attempt_idx + 1,
                    "agent": recovery_agent,
                    "decision": recovery_spawn.get("decision"),
                    "reasonCode": recovery_spawn.get("reasonCode"),
                    "detail": recovery_spawn.get("detail"),
                }
                recovery["attempts"].append(recovery_entry)
                if recovery_spawn.get("decision") == "done":
                    recovery["applied"] = True
                    recovery["agent"] = recovery_agent
                    recovery["decision"] = recovery_spawn.get("decision")
                    spawn = recovery_spawn
                    break

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
        "recovery": recovery,
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


def run_autopilot(args: argparse.Namespace) -> Dict[str, Any]:
    if args.actor != "orchestrator":
        return {"ok": False, "error": "autopilot is restricted to actor=orchestrator", "handled": True, "intent": "autopilot"}
    max_steps = max(1, parse_int(getattr(args, "max_steps", 1), 1))
    step_time_budget_sec = parse_int(getattr(args, "step_time_budget_sec", -1), -1)
    steps: List[Dict[str, Any]] = []
    summary = {"done": 0, "blocked": 0, "manual": 0}
    stop_reason = "no_runnable_task"
    reason_code = "no_runnable_task"
    ok = True
    started_at = time.time()

    shared_seq: Optional[List[str]] = None
    if str(getattr(args, "spawn_output_seq", "") or "").strip():
        try:
            shared_seq = ensure_spawn_output_seq_queue(args)
        except Exception as err:
            return {
                "ok": False,
                "handled": True,
                "intent": "autopilot",
                "maxSteps": max_steps,
                "stepsRun": 0,
                "summary": summary,
                "stopReason": "invalid_spawn_output_seq",
                "reasonCode": "invalid_spawn_output_seq",
                "error": str(err),
                "visibilityMode": str(getattr(args, "visibility_mode", VISIBILITY_MODES[0])),
                "steps": [],
            }

    for idx in range(max_steps):
        task = choose_task_for_run(args.root, "")
        if not isinstance(task, dict):
            stop_reason = "no_runnable_task"
            reason_code = "no_runnable_task"
            break
        task_id = str(task.get("taskId") or "").strip()
        if not task_id:
            stop_reason = "invalid_task"
            reason_code = "invalid_task"
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
            session_id=getattr(args, "session_id", ""),
            group_id=getattr(args, "group_id", DEFAULT_GROUP_ID),
            account_id=getattr(args, "account_id", DEFAULT_ACCOUNT_ID),
            mode=getattr(args, "mode", "send"),
            timeout_sec=parse_int(getattr(args, "timeout_sec", 120), 120),
            spawn=bool(getattr(args, "spawn", True)),
            spawn_cmd=str(getattr(args, "spawn_cmd", "") or ""),
            spawn_output=str(getattr(args, "spawn_output", "") or ""),
            spawn_output_seq=str(getattr(args, "spawn_output_seq", "") or ""),
            auto_recover=bool(getattr(args, "auto_recover", False)),
            recovery_max_attempts=parse_int(getattr(args, "recovery_max_attempts", 2), 2),
            visibility_mode=str(getattr(args, "visibility_mode", VISIBILITY_MODES[0]) or VISIBILITY_MODES[0]),
        )
        if shared_seq is not None:
            d_args._spawn_output_seq_queue = shared_seq
        step_started = time.time()
        dispatch_result = dispatch_once(d_args)
        step_elapsed_sec = round(time.time() - step_started, 3)
        steps.append(
            {
                "index": idx + 1,
                "taskId": task_id,
                "agent": agent,
                "stepElapsedSec": step_elapsed_sec,
                "dispatch": dispatch_result,
            }
        )

        if step_time_budget_sec >= 0 and step_elapsed_sec > step_time_budget_sec:
            ok = False
            stop_reason = "task_time_budget_exceeded"
            reason_code = "task_time_budget_exceeded"
            break

        if not dispatch_result.get("ok"):
            ok = False
            stop_reason = "dispatch_failed"
            reason_code = str((dispatch_result.get("spawn") or {}).get("reasonCode") or "dispatch_failed")
            break

        if dispatch_result.get("autoClose"):
            if str((dispatch_result.get("spawn") or {}).get("decision") or "") == "done":
                summary["done"] += 1
            else:
                summary["blocked"] += 1
        else:
            summary["manual"] += 1
        stop_reason = "max_steps_reached"
        reason_code = "max_steps_reached"

    return {
        "ok": ok,
        "handled": True,
        "intent": "autopilot",
        "maxSteps": max_steps,
        "stepTimeBudgetSec": step_time_budget_sec,
        "stepsRun": len(steps),
        "summary": summary,
        "stopReason": stop_reason,
        "reasonCode": reason_code,
        "elapsedSec": round(time.time() - started_at, 3),
        "visibilityMode": str(getattr(args, "visibility_mode", VISIBILITY_MODES[0])),
        "steps": steps,
    }


def cmd_autopilot(args: argparse.Namespace) -> int:
    result = run_autopilot(args)
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get("ok") else 1


def cmd_scheduler_run(args: argparse.Namespace) -> int:
    if args.actor != "orchestrator":
        print(json.dumps({"ok": False, "error": "scheduler-run is restricted to actor=orchestrator"}))
        return 1

    governance = load_governance_state(args.root)
    if governance.get("paused"):
        result = {
            "ok": False,
            "handled": True,
            "intent": "scheduler_run",
            "reasonCode": "scheduler_paused",
            "governance": governance,
        }
        print(json.dumps(result, ensure_ascii=True))
        return 1

    state_file = str(getattr(args, "state_file", "") or scheduler_state_path(args.root))
    state = load_json_file(state_file, {"lastRunTs": 0, "runs": []})
    now_ts = int(time.time())
    last_run_ts = parse_int(state.get("lastRunTs", 0), 0)
    debounce_sec = max(0, parse_int(getattr(args, "debounce_sec", 0), 0))
    if debounce_sec > 0 and last_run_ts > 0 and (now_ts - last_run_ts) < debounce_sec:
        retry_after = debounce_sec - (now_ts - last_run_ts)
        result = {
            "ok": False,
            "handled": True,
            "intent": "scheduler_run",
            "throttled": True,
            "reasonCode": "scheduler_debounced",
            "retryAfterSec": retry_after,
            "lastRunTs": last_run_ts,
        }
        print(json.dumps(result, ensure_ascii=True))
        return 1

    window_sec = max(1, parse_int(getattr(args, "window_sec", 3600), 3600))
    max_runs = max(1, parse_int(getattr(args, "max_runs", 24), 24))
    runs_raw = state.get("runs", [])
    runs: List[int] = []
    if isinstance(runs_raw, list):
        for item in runs_raw:
            ts = parse_int(item, 0)
            if ts > 0 and ts >= now_ts - window_sec:
                runs.append(ts)
    if len(runs) >= max_runs:
        result = {
            "ok": False,
            "handled": True,
            "intent": "scheduler_run",
            "throttled": True,
            "reasonCode": "scheduler_window_limit",
            "windowSec": window_sec,
            "maxRuns": max_runs,
        }
        print(json.dumps(result, ensure_ascii=True))
        return 1

    cycles = max(1, parse_int(getattr(args, "cycles", 1), 1))
    autopilot_steps = max(1, parse_int(getattr(args, "autopilot_steps", 1), 1))
    task_time_budget_sec = parse_int(getattr(args, "task_time_budget_sec", -1), -1)
    cycle_time_budget_sec = parse_int(getattr(args, "cycle_time_budget_sec", -1), -1)
    budget_degrade = str(getattr(args, "budget_degrade", "stop_run") or "stop_run").strip().lower()
    if budget_degrade not in {"stop_run", "manual_handoff", "reduced_context"}:
        budget_degrade = "stop_run"
    cycles_out: List[Dict[str, Any]] = []
    ok = True
    stop_reason = "completed"
    reason_code = "completed"
    started_at = time.time()
    degrade_applied = ""
    spawn_enabled = bool(getattr(args, "spawn", True))

    shared_seq: Optional[List[str]] = None
    if str(getattr(args, "spawn_output_seq", "") or "").strip():
        try:
            shared_seq = ensure_spawn_output_seq_queue(args)
        except Exception as err:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "handled": True,
                        "intent": "scheduler_run",
                        "error": str(err),
                        "reasonCode": "invalid_spawn_output_seq",
                    },
                    ensure_ascii=True,
                )
            )
            return 1

    def budget_exhausted() -> bool:
        return cycle_time_budget_sec >= 0 and (time.time() - started_at) >= cycle_time_budget_sec

    for idx in range(cycles):
        if budget_exhausted():
            if budget_degrade == "stop_run":
                ok = False
                stop_reason = "budget_cycle_exhausted"
                reason_code = "budget_cycle_exhausted"
                break
            if not degrade_applied:
                degrade_applied = budget_degrade
            if budget_degrade == "manual_handoff":
                spawn_enabled = False
                stop_reason = "budget_degraded_manual_handoff"
                reason_code = "budget_degraded_manual_handoff"
            elif budget_degrade == "reduced_context":
                stop_reason = "budget_degraded_reduced_context"
                reason_code = "budget_degraded_reduced_context"

        a_args = argparse.Namespace(
            root=args.root,
            actor="orchestrator",
            session_id=getattr(args, "session_id", ""),
            group_id=getattr(args, "group_id", DEFAULT_GROUP_ID),
            account_id=getattr(args, "account_id", DEFAULT_ACCOUNT_ID),
            mode=getattr(args, "mode", "send"),
            timeout_sec=parse_int(getattr(args, "timeout_sec", 120), 120),
            spawn=spawn_enabled,
            spawn_cmd=str(getattr(args, "spawn_cmd", "") or ""),
            spawn_output=str(getattr(args, "spawn_output", "") or ""),
            spawn_output_seq=str(getattr(args, "spawn_output_seq", "") or ""),
            auto_recover=bool(getattr(args, "auto_recover", False)),
            recovery_max_attempts=parse_int(getattr(args, "recovery_max_attempts", 2), 2),
            max_steps=autopilot_steps,
            step_time_budget_sec=task_time_budget_sec,
            visibility_mode=str(getattr(args, "visibility_mode", VISIBILITY_MODES[0]) or VISIBILITY_MODES[0]),
        )
        if degrade_applied == "reduced_context":
            a_args.visibility_mode = VISIBILITY_MODES[0]
        if shared_seq is not None:
            a_args._spawn_output_seq_queue = shared_seq
        auto_result = run_autopilot(a_args)
        cycles_out.append({"index": idx + 1, "autopilot": auto_result})
        if not auto_result.get("ok"):
            ok = False
            stop_reason = "autopilot_failed"
            reason_code = str(auto_result.get("reasonCode") or "autopilot_failed")
            break
        if parse_int(auto_result.get("stepsRun", 0), 0) <= 0:
            stop_reason = "idle"
            reason_code = "idle"
            break

    state["lastRunTs"] = now_ts
    state["lastRunAt"] = now_iso()
    runs.append(now_ts)
    state["runs"] = runs[-max(max_runs * 2, max_runs) :]
    save_json_file(state_file, state)

    result = {
        "ok": ok,
        "handled": True,
        "intent": "scheduler_run",
        "cyclesRequested": cycles,
        "cyclesRun": len(cycles_out),
        "cycles": cycles_out,
        "stopReason": stop_reason,
        "reasonCode": reason_code,
        "elapsedSec": round(time.time() - started_at, 3),
        "stateFile": state_file,
        "governance": governance,
        "budget": {
            "cycleTimeBudgetSec": cycle_time_budget_sec,
            "taskTimeBudgetSec": task_time_budget_sec,
            "degradePolicy": budget_degrade,
            "degradeApplied": degrade_applied,
        },
        "costTelemetry": {
            "dispatches": sum(parse_int((c.get("autopilot") or {}).get("stepsRun", 0), 0) for c in cycles_out),
            "doneCount": sum(parse_int(((c.get("autopilot") or {}).get("summary") or {}).get("done", 0), 0) for c in cycles_out),
            "blockedCount": sum(parse_int(((c.get("autopilot") or {}).get("summary") or {}).get("blocked", 0), 0) for c in cycles_out),
            "manualCount": sum(parse_int(((c.get("autopilot") or {}).get("summary") or {}).get("manual", 0), 0) for c in cycles_out),
        },
    }
    print(json.dumps(result, ensure_ascii=True))
    return 0 if ok else 1


def cmd_govern(args: argparse.Namespace) -> int:
    if args.actor != "orchestrator":
        print(json.dumps({"ok": False, "error": "govern is restricted to actor=orchestrator"}))
        return 1

    action = str(getattr(args, "action", "status") or "status").strip().lower()
    state = load_governance_state(args.root)
    changed = False

    if action == "pause":
        state["paused"] = True
        state["pauseReason"] = clip(str(getattr(args, "reason", "") or "manual"))
        state["pausedAt"] = now_iso()
        changed = True
    elif action == "resume":
        state["paused"] = False
        state["pauseReason"] = ""
        state["pausedAt"] = ""
        changed = True
    elif action == "freeze":
        task_id = str(getattr(args, "task_id", "") or "").strip()
        if not task_id:
            print(json.dumps({"ok": False, "error": "--task-id is required for action=freeze"}))
            return 1
        frozen = set(state.get("frozenTaskIds") or [])
        frozen.add(task_id)
        state["frozenTaskIds"] = sorted(frozen)
        changed = True
    elif action == "unfreeze":
        task_id = str(getattr(args, "task_id", "") or "").strip()
        if not task_id:
            print(json.dumps({"ok": False, "error": "--task-id is required for action=unfreeze"}))
            return 1
        frozen = set(state.get("frozenTaskIds") or [])
        frozen.discard(task_id)
        state["frozenTaskIds"] = sorted(frozen)
        changed = True
    elif action == "status":
        pass
    else:
        print(json.dumps({"ok": False, "error": f"unsupported action: {action}"}))
        return 1

    state = normalize_governance_state(state)
    if changed:
        save_governance_state(args.root, state)
    result = {"ok": True, "handled": True, "intent": "govern", "action": action, "changed": changed, "state": state}
    print(json.dumps(result, ensure_ascii=True))
    return 0


def cmd_decompose_goal(args: argparse.Namespace) -> int:
    if args.actor != "orchestrator":
        print(json.dumps({"ok": False, "error": "decompose-goal is restricted to actor=orchestrator"}))
        return 1

    goal = clip(str(getattr(args, "goal", "") or ""), 400)
    if not goal:
        print(json.dumps({"ok": False, "error": "goal cannot be empty"}))
        return 1

    max_tasks = max(1, min(20, parse_int(getattr(args, "max_tasks", 6), 6)))
    min_confidence = clamp_float(parse_float(getattr(args, "min_confidence", 0.6), 0.6), 0.0, 1.0)
    decompose_output = str(getattr(args, "decompose_output", "") or "")

    try:
        plan = build_goal_decomposition(goal, max_tasks=max_tasks, decompose_output=decompose_output)
    except Exception as err:
        print(json.dumps({"ok": False, "error": f"failed to decompose goal: {err}"}))
        return 1

    confidence = clamp_float(parse_float(plan.get("confidence", 0.0), 0.0), 0.0, 1.0)
    plan_tasks_raw = plan.get("tasks")
    plan_tasks: List[Dict[str, Any]] = []
    if isinstance(plan_tasks_raw, list):
        used_plan_ids = set()
        for idx, item in enumerate(plan_tasks_raw[:max_tasks], start=1):
            if not isinstance(item, dict):
                continue
            plan_id = str(item.get("id") or f"task{idx}").strip() or f"task{idx}"
            if plan_id in used_plan_ids:
                plan_id = f"{plan_id}_{idx}"
            used_plan_ids.add(plan_id)
            deps_raw = item.get("dependsOn") or []
            deps: List[str] = []
            if isinstance(deps_raw, list):
                deps = [str(x).strip() for x in deps_raw if str(x).strip()]
            elif isinstance(deps_raw, str) and deps_raw.strip():
                deps = [deps_raw.strip()]
            plan_tasks.append(
                {
                    "id": plan_id,
                    "index": idx,
                    "title": clip(str(item.get("title") or ""), 120),
                    "ownerHint": str(item.get("ownerHint") or suggest_agent_from_title(str(item.get("title") or ""))),
                    "dependsOn": deps,
                    "priority": max(0, min(100, parse_int(item.get("priority", 70), 70))),
                    "impact": max(0, min(100, parse_int(item.get("impact", 70), 70))),
                }
            )

    proposal = [
        {
            "planId": item["id"],
            "index": item["index"],
            "title": item["title"],
            "ownerHint": item["ownerHint"],
            "dependsOn": item["dependsOn"],
            "priority": item["priority"],
            "impact": item["impact"],
        }
        for item in plan_tasks
    ]

    require_approval = bool(getattr(args, "require_approval", False))
    force_apply = bool(getattr(args, "force_apply", False))
    pending_approval = (require_approval or confidence < min_confidence) and not force_apply
    approval_reason = "manual_approval_required" if require_approval else "low_confidence"

    if pending_approval:
        review_text = "\n".join(
            [
                f"[REVIEW] 目标拆解待确认 | confidence={confidence:.2f} | min={min_confidence:.2f}",
                f"目标: {goal}",
                f"建议任务数: {len(proposal)}",
            ]
        )
        sent = send_group_message(
            str(getattr(args, "group_id", DEFAULT_GROUP_ID)),
            str(getattr(args, "account_id", DEFAULT_ACCOUNT_ID)),
            review_text,
            str(getattr(args, "mode", "send")),
        )
        result = {
            "ok": True,
            "handled": True,
            "intent": "decompose_goal",
            "goal": goal,
            "confidence": confidence,
            "minConfidence": min_confidence,
            "pendingApproval": True,
            "reasonCode": "needs_approval",
            "approvalReason": approval_reason,
            "proposal": proposal,
            "createdCount": 0,
            "mergedCount": 0,
            "planTaskMap": [],
            "send": sent,
        }
        print(json.dumps(result, ensure_ascii=True))
        return 0

    snapshot = load_snapshot(args.root)
    existing_tasks = snapshot.get("tasks", {})
    existing_title_map: Dict[str, str] = {}
    existing_status_map: Dict[str, str] = {}
    for task_id, task in existing_tasks.items():
        if not isinstance(task, dict):
            continue
        title_key = normalize_title_key(str(task.get("title") or ""))
        if not title_key:
            continue
        status = str(task.get("status") or "")
        prev_task_id = existing_title_map.get(title_key)
        prev_status = existing_status_map.get(title_key, "")
        prefer_new = False
        if not prev_task_id:
            prefer_new = True
        elif prev_status == "done" and status != "done":
            prefer_new = True
        if prefer_new:
            existing_title_map[title_key] = str(task_id)
            existing_status_map[title_key] = status

    plan_to_task: Dict[str, str] = {}
    created: List[Dict[str, Any]] = []
    merged: List[Dict[str, Any]] = []
    apply_results: List[Dict[str, Any]] = []
    errors: List[str] = []
    plan_task_map: List[Dict[str, Any]] = []

    for item in plan_tasks:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        owner_hint = str(item.get("ownerHint") or "coder")
        if owner_hint not in BOT_ROLES:
            owner_hint = suggest_agent_from_title(title)
        title_key = normalize_title_key(title)
        plan_id = str(item.get("id") or "")
        existing_task_id = existing_title_map.get(title_key) if title_key else ""

        if existing_task_id:
            plan_to_task[plan_id] = existing_task_id
            merged_item = {"planId": plan_id, "taskId": existing_task_id, "title": title, "ownerHint": owner_hint}
            merged.append(merged_item)
            plan_task_map.append({**merged_item, "action": "merged"})
            continue

        apply_obj = board_apply(args.root, "orchestrator", f"@{owner_hint} create task: {title}")
        apply_results.append(apply_obj)
        if not bool(apply_obj.get("ok")):
            errors.append(f"create task failed for {plan_id}: {apply_obj.get('error') or 'unknown'}")
            continue
        task_id = str(apply_obj.get("taskId") or "")
        if not task_id:
            errors.append(f"missing taskId for created task {plan_id}")
            continue
        plan_to_task[plan_id] = task_id
        if title_key:
            existing_title_map[title_key] = task_id
        created_item = {"planId": plan_id, "taskId": task_id, "title": title, "ownerHint": owner_hint}
        created.append(created_item)
        plan_task_map.append({**created_item, "action": "created"})

    routing = load_task_routing(args.root)
    priorities = dict(routing.get("priorities") or {})
    depends_on = dict(routing.get("dependsOn") or {})
    title_to_task_id: Dict[str, str] = {}
    for task_id, task in existing_tasks.items():
        if not isinstance(task, dict):
            continue
        key = normalize_title_key(str(task.get("title") or ""))
        if key:
            title_to_task_id[key] = str(task_id)
    for item in plan_task_map:
        key = normalize_title_key(str(item.get("title") or ""))
        if key:
            title_to_task_id[key] = str(item.get("taskId") or "")

    plan_id_order = [str(item.get("id") or "") for item in plan_tasks]
    plan_index_to_id = {str(idx + 1): pid for idx, pid in enumerate(plan_id_order) if pid}
    known_task_ids = set(str(k) for k in existing_tasks.keys()) | set(plan_to_task.values())

    def resolve_dep(ref: str) -> str:
        dep_ref = str(ref or "").strip()
        if not dep_ref:
            return ""
        if dep_ref in plan_to_task:
            return str(plan_to_task.get(dep_ref) or "")
        if dep_ref in plan_index_to_id:
            mapped = plan_to_task.get(plan_index_to_id[dep_ref], "")
            return str(mapped or "")
        dep_upper = dep_ref.upper()
        if dep_upper in known_task_ids:
            return dep_upper
        key = normalize_title_key(dep_ref)
        if key:
            return str(title_to_task_id.get(key) or "")
        return ""

    for item in plan_tasks:
        plan_id = str(item.get("id") or "")
        task_id = str(plan_to_task.get(plan_id) or "")
        if not task_id:
            continue
        priorities[task_id] = max(0, min(100, parse_int(item.get("priority", 70), 70)))
        raw_deps = item.get("dependsOn") or []
        dep_ids: List[str] = []
        for dep_ref in raw_deps:
            dep_task_id = resolve_dep(str(dep_ref))
            if dep_task_id and dep_task_id != task_id and dep_task_id not in dep_ids:
                dep_ids.append(dep_task_id)
        if dep_ids:
            depends_on[task_id] = dep_ids

    routing_path = routing_state_path(args.root)
    save_json_file(routing_path, {"priorities": priorities, "dependsOn": depends_on})

    summary_text = "\n".join(
        [
            f"[TASK] 目标拆解完成 | confidence={confidence:.2f}",
            f"目标: {goal}",
            f"新增{len(created)}，合并{len(merged)}，总计划{len(plan_tasks)}",
        ]
    )
    sent = send_group_message(
        str(getattr(args, "group_id", DEFAULT_GROUP_ID)),
        str(getattr(args, "account_id", DEFAULT_ACCOUNT_ID)),
        summary_text,
        str(getattr(args, "mode", "send")),
    )

    ok = not errors
    result = {
        "ok": ok,
        "handled": True,
        "intent": "decompose_goal",
        "goal": goal,
        "confidence": confidence,
        "minConfidence": min_confidence,
        "pendingApproval": False,
        "reasonCode": "applied" if ok else "partial_apply_failed",
        "proposal": proposal,
        "createdCount": len(created),
        "mergedCount": len(merged),
        "created": created,
        "merged": merged,
        "planTaskMap": plan_task_map,
        "routing": {"path": routing_path, "updatedTaskCount": len(plan_to_task)},
        "errors": errors,
        "send": sent,
        "source": plan.get("source"),
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


def governance_state_path(root: str) -> str:
    return os.path.join(root, "state", "governance.state.json")


def scheduler_state_path(root: str) -> str:
    return os.path.join(root, "state", "scheduler.state.json")


def routing_state_path(root: str) -> str:
    return os.path.join(root, "state", "task-routing.json")


def normalize_governance_state(raw: Dict[str, Any]) -> Dict[str, Any]:
    state = dict(DEFAULT_GOVERNANCE_STATE)
    if not isinstance(raw, dict):
        return state
    state["paused"] = bool(raw.get("paused", False))
    state["pauseReason"] = clip(str(raw.get("pauseReason") or ""), 200)
    state["pausedAt"] = str(raw.get("pausedAt") or "")
    frozen = raw.get("frozenTaskIds")
    if isinstance(frozen, list):
        task_ids: List[str] = []
        for item in frozen:
            tid = str(item or "").strip()
            if tid:
                task_ids.append(tid)
        state["frozenTaskIds"] = sorted(set(task_ids))
    return state


def load_governance_state(root: str) -> Dict[str, Any]:
    path = governance_state_path(root)
    return normalize_governance_state(load_json_file(path, dict(DEFAULT_GOVERNANCE_STATE)))


def save_governance_state(root: str, data: Dict[str, Any]) -> None:
    path = governance_state_path(root)
    save_json_file(path, normalize_governance_state(data))


def load_task_routing(root: str) -> Dict[str, Any]:
    path = routing_state_path(root)
    default = {"priorities": {}, "dependsOn": {}}
    raw = load_json_file(path, default)
    if not isinstance(raw, dict):
        return default
    priorities = raw.get("priorities") if isinstance(raw.get("priorities"), dict) else {}
    depends_on = raw.get("dependsOn") if isinstance(raw.get("dependsOn"), dict) else {}
    return {"priorities": priorities, "dependsOn": depends_on}


def parse_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def parse_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def ensure_spawn_output_seq_queue(args: argparse.Namespace) -> List[str]:
    queue = getattr(args, "_spawn_output_seq_queue", None)
    if isinstance(queue, list):
        return queue
    raw = str(getattr(args, "spawn_output_seq", "") or "").strip()
    if not raw:
        queue = []
        setattr(args, "_spawn_output_seq_queue", queue)
        return queue
    parsed = parse_json_loose(raw)
    if not isinstance(parsed, list):
        raise ValueError("--spawn-output-seq must be a JSON array")
    queue = []
    for item in parsed:
        if isinstance(item, str):
            queue.append(item)
        else:
            queue.append(json.dumps(item, ensure_ascii=True))
    setattr(args, "_spawn_output_seq_queue", queue)
    return queue


def next_spawn_output(args: argparse.Namespace) -> str:
    queue = ensure_spawn_output_seq_queue(args)
    if not queue:
        return ""
    return str(queue.pop(0) or "")


def choose_recovery_agent(base_agent: str, attempt_idx: int) -> str:
    preferred = [a for a in RECOVERY_AGENT_CHAIN if a != base_agent]
    if base_agent in RECOVERY_AGENT_CHAIN:
        preferred.append(base_agent)
    if not preferred:
        preferred = ["debugger"]
    idx = attempt_idx % len(preferred)
    return preferred[idx]


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


def normalize_title_key(title: str) -> str:
    s = str(title or "").strip().lower()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[^0-9a-z_\u4e00-\u9fff]+", "", s)
    return s


def clamp_float(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def heuristic_decompose_goal(goal: str, max_tasks: int) -> Dict[str, Any]:
    raw_goal = clip(goal, 200)
    segments = [clip(x.strip(" -"), 120) for x in re.split(r"[;\n。]+", raw_goal) if x.strip(" -")]
    tasks: List[Dict[str, Any]] = []
    if len(segments) >= 2:
        for idx, seg in enumerate(segments[:max_tasks], start=1):
            plan_id = f"task{idx}"
            deps = [f"task{idx-1}"] if idx > 1 else []
            tasks.append(
                {
                    "id": plan_id,
                    "title": seg,
                    "ownerHint": suggest_agent_from_title(seg),
                    "dependsOn": deps,
                    "priority": max(10, 95 - idx * 8),
                    "impact": max(10, 90 - idx * 5),
                }
            )
        confidence = 0.68
    else:
        base = clip(raw_goal, 90)
        tasks = [
            {
                "id": "task1",
                "title": f"需求梳理: {base}",
                "ownerHint": "invest-analyst",
                "dependsOn": [],
                "priority": 90,
                "impact": 90,
            },
            {
                "id": "task2",
                "title": f"实现交付: {base}",
                "ownerHint": "coder",
                "dependsOn": ["task1"],
                "priority": 80,
                "impact": 80,
            },
            {
                "id": "task3",
                "title": f"验证回归: {base}",
                "ownerHint": "debugger",
                "dependsOn": ["task2"],
                "priority": 75,
                "impact": 70,
            },
        ][: max(1, max_tasks)]
        confidence = 0.52
    return {"confidence": confidence, "tasks": tasks, "source": "heuristic"}


def normalize_decompose_tasks(raw_tasks: Any, max_tasks: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(raw_tasks, list):
        return out
    for idx, item in enumerate(raw_tasks[: max(1, max_tasks)], start=1):
        if isinstance(item, str):
            title = clip(item, 120)
            if not title:
                continue
            out.append(
                {
                    "id": f"task{idx}",
                    "title": title,
                    "ownerHint": suggest_agent_from_title(title),
                    "dependsOn": [],
                    "priority": max(10, 95 - idx * 8),
                    "impact": max(10, 90 - idx * 5),
                }
            )
            continue
        if not isinstance(item, dict):
            continue
        title = clip(str(item.get("title") or item.get("task") or item.get("summary") or ""), 120)
        if not title:
            continue
        plan_id = clip(str(item.get("id") or item.get("planId") or item.get("key") or f"task{idx}"), 40)
        owner = str(
            item.get("ownerHint") or item.get("owner") or item.get("assignee") or item.get("agent") or ""
        ).strip()
        if owner not in BOT_ROLES:
            owner = suggest_agent_from_title(title)
        deps_raw = item.get("dependsOn") or item.get("blockedBy") or item.get("dependencies") or []
        deps: List[str] = []
        if isinstance(deps_raw, list):
            for dep in deps_raw[:8]:
                dep_text = str(dep or "").strip()
                if dep_text:
                    deps.append(dep_text)
        elif isinstance(deps_raw, str):
            dep_text = deps_raw.strip()
            if dep_text:
                deps.append(dep_text)
        out.append(
            {
                "id": plan_id or f"task{idx}",
                "title": title,
                "ownerHint": owner,
                "dependsOn": deps,
                "priority": max(0, min(100, parse_int(item.get("priority", 70), 70))),
                "impact": max(0, min(100, parse_int(item.get("impact", 70), 70))),
            }
        )
    return out


def build_goal_decomposition(goal: str, max_tasks: int, decompose_output: str = "") -> Dict[str, Any]:
    raw = str(decompose_output or "").strip()
    if not raw:
        return heuristic_decompose_goal(goal, max_tasks)
    parsed = parse_json_loose(raw)
    if not isinstance(parsed, dict):
        raise ValueError("decompose output must be a JSON object")
    tasks = normalize_decompose_tasks(parsed.get("tasks"), max_tasks)
    if not tasks:
        return heuristic_decompose_goal(goal, max_tasks)
    confidence = clamp_float(parse_float(parsed.get("confidence", 0.65), 0.65), 0.0, 1.0)
    return {"confidence": confidence, "tasks": tasks, "source": "provided"}


def choose_task_for_run(root: str, requested: str) -> Optional[Dict[str, Any]]:
    data = load_snapshot(root)
    tasks = data.get("tasks", {})
    governance = load_governance_state(root)
    frozen_task_ids = set(str(x) for x in (governance.get("frozenTaskIds") or []))
    routing = load_task_routing(root)
    priorities = routing.get("priorities") if isinstance(routing.get("priorities"), dict) else {}
    depends_on = routing.get("dependsOn") if isinstance(routing.get("dependsOn"), dict) else {}

    def task_id_of(task_obj: Dict[str, Any]) -> str:
        return str(task_obj.get("taskId") or "").strip()

    def priority_of(task_obj: Dict[str, Any]) -> int:
        tid = task_id_of(task_obj)
        return parse_int(priorities.get(tid, 0), 0)

    def deps_ready(task_obj: Dict[str, Any]) -> bool:
        tid = task_id_of(task_obj)
        raw_deps = depends_on.get(tid)
        deps: List[str] = []
        if isinstance(raw_deps, list):
            deps = [str(x or "").strip() for x in raw_deps if str(x or "").strip()]
        for dep in deps:
            dep_task = tasks.get(dep)
            if not isinstance(dep_task, dict):
                return False
            if str(dep_task.get("status") or "") != "done":
                return False
        return True

    if requested:
        t = tasks.get(requested)
        if isinstance(t, dict) and requested not in frozen_task_ids and deps_ready(t):
            return t
        return None
    candidates = []
    for t in tasks.values():
        if not isinstance(t, dict):
            continue
        if str(t.get("status") or "") not in {"pending", "claimed", "in_progress", "review"}:
            continue
        task_id = task_id_of(t)
        if not task_id or task_id in frozen_task_ids:
            continue
        if not deps_ready(t):
            continue
        candidates.append(t)
    if not candidates:
        return None
    candidates.sort(key=lambda x: (-priority_of(x), task_id_of(x)))
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


def validate_structured_report(expected_task_id: str, expected_role: str, report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(report, dict):
        return {"ok": True, "reasonCode": "accepted"}
    if not bool(report.get("structured")):
        return {"ok": True, "reasonCode": "accepted"}

    status = str(report.get("status") or "").strip().lower()
    if status and status not in {"done", "blocked", "progress"}:
        return {
            "ok": False,
            "reasonCode": "schema_status_invalid",
            "reason": "结构化回报 status 必须是 done|blocked|progress。",
        }

    source_task_id = str(report.get("sourceTaskId") or "").strip()
    if source_task_id and source_task_id != expected_task_id:
        return {
            "ok": False,
            "reasonCode": "schema_task_mismatch",
            "reason": f"结构化回报 taskId={source_task_id} 与任务 {expected_task_id} 不一致。",
        }

    source_agent = str(report.get("sourceAgent") or "").strip().lower()
    if source_agent and source_agent != expected_role.lower():
        return {
            "ok": False,
            "reasonCode": "schema_agent_mismatch",
            "reason": f"结构化回报 agent={source_agent} 与执行角色 {expected_role} 不一致。",
        }

    if status == "done" and not str(report.get("summary") or "").strip():
        return {
            "ok": False,
            "reasonCode": "schema_missing_summary",
            "reason": "status=done 时 summary 不能为空。",
        }

    return {"ok": True, "reasonCode": "accepted"}


def evaluate_acceptance(root: str, role: str, text: str, report: Optional[Dict[str, Any]] = None, task_id: str = "") -> Dict[str, Any]:
    note = (text or "").strip()
    policy = load_acceptance_policy(root)
    global_conf = policy.get("global") if isinstance(policy, dict) else {}
    role_conf = (policy.get("roles") or {}).get(role) if isinstance(policy, dict) else {}
    if not isinstance(global_conf, dict):
        global_conf = {}
    if not isinstance(role_conf, dict):
        role_conf = {}

    if task_id:
        schema_check = validate_structured_report(task_id, role, report)
        if not schema_check.get("ok"):
            return schema_check

    verify_commands = normalize_verify_commands((report or {}).get("verifyCommands"))
    if verify_commands:
        verify_results = run_verify_commands(root, verify_commands)
        failed = [x for x in verify_results if not bool(x.get("ok"))]
        if failed:
            return {
                "ok": False,
                "reasonCode": "verify_command_failed",
                "reason": f"验证命令失败: {clip(str(failed[0].get('cmd') or ''), 120)}",
                "verifyResults": verify_results,
            }

    evidence_entries: List[str] = []
    if isinstance(report, dict):
        evidence_entries.extend(normalize_string_list(report.get("evidence"), limit=8, item_limit=240))
    if has_evidence(note):
        evidence_entries.append(clip(note, 240))
    hard_evidence_count = count_hard_evidence(evidence_entries)

    require_evidence = bool(global_conf.get("requireEvidence", True))
    if require_evidence and not evidence_entries:
        return {
            "ok": False,
            "reasonCode": "missing_evidence",
            "reason": "缺少可验证证据（文件/日志/链接/命令输出）。",
        }
    if require_evidence and hard_evidence_count <= 0:
        return {
            "ok": False,
            "reasonCode": "missing_hard_evidence",
            "reason": "证据存在但缺少硬证据（文件路径/日志/URL/测试输出）。",
        }

    if looks_stage_only(note):
        return {
            "ok": False,
            "reasonCode": "stage_only",
            "reason": "仅包含阶段性描述，未给出最终验收结果。",
        }

    required_any = role_conf.get("requireAny")
    if isinstance(required_any, list) and required_any:
        lower = "\n".join([note] + evidence_entries).lower()
        wanted = [str(x).strip() for x in required_any if str(x).strip()]
        matched = [kw for kw in wanted if kw.lower() in lower]
        if not matched:
            return {
                "ok": False,
                "reasonCode": "role_policy_missing_keyword",
                "reason": f"{role} 交付缺少验收关键词（至少包含其一：{', '.join(wanted[:6])}）。",
            }

    return {
        "ok": True,
        "reasonCode": "accepted",
        "reason": "通过验收策略",
        "hardEvidenceCount": hard_evidence_count,
        "verifyCommandsRun": len(verify_commands),
    }


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

    # Command: @orchestrator decompose [goal:] <text>
    m = re.match(r"^decompose(?:\s+goal)?\s*:?\s+(.+)$", cmd_body, flags=re.IGNORECASE)
    if m:
        d_args = argparse.Namespace(
            root=args.root,
            actor="orchestrator",
            goal=(m.group(1) or "").strip(),
            mode=args.mode,
            group_id=args.group_id,
            account_id=args.account_id,
            max_tasks=parse_int(getattr(args, "decompose_max_tasks", 6), 6),
            min_confidence=parse_float(getattr(args, "decompose_min_confidence", 0.6), 0.6),
            require_approval=bool(getattr(args, "decompose_require_approval", False)),
            force_apply=False,
            decompose_output=str(getattr(args, "decompose_output", "") or ""),
        )
        return cmd_decompose_goal(d_args)

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
            spawn_output_seq=args.spawn_output_seq,
            auto_recover=args.auto_recover,
            recovery_max_attempts=args.recovery_max_attempts,
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
            spawn_output_seq=args.spawn_output_seq,
            auto_recover=args.auto_recover,
            recovery_max_attempts=args.recovery_max_attempts,
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
            spawn_output_seq=args.spawn_output_seq,
            auto_recover=args.auto_recover,
            recovery_max_attempts=args.recovery_max_attempts,
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
            spawn_output_seq=args.spawn_output_seq,
            auto_recover=args.auto_recover,
            recovery_max_attempts=args.recovery_max_attempts,
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
    p_dispatch.add_argument("--spawn-output-seq", default="")
    p_dispatch.add_argument("--auto-recover", action="store_true", default=False)
    p_dispatch.add_argument("--recovery-max-attempts", type=int, default=2)
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
    p_autopilot.add_argument("--spawn-output-seq", default="")
    p_autopilot.add_argument("--auto-recover", action="store_true", default=False)
    p_autopilot.add_argument("--recovery-max-attempts", type=int, default=2)
    p_autopilot.add_argument("--max-steps", type=int, default=3)
    p_autopilot.add_argument("--step-time-budget-sec", type=int, default=-1)
    p_autopilot.add_argument("--visibility-mode", choices=list(VISIBILITY_MODES), default=VISIBILITY_MODES[0])
    p_autopilot.set_defaults(func=cmd_autopilot)

    p_scheduler = sub.add_parser("scheduler-run")
    p_scheduler.add_argument("--root", required=True)
    p_scheduler.add_argument("--actor", default="orchestrator")
    p_scheduler.add_argument("--group-id", default=DEFAULT_GROUP_ID)
    p_scheduler.add_argument("--account-id", default=DEFAULT_ACCOUNT_ID)
    p_scheduler.add_argument("--mode", choices=["send", "dry-run"], default="send")
    p_scheduler.add_argument("--session-id", default="")
    p_scheduler.add_argument("--timeout-sec", type=int, default=120)
    p_scheduler.add_argument("--spawn", dest="spawn", action="store_true", default=True)
    p_scheduler.add_argument("--no-spawn", dest="spawn", action="store_false")
    p_scheduler.add_argument("--spawn-cmd", default="")
    p_scheduler.add_argument("--spawn-output", default="")
    p_scheduler.add_argument("--spawn-output-seq", default="")
    p_scheduler.add_argument("--auto-recover", action="store_true", default=False)
    p_scheduler.add_argument("--recovery-max-attempts", type=int, default=2)
    p_scheduler.add_argument("--visibility-mode", choices=list(VISIBILITY_MODES), default=VISIBILITY_MODES[0])
    p_scheduler.add_argument("--cycles", type=int, default=1)
    p_scheduler.add_argument("--autopilot-steps", type=int, default=1)
    p_scheduler.add_argument("--task-time-budget-sec", type=int, default=-1)
    p_scheduler.add_argument("--cycle-time-budget-sec", type=int, default=-1)
    p_scheduler.add_argument("--budget-degrade", choices=["stop_run", "manual_handoff", "reduced_context"], default="stop_run")
    p_scheduler.add_argument("--debounce-sec", type=int, default=0)
    p_scheduler.add_argument("--window-sec", type=int, default=3600)
    p_scheduler.add_argument("--max-runs", type=int, default=24)
    p_scheduler.add_argument("--state-file", default="")
    p_scheduler.set_defaults(func=cmd_scheduler_run)

    p_govern = sub.add_parser("govern")
    p_govern.add_argument("--root", required=True)
    p_govern.add_argument("--actor", default="orchestrator")
    p_govern.add_argument("--action", choices=["pause", "resume", "freeze", "unfreeze", "status"], default="status")
    p_govern.add_argument("--task-id", default="")
    p_govern.add_argument("--reason", default="")
    p_govern.set_defaults(func=cmd_govern)

    p_decompose = sub.add_parser("decompose-goal")
    p_decompose.add_argument("--root", required=True)
    p_decompose.add_argument("--actor", default="orchestrator")
    p_decompose.add_argument("--goal", required=True)
    p_decompose.add_argument("--mode", choices=["send", "dry-run"], default="send")
    p_decompose.add_argument("--group-id", default=DEFAULT_GROUP_ID)
    p_decompose.add_argument("--account-id", default=DEFAULT_ACCOUNT_ID)
    p_decompose.add_argument("--max-tasks", type=int, default=6)
    p_decompose.add_argument("--min-confidence", type=float, default=0.6)
    p_decompose.add_argument("--require-approval", action="store_true")
    p_decompose.add_argument("--force-apply", action="store_true")
    p_decompose.add_argument("--decompose-output", default="")
    p_decompose.set_defaults(func=cmd_decompose_goal)

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
    p_feishu.add_argument("--spawn-output-seq", default="")
    p_feishu.add_argument("--auto-recover", action="store_true")
    p_feishu.add_argument("--recovery-max-attempts", type=int, default=2)
    p_feishu.add_argument("--decompose-min-confidence", type=float, default=0.6)
    p_feishu.add_argument("--decompose-require-approval", action="store_true")
    p_feishu.add_argument("--decompose-max-tasks", type=int, default=6)
    p_feishu.add_argument("--decompose-output", default="")
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
