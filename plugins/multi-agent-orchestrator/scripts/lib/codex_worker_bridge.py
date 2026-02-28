#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import tempfile
from typing import Any, Dict, List

TASK_CONTEXT_STATE_FILE = "task-context-map.json"
DEFAULT_CODER_WORKSPACE = os.path.expanduser("~/.openclaw/agents/coder/workspace")


def clip(text: str, limit: int = 300) -> str:
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
    raise ValueError("no json object found")


def state_path(root: str) -> str:
    return os.path.join(root, "state", TASK_CONTEXT_STATE_FILE)


def lookup_project_path(root: str, task_id: str) -> str:
    path = state_path(root)
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except Exception:
        return ""
    tasks = obj.get("tasks") if isinstance(obj, dict) else {}
    if not isinstance(tasks, dict):
        return ""
    entry = tasks.get(task_id)
    if not isinstance(entry, dict):
        return ""
    p = str(entry.get("projectPath") or "").strip()
    p = os.path.abspath(os.path.expanduser(p)) if p else ""
    if p and os.path.isdir(p):
        return p
    return ""


def resolve_workspace(args: argparse.Namespace) -> str:
    if args.workspace:
        p = os.path.abspath(os.path.expanduser(args.workspace))
        if os.path.isdir(p):
            return p
    mapped = lookup_project_path(args.root, args.task_id)
    if mapped:
        return mapped
    env_cd = os.environ.get("CODEX_WORKER_CD", "").strip()
    if env_cd:
        p = os.path.abspath(os.path.expanduser(env_cd))
        if os.path.isdir(p):
            return p
    if os.path.isdir(DEFAULT_CODER_WORKSPACE):
        return DEFAULT_CODER_WORKSPACE
    return os.getcwd()


def as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def normalize_result(task_id: str, agent: str, raw: Dict[str, Any], fallback_text: str = "") -> Dict[str, Any]:
    status = str(raw.get("status") or raw.get("taskStatus") or "progress").strip().lower()
    if status not in {"done", "blocked", "progress"}:
        status = "progress"

    summary = str(raw.get("summary") or raw.get("message") or fallback_text or "已执行").strip()
    if not summary:
        summary = "已执行"

    evidence = as_list(raw.get("evidence"))
    if not evidence:
        for k in ("result", "output", "message"):
            v = str(raw.get(k) or "").strip()
            if v:
                evidence.append(clip(v, 180))
                break

    changes = raw.get("changes")
    if not isinstance(changes, list):
        changes = []

    risks = as_list(raw.get("risks"))
    next_actions = as_list(raw.get("nextActions"))

    return {
        "taskId": task_id,
        "agent": agent,
        "status": status,
        "summary": clip(summary, 500),
        "changes": changes,
        "evidence": evidence,
        "risks": risks,
        "nextActions": next_actions,
    }


def build_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "taskId": {"type": "string"},
            "agent": {"type": "string"},
            "status": {"type": "string", "enum": ["done", "blocked", "progress"]},
            "summary": {"type": "string"},
            "changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "path": {"type": "string"},
                        "summary": {"type": "string"},
                    },
                },
            },
            "evidence": {"type": "array", "items": {"type": "string"}},
            "risks": {"type": "array", "items": {"type": "string"}},
            "nextActions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["status", "summary"],
    }


def blocked(task_id: str, agent: str, reason: str, evidence: List[str]) -> Dict[str, Any]:
    return {
        "taskId": task_id,
        "agent": agent,
        "status": "blocked",
        "summary": clip(reason, 500),
        "changes": [],
        "evidence": [clip(x, 220) for x in evidence if x],
        "risks": [],
        "nextActions": ["请人工复核并补充执行上下文"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--timeout-sec", type=int, default=120)
    parser.add_argument("--workspace", default="")
    args = parser.parse_args()

    fake = os.environ.get("CODEX_WORKER_FAKE_OUTPUT", "").strip()
    if fake:
        try:
            obj = parse_json_loose(fake)
            if not isinstance(obj, dict):
                obj = {"status": "progress", "summary": str(obj)}
            result = normalize_result(args.task_id, args.agent, obj, fallback_text=fake)
        except Exception as err:
            result = blocked(args.task_id, args.agent, f"fake output parse failed: {err}", [fake])
        print(json.dumps(result, ensure_ascii=False))
        return 0

    workspace = resolve_workspace(args)

    with tempfile.TemporaryDirectory(prefix="codex-worker-") as tmp:
        schema_path = os.path.join(tmp, "schema.json")
        out_path = os.path.join(tmp, "output.json")
        with open(schema_path, "w", encoding="utf-8") as f:
            json.dump(build_schema(), f, ensure_ascii=False, indent=2)
            f.write("\n")

        cmd = [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--cd",
            workspace,
            "--output-schema",
            schema_path,
            "--output-last-message",
            out_path,
            "-",
        ]

        try:
            proc = subprocess.run(
                cmd,
                input=args.task,
                text=True,
                capture_output=True,
                check=False,
                timeout=max(30, int(args.timeout_sec) + 20),
            )
        except Exception as err:
            result = blocked(args.task_id, args.agent, f"codex exec failed: {err}", [])
            print(json.dumps(result, ensure_ascii=False))
            return 0

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()

        raw_obj: Dict[str, Any] = {}
        if os.path.exists(out_path):
            try:
                with open(out_path, "r", encoding="utf-8") as f:
                    out_text = f.read().strip()
                parsed = parse_json_loose(out_text)
                if isinstance(parsed, dict):
                    raw_obj = parsed
            except Exception:
                raw_obj = {}

        if not raw_obj and stdout:
            try:
                parsed = parse_json_loose(stdout)
                if isinstance(parsed, dict):
                    raw_obj = parsed
            except Exception:
                raw_obj = {}

        if proc.returncode != 0:
            result = blocked(
                args.task_id,
                args.agent,
                f"codex exec exit={proc.returncode}",
                [clip(stderr, 220), clip(stdout, 220)],
            )
            print(json.dumps(result, ensure_ascii=False))
            return 0

        if not raw_obj:
            result = blocked(args.task_id, args.agent, "codex output is empty or invalid", [clip(stdout, 220), clip(stderr, 220)])
            print(json.dumps(result, ensure_ascii=False))
            return 0

        result = normalize_result(args.task_id, args.agent, raw_obj, fallback_text=stdout)
        print(json.dumps(result, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
