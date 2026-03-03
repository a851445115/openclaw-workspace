#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import time
from typing import Any, Dict, List

TASK_CONTEXT_STATE_FILE = "task-context-map.json"
DEFAULT_CLAUDE_MODEL = "claude-opus-4-5-20251101"


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

    env_cd = os.environ.get("CLAUDE_WORKER_CD", "").strip()
    if env_cd:
        p = os.path.abspath(os.path.expanduser(env_cd))
        if os.path.isdir(p):
            return p

    role_workspace = os.path.abspath(os.path.expanduser(f"~/.openclaw/agents/{args.agent}/workspace"))
    if os.path.isdir(role_workspace):
        return role_workspace

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


def safe_int(value: Any, default: int = -1) -> int:
    try:
        out = int(value)
    except Exception:
        return default
    return out if out >= 0 else default


def normalize_timeout_sec(value: Any, default: int = 0) -> int:
    parsed = safe_int(value, default)
    return max(0, parsed)


def extract_usage_pair(usage: Dict[str, Any]) -> int:
    prompt = safe_int(usage.get("prompt_tokens"), -1)
    completion = safe_int(usage.get("completion_tokens"), -1)
    if prompt >= 0 or completion >= 0:
        return max(0, prompt) + max(0, completion)

    input_tokens = safe_int(usage.get("input_tokens"), -1)
    output_tokens = safe_int(usage.get("output_tokens"), -1)
    if input_tokens >= 0 or output_tokens >= 0:
        return max(0, input_tokens) + max(0, output_tokens)

    return -1


def extract_token_usage(raw: Dict[str, Any]) -> int:
    if not isinstance(raw, dict):
        return 0

    buckets = [raw]
    metrics = raw.get("metrics")
    if isinstance(metrics, dict):
        buckets.append(metrics)
    usage = raw.get("usage")
    if isinstance(usage, dict):
        buckets.append(usage)

    for bucket in buckets:
        for key in ("total_tokens", "totalTokens"):
            parsed = safe_int(bucket.get(key))
            if parsed >= 0:
                return parsed

    for bucket in buckets:
        for key in ("tokenUsage", "token_usage", "tokens"):
            parsed = safe_int(bucket.get(key))
            if parsed >= 0:
                return parsed

    for bucket in buckets:
        paired_usage = extract_usage_pair(bucket)
        if paired_usage >= 0:
            return paired_usage

    return 0


def attach_metrics(result: Dict[str, Any], raw: Dict[str, Any], start_ms: int) -> Dict[str, Any]:
    elapsed_ms = max(0, int(time.time() * 1000) - int(start_ms))
    token_usage = extract_token_usage(raw)
    if isinstance(raw, dict):
        metrics = raw.get("metrics")
        if isinstance(metrics, dict):
            metric_elapsed = safe_int(metrics.get("elapsedMs"))
            if metric_elapsed >= 0:
                elapsed_ms = metric_elapsed
    result["metrics"] = {
        "elapsedMs": elapsed_ms,
        "tokenUsage": max(0, token_usage),
    }
    return result


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
            "metrics": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "elapsedMs": {"type": "integer"},
                    "tokenUsage": {"type": "integer"},
                },
            },
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


