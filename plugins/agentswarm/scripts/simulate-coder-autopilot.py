#!/usr/bin/env python3
"""Local simulation for coder auto-assignment detection and outbound report format."""

import argparse
import json
import os
import re
from typing import Dict, Tuple

ALLOWLIST_GROUP_ID = "oc_041146c92a9ccb403a7f4f48fb59701d"
MILESTONE_PREFIXES = ("[DONE]", "[BLOCKED]", "[CLAIM]")
TASK_ID_RE = re.compile(r"\b(T-\d+)\b", flags=re.IGNORECASE)


def load_mentions(path: str) -> Dict[str, Dict[str, str]]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}

    out: Dict[str, Dict[str, str]] = {}
    if isinstance(raw, dict):
        for key in ("byRole", "byAccountId"):
            block = raw.get(key)
            if isinstance(block, dict):
                for role, info in block.items():
                    if isinstance(role, str) and isinstance(info, dict):
                        out.setdefault(role, info)
    return out


def mention_tag(role: str, mentions: Dict[str, Dict[str, str]]) -> str:
    info = mentions.get(role, {})
    open_id = str(info.get("open_id") or "").strip()
    name = str(info.get("name") or role).strip() or role
    safe_name = name.replace("<", "").replace(">", "")
    if not open_id:
        return f"@{role}"
    return f'<at user_id="{open_id}">{safe_name}</at>'


def has_task_assignment(text: str, self_role: str, self_open_id: str) -> bool:
    if "[TASK]" not in text:
        return False
    if not TASK_ID_RE.search(text):
        return False

    owner_re = re.compile(rf"负责人\s*=\s*{re.escape(self_role)}\b", flags=re.IGNORECASE)
    owner_hit = bool(owner_re.search(text))

    mention_name_re = re.compile(rf"<at\b[^>]*>\s*{re.escape(self_role)}\s*</at>", flags=re.IGNORECASE)
    mention_name_hit = bool(mention_name_re.search(text))

    mention_openid_hit = False
    if self_open_id:
        mention_openid_re = re.compile(
            rf"<at\b[^>]*\buser_id\s*=\s*[\"']{re.escape(self_open_id)}[\"'][^>]*>",
            flags=re.IGNORECASE,
        )
        mention_openid_hit = bool(mention_openid_re.search(text))

    return owner_hit or mention_name_hit or mention_openid_hit


def extract_task_title(text: str, task_id: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s.lower().startswith("任务:"):
            return s.split(":", 1)[1].strip()[:80]

    task_line = ""
    for line in text.splitlines():
        if "[TASK]" in line and task_id.lower() in line.lower():
            task_line = line.strip()
            break

    if task_line:
        right = task_line.split(task_id, 1)[-1]
        right = right.replace("|", " ")
        right = re.sub(r"负责人\s*=\s*\S+", "", right, flags=re.IGNORECASE)
        right = " ".join(right.split())
        if right:
            return right[:80]

    return "未提取到标题"


def classify(text: str, actor: str, self_role: str, group_id: str, self_open_id: str) -> Tuple[bool, str, str, str]:
    normalized = (text or "").strip()
    actor_norm = (actor or "").strip().lower()

    if group_id != ALLOWLIST_GROUP_ID:
        return False, "group_not_allowlisted", "", ""
    if actor_norm == self_role.lower():
        return False, "self_message", "", ""
    if any(prefix in normalized for prefix in MILESTONE_PREFIXES):
        return False, "orchestrator_milestone", "", ""
    if not has_task_assignment(normalized, self_role, self_open_id):
        return False, "not_a_coder_assignment", "", ""

    m = TASK_ID_RE.search(normalized)
    if not m:
        return False, "missing_task_id", "", ""
    task_id = m.group(1).upper()
    title = extract_task_title(normalized, task_id)
    return True, "accepted", task_id, title


def build_report(task_id: str, title: str, orchestrator_tag: str) -> str:
    return "\n".join(
        [
            f"{orchestrator_tag} {task_id} 已完成",
            f"标题: {title}",
            "证据:",
            "- 文件: /abs/path/to/changed/file.py",
            "- 命令: python3 -m pytest tests/test_task.py",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulate coder auto-task trigger and outbound report")
    parser.add_argument("--message", required=True, help="Inbound Feishu message text")
    parser.add_argument("--group-id", default=ALLOWLIST_GROUP_ID)
    parser.add_argument("--actor", default="orchestrator")
    parser.add_argument("--self-role", default="coder")
    parser.add_argument("--config", default=os.path.join("config", "feishu-bot-openids.json"))
    args = parser.parse_args()

    mentions = load_mentions(args.config)
    self_open_id = str(mentions.get(args.self_role, {}).get("open_id") or "").strip()
    should_act, reason, task_id, title = classify(
        text=args.message,
        actor=args.actor,
        self_role=args.self_role,
        group_id=args.group_id,
        self_open_id=self_open_id,
    )

    out = {
        "ok": True,
        "shouldAct": should_act,
        "reason": reason,
        "groupId": args.group_id,
        "actor": args.actor,
        "taskId": task_id,
        "title": title,
    }

    if should_act:
        orchestrator_tag = mention_tag("orchestrator", mentions)
        out["reportPreview"] = build_report(task_id, title, orchestrator_tag)

    print(json.dumps(out, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
