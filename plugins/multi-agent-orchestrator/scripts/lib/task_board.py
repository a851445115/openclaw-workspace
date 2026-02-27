#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone

MESSAGE_TYPES = {
    "TASK": "[TASK]",
    "CLAIM": "[CLAIM]",
    "DONE": "[DONE]",
    "BLOCKED": "[BLOCKED]",
    "REVIEW": "[REVIEW]",
    "DIAG": "[DIAG]",
}

ALLOWED_TRANSITIONS = {
    "pending": {"claimed", "blocked"},
    "claimed": {"in_progress", "done", "blocked"},
    "in_progress": {"review", "done", "blocked", "failed"},
    "review": {"done", "in_progress", "blocked"},
    "blocked": {"in_progress", "claimed"},
    "failed": {"in_progress"},
    "done": set(),
}

LOCK_FILENAME = "task-board.lock"
LOCK_TTL_SEC = 45
LOCK_WAIT_SEC = 8
LOCK_POLL_SEC = 0.12


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def now_ts() -> int:
    return int(time.time())


def ensure_state(root):
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


def lock_path(root: str) -> str:
    return os.path.join(root, "state", "locks", LOCK_FILENAME)


def read_lock_meta(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def is_lock_stale(meta: dict, now: int) -> bool:
    if not isinstance(meta, dict):
        return True
    expires_at = int(meta.get("expiresAtTs", 0))
    return expires_at and expires_at <= now


def acquire_board_lock(root: str, owner: str):
    path = lock_path(root)
    deadline = now_ts() + LOCK_WAIT_SEC
    token = str(uuid.uuid4())

    while now_ts() <= deadline:
        current_ts = now_ts()
        payload = {
            "token": token,
            "owner": owner,
            "pid": os.getpid(),
            "createdAt": now_iso(),
            "expiresAtTs": current_ts + LOCK_TTL_SEC,
        }
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8"))
            finally:
                os.close(fd)
            return {"path": path, "token": token}
        except FileExistsError:
            meta = read_lock_meta(path)
            if is_lock_stale(meta, current_ts):
                try:
                    os.remove(path)
                    continue
                except FileNotFoundError:
                    continue
                except OSError:
                    pass
            time.sleep(LOCK_POLL_SEC)
    raise TimeoutError(f"lock busy: {path}")


def release_board_lock(lock):
    path = lock.get("path")
    token = lock.get("token")
    if not path:
        return
    meta = read_lock_meta(path)
    if not isinstance(meta, dict):
        return
    if meta.get("token") != token:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def load_snapshot(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "tasks" not in data or not isinstance(data["tasks"], dict):
        raise ValueError("invalid snapshot format: tasks must be object")
    return data


def save_snapshot(path, data):
    data.setdefault("meta", {})
    data["meta"]["updatedAt"] = now_iso()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=True, indent=2)
        f.write("\n")


def append_event(jsonl_path, event):
    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=True) + "\n")


def make_event(task_id, event_type, actor, message_type, payload):
    return {
        "eventId": str(uuid.uuid4()),
        "taskId": task_id,
        "type": event_type,
        "messageType": message_type,
        "actor": actor,
        "at": now_iso(),
        "payload": payload,
    }


def parse_override(text):
    m = re.match(r"^\s*@([A-Za-z0-9_.-]+)\s+(.*)$", text)
    if not m:
        return None, text.strip()
    return m.group(1), m.group(2).strip()


def parse_route(text):
    override, body = parse_override(text)

    m = re.match(
        r"^create\s+task(?:\s+([A-Za-z0-9_-]+))?\s*:?\s*(.+)$", body, flags=re.IGNORECASE
    )
    if m:
        task_id = m.group(1)
        title = m.group(2).strip()
        return {"intent": "create_task", "overrideAgent": override, "taskId": task_id, "title": title}

    m = re.match(r"^claim\s+task\s+([A-Za-z0-9_-]+)$", body, flags=re.IGNORECASE)
    if m:
        return {"intent": "claim_task", "overrideAgent": override, "taskId": m.group(1)}

    m = re.match(r"^mark\s+done\s+([A-Za-z0-9_-]+)(?:\s*:?\s*(.*))?$", body, flags=re.IGNORECASE)
    if m:
        return {
            "intent": "mark_done",
            "overrideAgent": override,
            "taskId": m.group(1),
            "result": (m.group(2) or "").strip(),
        }

    m = re.match(r"^block\s+task\s+([A-Za-z0-9_-]+)(?:\s*:?\s*(.*))?$", body, flags=re.IGNORECASE)
    if m:
        return {
            "intent": "block_task",
            "overrideAgent": override,
            "taskId": m.group(1),
            "reason": (m.group(2) or "").strip(),
        }

    m = re.match(r"^escalate\s+task\s+([A-Za-z0-9_-]+)(?:\s*:?\s*(.*))?$", body, flags=re.IGNORECASE)
    if m:
        return {
            "intent": "escalate_task",
            "overrideAgent": override,
            "taskId": m.group(1),
            "reason": (m.group(2) or "").strip(),
        }

    m = re.match(r"^status(?:\s+([A-Za-z0-9_-]+))?$", body, flags=re.IGNORECASE)
    if m:
        return {"intent": "status", "overrideAgent": override, "taskId": m.group(1)}

    m = re.match(r"^synthesize(?:\s+([A-Za-z0-9_-]+))?$", body, flags=re.IGNORECASE)
    if m:
        return {"intent": "synthesize", "overrideAgent": override, "taskId": m.group(1)}

    return {"intent": "unknown", "overrideAgent": override, "raw": body.strip()}


