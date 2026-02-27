#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_GROUP_ID = "oc_041146c92a9ccb403a7f4f48fb59701d"
DEFAULT_ACCOUNT_ID = "orchestrator"
DEFAULT_ALLOWED_BROADCASTERS = {"orchestrator"}
OPTIONAL_BROADCASTER = "broadcaster"
CLARIFY_ROLES = {"coder", "invest-analyst", "debugger", "broadcaster"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clip(text: Optional[str], limit: int = 160) -> str:
    s = " ".join((text or "").split())
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "..."


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


def status_zh(status: str) -> str:
    s = (status or "").strip()
    return STATUS_ZH.get(s, s or "-")


def build_three_line(prefix: str, task_id: str, status: str, owner_or_hint: str, key_line: str) -> str:
    # Keep protocol tag prefix (e.g. [TASK]) for easy filtering/search.
    line1 = f"{prefix} {task_id} | 状态={status_zh(status)} | {owner_or_hint}"
    line2 = key_line.strip()
    return f"{line1}\n{line2}"


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
    parsed = None
    try:
        parsed = parse_json_loose(stdout) if stdout else None
    except Exception:
        parsed = None
    out = {"ok": True, "dryRun": False, "payload": payload}
    if parsed is not None:
        out["result"] = parsed
    if stderr:
        out["stderr"] = clip(stderr, 500)
    return out


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


def cmd_publish_apply(args: argparse.Namespace) -> int:
    try:
        apply_obj = parse_json_loose(args.apply_json)
    except Exception as err:
        print(json.dumps({"ok": False, "error": f"invalid apply json: {err}"}))
        return 1

    if args.mode == "off":
        print(json.dumps({"ok": True, "skipped": True, "reason": "mode=off"}))
        return 0

    if not actor_allowed(args.actor, args.allow_broadcaster):
        print(
            json.dumps(
                {
                    "ok": True,
                    "skipped": True,
                    "reason": f"actor not allowed to broadcast: {args.actor}",
                }
            )
        )
        return 0

    messages = build_apply_messages(args.root, apply_obj, args.include_escalate_blocked)
    if not messages:
        print(json.dumps({"ok": True, "skipped": True, "reason": "no milestone message for intent"}))
        return 0

    results = []
    for msg in messages:
        sent = send_group_message(args.group_id, args.account_id, msg["text"], args.mode)
        results.append({"message": msg, "send": sent})
    ok = all(r["send"].get("ok") for r in results)
    print(json.dumps({"ok": ok, "count": len(results), "results": results}, ensure_ascii=True))
    return 0 if ok else 1


def gateway_sessions_list() -> Dict[str, Any]:
    cmd = ["openclaw", "gateway", "call", "sessions.list", "--json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"sessions.list failed: {clip(proc.stderr or proc.stdout, 300)}")
    return parse_json_loose(proc.stdout or "")


def resolve_orchestrator_session(group_id: str, explicit_session_id: Optional[str]) -> str:
    if explicit_session_id:
        return explicit_session_id
    data = gateway_sessions_list()
    sessions = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(sessions, list):
        raise RuntimeError("sessions.list response missing sessions array")
    target_key = f"agent:orchestrator:feishu:group:{group_id}"
    for s in sessions:
        if isinstance(s, dict) and s.get("key") == target_key and s.get("sessionId"):
            return str(s["sessionId"])
    for s in sessions:
        if isinstance(s, dict) and str(s.get("key", "")).startswith("agent:orchestrator:") and s.get(
            "sessionId"
        ):
            return str(s["sessionId"])
    raise RuntimeError("cannot resolve orchestrator session id")


def run_spawn(
    session_id: str, target_agent: str, task_prompt: str, timeout_sec: int
) -> Dict[str, Any]:
    message = f"/subagents spawn {target_agent} {task_prompt}".strip()
    cmd = [
        "openclaw",
        "agent",
        "--agent",
        "orchestrator",
        "--session-id",
        session_id,
        "--message",
        message,
        "--json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout_sec)
    out = {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "command": cmd,
        "stdout": clip(proc.stdout, 4000),
        "stderr": clip(proc.stderr, 1200),
        "rawMessage": message,
    }
    if proc.returncode == 0:
        try:
            out["parsed"] = parse_json_loose(proc.stdout or "")
        except Exception:
            pass
    return out


def cmd_dispatch(args: argparse.Namespace) -> int:
    if args.actor != "orchestrator":
        print(json.dumps({"ok": False, "error": "dispatch is restricted to actor=orchestrator"}))
        return 1

    data = load_snapshot(args.root)
    task = data.get("tasks", {}).get(args.task_id)
    if not isinstance(task, dict):
        print(json.dumps({"ok": False, "error": f"task not found: {args.task_id}"}))
        return 1

    pre_text = "\n".join(
        [
            f"[CLAIM] {args.task_id} | 状态={status_zh(str(task.get('status') or '-'))} | 指派={args.agent}",
            f"标题: {clip(task.get('title') or '未命名任务')}",
            "派发: 已请求后台执行 (subagent)",
        ]
    )
    pre_send = send_group_message(args.group_id, args.account_id, pre_text, args.mode)

    spawn_task = clip(args.task or f"{args.task_id}: {task.get('title') or 'untitled'}", 500)
    spawn_result: Dict[str, Any]
    if args.mode == "dry-run":
        spawn_result = {
            "ok": True,
            "dryRun": True,
            "note": "spawn skipped in dry-run mode",
            "message": f"/subagents spawn {args.agent} {spawn_task}",
        }
    else:
        try:
            session_id = resolve_orchestrator_session(args.group_id, args.session_id)
            spawn_result = run_spawn(session_id, args.agent, spawn_task, args.timeout_sec)
        except Exception as err:
            spawn_result = {"ok": False, "error": str(err)}

    post_status = "已提交" if spawn_result.get("ok") else "失败"
    detail = spawn_result.get("error") or spawn_result.get("stderr") or spawn_result.get("stdout") or post_status
    post_text = "\n".join(
        [
            f"[CLAIM] {args.task_id} | 状态={status_zh(str(task.get('status') or '-'))} | 指派={args.agent}",
            f"派发结果: {post_status}",
            f"详情: {clip(detail, 180)}",
        ]
    )
    post_send = send_group_message(args.group_id, args.account_id, post_text, args.mode)

    ok = bool(pre_send.get("ok")) and bool(post_send.get("ok")) and bool(spawn_result.get("ok"))
    print(
        json.dumps(
            {
                "ok": ok,
                "taskId": args.task_id,
                "agent": args.agent,
                "pre": pre_send,
                "spawn": spawn_result,
                "post": post_send,
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
    p_pub.add_argument("--include-escalate-blocked", action="store_true")
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

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
