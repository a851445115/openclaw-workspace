#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_GROUP_ID = "oc_041146c92a9ccb403a7f4f48fb59701d"
DEFAULT_ACCOUNT_ID = "orchestrator"
DEFAULT_ALLOWED_BROADCASTERS = {"orchestrator"}
OPTIONAL_BROADCASTER = "broadcaster"
CLARIFY_ROLES = {"coder", "invest-analyst", "debugger", "broadcaster"}
DONE_HINTS = ("[DONE]", " done", "completed", "finish", "完成", "已完成", "通过", "verified")
BLOCKED_HINTS = ("[BLOCKED]", "blocked", "failed", "error", "exception", "失败", "阻塞", "卡住", "无法")
EVIDENCE_HINTS = ("/", ".py", ".md", "http", "截图", "日志", "log", "输出", "result", "测试")
BOT_OPENID_CONFIG_CANDIDATES = (
    os.path.join("config", "feishu-bot-openids.json"),
    os.path.join("state", "feishu-bot-openids.json"),
)


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


def cmd_dispatch(args: argparse.Namespace) -> int:
    if args.actor != "orchestrator":
        print(json.dumps({"ok": False, "error": "dispatch is restricted to actor=orchestrator"}))
        return 1

    task = get_task(args.root, args.task_id)
    if not isinstance(task, dict):
        print(json.dumps({"ok": False, "error": f"task not found: {args.task_id}"}))
        return 1

    claimed = ensure_claimed(args.root, args.task_id, args.agent)
    if not isinstance(claimed, dict) or not claimed.get("ok"):
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"failed to claim task: {args.task_id}",
                    "claim": claimed,
                },
                ensure_ascii=True,
            )
        )
        return 1

    task = get_task(args.root, args.task_id) or task
    status = str(task.get("status") or "")
    title = clip(task.get("title") or "未命名任务")
    dispatch_task = clip(args.task or f"{args.task_id}: {task.get('title') or 'untitled'}", 300)

    claim_text = "\n".join(
        [
            f"[CLAIM] {args.task_id} | 状态={status_zh(status or '-')} | 指派={args.agent}",
            f"标题: {title}",
            "派发模式: 手动协作（Simple Wake-up v1）",
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
            f"请 {assignee_mention} 完成后回报：{report_template}。",
        ]
    )
    task_send = send_group_message(args.group_id, args.account_id, task_text, args.mode)

    ok = bool(claimed.get("ok")) and bool(claim_send.get("ok")) and bool(task_send.get("ok"))
    print(
        json.dumps(
            {
                "ok": ok,
                "handled": True,
                "intent": "dispatch",
                "taskId": args.task_id,
                "agent": args.agent,
                "dispatchMode": "manual",
                "claim": claimed,
                "claimSend": claim_send,
                "taskSend": task_send,
                "waitForReport": True,
                "autoClose": False,
                "reportTemplate": report_template,
            },
            ensure_ascii=True,
        )
    )
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
    now_ts = int(time.time())
    last = entries.get(key, {})
    last_ts = int(last.get("ts", 0)) if isinstance(last, dict) else 0
    wait = args.cooldown_sec - (now_ts - last_ts)
    if wait > 0 and not args.force:
        print(
            json.dumps(
                {
                    "ok": False,
                    "throttled": True,
                    "retryAfterSec": wait,
                    "lastAt": last.get("at") if isinstance(last, dict) else None,
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
        entries[key] = {"ts": now_ts, "at": now_iso(), "taskId": args.task_id, "by": args.actor}
        save_json_file(state_file, state)
    print(json.dumps({"ok": bool(sent.get("ok")), "send": sent, "throttleKey": key}, ensure_ascii=True))
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
        if t.get("status") in {"pending", "claimed", "in_progress"}:
            candidates.append(t)
    if not candidates:
        return None
    candidates.sort(key=lambda x: x.get("taskId") or "")
    return candidates[0]


def has_evidence(text: str) -> bool:
    lower = text.lower()
    return any(h.lower() in lower for h in EVIDENCE_HINTS)


def parse_wakeup_kind(text: str) -> str:
    lower = text.lower()
    if any(h.lower() in lower for h in BLOCKED_HINTS):
        return "blocked"
    if any(h.lower() in lower for h in DONE_HINTS):
        return "done"
    return "progress"


def find_task_id(text: str) -> str:
    m = re.search(r"\bT-\d+\b", text, flags=re.IGNORECASE)
    return m.group(0).upper() if m else ""


def cmd_feishu_router(args: argparse.Namespace) -> int:
    text = (args.text or "").strip()
    norm = text.replace("＠", "@").strip()
    if not norm:
        print(json.dumps({"ok": False, "handled": False, "error": "empty text"}))
        return 1

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
        )
        rc = cmd_dispatch(d_args)
        return rc
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

        if kind == "done" and has_evidence(norm):
            apply_obj = board_apply(args.root, "orchestrator", f"mark done {task_id}: {clip(norm, 120)}")
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
            print(json.dumps({"ok": ok, "handled": True, "intent": "wakeup", "kind": kind, "verify": "self-check", "apply": apply_obj, "publish": publish}))
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
    p_dispatch.set_defaults(func=cmd_dispatch)

    p_clarify = sub.add_parser("clarify")
    p_clarify.add_argument("--root", required=True)
    p_clarify.add_argument("--task-id", required=True)
    p_clarify.add_argument("--role", required=True)
    p_clarify.add_argument("--question", required=True)
    p_clarify.add_argument("--actor", default="orchestrator")
    p_clarify.add_argument("--group-id", default=DEFAULT_GROUP_ID)
    p_clarify.add_argument("--account-id", default=DEFAULT_ACCOUNT_ID)
    p_clarify.add_argument("--cooldown-sec", type=int, default=180)
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
    p_feishu.set_defaults(func=cmd_feishu_router)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