def next_task_id(tasks):
    nums = []
    for tid in tasks.keys():
        m = re.match(r"^T-(\d+)$", tid)
        if m:
            nums.append(int(m.group(1)))
    n = (max(nums) + 1) if nums else 1
    return f"T-{n:03d}"


def validate_transition(current: str, target: str) -> bool:
    if current == target:
        return True
    return target in ALLOWED_TRANSITIONS.get(current, set())


def cmd_init(args):
    jsonl, snapshot = ensure_state(args.root)
    print(json.dumps({"ok": True, "jsonl": jsonl, "snapshot": snapshot}))
    return 0


def cmd_route(args):
    route = parse_route(args.text)
    route["actor"] = args.actor
    print(json.dumps(route, ensure_ascii=True))
    return 0


def cmd_apply(args):
    jsonl, snapshot = ensure_state(args.root)
    lock = None
    route = parse_route(args.text)
    actor = args.actor
    assignee = route.get("overrideAgent") or actor

    intent = route["intent"]
    read_only = intent in {"status", "synthesize", "unknown"}

    try:
        if not read_only:
            lock = acquire_board_lock(args.root, owner=f"apply:{actor}:{intent}")

        data = load_snapshot(snapshot)
        tasks = data["tasks"]

        if intent == "create_task":
            task_id = route.get("taskId") or next_task_id(tasks)
            if task_id in tasks:
                print(json.dumps({"ok": False, "error": f"task exists: {task_id}"}))
                return 1
            title = route.get("title") or "untitled"
            task = {
                "taskId": task_id,
                "title": title,
                "status": "pending",
                "owner": None,
                "assigneeHint": assignee,
                "createdBy": actor,
                "createdAt": now_iso(),
                "updatedAt": now_iso(),
                "blockedReason": None,
                "result": None,
                "review": None,
                "relatedTo": None,
                "projectId": None,
                "history": [],
            }
            event = make_event(
                task_id,
                "task_created",
                actor,
                MESSAGE_TYPES["TASK"],
                {"title": title, "assigneeHint": assignee},
            )
            task["history"].append(event["eventId"])
            tasks[task_id] = task
            append_event(jsonl, event)
            save_snapshot(snapshot, data)
            print(json.dumps({"ok": True, "intent": intent, "taskId": task_id, "assigneeHint": assignee}))
            return 0

        if intent in {"claim_task", "mark_done", "block_task", "escalate_task", "status", "synthesize"}:
            task_id = route.get("taskId")
        else:
            task_id = None

        if intent == "status":
            if task_id:
                task = tasks.get(task_id)
                if not task:
                    print(json.dumps({"ok": False, "error": f"task not found: {task_id}"}))
                    return 1
                print(json.dumps({"ok": True, "task": task}, ensure_ascii=True))
                return 0
            by_status = {}
            for t in tasks.values():
                by_status[t["status"]] = by_status.get(t["status"], 0) + 1
            print(json.dumps({"ok": True, "counts": by_status, "total": len(tasks)}))
            return 0

        if intent == "synthesize":
            selected = []
            for t in tasks.values():
                if task_id and t["taskId"] != task_id:
                    continue
                if t["status"] in {"done", "review", "blocked"} or t.get("relatedTo"):
                    selected.append(t)
            lines = ["SYNTHESIS REPORT"]
            for t in sorted(selected, key=lambda x: x["taskId"]):
                detail = t.get("result") or t.get("review") or t.get("blockedReason") or "(no detail)"
                rel = f" relatedTo={t.get('relatedTo')}" if t.get("relatedTo") else ""
                lines.append(f"- {t['taskId']} [{t['status']}] owner={t.get('owner') or '-'}{rel} :: {detail}")
            if len(lines) == 1:
                lines.append("- no completed/review/blocked tasks found")
            print(json.dumps({"ok": True, "intent": intent, "report": "\n".join(lines)}))
            return 0

        if not task_id or task_id not in tasks:
            print(json.dumps({"ok": False, "error": f"task not found: {task_id}"}))
            return 1

        task = tasks[task_id]

        if intent == "claim_task":
            prev = task["status"]
            target = "claimed" if prev == "pending" else "in_progress"
            if not validate_transition(prev, target):
                print(json.dumps({"ok": False, "error": f"invalid transition: {prev} -> {target}"}))
                return 1
            task["status"] = target
            task["owner"] = assignee
            task["updatedAt"] = now_iso()
            event = make_event(
                task_id,
                "task_claimed",
                actor,
                MESSAGE_TYPES["CLAIM"],
                {"from": prev, "to": task["status"], "owner": assignee},
            )
            task["history"].append(event["eventId"])
            append_event(jsonl, event)
            save_snapshot(snapshot, data)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "intent": intent,
                        "taskId": task_id,
                        "owner": assignee,
                        "status": task["status"],
                    }
                )
            )
            return 0

        if intent == "mark_done":
            prev = task["status"]
            if not validate_transition(prev, "done"):
                print(json.dumps({"ok": False, "error": f"invalid transition: {prev} -> done"}))
                return 1
            task["status"] = "done"
            task["owner"] = task.get("owner") or assignee
            task["result"] = route.get("result") or task.get("result") or "done"
            task["updatedAt"] = now_iso()
            event = make_event(
                task_id,
                "task_done",
                actor,
                MESSAGE_TYPES["DONE"],
                {"from": prev, "to": "done", "result": task["result"]},
            )
            task["history"].append(event["eventId"])
            append_event(jsonl, event)
            save_snapshot(snapshot, data)
            print(json.dumps({"ok": True, "intent": intent, "taskId": task_id, "status": "done"}))
            return 0

        def apply_block(tid, reason, message_type):
            t = tasks[tid]
            prev_status = t["status"]
            if not validate_transition(prev_status, "blocked"):
                return None, {"ok": False, "error": f"invalid transition: {prev_status} -> blocked"}
            t["status"] = "blocked"
            t["blockedReason"] = reason
            t["updatedAt"] = now_iso()
            ev = make_event(
                tid,
                "task_blocked",
                actor,
                message_type,
                {"from": prev_status, "to": "blocked", "reason": reason},
            )
            t["history"].append(ev["eventId"])
            append_event(jsonl, ev)
            return ev, None

        if intent == "block_task":
            reason = route.get("reason") or "unspecified blocker"
            _, err = apply_block(task_id, reason, MESSAGE_TYPES["BLOCKED"])
            if err:
                print(json.dumps(err))
                return 1
            save_snapshot(snapshot, data)
            print(json.dumps({"ok": True, "intent": intent, "taskId": task_id, "status": "blocked"}))
            return 0

        if intent == "escalate_task":
            reason = route.get("reason") or "unspecified escalation"
            _, err = apply_block(task_id, reason, MESSAGE_TYPES["BLOCKED"])
            if err:
                print(json.dumps(err))
                return 1

            diag_task_id = next_task_id(tasks)
            diag_title = f"DIAG {task_id}: {reason}" if reason else f"DIAG {task_id}"
            diag = {
                "taskId": diag_task_id,
                "title": diag_title,
                "status": "pending",
                "owner": None,
                "assigneeHint": "debugger",
                "createdBy": actor,
                "createdAt": now_iso(),
                "updatedAt": now_iso(),
                "blockedReason": None,
                "result": None,
                "review": None,
                "relatedTo": task_id,
                "projectId": task.get("projectId"),
                "history": [],
            }
            ev = make_event(
                diag_task_id,
                "diag_task_created",
                actor,
                MESSAGE_TYPES["DIAG"],
                {"title": diag_title, "assigneeHint": "debugger", "relatedTo": task_id},
            )
            diag["history"].append(ev["eventId"])
            tasks[diag_task_id] = diag
            append_event(jsonl, ev)

            save_snapshot(snapshot, data)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "intent": intent,
                        "taskId": task_id,
                        "status": "blocked",
                        "diagTaskId": diag_task_id,
                        "diagAssigneeHint": "debugger",
                    }
                )
            )
            return 0

        print(json.dumps({"ok": False, "error": f"unsupported intent: {intent}"}))
        return 1
    finally:
        if lock:
            release_board_lock(lock)


def cmd_transition(args):
    if args.to_status not in ALLOWED_TRANSITIONS.get(args.from_status, set()):
        print(f"invalid transition: {args.from_status} -> {args.to_status}", file=sys.stderr)
        return 1
    print(f"valid transition: {args.from_status} -> {args.to_status}")
    return 0


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("--root", required=True)
    p_init.set_defaults(func=cmd_init)

    p_route = sub.add_parser("route")
    p_route.add_argument("--actor", required=True)
    p_route.add_argument("--text", required=True)
    p_route.set_defaults(func=cmd_route)

    p_apply = sub.add_parser("apply")
    p_apply.add_argument("--root", required=True)
    p_apply.add_argument("--actor", required=True)
    p_apply.add_argument("--text", required=True)
    p_apply.set_defaults(func=cmd_apply)

    p_transition = sub.add_parser("transition")
    p_transition.add_argument("--from", dest="from_status", required=True)
    p_transition.add_argument("--to", dest="to_status", required=True)
    p_transition.set_defaults(func=cmd_transition)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