def looks_like_report(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    if str(raw.get("status") or raw.get("taskStatus") or "").strip():
        return True
    if str(raw.get("summary") or raw.get("message") or "").strip():
        return True
    for key in ("changes", "evidence", "risks", "nextActions"):
        if key in raw:
            return True
    return False


def parse_report_candidate(candidate: Any) -> Dict[str, Any]:
    if isinstance(candidate, dict) and looks_like_report(candidate):
        return candidate
    if isinstance(candidate, str):
        try:
            parsed = parse_json_loose(candidate)
        except Exception:
            return {}
        if isinstance(parsed, dict) and looks_like_report(parsed):
            return parsed
    return {}


def extract_report_dict(raw_obj: Any) -> Dict[str, Any]:
    candidates: List[Any] = []
    if isinstance(raw_obj, dict):
        for key in ("structured_output", "structuredOutput"):
            candidates.append(raw_obj.get(key))

        for candidate in candidates:
            report = parse_report_candidate(candidate)
            if report:
                return report

    if isinstance(raw_obj, dict) and looks_like_report(raw_obj):
        return raw_obj

    if isinstance(raw_obj, dict):
        for key in ("result", "output", "response", "data", "message"):
            candidates.append(raw_obj.get(key))
        content = raw_obj.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    candidates.extend([item.get("text"), item.get("content"), item.get("message"), item.get("input")])
                else:
                    candidates.append(item)

    for candidate in candidates:
        report = parse_report_candidate(candidate)
        if report:
            return report

    if isinstance(raw_obj, dict):
        return raw_obj
    return {}


def load_fake_output(raw_value: str) -> str:
    hint = (raw_value or "").strip()
    if not hint:
        return ""
    candidate = os.path.abspath(os.path.expanduser(hint))
    if os.path.isfile(candidate):
        with open(candidate, "r", encoding="utf-8") as f:
            return f.read().strip()
    return hint


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--timeout-sec", type=int, default=0)
    parser.add_argument("--workspace", default="")
    args = parser.parse_args()
    start_ms = int(time.time() * 1000)

    fake = os.environ.get("CLAUDE_WORKER_FAKE_OUTPUT", "").strip()
    if fake:
        fake_text = load_fake_output(fake)
        obj: Dict[str, Any] = {}
        try:
            parsed = parse_json_loose(fake_text)
            if isinstance(parsed, dict):
                obj = parsed
            else:
                obj = {"status": "progress", "summary": str(parsed)}
            report_obj = extract_report_dict(obj)
            result = normalize_result(args.task_id, args.agent, report_obj, fallback_text=fake_text)
        except Exception as err:
            result = blocked(args.task_id, args.agent, f"fake output parse failed: {err}", [fake_text])
        print(json.dumps(attach_metrics(result, obj, start_ms), ensure_ascii=False))
        return 0

    workspace = resolve_workspace(args)
    schema_text = json.dumps(build_schema(), ensure_ascii=True, separators=(",", ":"))
    cmd = [
        "claude",
        "--print",
        "--model",
        DEFAULT_CLAUDE_MODEL,
        "--output-format",
        "json",
        "--json-schema",
        schema_text,
        "--dangerously-skip-permissions",
        "--permission-mode",
        "bypassPermissions",
        "--add-dir",
        workspace,
        "-p",
        args.task,
    ]

    try:
        timeout_sec = normalize_timeout_sec(args.timeout_sec, default=0)
        run_timeout = None if timeout_sec <= 0 else max(30, timeout_sec + 20)
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            check=False,
            cwd=workspace,
            timeout=run_timeout,
        )
    except Exception as err:
        result = blocked(args.task_id, args.agent, f"claude print failed: {err}", [])
        print(json.dumps(attach_metrics(result, {}, start_ms), ensure_ascii=False))
        return 0

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    raw_obj: Dict[str, Any] = {}
    if stdout:
        try:
            parsed = parse_json_loose(stdout)
            if isinstance(parsed, dict):
                raw_obj = parsed
            else:
                raw_obj = {"output": parsed}
        except Exception:
            raw_obj = {}

    if proc.returncode != 0:
        result = blocked(
            args.task_id,
            args.agent,
            f"claude print exit={proc.returncode}",
            [clip(stderr, 220), clip(stdout, 220)],
        )
        print(json.dumps(attach_metrics(result, raw_obj, start_ms), ensure_ascii=False))
        return 0

    if not raw_obj:
        result = blocked(args.task_id, args.agent, "claude output is empty or invalid", [clip(stdout, 220), clip(stderr, 220)])
        print(json.dumps(attach_metrics(result, {}, start_ms), ensure_ascii=False))
        return 0

    report_obj = extract_report_dict(raw_obj)
    result = normalize_result(args.task_id, args.agent, report_obj, fallback_text=stdout)
    print(json.dumps(attach_metrics(result, raw_obj, start_ms), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
