#!/usr/bin/env python3
import argparse
import json
import logging
import math
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    import fcntl
except Exception:  # pragma: no cover
    fcntl = None

SCRIPT_LIB_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_LIB_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_LIB_DIR)
import priority_engine
import recovery_loop
import budget_policy
import governance
import evidence_normalizer
import task_decomposer
import ops_metrics
import strategy_library
import knowledge_adapter
import session_registry
import context_pack
import collaboration_hub

LOGGER = logging.getLogger(__name__)

DEFAULT_GROUP_ID = "oc_041146c92a9ccb403a7f4f48fb59701d"
DEFAULT_ACCOUNT_ID = "orchestrator"
DEFAULT_ALLOWED_BROADCASTERS = {"orchestrator"}
OPTIONAL_BROADCASTER = "broadcaster"
CLARIFY_ROLES = {
    "coder",
    "invest-analyst",
    "debugger",
    "broadcaster",
    "knowledge-curator",
    "paper-ingestor",
    "paper-summarizer",
}
BOT_ROLES = set(CLARIFY_ROLES) | {"orchestrator"}
MILESTONE_PREFIXES = ("[TASK]", "[CLAIM]", "[DONE]", "[BLOCKED]", "[DIAG]", "[REVIEW]")
DONE_HINTS = ("[DONE]", " done", "completed", "finish", "完成", "已完成", "verified")
BLOCKED_HINTS = ("[BLOCKED]", "blocked", "failed", "error", "exception", "失败", "未通过", "阻塞", "卡住", "无法")
FAILED_SIGNAL_PATTERNS = (
    re.compile(r"\b[1-9]\d*\s+(?:failed|failures|errors?|exceptions?)\b", flags=re.IGNORECASE),
    re.compile(r"\b(?:tests?|test suites?)\s+failed\b", flags=re.IGNORECASE),
    re.compile(r"\bFAILED\s+tests?/[^\s;]+", flags=re.IGNORECASE),
    re.compile(r"\bTraceback\s+\(most recent call last\)", flags=re.IGNORECASE),
    re.compile(r"(?:测试失败|验证失败|未通过|不通过)"),
)
ZERO_FAILURE_COUNTER_RE = re.compile(
    r"\b0\s+(?:(?:tests?|test suites?)\s+failed|failed|failures|errors?|exceptions?)\b",
    flags=re.IGNORECASE,
)
EVIDENCE_HINTS = ("/", ".py", ".md", "http", "截图", "日志", "log", "输出", "result", "测试")
STAGE_ONLY_HINTS = ("接下来", "下一步", "准备", "我先", "随后", "稍后", "计划", "will", "next", "going to", "plan to")
BOT_OPENID_CONFIG_CANDIDATES = (
    os.path.join("config", "feishu-bot-openids.json"),
    os.path.join("state", "feishu-bot-openids.json"),
)
VISIBILITY_MODES = ("milestone_only", "handoff_visible", "full_visible")
DEFAULT_VISIBILITY_MODE = "handoff_visible"
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
        "knowledge-curator": {
            "requireAny": ["知识", "knowledge", "整理", "归档", "标签", "tag"],
        },
        "paper-ingestor": {
            "requireAny": ["论文", "paper", "ingest", "采集", "下载", "来源"],
        },
        "paper-summarizer": {
            "requireAny": ["摘要", "summary", "总结", "要点", "结论", "insight"],
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
RUNTIME_POLICY_CONFIG_CANDIDATES = (
    os.path.join("config", "runtime-policy.json"),
    os.path.join("state", "runtime-policy.json"),
)
SPAWN_EXECUTOR_OPENCLAW = "openclaw_agent"
SPAWN_EXECUTOR_CODEX = "codex_cli"
SPAWN_EXECUTOR_CLAUDE = "claude_cli"
SUPPORTED_SPAWN_EXECUTORS = {
    SPAWN_EXECUTOR_OPENCLAW,
    SPAWN_EXECUTOR_CODEX,
    SPAWN_EXECUTOR_CLAUDE,
}
DEFAULT_EXECUTOR_ROUTING: Dict[str, str] = {
    "coder": SPAWN_EXECUTOR_CLAUDE,
    "debugger": SPAWN_EXECUTOR_CODEX,
}
DEFAULT_XHS_WORKFLOW_ROOT = "/Users/chengren17/.openclaw/projects/paper-xhs-3min-workflow"
DEFAULT_XHS_OUTPUT_ROOT = "/Users/chengren17/xhs-share"
DEFAULT_XHS_N8N_TRIGGER_SCRIPT = "/Users/chengren17/.openclaw/n8n/trigger-xhs-workflow.sh"
XHS_WORKFLOW_NAME = "paper-xhs-3min"
XHS_TEMPLATE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "templates", "workflows", XHS_WORKFLOW_NAME)
)
XHS_CONTEXT_MARKER_FILE = "orchestrator-bootstrap.json"
XHS_ALLOWED_PLACEHOLDERS = {"paper_id", "workflow_root", "run_dir", "pdf_path"}
XHS_PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
XHS_OUTPUT_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
XHS_STAGE_DEFINITIONS: Tuple[Dict[str, str], ...] = (
    {
        "stageId": "A0",
        "title": "Extract source text and metadata",
        "ownerHint": "paper-ingestor",
        "templateFile": "stage-a0-extract.md",
    },
    {
        "stageId": "A",
        "title": "Ingest findings into run workspace",
        "ownerHint": "paper-ingestor",
        "templateFile": "stage-a-ingest.md",
    },
    {
        "stageId": "B",
        "title": "Draft XHS summary",
        "ownerHint": "paper-summarizer",
        "templateFile": "stage-b-summary-draft.md",
    },
    {
        "stageId": "C",
        "title": "Citation and factual checks",
        "ownerHint": "invest-analyst",
        "templateFile": "stage-c-citation-check.md",
    },
    {
        "stageId": "D",
        "title": "Publish-ready post assembly",
        "ownerHint": "broadcaster",
        "templateFile": "stage-d-publish.md",
    },
    {
        "stageId": "E",
        "title": "Generate image prompts",
        "ownerHint": "paper-summarizer",
        "templateFile": "stage-e-image-prompts.md",
    },
    {
        "stageId": "F",
        "title": "Update knowledge base",
        "ownerHint": "knowledge-curator",
        "templateFile": "stage-f-kb.md",
    },
    {
        "stageId": "G",
        "title": "Quality gate review",
        "ownerHint": "debugger",
        "templateFile": "stage-g-quality-gate.md",
    },
    {
        "stageId": "H",
        "title": "Conversion package export",
        "ownerHint": "coder",
        "templateFile": "stage-h-conversion.md",
    },
    {
        "stageId": "I",
        "title": "Weekly review synthesis",
        "ownerHint": "invest-analyst",
        "templateFile": "stage-i-weekly-review.md",
    },
    {
        "stageId": "J",
        "title": "Reproduction scope and hypothesis mapping",
        "ownerHint": "invest-analyst",
        "templateFile": "stage-j-repro-scope.md",
    },
    {
        "stageId": "K",
        "title": "Reproduction data pipeline with synthetic fallback",
        "ownerHint": "paper-ingestor",
        "templateFile": "stage-k-repro-data.md",
    },
    {
        "stageId": "L",
        "title": "Core model and algorithm implementation",
        "ownerHint": "coder",
        "templateFile": "stage-l-repro-impl.md",
    },
    {
        "stageId": "M",
        "title": "Experiment execution and metric collection",
        "ownerHint": "coder",
        "templateFile": "stage-m-repro-run.md",
    },
    {
        "stageId": "N",
        "title": "Reproduction integrity audit",
        "ownerHint": "debugger",
        "templateFile": "stage-n-repro-audit.md",
    },
    {
        "stageId": "O",
        "title": "Reproduction report and artifact package",
        "ownerHint": "knowledge-curator",
        "templateFile": "stage-o-repro-report.md",
    },
)
SCHEDULER_STATE_FILE = "scheduler.kernel.json"
SCHEDULER_DEFAULT_INTERVAL_SEC = 300
SCHEDULER_MIN_INTERVAL_SEC = 60
SCHEDULER_MAX_INTERVAL_SEC = 86400
SCHEDULER_DEFAULT_MAX_STEPS = 1
SCHEDULER_DAEMON_STATE_FILE = "scheduler.daemon.json"
SCHEDULER_DAEMON_DEFAULT_POLL_SEC = 5
SCHEDULER_DAEMON_MIN_POLL_SEC = 0
SCHEDULER_DAEMON_MAX_POLL_SEC = 3600
AUTOPILOT_RUNTIME_STATE_FILE = "autopilot.runtime.json"
AUTOPILOT_RUNTIME_LOCK_FILE = "autopilot.runtime.lock"
# 0 means unlimited. Keep this unlimited by default so long workflow hints
# are not silently dropped from dispatch context.
KNOWLEDGE_HINT_PROMPT_LIMIT = 0


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def record_ops_event(root: str, event: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    try:
        return ops_metrics.append_event(root, event, data)
    except Exception:
        return {"ok": False, "event": event, "error": "metrics_write_failed"}


def clip(text: Optional[str], limit: int = 160) -> str:
    s = " ".join((text or "").split())
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "..."


def collaboration_thread_id(task_id: str, role: str) -> str:
    task_key = str(task_id or "").strip()
    role_key = governance.canonical_agent(role) or str(role or "").strip().lower()
    if not task_key or not role_key:
        return ""
    return f"{task_key}:{role_key}"


def resolve_collaboration_thread_summary(root: str, task_id: str, role: str) -> Dict[str, Any]:
    thread_id = collaboration_thread_id(task_id, role)
    if not thread_id:
        return {"ok": False, "available": False, "threadId": "", "reason": "thread_id_missing"}

    try:
        summary = collaboration_hub.summarize_thread(root, thread_id)
    except Exception as err:
        LOGGER.warning(
            "failed to summarize collaboration thread: taskId=%s role=%s threadId=%s",
            task_id,
            role,
            thread_id,
            exc_info=True,
        )
        return {
            "ok": False,
            "available": False,
            "threadId": thread_id,
            "reason": "summary_read_failed",
            "error": clip(str(err), 180),
        }

    if not isinstance(summary, dict):
        LOGGER.warning(
            "invalid collaboration summary payload: taskId=%s role=%s threadId=%s",
            task_id,
            role,
            thread_id,
        )
        return {
            "ok": False,
            "available": False,
            "threadId": thread_id,
            "reason": "summary_invalid",
        }

    try:
        message_count = max(0, int(summary.get("messageCount") or 0))
    except Exception:
        message_count = 0
    status = str(summary.get("status") or "").strip().lower()
    available = bool(summary) and (message_count > 0 or status not in {"", "missing"})
    reason = "summary_available" if available else "summary_missing"
    return {
        "ok": True,
        "available": available,
        "threadId": thread_id,
        "reason": reason,
        "summary": summary,
    }


def resolve_collaboration_escalation(root: str, summary_state: Dict[str, Any]) -> Dict[str, Any]:
    escalation: Dict[str, Any] = {
        "required": False,
        "reason": "not_required",
        "maxRounds": 0,
        "timeoutMinutes": 0,
    }

    try:
        policy = collaboration_hub.load_policy(root)
    except Exception as err:
        LOGGER.warning("failed to load collaboration policy for escalation check", exc_info=True)
        escalation["reason"] = "policy_load_failed"
        escalation["error"] = clip(str(err), 180)
        return escalation

    try:
        max_rounds = max(0, int(policy.get("maxRoundsPerThread") or 0))
    except Exception:
        max_rounds = 0
    try:
        timeout_minutes = max(0, int(policy.get("timeoutMinutes") or 0))
    except Exception:
        timeout_minutes = 0

    escalation["maxRounds"] = max_rounds
    escalation["timeoutMinutes"] = timeout_minutes

    thread_summary = (
        summary_state.get("summary")
        if isinstance(summary_state, dict) and isinstance(summary_state.get("summary"), dict)
        else {}
    )
    if collaboration_hub.should_escalate_round_limit(thread_summary, max_rounds):
        escalation["required"] = True
        escalation["reason"] = "round_limit"
        return escalation
    if collaboration_hub.should_escalate_timeout(thread_summary, timeout_minutes, now_iso_value=now_iso()):
        escalation["required"] = True
        escalation["reason"] = "timeout"
    return escalation


def relay_wakeup_collaboration_event(root: str, task_id: str, actor: str, kind: str, text: str) -> Dict[str, Any]:
    task_key = str(task_id or "").strip()
    actor_key = governance.canonical_agent(actor) or str(actor or "").strip().lower()
    if not task_key:
        return {"ok": False, "reason": "task_id_missing"}
    if not actor_key:
        return {"ok": False, "reason": "actor_missing"}
    if actor_key == "orchestrator":
        return {"ok": True, "skipped": True, "reason": "actor_is_orchestrator"}

    thread_id = collaboration_thread_id(task_key, actor_key)
    if not thread_id:
        return {"ok": False, "reason": "thread_id_missing"}

    kind_key = str(kind or "").strip().lower()
    message_type = "decision" if kind_key in {"done", "blocked"} else "answer"
    created_at = now_iso()
    hint_text = clip(text, 220)
    summary = (
        clip(f"{actor_key} wakeup decision for {task_key}: {kind_key or 'progress'}", 180)
        if message_type == "decision"
        else clip(f"{actor_key} wakeup progress update for {task_key}", 180)
    )
    request = (
        "请 orchestrator 基于该决策更新状态并确认下一步。"
        if message_type == "decision"
        else "请 orchestrator 确认是否继续协作或升级。"
    )
    evidence = merge_unique_strings([hint_text, f"kind:{kind_key or 'progress'}", "source:wakeup"], limit=4, item_limit=220)
    payload = {
        "taskId": task_key,
        "threadId": thread_id,
        "fromAgent": actor_key,
        "toAgent": "orchestrator",
        "messageType": message_type,
        "summary": summary,
        "evidence": evidence,
        "request": request,
        "deadline": created_at,
        "createdAt": created_at,
    }
    try:
        append_result = collaboration_hub.append_message(root, payload)
    except Exception as err:
        LOGGER.warning(
            "failed to append wakeup collaboration relay: taskId=%s actor=%s threadId=%s",
            task_key,
            actor_key,
            thread_id,
            exc_info=True,
        )
        return {
            "ok": False,
            "threadId": thread_id,
            "messageType": message_type,
            "reason": "append_exception",
            "error": clip(str(err), 200),
        }

    if append_result.get("ok"):
        return {
            "ok": True,
            "threadId": thread_id,
            "messageType": message_type,
            "createdAt": created_at,
            "reason": "appended",
        }

    reason = str(append_result.get("reason") or append_result.get("error") or "append_failed").strip() or "append_failed"
    relay: Dict[str, Any] = {
        "ok": False,
        "threadId": thread_id,
        "messageType": message_type,
        "reason": clip(reason, 200),
    }
    errors = append_result.get("errors")
    if isinstance(errors, list) and errors:
        relay["error"] = clip("; ".join(str(item) for item in errors if str(item).strip()), 200)
    elif append_result.get("error"):
        relay["error"] = clip(str(append_result.get("error")), 200)
    return relay


def maybe_relay_wakeup_collaboration_event(
    root: str,
    task_id: str,
    actor: str,
    kind: str,
    text: str,
    mode: str,
) -> Dict[str, Any]:
    actor_key = governance.canonical_agent(actor) or str(actor or "").strip().lower()
    thread_id = collaboration_thread_id(task_id, actor_key) if task_id and actor_key else ""
    message_type = "decision" if str(kind or "").strip().lower() in {"done", "blocked"} else "answer"
    if str(mode or "").strip().lower() != "send":
        relay: Dict[str, Any] = {
            "ok": True,
            "skipped": True,
            "reason": "mode_not_send",
            "messageType": message_type,
        }
        if thread_id:
            relay["threadId"] = thread_id
        return relay

    # Wakeup relay is best-effort and must not impact the wakeup main path result.
    return relay_wakeup_collaboration_event(root, task_id, actor, kind, text)


def best_effort_wakeup_collaboration_relay(
    root: str,
    task_id: str,
    actor: str,
    kind: str,
    text: str,
    mode: str,
) -> Dict[str, Any]:
    actor_key = governance.canonical_agent(actor) or str(actor or "").strip().lower()
    thread_id = collaboration_thread_id(task_id, actor_key) if task_id and actor_key else ""
    message_type = "decision" if str(kind or "").strip().lower() in {"done", "blocked"} else "answer"
    try:
        return maybe_relay_wakeup_collaboration_event(root, task_id, actor, kind, text, mode)
    except Exception as err:
        LOGGER.warning(
            "unexpected wakeup collaboration relay failure: taskId=%s actor=%s kind=%s",
            task_id,
            actor_key,
            kind,
            exc_info=True,
        )
        relay: Dict[str, Any] = {
            "ok": False,
            "reason": "relay_exception",
            "messageType": message_type,
            "error": clip(str(err), 200),
        }
        if thread_id:
            relay["threadId"] = thread_id
        return relay


def normalize_timeout_sec(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = int(default)
    return max(0, parsed)


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


AGENT_REPORT_STATUSES = {
    "done",
    "blocked",
    "progress",
    "completed",
    "success",
    "succeeded",
    "failed",
    "error",
}
JSON_FENCE_RE = re.compile(r"```json\s*(\{.*?\})\s*```", flags=re.IGNORECASE | re.DOTALL)


def looks_like_agent_report(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    status = str(obj.get("status") or obj.get("taskStatus") or "").strip().lower()
    if status not in AGENT_REPORT_STATUSES:
        return False
    return any(
        key in obj
        for key in (
            "taskId",
            "agent",
            "summary",
            "evidence",
            "changes",
            "nextActions",
            "risks",
        )
    )


def extract_payload_texts(spawn_obj: Any) -> List[str]:
    if not isinstance(spawn_obj, dict):
        return []
    payloads: List[Any] = []
    result = spawn_obj.get("result")
    if isinstance(result, dict) and isinstance(result.get("payloads"), list):
        payloads.extend(result.get("payloads") or [])
    if isinstance(spawn_obj.get("payloads"), list):
        payloads.extend(spawn_obj.get("payloads") or [])

    texts: List[str] = []
    for item in payloads:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            if text:
                texts.append(text)
        elif isinstance(item, str):
            text = item.strip()
            if text:
                texts.append(text)
    return texts


def extract_structured_report_from_text(text: str) -> Optional[Dict[str, Any]]:
    candidate: Optional[Dict[str, Any]] = None
    for match in JSON_FENCE_RE.finditer(text or ""):
        try:
            obj = json.loads(match.group(1))
        except Exception:
            continue
        if looks_like_agent_report(obj):
            candidate = obj
    stripped = (text or "").strip()
    if candidate is not None:
        return candidate
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            obj = json.loads(stripped)
        except Exception:
            return None
        if looks_like_agent_report(obj):
            return obj
    return None


def extract_structured_report_from_spawn(spawn_obj: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(spawn_obj, dict):
        return None
    if looks_like_agent_report(spawn_obj):
        return spawn_obj
    report = spawn_obj.get("report")
    if looks_like_agent_report(report):
        return report

    candidate: Optional[Dict[str, Any]] = None
    for text in extract_payload_texts(spawn_obj):
        parsed = extract_structured_report_from_text(text)
        if parsed is not None:
            candidate = parsed
    return candidate


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


def merge_unique_strings(items: List[str], limit: int = 8, item_limit: int = 220) -> List[str]:
    out: List[str] = []
    for raw in items:
        text = clip(raw, item_limit)
        if not text or text in out:
            continue
        out.append(text)
        if len(out) >= limit:
            break
    return out[:limit]


def collect_spawn_artifact_index(spawn: Dict[str, Any]) -> List[str]:
    report = spawn.get("normalizedReport") if isinstance(spawn.get("normalizedReport"), dict) else {}
    candidates: List[str] = []
    candidates.extend(normalize_string_list(report.get("hardEvidence"), limit=8, item_limit=220))
    candidates.extend(normalize_string_list(report.get("evidence"), limit=8, item_limit=220))
    candidates.extend(normalize_string_list(report.get("changes"), limit=6, item_limit=220))
    candidates.extend(normalize_string_list(spawn.get("stdout"), limit=2, item_limit=220))
    return merge_unique_strings(candidates, limit=10, item_limit=220)


def collect_spawn_unfinished_checklist(spawn: Dict[str, Any]) -> List[str]:
    report = spawn.get("normalizedReport") if isinstance(spawn.get("normalizedReport"), dict) else {}
    items = normalize_string_list(report.get("nextActions"), limit=6, item_limit=220)
    if items:
        return items
    detail = clip(spawn.get("detail"), 220)
    if detail:
        return [detail]
    return []


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
    if any(k in text for k in ("repro", "replica", "复现", "算法", "模型", "benchmark", "experiment")):
        return "reproduction"
    if agent_norm == "debugger" or any(k in text for k in ("debug", "bug", "故障", "异常", "排查", "trace", "error")):
        return "debug"
    if agent_norm == "invest-analyst" or any(k in text for k in ("research", "分析", "调研", "source", "report")):
        return "research"
    if agent_norm == "broadcaster" or any(k in text for k in ("broadcast", "公告", "发布", "summary", "同步")):
        return "broadcast"
    return "coding"


def requirements_for_kind(kind: str) -> List[str]:
    if kind == "reproduction":
        return [
            "论文核心方法必须逐项落地到可运行代码，不允许只写伪代码或口头说明。",
            "结果必须来自真实运行（训练/推理/评估），严禁伪造或手填指标。",
            "若原始数据不可得，必须提供可复现的数据生成脚本并记录生成规则、随机种子与规模。",
            "每个关键结论都要有对应证据（命令、日志、产物路径、指标文件）。",
        ]
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


def acceptance_keywords_for_agent(root: str, agent: str) -> List[str]:
    policy = load_acceptance_policy(root)
    role_key = governance.canonical_agent(agent) or str(agent or "").strip().lower()
    roles = policy.get("roles") if isinstance(policy.get("roles"), dict) else {}
    role_conf = roles.get(role_key) if isinstance(roles.get(role_key), dict) else {}
    required_any = role_conf.get("requireAny")
    out: List[str] = []
    if isinstance(required_any, list):
        for item in required_any:
            token = str(item or "").strip()
            if not token or token in out:
                continue
            out.append(token)
            if len(out) >= 12:
                break
    return out


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


def normalize_knowledge_hints(raw: Any, limit: int = KNOWLEDGE_HINT_PROMPT_LIMIT) -> List[str]:
    out: List[str] = []
    if not isinstance(raw, list):
        return out
    max_items = max(0, int(limit))
    for item in raw:
        text = clip(str(item or "").strip(), 200)
        if not text or text in out:
            continue
        out.append(text)
        if max_items > 0 and len(out) >= max_items:
            break
    return out


def normalize_knowledge_tags(raw: Any) -> List[str]:
    out: List[str] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        tag = str(item or "").strip()
        if not tag or tag in out:
            continue
        out.append(tag)
    return out


def resolve_dispatch_knowledge(root: str, task: Dict[str, Any], agent: str, dispatch_task: str) -> Tuple[Dict[str, Any], List[str]]:
    meta: Dict[str, Any] = {
        "degraded": False,
        "degradeReason": "",
        "knowledgeTags": [],
    }
    task_id = str(task.get("taskId") or "")
    try:
        payload = knowledge_adapter.fetch_feedback(root, task_id=task_id, agent=agent, objective=dispatch_task)
    except Exception as err:
        meta["degraded"] = True
        meta["degradeReason"] = clip(str(err), 200)
        return meta, []

    if not isinstance(payload, dict):
        return meta, []

    meta["degraded"] = bool(payload.get("degraded"))
    meta["degradeReason"] = clip(str(payload.get("degradeReason") or ""), 200) if payload.get("degradeReason") else ""
    meta["knowledgeTags"] = normalize_knowledge_tags(payload.get("knowledgeTags"))
    return meta, normalize_knowledge_hints(payload.get("hints"))


def resolve_prompt_strategy(root: str, task: Dict[str, Any], agent: str, dispatch_task: str) -> Dict[str, Any]:
    task_id = str(task.get("taskId") or "")
    title = str(task.get("title") or "")
    task_kind = infer_task_kind(agent, title, dispatch_task)
    library = strategy_library.load_strategy_library(root)
    return strategy_library.resolve_strategy(library, agent, task_kind, task_id=task_id)


def build_agent_prompt(
    root: str,
    task: Dict[str, Any],
    agent: str,
    dispatch_task: str,
    strategy: Optional[Dict[str, Any]] = None,
    knowledge_hints: Optional[List[str]] = None,
    retry_context: Optional[Dict[str, Any]] = None,
    collab_thread_summary: Optional[Dict[str, Any]] = None,
) -> str:
    task_id = str(task.get("taskId") or "")
    title = str(task.get("title") or "")
    project_path = lookup_task_project_path(root, task_id)
    task_kind = infer_task_kind(agent, title, dispatch_task)
    requirements = requirements_for_kind(task_kind)
    integrity_guardrails = [
        "No fabricated evidence, metrics, or completion claims.",
        "No shortcut simulation for model/algorithm reproduction; run real commands and capture outputs.",
        "If source data is unavailable, generate synthetic data via scripts and document assumptions/seeds.",
    ]
    schema = build_structured_output_schema(task_id, agent)
    board_snapshot = build_prompt_board_snapshot(root, task_id)
    history = read_recent_task_events(root, task_id, limit=8)
    selected_strategy = strategy if isinstance(strategy, dict) else resolve_prompt_strategy(root, task, agent, dispatch_task)
    hints = normalize_knowledge_hints(knowledge_hints)
    retry_pack = retry_context if isinstance(retry_context, dict) else {}
    collab_summary = collab_thread_summary if isinstance(collab_thread_summary, dict) else {}

    task_context = {
        "taskId": task_id,
        "title": clip(title, 120),
        "currentStatus": str(task.get("status") or ""),
        "owner": str(task.get("owner") or ""),
        "assigneeHint": str(task.get("assigneeHint") or ""),
        "projectId": str(task.get("projectId") or ""),
        "relatedTo": str(task.get("relatedTo") or ""),
        "objective": str(dispatch_task or ""),
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
    ]
    if collab_summary:
        lines.extend(
            [
                "COLLAB_THREAD_SUMMARY:",
                json.dumps(collab_summary, ensure_ascii=False, indent=2),
            ]
        )
    if retry_pack:
        lines.extend(
            [
                "RETRY_CONTEXT_PACK:",
                json.dumps(retry_pack, ensure_ascii=False, indent=2),
            ]
        )
    if hints:
        lines.append("KNOWLEDGE_HINTS:")
        for idx, hint in enumerate(hints, start=1):
            lines.append(f"{idx}. {hint}")
    if bool(selected_strategy.get("enabled")) and str(selected_strategy.get("content") or "").strip():
        lines.extend(
            [
                "ROLE_STRATEGY:",
                json.dumps(
                    {
                        "strategyId": str(selected_strategy.get("strategyId") or ""),
                        "source": str(selected_strategy.get("source") or ""),
                        "matchedBy": str(selected_strategy.get("matchedBy") or ""),
                        "enabled": bool(selected_strategy.get("enabled")),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                str(selected_strategy.get("content") or ""),
            ]
        )
    lines.extend(
        [
        "EXECUTION_REQUIREMENTS:",
        ]
    )
    for idx, item in enumerate(requirements, start=1):
        lines.append(f"{idx}. {item}")
    
    # Add environment requirements if project needs specific conda env
    env_requirements = []
    if project_path and "paper-xhs-3min-workflow" in project_path:
        env_requirements.append("必须使用 conda 环境 'workplace'（包含 python + gurobi 优化求解器）")
        env_requirements.append("对于优化问题（SDP/DRO等），必须调用真实求解器（CVXPY/Gurobi/MOSEK），不允许启发式规则替代")
        env_requirements.append("所有 Python 命令必须在 workplace 环境中执行：conda run -n workplace python ...")
    
    if env_requirements:
        lines.append("ENVIRONMENT_REQUIREMENTS:")
        for idx, item in enumerate(env_requirements, start=1):
            lines.append(f"{idx}. {item}")
    
    acceptance_keywords = acceptance_keywords_for_agent(root, agent)
    if acceptance_keywords:
        lines.extend(
            [
                "DONE_GATE_HINTS:",
                "1. status=done 时，summary 或 evidence 至少包含下列任一关键词："
                + ", ".join(acceptance_keywords),
            ]
        )
    if (governance.canonical_agent(agent) or str(agent or "").strip().lower()) == "debugger":
        lines.extend(
            [
                "COLLABORATION_HINTS:",
                "1. For complex debugging tasks, proactively enable subagent workflow.",
                "2. Delegate independent checks (repro/log diff/hypothesis validation) to subagents and then merge findings.",
            ]
        )
    lines.append("INTEGRITY_GUARDRAILS:")
    for idx, item in enumerate(integrity_guardrails, start=1):
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
    return "\n".join(lines)


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


def resolve_feishu_openapi_host(domain: str) -> str:
    raw = (domain or "").strip()
    norm = raw.lower()
    if norm in {"", "feishu"}:
        return "open.feishu.cn"
    if norm == "lark":
        return "open.larksuite.com"
    if "://" in raw:
        parsed = urllib.parse.urlparse(raw)
        host = parsed.netloc.strip()
    else:
        host = raw.strip().strip("/")
    return host or "open.feishu.cn"


def load_openclaw_feishu_credentials(account_id: str) -> Optional[Dict[str, str]]:
    candidates: List[str] = []
    env_cfg = os.environ.get("OPENCLAW_CONFIG", "").strip()
    if env_cfg:
        candidates.append(env_cfg)
    env_home = os.environ.get("OPENCLAW_HOME", "").strip()
    if env_home:
        candidates.append(os.path.join(env_home, "openclaw.json"))
    candidates.append(os.path.expanduser("~/.openclaw/openclaw.json"))

    seen: set = set()
    for path in candidates:
        if not path or path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            continue

        channels = cfg.get("channels")
        if not isinstance(channels, dict):
            continue
        feishu = channels.get("feishu")
        if not isinstance(feishu, dict):
            continue

        account_cfg: Dict[str, Any] = {}
        accounts = feishu.get("accounts")
        if isinstance(accounts, dict):
            raw_account = accounts.get(account_id)
            if isinstance(raw_account, dict):
                account_cfg = raw_account

        merged = dict(feishu)
        merged.pop("accounts", None)
        merged.update(account_cfg)

        app_id = str(merged.get("appId") or "").strip()
        app_secret = str(merged.get("appSecret") or "").strip()
        if not app_id or not app_secret:
            continue
        if merged.get("enabled", True) is False:
            continue

        return {
            "appId": app_id,
            "appSecret": app_secret,
            "host": resolve_feishu_openapi_host(str(merged.get("domain") or "feishu")),
        }

    return None


def feishu_post_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout_sec: int = 20) -> Dict[str, Any]:
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        return parse_json_loose(text or "{}")
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")
        try:
            obj = parse_json_loose(detail or "{}")
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        return {"code": -1, "msg": f"http {err.code}: {clip(detail, 200)}"}
    except Exception as err:
        return {"code": -1, "msg": clip(str(err), 200)}


def normalize_card_for_feishu_interactive(card: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(card, dict):
        return {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "Orchestrator 控制台"}},
            "elements": [{"tag": "markdown", "content": "控制台"}],
        }

    schema = str(card.get("schema") or "").strip()
    if schema == "2.0":
        return card

    card_type = str(card.get("type") or "").strip().lower()
    if card_type != "adaptivecard":
        return card

    body = card.get("body")
    lines: List[str] = []
    if isinstance(body, list):
        for item in body:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "").strip().lower() != "textblock":
                continue
            text = str(item.get("text") or "").strip()
            if text:
                lines.append(text)

    elements: List[Dict[str, Any]] = []
    text_md = "\n".join(lines).strip() or "Orchestrator 控制台"
    elements.append({"tag": "markdown", "content": text_md})

    buttons: List[Dict[str, Any]] = []
    raw_actions = card.get("actions")
    if isinstance(raw_actions, list):
        for action in raw_actions:
            if not isinstance(action, dict):
                continue
            if str(action.get("type") or "").strip().lower() != "action.submit":
                continue
            title = clip(str(action.get("title") or "执行"), 24)
            button: Dict[str, Any] = {
                "tag": "button",
                "text": {"tag": "plain_text", "content": title},
                "type": "default",
            }
            data = action.get("data")
            if isinstance(data, dict):
                command = str(data.get("command") or "").strip()
                if command:
                    button["value"] = {"command": command}
            buttons.append(button)

    for i in range(0, len(buttons), 3):
        elements.append({"tag": "action", "actions": buttons[i : i + 3]})

    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "Orchestrator 控制台"}},
        "elements": elements,
    }


def send_group_card_via_feishu_api(group_id: str, account_id: str, card: Dict[str, Any]) -> Dict[str, Any]:
    creds = load_openclaw_feishu_credentials(account_id)
    if not creds:
        return {"ok": False, "error": f"feishu credentials unavailable for account={account_id}"}

    host = creds.get("host") or "open.feishu.cn"
    token_url = f"https://{host}/open-apis/auth/v3/tenant_access_token/internal"
    token_obj = feishu_post_json(
        token_url,
        {"app_id": creds.get("appId"), "app_secret": creds.get("appSecret")},
    )
    raw_token_code = token_obj.get("code")
    try:
        token_code = int(raw_token_code) if raw_token_code is not None else -1
    except Exception:
        token_code = -1
    if token_code != 0:
        return {
            "ok": False,
            "stage": "token",
            "host": host,
            "code": token_code,
            "msg": clip(str(token_obj.get("msg") or ""), 200),
        }

    token = str(token_obj.get("tenant_access_token") or "").strip()
    if not token:
        return {"ok": False, "stage": "token", "host": host, "error": "missing tenant_access_token"}

    send_url = f"https://{host}/open-apis/im/v1/messages?receive_id_type=chat_id"
    card_payload = normalize_card_for_feishu_interactive(card)
    send_obj = feishu_post_json(
        send_url,
        {
            "receive_id": group_id,
            "msg_type": "interactive",
            "content": json.dumps(card_payload, ensure_ascii=False),
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    raw_send_code = send_obj.get("code")
    try:
        send_code = int(raw_send_code) if raw_send_code is not None else -1
    except Exception:
        send_code = -1
    data = send_obj.get("data") if isinstance(send_obj.get("data"), dict) else {}
    message_id = str(data.get("message_id") or "").strip()
    return {
        "ok": send_code == 0 and bool(message_id),
        "stage": "send",
        "host": host,
        "code": send_code,
        "msg": clip(str(send_obj.get("msg") or ""), 200),
        "messageId": message_id,
    }


def send_group_card(group_id: str, account_id: str, card: Dict[str, Any], mode: str, fallback_text: str = "") -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "channel": "feishu",
        "accountId": account_id,
        "target": f"chat:{group_id}",
        "card": card,
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
        "--card",
        json.dumps(card, ensure_ascii=False),
        "--json",
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=45)
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        out = {
            "ok": False,
            "error": f"card send failed (exit={proc.returncode})",
            "stdout": clip(stdout, 500),
            "stderr": clip(stderr, 500),
            "payload": payload,
        }
        direct = send_group_card_via_feishu_api(group_id, account_id, card)
        out["directCard"] = direct
        if direct.get("ok"):
            out["ok"] = True
            out["error"] = ""
            out["recoveredBy"] = "direct_feishu_api"
            out["deliveryMessageId"] = str(direct.get("messageId") or "")
            return out
        if fallback_text:
            fallback = send_group_message(group_id, account_id, fallback_text, mode)
            out["fallback"] = fallback
            if fallback.get("ok"):
                out["ok"] = True
                out["degradedToText"] = True
                out["error"] = ""
        return out

    out = {"ok": True, "dryRun": False, "payload": payload}
    try:
        if stdout:
            out["result"] = parse_json_loose(stdout)
    except Exception:
        pass
    if stderr:
        out["stderr"] = clip(stderr, 500)

    # Some channel adapters may return exit=0 but without delivery ack for cards.
    # In this case, treat as degraded and fallback to text to avoid silent no-reply.
    result_obj = out.get("result")
    message_id = ""
    if isinstance(result_obj, dict):
        payload_obj = result_obj.get("payload")
        if isinstance(payload_obj, dict):
            nested = payload_obj.get("result")
            if isinstance(nested, dict):
                message_id = str(nested.get("messageId") or "").strip()
        if not message_id:
            message_id = str(result_obj.get("messageId") or "").strip()
    if not message_id:
        direct = send_group_card_via_feishu_api(group_id, account_id, card)
        out["directCard"] = direct
        if direct.get("ok"):
            out["ok"] = True
            out["recoveredBy"] = "direct_feishu_api"
            out["deliveryMessageId"] = str(direct.get("messageId") or "")
        elif fallback_text:
            fallback = send_group_message(group_id, account_id, fallback_text, mode)
            out["fallback"] = fallback
            out["degradedToText"] = True
            out["ok"] = bool(fallback.get("ok"))
            if not out["ok"]:
                out["error"] = "card send missing delivery ack and all fallbacks failed"

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


def cleanup_done_state(root: str, task_id: str, session_agent: str = "", session_executor: str = "") -> Dict[str, Any]:
    task_key = str(task_id or "").strip()
    out: Dict[str, Any] = {
        "taskId": task_key,
        "contextCleared": False,
        "recoveryCleared": False,
        "sessionUpdated": False,
        "session": {},
    }
    if not task_key:
        return out

    try:
        cleared = context_pack.clear_task(root, task_key)
        out["contextCleared"] = bool(cleared.get("cleared"))
    except Exception:
        LOGGER.warning("done cleanup failed for retry context: taskId=%s", task_key, exc_info=True)

    try:
        cleared = recovery_loop.clear_task(root, task_key)
        out["recoveryCleared"] = bool(cleared.get("cleared"))
    except Exception:
        LOGGER.warning("done cleanup failed for recovery state: taskId=%s", task_key, exc_info=True)

    try:
        if session_agent and session_executor:
            session_record = session_registry.mark_done(root, task_key, session_agent, session_executor)
            session_meta = session_registry.build_session_metadata(session_record)
            if session_meta:
                out["session"] = session_meta
                out["sessionUpdated"] = True

        bulk_done = session_registry.mark_task_done(root, task_key)
        if int(bulk_done.get("updated") or 0) > 0:
            out["sessionUpdated"] = True
    except Exception:
        LOGGER.warning("done cleanup failed for session registry: taskId=%s", task_key, exc_info=True)

    return out


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
    normalized_evidence = evidence_normalizer.normalize_evidence(base, acceptance_text)
    hard_evidence = normalize_string_list(normalized_evidence.get("hardEvidence"), limit=8, item_limit=240)
    soft_evidence = normalize_string_list(normalized_evidence.get("softEvidence"), limit=8, item_limit=220)
    normalized_text = str(normalized_evidence.get("normalizedText") or acceptance_text).strip()
    if normalized_text:
        acceptance_text = normalized_text

    detail_parts: List[str] = []
    if summary:
        detail_parts.append(summary)
    if hard_evidence:
        detail_parts.append("硬证据: " + "; ".join(hard_evidence[:2]))
    elif evidence:
        detail_parts.append("证据: " + "; ".join(evidence[:2]))
    elif soft_evidence:
        detail_parts.append("线索: " + "; ".join(soft_evidence[:2]))
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
        "hardEvidence": hard_evidence,
        "softEvidence": soft_evidence,
        "normalizedText": normalized_text,
        "acceptanceText": acceptance_text,
        "detail": detail,
        "structured": structured,
    }


def classify_spawn_result(root: str, task_id: str, role: str, spawn_obj: Dict[str, Any], fallback_text: str = "") -> Dict[str, Any]:
    structured_report = extract_structured_report_from_spawn(spawn_obj)
    source_obj = structured_report if isinstance(structured_report, dict) else spawn_obj
    status_hint = str(source_obj.get("status") or source_obj.get("taskStatus") or "").strip().lower()
    ok_flag = spawn_obj.get("ok")
    # If we found a nested structured report in worker payloads, ignore noisy wrapper text.
    report = normalize_spawn_report(
        task_id,
        role,
        source_obj,
        fallback_text="" if structured_report is not None else fallback_text,
    )
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
        accepted = evaluate_acceptance(root, role, text or detail, structured_report=report)
        if accepted.get("ok"):
            return {
                "decision": "done",
                "detail": clip(detail or text or f"{task_id} 子代理返回完成", 200),
                "reasonCode": "done_with_evidence",
                "acceptanceReasonCode": str(accepted.get("reasonCode") or "accepted"),
                "report": report,
            }
        return {
            "decision": "blocked",
            "detail": clip(
                f"{accepted.get('reason') or '未通过验收策略'} | {clip(detail or text or f'{task_id} 子代理结果未通过验收', 120)}",
                200,
            ),
            "reasonCode": "incomplete_output",
            "acceptanceReasonCode": str(accepted.get("reasonCode") or "acceptance_failed"),
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


def nonneg_int(value: Any, default: int = 0) -> int:
    try:
        out = int(value)
    except Exception:
        return default
    return out if out >= 0 else default


def extract_usage_pair(usage: Dict[str, Any]) -> int:
    prompt_tokens = nonneg_int(usage.get("prompt_tokens"), -1)
    completion_tokens = nonneg_int(usage.get("completion_tokens"), -1)
    if prompt_tokens >= 0 or completion_tokens >= 0:
        return max(0, prompt_tokens) + max(0, completion_tokens)

    input_tokens = nonneg_int(usage.get("input_tokens"), -1)
    output_tokens = nonneg_int(usage.get("output_tokens"), -1)
    if input_tokens >= 0 or output_tokens >= 0:
        return max(0, input_tokens) + max(0, output_tokens)

    return -1


def extract_token_usage_from_spawn(payload: Dict[str, Any]) -> int:
    if not isinstance(payload, dict):
        return 0

    buckets = [payload]
    metrics = payload.get("metrics")
    if isinstance(metrics, dict):
        buckets.append(metrics)
    usage = payload.get("usage")
    if isinstance(usage, dict):
        buckets.append(usage)

    for bucket in buckets:
        for key in ("total_tokens", "totalTokens"):
            parsed = nonneg_int(bucket.get(key), -1)
            if parsed >= 0:
                return parsed

    for bucket in buckets:
        for key in ("tokenUsage", "token_usage", "tokens"):
            parsed = nonneg_int(bucket.get(key), -1)
            if parsed >= 0:
                return parsed

    for bucket in buckets:
        paired_usage = extract_usage_pair(bucket)
        if paired_usage >= 0:
            return paired_usage

    return 0


def extract_elapsed_ms_from_spawn(payload: Dict[str, Any], fallback_ms: int = 0) -> int:
    if not isinstance(payload, dict):
        return max(0, int(fallback_ms))

    for key in ("elapsedMs", "elapsed_ms", "durationMs", "duration_ms"):
        parsed = nonneg_int(payload.get(key), -1)
        if parsed >= 0:
            return parsed

    metrics = payload.get("metrics")
    if isinstance(metrics, dict):
        for key in ("elapsedMs", "elapsed_ms", "durationMs", "duration_ms"):
            parsed = nonneg_int(metrics.get(key), -1)
            if parsed >= 0:
                return parsed

    return max(0, int(fallback_ms))


def collect_spawn_metrics(payload: Dict[str, Any], fallback_elapsed_ms: int = 0) -> Dict[str, int]:
    return {
        "elapsedMs": extract_elapsed_ms_from_spawn(payload, fallback_ms=fallback_elapsed_ms),
        "tokenUsage": extract_token_usage_from_spawn(payload),
    }


def run_dispatch_spawn(args: argparse.Namespace, task_prompt: str) -> Dict[str, Any]:
    start_ms = int(time.time() * 1000)
    plan = resolve_spawn_plan(args, task_prompt)
    executor = str(plan.get("executor") or "openclaw_agent")
    planned_cmd = list(plan.get("command") or [])
    timeout_sec = normalize_timeout_sec(getattr(args, "timeout_sec", 0), default=0)

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
            "metrics": {"elapsedMs": 0, "tokenUsage": 0},
        }

    if args.spawn_output:
        try:
            obj = parse_json_loose(args.spawn_output)
            if not isinstance(obj, dict):
                obj = {"raw": args.spawn_output}
            decision = classify_spawn_result(args.root, args.task_id, args.agent, obj, fallback_text=args.spawn_output)
            metrics = collect_spawn_metrics(obj, fallback_elapsed_ms=max(0, int(time.time() * 1000) - start_ms))
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
                "acceptanceReasonCode": decision.get("acceptanceReasonCode", ""),
                "normalizedReport": decision.get("report"),
                "metrics": metrics,
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
                "metrics": {"elapsedMs": max(0, int(time.time() * 1000) - start_ms), "tokenUsage": 0},
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
            "metrics": {"elapsedMs": 0, "tokenUsage": 0},
        }

    run_timeout = None if timeout_sec <= 0 else max(10, timeout_sec + 5)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=run_timeout)
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
    except subprocess.TimeoutExpired as err:
        elapsed_ms = max(0, int(time.time() * 1000) - start_ms)
        timeout_stdout = (str(err.stdout) if err.stdout else "").strip()
        timeout_stderr = (str(err.stderr) if err.stderr else "").strip()
        detail = clip(
            f"spawn timeout after {timeout_sec}s" if timeout_sec > 0 else "spawn timeout",
            200,
        )
        return {
            "ok": False,
            "error": f"spawn timeout after {timeout_sec}s" if timeout_sec > 0 else "spawn timeout",
            "stdout": timeout_stdout,
            "stderr": timeout_stderr,
            "command": cmd,
            "executor": executor,
            "plannedCommand": planned_cmd,
            "decision": "blocked",
            "detail": detail,
            "reasonCode": "spawn_failed",
            "spawnErrorKind": "timeout",
            "metrics": {"elapsedMs": elapsed_ms, "tokenUsage": 0},
        }
    except Exception as err:
        elapsed_ms = max(0, int(time.time() * 1000) - start_ms)
        return {
            "ok": False,
            "error": f"spawn execution error: {err}",
            "stdout": "",
            "stderr": "",
            "command": cmd,
            "executor": executor,
            "plannedCommand": planned_cmd,
            "decision": "blocked",
            "detail": clip(str(err), 200),
            "reasonCode": "spawn_failed",
            "spawnErrorKind": "exception",
            "metrics": {"elapsedMs": elapsed_ms, "tokenUsage": 0},
        }

    elapsed_ms = max(0, int(time.time() * 1000) - start_ms)

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

    metrics = collect_spawn_metrics(parsed, fallback_elapsed_ms=elapsed_ms)

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
            "metrics": metrics,
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
        "acceptanceReasonCode": decision.get("acceptanceReasonCode", ""),
        "normalizedReport": decision.get("report"),
        "metrics": metrics,
    }


def dispatch_once(args: argparse.Namespace) -> Dict[str, Any]:
    visibility_mode = str(getattr(args, "visibility_mode", DEFAULT_VISIBILITY_MODE) or DEFAULT_VISIBILITY_MODE)
    if visibility_mode not in VISIBILITY_MODES:
        visibility_mode = DEFAULT_VISIBILITY_MODE

    if args.actor != "orchestrator":
        return {"ok": False, "error": "dispatch is restricted to actor=orchestrator"}

    task = get_task(args.root, args.task_id)
    if not isinstance(task, dict):
        return {"ok": False, "error": f"task not found: {args.task_id}"}

    agent = governance.canonical_agent(getattr(args, "agent", ""))
    if not agent:
        return {"ok": False, "error": "agent is required"}
    args.agent = agent

    gate = governance.checkpoint_dispatch(args.root, args.actor, args.task_id, args.agent)
    if not bool(gate.get("allowed")):
        out = {
            "ok": False,
            "handled": True,
            "intent": "dispatch",
            "taskId": args.task_id,
            "agent": args.agent,
            "reason": str(gate.get("reason") or "governance_blocked"),
            "governance": gate,
        }
        if gate.get("approvalId"):
            out["approvalId"] = gate.get("approvalId")
        if gate.get("consumed"):
            out["consumed"] = gate.get("consumed")
        return out

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
    explicit_task = str(getattr(args, "task", "") or "").strip()
    bound_dispatch_prompt = lookup_task_dispatch_prompt(args.root, args.task_id)
    if explicit_task:
        dispatch_task = explicit_task
        objective_source = "explicit_task"
    elif bound_dispatch_prompt:
        dispatch_task = bound_dispatch_prompt
        objective_source = "bound_dispatch_prompt"
    else:
        dispatch_task = f"{args.task_id}: {task.get('title') or 'untitled'}"
        objective_source = "task_fallback"
    dispatch_task_message = clip(dispatch_task, 300)
    knowledge_meta, knowledge_hints = resolve_dispatch_knowledge(args.root, task, args.agent, dispatch_task)
    selected_strategy = resolve_prompt_strategy(args.root, task, args.agent, dispatch_task)
    retry_context_pack = context_pack.build_retry_context(args.root, args.task_id)
    collab_summary_state = resolve_collaboration_thread_summary(args.root, args.task_id, args.agent)
    collab_escalation = resolve_collaboration_escalation(args.root, collab_summary_state)
    if isinstance(collab_summary_state, dict):
        collab_summary_state["escalation"] = collab_escalation

    escalation_hint = ""
    if bool(collab_escalation.get("required")):
        escalation_reason = str(collab_escalation.get("reason") or "").strip().lower()
        reason_label = "轮次上限" if escalation_reason == "round_limit" else "超时阈值" if escalation_reason == "timeout" else "升级阈值"
        escalation_hint = f"协作线程已触发升级门槛（{reason_label}），请优先给出可直接升级/仲裁的结论。"

    collab_summary_for_prompt = (
        collab_summary_state.get("summary")
        if bool(collab_summary_state.get("available")) and isinstance(collab_summary_state.get("summary"), dict)
        else None
    )
    agent_prompt = build_agent_prompt(
        args.root,
        task,
        args.agent,
        dispatch_task,
        strategy=selected_strategy,
        knowledge_hints=knowledge_hints,
        retry_context=retry_context_pack,
        collab_thread_summary=collab_summary_for_prompt,
    )
    if escalation_hint:
        agent_prompt = "\n".join(
            [
                agent_prompt,
                "",
                "COLLAB_ESCALATION_HINTS:",
                f"1. {escalation_hint}",
            ]
        )

    dispatch_mode_line = "派发模式: 手动协作（等待回报）" if not args.spawn else "派发模式: 自动执行闭环（spawn并回写看板）"

    claim_text = "\n".join(
        [
            f"[CLAIM] {args.task_id} | 状态={status_zh(status or '-')} | 指派={args.agent}",
            f"标题: {title}",
            dispatch_mode_line,
        ]
    )

    mentions = load_bot_mentions(args.root)
    orchestrator_mention = mention_tag_for("orchestrator", mentions, fallback="@orchestrator")
    assignee_mention = mention_tag_for(args.agent, mentions, fallback=f"@{args.agent}")
    report_template = f"{orchestrator_mention} {args.task_id} 已完成，证据: 日志/截图/链接"
    task_lines = [
        f"[TASK] {args.task_id} | 负责人={args.agent}",
        f"任务: {dispatch_task_message}",
    ]
    if escalation_hint:
        task_lines.append(f"提醒: {escalation_hint}")
    task_lines.append(f"请 {assignee_mention} 执行，完成后按模板回报：{report_template}。")
    task_text = "\n".join(task_lines)
    claim_send: Dict[str, Any] = {"ok": True, "skipped": True, "reason": "not_sent"}
    task_send: Dict[str, Any] = {"ok": True, "skipped": True, "reason": "not_sent"}
    session_meta: Dict[str, Any] = {}
    session_executor = ""

    spawn = {
        "ok": True,
        "skipped": True,
        "reason": "spawn disabled",
        "decision": "",
        "detail": "",
        "command": [],
        "stdout": "",
        "stderr": "",
        "metrics": {"elapsedMs": 0, "tokenUsage": 0},
    }
    close_apply: Dict[str, Any] = {"ok": True, "skipped": True, "reason": "spawn disabled"}
    close_publish: Dict[str, Any] = {"ok": True, "skipped": True, "reason": "spawn disabled"}
    worker_report: Dict[str, Any] = {"ok": True, "skipped": True, "reason": "visibility mode not enabled"}
    spawn_attempt_count = 0
    aggregate_elapsed_ms = 0
    aggregate_token_usage = 0

    if args.spawn:
        try:
            session_plan = resolve_spawn_plan(args, agent_prompt)
            session_executor = str(session_plan.get("executor") or "")
        except Exception:
            session_executor = ""
        try:
            session_record = session_registry.ensure_session(
                args.root,
                args.task_id,
                args.agent,
                session_executor or "unknown",
            )
            session_meta = session_registry.build_session_metadata(session_record)
        except Exception:
            session_meta = {}

        reason_code_hint = ""
        if args.spawn_output:
            try:
                parsed_hint = parse_json_loose(args.spawn_output)
                if not isinstance(parsed_hint, dict):
                    parsed_hint = {"raw": args.spawn_output}
                classified_hint = classify_spawn_result(
                    args.root,
                    args.task_id,
                    args.agent,
                    parsed_hint,
                    fallback_text=args.spawn_output,
                )
                hinted_reason = str(classified_hint.get("reasonCode") or "").strip()
                if recovery_loop.should_trigger_recovery(hinted_reason):
                    reason_code_hint = hinted_reason
            except Exception:
                reason_code_hint = ""

        active_cooldown = recovery_loop.get_active_cooldown(args.root, args.task_id, reason_code_hint)
        if isinstance(active_cooldown, dict) and active_cooldown.get("cooldownActive"):
            next_assignee = str(active_cooldown.get("nextAssignee") or "human")
            action = str(active_cooldown.get("action") or ("human" if next_assignee == "human" else "retry"))
            cooldown_until = str(active_cooldown.get("cooldownUntil") or "")
            detail = clip(
                f"{args.task_id} 冷却中，跳过重试执行；next={next_assignee} action={action} until={cooldown_until}",
                200,
            )
            spawn = {
                "ok": True,
                "skipped": True,
                "spawnSkipped": True,
                "reason": "cooldown_active",
                "decision": "blocked",
                "detail": detail,
                "command": [],
                "stdout": "",
                "stderr": "",
                "reasonCode": str(active_cooldown.get("reasonCode") or ""),
                "attempt": int(active_cooldown.get("attempt") or 0),
                "nextAssignee": next_assignee,
                "action": action,
                "recoveryState": str(active_cooldown.get("recoveryState") or ""),
                "cooldownActive": True,
                "cooldownUntil": cooldown_until,
                "cooldownUntilTs": int(active_cooldown.get("cooldownUntilTs") or 0),
                "metrics": {"elapsedMs": 0, "tokenUsage": 0},
            }
            claim_send = {"ok": True, "skipped": True, "reason": "cooldown_active"}
            task_send = {"ok": True, "skipped": True, "reason": "cooldown_active"}
        else:
            precheck = budget_policy.precheck_budget(args.root, args.task_id, args.agent)
            if not bool(precheck.get("allowed")):
                exceeded_keys = [str(x) for x in (precheck.get("exceededKeys") or []) if str(x)]
                degrade_action = str(precheck.get("degradeAction") or "manual_handoff")
                detail = clip(
                    f"{args.task_id} 预算超限（precheck）: {','.join(exceeded_keys) or 'unknown'}",
                    200,
                )
                spawn = {
                    "ok": False,
                    "skipped": False,
                    "spawnSkipped": True,
                    "precheckBlocked": True,
                    "reason": "budget_precheck_blocked",
                    "decision": "blocked",
                    "detail": detail,
                    "command": [],
                    "stdout": "",
                    "stderr": "",
                    "executor": "budget_guard",
                    "plannedCommand": [],
                    "reasonCode": "budget_exceeded",
                    "exceededKeys": exceeded_keys,
                    "degradeAction": degrade_action,
                    "nextAssignee": str(precheck.get("nextAssignee") or "human"),
                    "action": "escalate",
                    "budgetSnapshot": precheck.get("budgetSnapshot"),
                    "metrics": {"elapsedMs": 0, "tokenUsage": 0},
                }
                claim_send = {"ok": True, "skipped": True, "reason": "budget_precheck_blocked"}
                task_send = {"ok": True, "skipped": True, "reason": "budget_precheck_blocked"}
            else:
                claim_send = send_group_message(args.group_id, args.account_id, claim_text, args.mode)
                task_send = send_group_message(args.group_id, args.account_id, task_text, args.mode)
                spawn = run_dispatch_spawn(args, agent_prompt)
                spawn_attempt_count = 0 if spawn.get("skipped") else 1
                if not spawn.get("skipped"):
                    try:
                        session_executor = str(spawn.get("executor") or session_executor or "unknown")
                        session_record = session_registry.record_attempt(
                            args.root,
                            args.task_id,
                            args.agent,
                            session_executor,
                            reason_code=str(spawn.get("reasonCode") or ""),
                            detail=str(spawn.get("detail") or ""),
                        )
                        session_meta = session_registry.build_session_metadata(session_record)
                    except Exception:
                        LOGGER.warning(
                            "session record_attempt failed: taskId=%s agent=%s reasonCode=%s",
                            args.task_id,
                            args.agent,
                            str(spawn.get("reasonCode") or ""),
                            exc_info=True,
                        )
                metrics = spawn.get("metrics") if isinstance(spawn.get("metrics"), dict) else {}
                aggregate_elapsed_ms += nonneg_int(metrics.get("elapsedMs"), 0)
                aggregate_token_usage += nonneg_int(metrics.get("tokenUsage"), 0)
                if (
                    not spawn.get("skipped")
                    and not args.spawn_output
                    and spawn.get("decision") == "blocked"
                    and str(spawn.get("reasonCode") or "") in {"incomplete_output", "missing_evidence", "stage_only", "role_policy_missing_keyword"}
                ):
                    first_attempt_spawn = dict(spawn)
                    failure_pack: Dict[str, Any] = {}
                    try:
                        failure_pack = context_pack.record_failure(
                            args.root,
                            task_id=args.task_id,
                            agent=args.agent,
                            executor=str(spawn.get("executor") or session_executor or "unknown"),
                            prompt_text=agent_prompt,
                            output_text=str(spawn.get("stdout") or spawn.get("detail") or ""),
                            blocked_reason=str(spawn.get("reasonCode") or "blocked"),
                            artifact_index=collect_spawn_artifact_index(spawn),
                            unfinished_checklist=collect_spawn_unfinished_checklist(spawn),
                            decision=str(spawn.get("decision") or "blocked"),
                            reason_code=str(spawn.get("reasonCode") or ""),
                        )
                    except Exception:
                        LOGGER.warning(
                            "inline retry record_failure failed: taskId=%s reasonCode=%s",
                            args.task_id,
                            str(spawn.get("reasonCode") or ""),
                            exc_info=True,
                        )
                    retry_prompt_pack = (
                        dict(failure_pack)
                        if isinstance(failure_pack, dict) and failure_pack
                        else context_pack.build_retry_context(args.root, args.task_id)
                    )
                    retry_prompt = (
                        agent_prompt
                        + "\nRETRY_CONTEXT_PACK:\n"
                        + json.dumps(retry_prompt_pack, ensure_ascii=False, indent=2)
                        + "\n\n交付硬性要求：请直接给出最终可验证结果（改动文件/命令输出/commit哈希/验证结论），不要只给阶段性进度。"
                    )
                    retry_spawn = run_dispatch_spawn(args, retry_prompt)
                    spawn_attempt_count += 1
                    if not retry_spawn.get("skipped"):
                        try:
                            session_executor = str(retry_spawn.get("executor") or session_executor or "unknown")
                            session_record = session_registry.record_attempt(
                                args.root,
                                args.task_id,
                                args.agent,
                                session_executor,
                                reason_code=str(retry_spawn.get("reasonCode") or ""),
                                detail=str(retry_spawn.get("detail") or ""),
                            )
                            session_meta = session_registry.build_session_metadata(session_record)
                        except Exception:
                            LOGGER.warning(
                                "session retry record_attempt failed: taskId=%s agent=%s reasonCode=%s",
                                args.task_id,
                                args.agent,
                                str(retry_spawn.get("reasonCode") or ""),
                                exc_info=True,
                            )
                    retry_metrics = retry_spawn.get("metrics") if isinstance(retry_spawn.get("metrics"), dict) else {}
                    aggregate_elapsed_ms += nonneg_int(retry_metrics.get("elapsedMs"), 0)
                    aggregate_token_usage += nonneg_int(retry_metrics.get("tokenUsage"), 0)
                    final_spawn = retry_spawn if isinstance(retry_spawn, dict) and not retry_spawn.get("skipped") else first_attempt_spawn
                    spawn = dict(final_spawn) if isinstance(final_spawn, dict) else first_attempt_spawn
                    spawn["retried"] = True
                    spawn["retry"] = retry_spawn
                    spawn["firstAttempt"] = first_attempt_spawn
                spawn["metrics"] = {
                    "elapsedMs": aggregate_elapsed_ms,
                    "tokenUsage": aggregate_token_usage,
                }
    else:
        claim_send = send_group_message(args.group_id, args.account_id, claim_text, args.mode)
        task_send = send_group_message(args.group_id, args.account_id, task_text, args.mode)

    if args.spawn:
        if spawn.get("skipped"):
            close_apply = {"ok": True, "skipped": True, "reason": spawn.get("reason", "spawn skipped")}
            close_publish = {"ok": True, "skipped": True, "reason": "spawn skipped"}
        else:
            if not bool(spawn.get("precheckBlocked")) and spawn_attempt_count > 0:
                metrics = spawn.get("metrics") if isinstance(spawn.get("metrics"), dict) else {}
                budget_check = budget_policy.record_and_check_budget(
                    args.root,
                    args.task_id,
                    args.agent,
                    nonneg_int(metrics.get("tokenUsage"), 0),
                    nonneg_int(metrics.get("elapsedMs"), 0),
                    spawn_attempt_count,
                )
                spawn["budgetSnapshot"] = budget_check.get("budgetSnapshot")
                if not bool(budget_check.get("allowed")):
                    exceeded_keys = [str(x) for x in (budget_check.get("exceededKeys") or []) if str(x)]
                    degrade_action = str(budget_check.get("degradeAction") or "manual_handoff")
                    base_detail = clip(spawn.get("detail") or f"{args.task_id} 超预算", 120)
                    tail = f"budget_exceeded:{','.join(exceeded_keys) or 'unknown'}"
                    spawn["decision"] = "blocked"
                    spawn["reasonCode"] = "budget_exceeded"
                    spawn["exceededKeys"] = exceeded_keys
                    spawn["degradeAction"] = degrade_action
                    spawn["nextAssignee"] = str(budget_check.get("nextAssignee") or "human")
                    spawn["action"] = "escalate"
                    spawn["detail"] = clip(f"{base_detail} | {tail} | degrade:{degrade_action}", 200)

            decision = spawn.get("decision") or "blocked"
            detail = clip(spawn.get("detail") or f"{args.task_id} 子代理执行结果未明确", 200)
            recovery_decision: Optional[Dict[str, Any]] = None
            reason_code = str(spawn.get("reasonCode") or "").strip()
            if decision != "done" and recovery_loop.should_trigger_recovery(reason_code):
                recovery_decision = recovery_loop.decide_recovery(
                    args.root,
                    args.task_id,
                    args.agent,
                    reason_code,
                )
                spawn["attempt"] = int(recovery_decision.get("attempt") or 0)
                spawn["nextAssignee"] = str(recovery_decision.get("nextAssignee") or "human")
                spawn["action"] = str(recovery_decision.get("action") or "escalate")
                spawn["recoveryState"] = str(recovery_decision.get("recoveryState") or "")
                spawn["cooldownActive"] = bool(recovery_decision.get("cooldownActive"))
                spawn["cooldownUntil"] = str(recovery_decision.get("cooldownUntil") or "")
            if decision == "done":
                close_apply = board_apply(args.root, "orchestrator", f"mark done {args.task_id}: {detail}")
            elif isinstance(recovery_decision, dict):
                recovery_action = str(recovery_decision.get("action") or "escalate")
                next_assignee = str(recovery_decision.get("nextAssignee") or "human")
                recovery_state = str(recovery_decision.get("recoveryState") or "")
                if recovery_action == "retry":
                    # Keep retry-able failures runnable so autopilot can continue the recovery chain.
                    close_apply = board_apply(args.root, "orchestrator", f"@{next_assignee} claim task {args.task_id}")
                else:
                    tail = recovery_state or ("escalated_to_human" if recovery_action == "escalate" else "human_handoff")
                    close_apply = board_apply(args.root, "orchestrator", f"block task {args.task_id}: {clip(detail + ' | ' + tail, 200)}")
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

            if decision == "done":
                final_executor = str(spawn.get("executor") or session_executor or "unknown")
                cleanup = cleanup_done_state(
                    args.root,
                    args.task_id,
                    session_agent=args.agent,
                    session_executor=final_executor,
                )
                retry_context_pack = {}
                cleaned_session = cleanup.get("session")
                if isinstance(cleaned_session, dict) and cleaned_session:
                    session_meta = cleaned_session
            else:
                try:
                    final_executor = str(spawn.get("executor") or session_executor or "unknown")
                    recorded = context_pack.record_failure(
                        args.root,
                        task_id=args.task_id,
                        agent=args.agent,
                        executor=final_executor,
                        prompt_text=agent_prompt,
                        output_text=str(spawn.get("stdout") or detail),
                        blocked_reason=reason_code or "blocked",
                        artifact_index=collect_spawn_artifact_index(spawn),
                        unfinished_checklist=collect_spawn_unfinished_checklist(spawn),
                        decision=str(decision or "blocked"),
                        reason_code=reason_code,
                    )
                    retry_context_pack = dict(recorded) if isinstance(recorded, dict) else context_pack.build_retry_context(args.root, args.task_id)
                except Exception:
                    retry_context_pack = context_pack.build_retry_context(args.root, args.task_id)
                try:
                    final_executor = str(spawn.get("executor") or session_executor or "unknown")
                    session_record = session_registry.mark_failed(
                        args.root,
                        args.task_id,
                        args.agent,
                        final_executor,
                        reason_code=reason_code or "blocked",
                        detail=detail,
                    )
                    session_meta = session_registry.build_session_metadata(session_record)
                except Exception:
                    LOGGER.warning(
                        "session mark_failed failed: taskId=%s agent=%s reasonCode=%s",
                        args.task_id,
                        args.agent,
                        reason_code,
                        exc_info=True,
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

    backfill_result: Dict[str, Any] = {"ok": True, "skipped": True, "reason": "spawn_done_or_not_enabled"}
    if args.spawn:
        spawn_decision = str(spawn.get("decision") or "").strip().lower()
        if spawn_decision and spawn_decision != "done":
            try:
                backfill_result = knowledge_adapter.backfill_failure_feedback(
                    args.root,
                    task_id=args.task_id,
                    agent=args.agent,
                    reason_code=str(spawn.get("reasonCode") or ""),
                    detail=str(spawn.get("detail") or ""),
                )
            except Exception as err:
                backfill_result = {"ok": False, "skipped": True, "error": clip(str(err), 200)}
    knowledge_meta["backfill"] = backfill_result

    if isinstance(session_meta, dict) and session_meta:
        spawn["session"] = session_meta
    if isinstance(retry_context_pack, dict) and retry_context_pack:
        spawn["retryContext"] = retry_context_pack

    auto_close = bool(args.spawn and not spawn.get("skipped"))
    selection = getattr(args, "selection", None)
    ok = (
        bool(claimed.get("ok"))
        and bool(claim_send.get("ok"))
        and bool(task_send.get("ok"))
        and bool(close_apply.get("ok"))
        and bool(close_publish.get("ok"))
        and bool(worker_report.get("ok"))
    )
    result = {
        "ok": ok,
        "handled": True,
        "intent": "dispatch",
        "taskId": args.task_id,
        "agent": args.agent,
        "strategyId": str(selected_strategy.get("strategyId") or ""),
        "strategy": selected_strategy,
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
        "objectiveSource": objective_source,
        "knowledge": knowledge_meta,
        "collaboration": collab_summary_state,
        "session": session_meta,
        "retryContext": retry_context_pack,
    }
    if isinstance(selection, dict):
        result["selection"] = selection

    if args.spawn and not spawn.get("skipped"):
        spawn_metrics = spawn.get("metrics") if isinstance(spawn.get("metrics"), dict) else {}
        cycle_ms = nonneg_int(spawn_metrics.get("elapsedMs"), 0)
        decision = str(spawn.get("decision") or "").strip().lower()
        event_payload = {
            "taskId": args.task_id,
            "agent": args.agent,
            "decision": decision,
            "reasonCode": str(spawn.get("reasonCode") or ""),
            "cycleMs": cycle_ms,
            "autoClose": auto_close,
            "dispatchMode": "spawn",
        }
        if decision == "done":
            record_ops_event(args.root, "dispatch_done", event_payload)
        elif decision == "blocked":
            record_ops_event(args.root, "dispatch_blocked", event_payload)

        recovery_action = str(spawn.get("action") or "").strip().lower()
        if decision != "done" and recovery_action:
            recovery_payload = {
                "taskId": args.task_id,
                "agent": args.agent,
                "reasonCode": str(spawn.get("reasonCode") or ""),
                "recoveryAction": recovery_action,
                "recoveryState": str(spawn.get("recoveryState") or ""),
                "nextAssignee": str(spawn.get("nextAssignee") or ""),
            }
            if recovery_action == "retry":
                record_ops_event(args.root, "recovery_scheduled", recovery_payload)
            else:
                record_ops_event(args.root, "recovery_escalated", recovery_payload)

    return result


def cmd_dispatch(args: argparse.Namespace) -> int:
    result = dispatch_once(args)
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get("ok") else 1


def autopilot_once(args: argparse.Namespace) -> Dict[str, Any]:
    if args.actor != "orchestrator":
        return {"ok": False, "error": "autopilot is restricted to actor=orchestrator"}
    gate = governance.checkpoint_autopilot(args.root, args.actor)
    if not bool(gate.get("allowed")):
        max_steps = max(1, int(args.max_steps))
        out = {
            "ok": True,
            "handled": True,
            "intent": "autopilot",
            "maxSteps": max_steps,
            "stepsRun": 0,
            "summary": {"done": 0, "blocked": 0, "manual": 0},
            "stopReason": str(gate.get("reason") or "governance_blocked"),
            "visibilityMode": str(args.visibility_mode),
            "steps": [],
            "skipped": True,
            "reason": str(gate.get("reason") or "governance_blocked"),
            "governance": gate,
        }
        record_ops_event(
            args.root,
            "autopilot_cycle",
            {
                "maxSteps": max_steps,
                "stepsRun": 0,
                "done": 0,
                "blocked": 0,
                "manual": 0,
                "stopReason": str(out.get("stopReason") or "governance_blocked"),
                "skipped": True,
            },
        )
        return out
    max_steps = max(1, int(args.max_steps))
    steps: List[Dict[str, Any]] = []
    summary = {"done": 0, "blocked": 0, "manual": 0}
    stop_reason = "no_runnable_task"
    ok = True
    excluded_task_ids: set = set()

    for idx in range(max_steps):
        task = choose_task_for_run(args.root, "", excluded_task_ids=excluded_task_ids)
        if not isinstance(task, dict):
            stop_reason = "no_runnable_task"
            break
        task_id = str(task.get("taskId") or "").strip()
        selection = task.get("_prioritySelection") if isinstance(task.get("_prioritySelection"), dict) else {}
        if not task_id:
            stop_reason = "invalid_task"
            ok = False
            break

        agent = governance.canonical_agent(task.get("owner") or task.get("assigneeHint") or "coder") or "coder"
        if agent not in BOT_ROLES:
            agent = governance.canonical_agent(suggest_agent_from_title(str(task.get("title") or ""))) or "coder"

        d_args = argparse.Namespace(
            root=args.root,
            task_id=task_id,
            agent=agent,
            task="",
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
            selection=selection,
        )
        dispatch_result = dispatch_once(d_args)
        steps.append(
            {
                "index": idx + 1,
                "taskId": task_id,
                "agent": agent,
                "selection": selection,
                "dispatch": dispatch_result,
            }
        )

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
            spawn = dispatch_result.get("spawn") if isinstance(dispatch_result.get("spawn"), dict) else {}
            if str(spawn.get("reason") or "") == "cooldown_active":
                excluded_task_ids.add(task_id)
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
    record_ops_event(
        args.root,
        "autopilot_cycle",
        {
            "maxSteps": max_steps,
            "stepsRun": len(steps),
            "done": int(summary.get("done") or 0),
            "blocked": int(summary.get("blocked") or 0),
            "manual": int(summary.get("manual") or 0),
            "stopReason": stop_reason,
            "ok": bool(ok),
            "spawnEnabled": bool(args.spawn),
        },
    )
    return result


def cmd_autopilot(args: argparse.Namespace) -> int:
    result = autopilot_once(args)
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get("ok") else 1


def cmd_autopilot_runner(args: argparse.Namespace) -> int:
    if args.actor != "orchestrator":
        print(json.dumps({"ok": False, "error": "autopilot-runner is restricted to actor=orchestrator"}, ensure_ascii=True))
        return 1

    with autopilot_runtime_lock(args.root):
        state = load_autopilot_runtime_state(args.root)
        state.update(
            {
                "running": True,
                "pid": os.getpid(),
                "startedAt": now_iso(),
                "endedAt": "",
                "status": "running",
                "maxSteps": int(args.max_steps),
                "mode": str(args.mode or "send"),
                "spawnEnabled": bool(args.spawn),
                "visibilityMode": str(args.visibility_mode or DEFAULT_VISIBILITY_MODE),
                "stopReason": "",
                "error": "",
            }
        )
        save_autopilot_runtime_state(args.root, state)

    result: Dict[str, Any]
    stop_reason = ""
    error_text = ""
    status = "finished"
    try:
        result = autopilot_once(args)
        stop_reason = str(result.get("stopReason") or "")
        if not bool(result.get("ok")):
            status = "failed"
            if not stop_reason:
                stop_reason = "dispatch_failed"
    except Exception as err:
        status = "failed"
        stop_reason = "runner_exception"
        error_text = clip(str(err), 240)
        result = {
            "ok": False,
            "handled": True,
            "intent": "autopilot",
            "maxSteps": int(args.max_steps),
            "stepsRun": 0,
            "summary": {"done": 0, "blocked": 0, "manual": 0},
            "stopReason": stop_reason,
            "error": error_text,
            "visibilityMode": str(args.visibility_mode),
            "steps": [],
        }
    finally:
        with autopilot_runtime_lock(args.root):
            last = load_autopilot_runtime_state(args.root)
            last_result = {
                "ok": bool(result.get("ok")),
                "stepsRun": int(result.get("stepsRun") or 0),
                "stopReason": str(result.get("stopReason") or stop_reason),
                "summary": result.get("summary") if isinstance(result.get("summary"), dict) else {},
                "at": now_iso(),
            }
            last.update(
                {
                    "running": False,
                    "pid": 0,
                    "endedAt": now_iso(),
                    "status": status,
                    "stopReason": stop_reason or str(result.get("stopReason") or ""),
                    "error": error_text,
                    "lastResult": last_result,
                }
            )
            save_autopilot_runtime_state(args.root, last)

    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    done_count = int(summary.get("done") or 0)
    blocked_count = int(summary.get("blocked") or 0)
    manual_count = int(summary.get("manual") or 0)
    final_msg = (
        f"[TASK] autopilot 已结束 | stepsRun={int(result.get('stepsRun') or 0)} | "
        f"stopReason={str(result.get('stopReason') or '-')}"
        f" | done={done_count} | blocked={blocked_count} | manual={manual_count}"
    )
    notify = send_group_message(args.group_id, args.account_id, final_msg, args.mode)
    result["notify"] = notify
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
        gate = governance.checkpoint_scheduler(args.root, args.actor)
        if not bool(gate.get("allowed")):
            run_result = {
                "ok": True,
                "skipped": True,
                "reason": str(gate.get("reason") or "governance_blocked"),
                "governance": gate,
            }
        elif not state.get("enabled") and not force:
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
            run_result["skipped"] = bool(run_result.get("skipped"))
            if auto.get("ok") and not run_result["skipped"]:
                state["lastRunTs"] = now_ts
                state["lastRunAt"] = now_iso()
                state["nextDueTs"] = now_ts + int(state.get("intervalSec") or SCHEDULER_DEFAULT_INTERVAL_SEC)

    state = save_scheduler_state(args.root, state)
    ok = bool(run_result.get("ok"))
    out = {
        "ok": ok,
        "handled": True,
        "intent": "scheduler_run",
        "action": action,
        "state": state,
        "run": run_result,
        "skipped": bool(run_result.get("skipped")),
        "reason": str(run_result.get("reason") or ""),
    }
    if should_tick:
        run_obj = run_result if isinstance(run_result, dict) else {}
        record_ops_event(
            args.root,
            "scheduler_tick",
            {
                "action": action,
                "force": force,
                "skipped": bool(run_obj.get("skipped")),
                "reason": str(run_obj.get("reason") or ""),
                "stepsRun": int(run_obj.get("stepsRun") or 0),
                "enabled": bool(state.get("enabled")),
                "maxSteps": int(state.get("maxSteps") or SCHEDULER_DEFAULT_MAX_STEPS),
            },
        )
    return out


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

    thread_id = collaboration_thread_id(args.task_id, args.role)
    collab_log: Dict[str, Any] = {"ok": True, "skipped": True, "threadId": thread_id, "reason": "send_not_ok"}
    if sent.get("ok") and args.mode != "send":
        collab_log = {"ok": True, "skipped": True, "threadId": thread_id, "reason": "mode_not_send"}
    elif sent.get("ok"):
        created_at = now_iso()
        deadline_sec = max(600, normalize_timeout_sec(getattr(args, "cooldown_sec", 0), default=600))
        deadline = (
            datetime.now(timezone.utc) + timedelta(seconds=deadline_sec)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        collab_payload = {
            "taskId": str(args.task_id),
            "threadId": thread_id,
            "fromAgent": "orchestrator",
            "toAgent": str(args.role),
            "messageType": "question",
            "summary": clip(f"clarify -> {args.role}: {q}", 180),
            "evidence": [f"group:{args.group_id}", f"mode:{args.mode}"],
            "request": q,
            "deadline": deadline,
            "createdAt": created_at,
        }
        try:
            append_result = collaboration_hub.append_message(args.root, collab_payload)
        except Exception as err:
            LOGGER.warning(
                "failed to append collaboration clarify log: taskId=%s role=%s threadId=%s",
                args.task_id,
                args.role,
                thread_id,
                exc_info=True,
            )
            collab_log = {
                "ok": False,
                "threadId": thread_id,
                "reason": "append_exception",
                "error": clip(str(err), 200),
            }
        else:
            if append_result.get("ok"):
                collab_log = {
                    "ok": True,
                    "threadId": thread_id,
                    "messageType": "question",
                    "createdAt": created_at,
                    "deadline": deadline,
                }
            else:
                reason = (
                    str(append_result.get("reason") or "")
                    or str(append_result.get("error") or "")
                    or clip(json.dumps(append_result, ensure_ascii=False), 200)
                )
                collab_log = {
                    "ok": False,
                    "threadId": thread_id,
                    "reason": clip(reason, 200),
                    "append": append_result,
                }
    print(
        json.dumps(
            {
                "ok": bool(sent.get("ok")),
                "send": sent,
                "throttleKey": key,
                "globalThrottleKey": global_key,
                "collabLog": collab_log,
            },
            ensure_ascii=True,
        )
    )
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


def normalize_xhs_output_name(raw: str) -> str:
    name = str(raw or "").strip()
    if not name:
        return "untitled-paper"
    cleaned = XHS_OUTPUT_NAME_RE.sub("-", name).strip("._-")
    return cleaned or "untitled-paper"


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
    decomposed = task_decomposer.decompose_project(project_path, project_name, doc_text)
    tasks: List[Dict[str, Any]] = []
    for item in decomposed:
        if not isinstance(item, dict):
            continue
        title = clip(str(item.get("title") or ""), 120)
        if not title:
            continue
        owner_hint = governance.canonical_agent(str(item.get("ownerHint") or "")) or suggest_agent_from_title(title)
        depends_on = [str(dep).strip() for dep in (item.get("dependsOn") or []) if str(dep).strip()]
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence"))))
        except Exception:
            confidence = 0.0
        tasks.append(
            {
                "title": title,
                "ownerHint": owner_hint,
                "dependsOn": depends_on,
                "confidence": round(confidence, 2),
            }
        )
    if not tasks:
        for title in list(DEFAULT_PROJECT_BOOTSTRAP_TASKS):
            tasks.append(
                {
                    "title": clip(title, 120),
                    "ownerHint": suggest_agent_from_title(title),
                    "dependsOn": [],
                    "confidence": 0.55,
                }
            )

    confidence_values = [float(task.get("confidence") or 0.0) for task in tasks]
    confidence_summary = {
        "count": len(confidence_values),
        "min": round(min(confidence_values), 2) if confidence_values else 0.0,
        "max": round(max(confidence_values), 2) if confidence_values else 0.0,
        "avg": round((sum(confidence_values) / len(confidence_values)), 2) if confidence_values else 0.0,
    }

    return {
        "projectPath": project_path,
        "projectName": project_name,
        "docPath": doc_path,
        "tasks": [str(task.get("title") or "") for task in tasks],
        "decompositionTasks": tasks,
        "decompositionCount": len(tasks),
        "confidenceSummary": confidence_summary,
    }


def write_project_depends_on(root: str, created_tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not created_tasks:
        return {"updatedTasks": 0, "tasksWithDependsOn": 0}

    _, snapshot_path = ensure_state(root)
    data = load_snapshot(root)
    tasks_obj = data.get("tasks")
    if not isinstance(tasks_obj, dict):
        return {"updatedTasks": 0, "tasksWithDependsOn": 0}

    title_to_tid: Dict[str, str] = {}
    for item in created_tasks:
        tid = str(item.get("taskId") or "").strip()
        title = str(item.get("title") or "").strip()
        if not tid or not title:
            continue
        key = task_decomposer.normalize_task_title(title)
        if key and key not in title_to_tid:
            title_to_tid[key] = tid

    updated = 0
    linked = 0
    for item in created_tasks:
        tid = str(item.get("taskId") or "").strip()
        if not tid:
            continue
        task = tasks_obj.get(tid)
        if not isinstance(task, dict):
            continue
        dep_ids: List[str] = []
        for dep_title in item.get("dependsOn") or []:
            dep_key = task_decomposer.normalize_task_title(str(dep_title))
            dep_tid = title_to_tid.get(dep_key, "")
            if dep_tid and dep_tid != tid and dep_tid not in dep_ids:
                dep_ids.append(dep_tid)
        task["dependsOn"] = dep_ids
        task["updatedAt"] = now_iso()
        updated += 1
        if dep_ids:
            linked += 1

    if updated > 0:
        save_json_file(snapshot_path, data)
    return {"updatedTasks": updated, "tasksWithDependsOn": linked}


def read_xhs_stage_template(template_file: str) -> str:
    path = os.path.join(XHS_TEMPLATE_DIR, template_file)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def render_xhs_stage_prompt(template_text: str, values: Dict[str, str]) -> str:
    placeholders = set(XHS_PLACEHOLDER_RE.findall(template_text or ""))
    unknown = sorted([key for key in placeholders if key not in XHS_ALLOWED_PLACEHOLDERS])
    if unknown:
        raise ValueError(f"xhs template has unsupported placeholders: {', '.join(unknown)}")

    rendered = template_text
    for key in sorted(placeholders):
        rendered = rendered.replace("{" + key + "}", str(values.get(key, "")))
    return rendered.strip()


def write_task_dependency_chain(root: str, task_ids: List[str]) -> Dict[str, Any]:
    if not task_ids:
        return {"updatedTasks": 0, "tasksWithDependsOn": 0}
    data = load_snapshot(root)
    tasks_obj = data.get("tasks")
    if not isinstance(tasks_obj, dict):
        return {"updatedTasks": 0, "tasksWithDependsOn": 0}

    updated = 0
    linked = 0
    for idx, task_id in enumerate(task_ids):
        task = tasks_obj.get(task_id)
        if not isinstance(task, dict):
            continue
        dep_ids: List[str] = []
        if idx > 0 and task_ids[idx - 1]:
            dep_ids.append(task_ids[idx - 1])
        task["dependsOn"] = dep_ids
        task["updatedAt"] = now_iso()
        updated += 1
        if dep_ids:
            linked += 1

    if updated > 0:
        _, snapshot_path = ensure_state(root)
        save_json_file(snapshot_path, data)
    return {"updatedTasks": updated, "tasksWithDependsOn": linked}


def xhs_bootstrap_once(args: argparse.Namespace) -> Dict[str, Any]:
    actor = str(getattr(args, "actor", "orchestrator") or "orchestrator").strip() or "orchestrator"
    if actor != "orchestrator":
        return {"ok": False, "handled": True, "intent": "xhs_bootstrap", "error": "xhs-bootstrap is restricted to actor=orchestrator"}

    paper_id = str(getattr(args, "paper_id", "") or "").strip()
    if not paper_id:
        return {"ok": False, "handled": True, "intent": "xhs_bootstrap", "error": "paper_id is required"}

    workflow_root = normalize_project_path(str(getattr(args, "workflow_root", "") or DEFAULT_XHS_WORKFLOW_ROOT))
    if not workflow_root or not os.path.isdir(workflow_root):
        return {
            "ok": False,
            "handled": True,
            "intent": "xhs_bootstrap",
            "error": f"workflow root not found: {clip(workflow_root or str(getattr(args, 'workflow_root', '') or ''), 200)}",
        }

    pdf_path = normalize_project_path(str(getattr(args, "pdf_path", "") or ""))
    if not pdf_path or not os.path.isfile(pdf_path):
        return {
            "ok": False,
            "handled": True,
            "intent": "xhs_bootstrap",
            "error": f"pdf path not found: {clip(pdf_path or str(getattr(args, 'pdf_path', '') or ''), 200)}",
        }

    run_dir_raw = str(getattr(args, "run_dir", "") or "").strip()
    paper_dir = normalize_xhs_output_name(paper_id)
    run_dir = normalize_project_path(run_dir_raw) if run_dir_raw else os.path.join(DEFAULT_XHS_OUTPUT_ROOT, paper_dir)
    os.makedirs(run_dir, exist_ok=True)

    context_marker = os.path.join(run_dir, XHS_CONTEXT_MARKER_FILE)
    context_obj = {
        "workflowName": XHS_WORKFLOW_NAME,
        "paperId": paper_id,
        "workflowRoot": workflow_root,
        "runDir": run_dir,
        "pdfPath": pdf_path,
        "bootstrappedAt": now_iso(),
    }
    save_json_file(context_marker, context_obj)

    values = {
        "paper_id": paper_id,
        "workflow_root": workflow_root,
        "run_dir": run_dir,
        "pdf_path": pdf_path,
    }
    project_name = f"{XHS_WORKFLOW_NAME}:{paper_id}"

    created: List[Dict[str, Any]] = []
    created_ids: List[str] = []
    for stage in XHS_STAGE_DEFINITIONS:
        stage_id = str(stage.get("stageId") or "").strip()
        stage_title = str(stage.get("title") or "").strip()
        owner_hint = governance.canonical_agent(str(stage.get("ownerHint") or "")) or "coder"
        template_file = str(stage.get("templateFile") or "").strip()

        try:
            template = read_xhs_stage_template(template_file)
            dispatch_prompt = render_xhs_stage_prompt(template, values)
        except Exception as err:
            return {
                "ok": False,
                "handled": True,
                "intent": "xhs_bootstrap",
                "error": f"failed to load stage template {template_file}: {clip(str(err), 200)}",
            }

        title = clip(f"[{project_name}] Stage {stage_id}: {stage_title}", 120)
        apply_obj = board_apply(args.root, "orchestrator", f"@{owner_hint} create task: {title}")
        publish = publish_apply_result(
            args.root,
            "orchestrator",
            apply_obj,
            args.group_id,
            args.account_id,
            args.mode,
            allow_broadcaster=False,
        )

        task_id = ""
        if isinstance(apply_obj, dict) and apply_obj.get("ok"):
            task_id = str(apply_obj.get("taskId") or "").strip()
        if task_id:
            created_ids.append(task_id)
            bind_task_project_context(args.root, task_id, workflow_root, project_name, dispatch_prompt=dispatch_prompt)
        created.append(
            {
                "stageId": stage_id,
                "ownerHint": owner_hint,
                "templateFile": template_file,
                "taskId": task_id,
                "apply": apply_obj,
                "publish": publish,
            }
        )

    depends_sync = write_task_dependency_chain(args.root, created_ids)

    kickoff: Dict[str, Any] = {"ok": True, "skipped": True, "reason": "no task created"}
    first_task = next((task_id for task_id in created_ids if task_id), "")
    if first_task:
        first_obj = get_task(args.root, first_task) or {}
        d_args = argparse.Namespace(
            root=args.root,
            task_id=first_task,
            agent=str(first_obj.get("assigneeHint") or "coder"),
            task="",
            actor="orchestrator",
            session_id=args.session_id,
            group_id=args.group_id,
            account_id=args.account_id,
            mode=args.mode,
            timeout_sec=args.timeout_sec,
            spawn=bool(getattr(args, "spawn", False)),
            spawn_cmd=args.spawn_cmd,
            spawn_output=args.spawn_output,
            visibility_mode=str(getattr(args, "visibility_mode", DEFAULT_VISIBILITY_MODE) or DEFAULT_VISIBILITY_MODE),
        )
        kickoff = dispatch_once(d_args)

    msg = (
        f"[TASK] XHS workflow bootstrapped: {paper_id} | created={len(created_ids)} | "
        f"run_dir={run_dir}"
    )
    ack = send_group_message(args.group_id, args.account_id, msg, args.mode)
    ok = (
        all(c.get("apply", {}).get("ok") and c.get("publish", {}).get("ok") for c in created)
        and bool(ack.get("ok"))
        and bool(kickoff.get("ok"))
    )

    return {
        "ok": ok,
        "handled": True,
        "intent": "xhs_bootstrap",
        "paperId": paper_id,
        "workflowRoot": workflow_root,
        "runDir": run_dir,
        "pdfPath": pdf_path,
        "contextMarker": context_marker,
        "createdCount": len(created_ids),
        "createdTaskIds": created_ids,
        "created": created,
        "dependsOnSync": depends_sync,
        "bootstrap": kickoff,
        "ack": ack,
    }


def cmd_xhs_bootstrap(args: argparse.Namespace) -> int:
    result = xhs_bootstrap_once(args)
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get("ok") else 1


def trigger_xhs_n8n_once(args: argparse.Namespace) -> Dict[str, Any]:
    actor = str(getattr(args, "actor", "orchestrator") or "orchestrator").strip() or "orchestrator"
    if actor != "orchestrator":
        return {"ok": False, "handled": True, "intent": "xhs_n8n_trigger", "error": "xhs n8n trigger is restricted to actor=orchestrator"}

    paper_id = str(getattr(args, "paper_id", "") or "").strip()
    if not paper_id:
        return {"ok": False, "handled": True, "intent": "xhs_n8n_trigger", "error": "paper_id is required"}

    raw_pdf_path = str(getattr(args, "pdf_path", "") or "").strip()
    pdf_path = normalize_project_path(raw_pdf_path)
    if not pdf_path or not os.path.isfile(pdf_path):
        return {
            "ok": False,
            "handled": True,
            "intent": "xhs_n8n_trigger",
            "error": f"pdf path not found: {clip(pdf_path or raw_pdf_path, 200)}",
        }

    trigger_script = normalize_project_path(str(getattr(args, "n8n_trigger_script", "") or DEFAULT_XHS_N8N_TRIGGER_SCRIPT))
    if not trigger_script:
        return {"ok": False, "handled": True, "intent": "xhs_n8n_trigger", "error": "n8n trigger script path is empty"}

    cmd = [
        trigger_script,
        paper_id,
        pdf_path,
        str(getattr(args, "group_id", DEFAULT_GROUP_ID) or DEFAULT_GROUP_ID),
        str(getattr(args, "account_id", DEFAULT_ACCOUNT_ID) or DEFAULT_ACCOUNT_ID),
    ]

    mode = str(getattr(args, "mode", "send") or "send")
    if mode == "dry-run":
        return {
            "ok": True,
            "handled": True,
            "intent": "xhs_n8n_trigger",
            "dryRun": True,
            "paperId": paper_id,
            "pdfPath": pdf_path,
            "plannedCommand": cmd,
        }

    if not os.path.isfile(trigger_script):
        err_msg = f"trigger script not found: {clip(trigger_script, 200)}"
        send = send_group_message(
            str(getattr(args, "group_id", DEFAULT_GROUP_ID) or DEFAULT_GROUP_ID),
            str(getattr(args, "account_id", DEFAULT_ACCOUNT_ID) or DEFAULT_ACCOUNT_ID),
            f"[BLOCKED] n8n 触发失败 | paper_id={paper_id} | {err_msg}",
            mode,
        )
        return {
            "ok": False,
            "handled": True,
            "intent": "xhs_n8n_trigger",
            "paperId": paper_id,
            "pdfPath": pdf_path,
            "error": err_msg,
            "send": send,
        }

    timeout_sec = int(getattr(args, "timeout_sec", 0) or 0)
    run_timeout = max(15, timeout_sec) if timeout_sec > 0 else 30
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=run_timeout)
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    response_obj: Dict[str, Any] = {}
    if stdout:
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                response_obj = parsed
        except Exception:
            response_obj = {"raw": clip(stdout, 400)}

    if proc.returncode != 0:
        detail = clip(stderr or stdout or f"exit={proc.returncode}", 200)
        send = send_group_message(
            str(getattr(args, "group_id", DEFAULT_GROUP_ID) or DEFAULT_GROUP_ID),
            str(getattr(args, "account_id", DEFAULT_ACCOUNT_ID) or DEFAULT_ACCOUNT_ID),
            f"[BLOCKED] n8n 触发失败 | paper_id={paper_id} | {detail}",
            mode,
        )
        return {
            "ok": False,
            "handled": True,
            "intent": "xhs_n8n_trigger",
            "paperId": paper_id,
            "pdfPath": pdf_path,
            "exitCode": proc.returncode,
            "error": detail,
            "stdout": clip(stdout, 400),
            "stderr": clip(stderr, 400),
            "send": send,
        }

    return {
        "ok": True,
        "handled": True,
        "intent": "xhs_n8n_trigger",
        "dryRun": False,
        "paperId": paper_id,
        "pdfPath": pdf_path,
        "command": cmd,
        "response": response_obj if response_obj else {"raw": clip(stdout, 400)},
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
        "3) @orchestrator 开始xhs流程n8n <paper_id> <pdf_path>",
        "4) @orchestrator 自动推进 开 [N] | 关 | 状态",
        "5) @orchestrator run / autopilot / dispatch 仍可继续使用",
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


def bind_task_project_context(
    root: str,
    task_id: str,
    project_path: str,
    project_name: str,
    dispatch_prompt: str = "",
) -> None:
    if not task_id:
        return
    state = load_task_context_state(root)
    tasks = state.setdefault("tasks", {})
    entry = tasks.get(task_id)
    if not isinstance(entry, dict):
        entry = {}
    if project_path:
        entry["projectPath"] = project_path
    if project_name:
        entry["projectName"] = project_name
    if dispatch_prompt:
        entry["dispatchPrompt"] = str(dispatch_prompt)
    entry["updatedAt"] = now_iso()
    tasks[task_id] = entry
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


def lookup_task_dispatch_prompt(root: str, task_id: str) -> str:
    if not task_id:
        return ""
    state = load_task_context_state(root)
    tasks = state.get("tasks", {})
    entry = tasks.get(task_id)
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("dispatchPrompt") or "").strip()


def _load_json_dict_or_empty(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _normalize_executor_name(raw: Any) -> str:
    token = str(raw or "").strip().lower()
    return token if token in SUPPORTED_SPAWN_EXECUTORS else ""


def load_executor_routing(root: str) -> Dict[str, str]:
    routing = dict(DEFAULT_EXECUTOR_ROUTING)
    script_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    search_roots = [script_root, root]

    for base in search_roots:
        for rel in RUNTIME_POLICY_CONFIG_CANDIDATES:
            path = os.path.join(base, rel)
            if not os.path.exists(path):
                continue
            policy = _load_json_dict_or_empty(path)
            orchestrator = policy.get("orchestrator")
            if not isinstance(orchestrator, dict):
                continue
            overrides = orchestrator.get("executorRouting")
            if not isinstance(overrides, dict):
                continue
            for role_raw, executor_raw in overrides.items():
                role_token = str(role_raw or "").strip()
                if not role_token:
                    continue
                if role_token in {"*", "default"}:
                    role = role_token
                else:
                    role = governance.canonical_agent(role_token) or role_token.lower()
                executor = _normalize_executor_name(executor_raw)
                if not executor:
                    continue
                routing[role] = executor
    return routing


def resolve_spawn_executor(root: str, agent: str) -> str:
    routing = load_executor_routing(root)
    role = governance.canonical_agent(agent) or str(agent or "").strip().lower()
    if role and role in routing:
        return routing[role]
    for fallback_key in ("default", "*"):
        fallback = routing.get(fallback_key)
        if fallback in SUPPORTED_SPAWN_EXECUTORS:
            return str(fallback)
    return SPAWN_EXECUTOR_OPENCLAW


WRITING_TASK_KEYWORDS = (
    "xhs",
    "draft",
    "citation",
    "fact check",
    "style-notes",
    "title variants",
    "cover copy",
    "post assembly",
    "weekly review synthesis",
    "reproduction report",
    "artifact package",
    "文案",
    "写作",
    "小红书",
    "标题",
    "封面",
    "图文",
    "核查",
    "润色",
    "发布",
)


def is_writing_task(task_prompt: str) -> bool:
    text = str(task_prompt or "").strip().lower()
    if not text:
        return False
    return any(keyword in text for keyword in WRITING_TASK_KEYWORDS)


def render_spawn_template(template: str, values: Dict[str, Any]) -> List[str]:
    rendered = template
    for key, raw in values.items():
        rendered = rendered.replace("{" + key + "}", shlex.quote(str(raw)))
    return shlex.split(rendered)


def append_spawn_workspace_arg(command: List[str], workspace: str) -> List[str]:
    ws = str(workspace or "").strip()
    if ws:
        command.extend(["--workspace", ws])
    return command


def resolve_spawn_plan(args: argparse.Namespace, task_prompt: str) -> Dict[str, Any]:
    timeout_sec = normalize_timeout_sec(getattr(args, "timeout_sec", 0), default=0)
    codex_bridge = os.path.join(os.path.dirname(__file__), "codex_worker_bridge.py")
    claude_bridge = os.path.join(os.path.dirname(__file__), "claude_worker_bridge.py")
    selected_executor = resolve_spawn_executor(args.root, str(args.agent or ""))
    if is_writing_task(task_prompt):
        selected_executor = SPAWN_EXECUTOR_CLAUDE
    selected_bridge = codex_bridge if selected_executor == SPAWN_EXECUTOR_CODEX else claude_bridge
    values = {
        "root": args.root,
        "task_id": args.task_id,
        "agent": args.agent,
        "task": task_prompt,
        "timeout_sec": timeout_sec,
        "bridge": selected_bridge,
        "codex_bridge": codex_bridge,
        "claude_bridge": claude_bridge,
    }

    raw_spawn_cmd = str(getattr(args, "spawn_cmd", "") or "").strip()
    if raw_spawn_cmd:
        return {
            "executor": "custom",
            "command": render_spawn_template(raw_spawn_cmd, values),
            "template": raw_spawn_cmd,
        }

    spawn_workspace = str(getattr(args, "workspace", "") or "").strip()

    if selected_executor == SPAWN_EXECUTOR_CLAUDE:
        template = "python3 {claude_bridge} --root {root} --task-id {task_id} --agent {agent} --task {task} --timeout-sec {timeout_sec}"
        return {
            "executor": SPAWN_EXECUTOR_CLAUDE,
            "command": append_spawn_workspace_arg(render_spawn_template(template, values), spawn_workspace),
            "template": template,
        }

    if selected_executor == SPAWN_EXECUTOR_CODEX:
        template = "python3 {bridge} --root {root} --task-id {task_id} --agent {agent} --task {task} --timeout-sec {timeout_sec}"
        return {
            "executor": SPAWN_EXECUTOR_CODEX,
            "command": append_spawn_workspace_arg(render_spawn_template(template, values), spawn_workspace),
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
    ]
    if timeout_sec > 0:
        command.extend(["--timeout", str(timeout_sec)])
    return {
        "executor": SPAWN_EXECUTOR_OPENCLAW,
        "command": command,
        "template": "",
    }


FALLBACK_RUNNABLE_STATUSES = {"pending", "claimed", "in_progress", "review"}
FALLBACK_STATUS_BONUS = {
    "pending": 0.0,
    "claimed": 2.0,
    "in_progress": 3.0,
    "review": 1.0,
}
FALLBACK_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$", flags=re.IGNORECASE)


def _fallback_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _fallback_normalize_task_id(value: Any) -> str:
    return _fallback_text(value).upper()


def _fallback_number(value: Any, default: float = 0.0) -> float:
    fallback = float(default)
    if value is None:
        return fallback
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        num = float(value)
        return num if math.isfinite(num) else fallback
    text = _fallback_text(value)
    if not text:
        return fallback
    try:
        num = float(text)
        return num if math.isfinite(num) else fallback
    except Exception:
        return fallback


def _fallback_dedupe(values: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in values:
        token = _fallback_text(raw)
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _fallback_normalize_refs(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return _fallback_dedupe([_fallback_text(item) for item in raw])
    if isinstance(raw, dict):
        refs: List[str] = []
        for key in ("taskId", "id", "ref", "value"):
            if key in raw:
                refs.extend(_fallback_normalize_refs(raw.get(key)))
        return _fallback_dedupe(refs)
    text = _fallback_text(raw)
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
            return _fallback_normalize_refs(parsed)
        except Exception:
            pass
    tokens = re.split(r"[\s,;]+", text)
    return _fallback_dedupe(tokens)


def _fallback_status(task: Any) -> str:
    if not isinstance(task, dict):
        return ""
    return _fallback_text(task.get("status")).lower()


def _fallback_task_is_ready(task: Dict[str, Any], tasks_by_id: Dict[str, Dict[str, Any]]) -> bool:
    status = _fallback_status(task)
    if status not in FALLBACK_RUNNABLE_STATUSES:
        return False

    for dep in _fallback_normalize_refs(task.get("dependsOn")):
        dep_id = _fallback_normalize_task_id(dep)
        dep_task = tasks_by_id.get(dep_id)
        if not isinstance(dep_task, dict):
            return False
        if _fallback_status(dep_task) != "done":
            return False

    for blocker in _fallback_normalize_refs(task.get("blockedBy")):
        token = _fallback_text(blocker)
        if not token:
            continue
        blocker_id = _fallback_normalize_task_id(token)
        if FALLBACK_TASK_ID_PATTERN.match(blocker_id):
            ref_task = tasks_by_id.get(blocker_id)
            if isinstance(ref_task, dict):
                if _fallback_status(ref_task) != "done":
                    return False
                continue
        # Non-task text blockers remain blocking in fallback.
        return False

    return True


def _fallback_score(task: Dict[str, Any]) -> float:
    status = _fallback_status(task)
    priority = _fallback_number(task.get("priority"), 0.0)
    impact = _fallback_number(task.get("impact"), 0.0)
    raw = (priority * 10.0) + (impact * 5.0) + FALLBACK_STATUS_BONUS.get(status, 0.0)
    return round(raw, 6) if math.isfinite(raw) else 0.0


def _fallback_prepare_tasks(raw_tasks: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    normalized: Dict[str, Dict[str, Any]] = {}
    for raw in (raw_tasks or {}).values():
        if not isinstance(raw, dict):
            continue
        task_id = _fallback_normalize_task_id(raw.get("taskId"))
        if not task_id:
            continue
        normalized[task_id] = raw
    return normalized


def _attach_fallback_selection(
    task: Dict[str, Any],
    score: float,
    reason_code: str,
    reason: str,
    ready_queue: List[Dict[str, Any]],
) -> Dict[str, Any]:
    out = dict(task)
    out["_prioritySelection"] = {
        "taskId": str(task.get("taskId") or ""),
        "score": score,
        "reasonCode": reason_code,
        "reason": reason,
        "readyQueueSize": len(ready_queue),
        "readyQueueTop": ready_queue[:3],
    }
    return out


def choose_task_for_run(root: str, requested: str, excluded_task_ids: Optional[set] = None) -> Optional[Dict[str, Any]]:
    data = load_snapshot(root)
    tasks = data.get("tasks", {})
    excluded = excluded_task_ids or set()
    try:
        selected = priority_engine.select_task(tasks, requested_task_id=requested, excluded_task_ids=excluded)
        picked = selected.get("selectedTask")
        if not isinstance(picked, dict):
            return None
        out = dict(picked)
        sel = selected.get("selection") if isinstance(selected.get("selection"), dict) else {}
        ready_queue = selected.get("readyQueue") if isinstance(selected.get("readyQueue"), list) else []
        out["_prioritySelection"] = {
            "taskId": str(sel.get("taskId") or out.get("taskId") or ""),
            "score": sel.get("score"),
            "reasonCode": str(sel.get("reasonCode") or ""),
            "reason": str(sel.get("reason") or ""),
            "readyQueueSize": len(ready_queue),
            "readyQueueTop": ready_queue[:3],
        }
        return out
    except Exception:
        # Backward-compatible fallback path if priority engine fails unexpectedly.
        # Keep fallback dependency-aware; only ready tasks are eligible.
        normalized_tasks = _fallback_prepare_tasks(tasks)
        excluded_norm = {_fallback_normalize_task_id(tid) for tid in excluded if _fallback_text(tid)}
        ready_queue: List[Dict[str, Any]] = []

        for task_id in sorted(normalized_tasks.keys()):
            if task_id in excluded_norm:
                continue
            task = normalized_tasks[task_id]
            if not _fallback_task_is_ready(task, normalized_tasks):
                continue
            score = _fallback_score(task)
            ready_queue.append(
                {
                    "taskId": str(task.get("taskId") or task_id),
                    "score": score,
                    "reasonCode": "fallback_ready_scored",
                    "reason": "selected by dependency-aware fallback",
                }
            )

        ready_queue.sort(key=lambda row: (-_fallback_number(row.get("score"), 0.0), _fallback_normalize_task_id(row.get("taskId"))))

        if requested:
            requested_id = _fallback_normalize_task_id(requested)
            task = normalized_tasks.get(requested_id)
            if not isinstance(task, dict) or requested_id in excluded_norm:
                return None
            if not _fallback_task_is_ready(task, normalized_tasks):
                return None
            score = _fallback_score(task)
            return _attach_fallback_selection(
                task,
                score,
                "requested_task_selected_fallback",
                "requested task selected by dependency-aware fallback",
                ready_queue,
            )

        if not ready_queue:
            return None

        selected_id = _fallback_normalize_task_id(ready_queue[0].get("taskId"))
        selected_task = normalized_tasks.get(selected_id)
        if not isinstance(selected_task, dict):
            return None
        return _attach_fallback_selection(
            selected_task,
            _fallback_number(ready_queue[0].get("score"), 0.0),
            "selected_from_fallback_ready_queue",
            "selected top ready task from dependency-aware fallback queue",
            ready_queue,
        )


def _normalize_acceptance_evidence(text: str, structured_report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    normalized = evidence_normalizer.normalize_evidence(structured_report or {}, text or "")
    hard_evidence = normalize_string_list(normalized.get("hardEvidence"), limit=12, item_limit=260)
    soft_evidence = normalize_string_list(normalized.get("softEvidence"), limit=12, item_limit=220)
    normalized_text = str(normalized.get("normalizedText") or text or "").strip()
    return {
        "hardEvidence": hard_evidence,
        "softEvidence": soft_evidence,
        "normalizedText": normalized_text,
    }


def has_evidence(text: str, structured_report: Optional[Dict[str, Any]] = None) -> bool:
    normalized = _normalize_acceptance_evidence(text, structured_report=structured_report)
    return bool(normalized.get("hardEvidence"))


def looks_stage_only(text: str, structured_report: Optional[Dict[str, Any]] = None) -> bool:
    lower = (text or "").lower()
    has_stage = any(h.lower() in lower for h in STAGE_ONLY_HINTS)
    return has_stage and not has_evidence(text, structured_report=structured_report)


def parse_wakeup_kind(text: str) -> str:
    lower = text.lower()
    if any(h.lower() in lower for h in BLOCKED_HINTS):
        return "blocked"
    if any(h.lower() in lower for h in DONE_HINTS):
        return "done"
    return "progress"


def has_failure_signal(text: str) -> bool:
    normalized = (text or "").strip()
    if not normalized:
        return False
    stripped = ZERO_FAILURE_COUNTER_RE.sub("", normalized)
    for pattern in FAILED_SIGNAL_PATTERNS:
        if pattern.search(stripped):
            return True
    return False


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
    search_roots = [script_root, root]
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


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _normalize_verify_commands(global_conf: Dict[str, Any], role_conf: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    default_timeout = max(
        0,
        _to_int(
            global_conf.get("verifyTimeoutSec", global_conf.get("evidenceTimeoutSec", 20)),
            20,
        ),
    )
    raw_entries: List[Any] = []
    for source in (global_conf.get("verifyCommands"), role_conf.get("verifyCommands")):
        if isinstance(source, list):
            raw_entries.extend(source)

    for entry in raw_entries:
        if isinstance(entry, str):
            cmd = entry.strip()
            if not cmd:
                continue
            out.append(
                {
                    "cmd": cmd,
                    "expectExitCode": 0,
                    "timeoutSec": default_timeout,
                }
            )
            continue
        if isinstance(entry, dict):
            cmd = str(entry.get("cmd") or "").strip()
            if not cmd:
                continue
            out.append(
                {
                    "cmd": cmd,
                    "expectExitCode": _to_int(entry.get("expectExitCode", 0), 0),
                    "timeoutSec": max(0, _to_int(entry.get("timeoutSec"), default_timeout)),
                }
            )
    return out


def _run_verify_commands(root: str, commands: List[Dict[str, Any]]) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    for idx, item in enumerate(commands):
        cmd = str(item.get("cmd") or "").strip()
        expect = _to_int(item.get("expectExitCode"), 0)
        timeout_sec = max(0, _to_int(item.get("timeoutSec"), 20))
        if not cmd:
            continue
        started = time.time()
        try:
            proc = subprocess.run(
                ["/bin/sh", "-lc", cmd],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                timeout=(None if timeout_sec <= 0 else timeout_sec),
            )
        except subprocess.TimeoutExpired:
            duration_ms = int((time.time() - started) * 1000)
            reason = f"verify command timeout: `{cmd}` timeout={timeout_sec}s"
            results.append(
                {
                    "index": idx,
                    "cmd": cmd,
                    "ok": False,
                    "timeout": True,
                    "timeoutSec": timeout_sec,
                    "durationMs": duration_ms,
                }
            )
            return {"ok": False, "reason": reason, "results": results}

        duration_ms = int((time.time() - started) * 1000)
        one = {
            "index": idx,
            "cmd": cmd,
            "ok": proc.returncode == expect,
            "exitCode": proc.returncode,
            "expectExitCode": expect,
            "timeoutSec": timeout_sec,
            "durationMs": duration_ms,
            "stdout": clip(proc.stdout or "", 200),
            "stderr": clip(proc.stderr or "", 200),
        }
        results.append(one)
        if proc.returncode != expect:
            reason = f"verify command failed: `{cmd}` exit={proc.returncode} expected={expect}"
            return {"ok": False, "reason": reason, "results": results}
    return {"ok": True, "reason": "verify commands passed", "results": results}


def evaluate_acceptance(
    root: str,
    role: str,
    text: str,
    structured_report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    note = (text or "").strip()
    policy = load_acceptance_policy(root)
    global_conf = policy.get("global") if isinstance(policy, dict) else {}
    role_conf = (policy.get("roles") or {}).get(role) if isinstance(policy, dict) else {}
    if not isinstance(global_conf, dict):
        global_conf = {}
    if not isinstance(role_conf, dict):
        role_conf = {}

    evidence_ctx = _normalize_acceptance_evidence(note, structured_report=structured_report)
    hard_evidence = normalize_string_list(evidence_ctx.get("hardEvidence"), limit=12, item_limit=260)
    soft_evidence = normalize_string_list(evidence_ctx.get("softEvidence"), limit=12, item_limit=220)
    normalized_note = str(evidence_ctx.get("normalizedText") or note).strip()

    if has_failure_signal(normalized_note):
        return {
            "ok": False,
            "reasonCode": "failure_signal_detected",
            "reason": "检测到失败信号，done 验收被阻断。",
            "hardEvidence": hard_evidence,
            "softEvidence": soft_evidence,
            "normalizedText": normalized_note,
        }

    require_evidence = bool(global_conf.get("requireEvidence", True))
    if require_evidence and not hard_evidence:
        return {
            "ok": False,
            "reasonCode": "missing_hard_evidence",
            "reason": "缺少硬证据（URL/文件路径或文件名/测试通过痕迹）。",
            "hardEvidence": hard_evidence,
            "softEvidence": soft_evidence,
            "normalizedText": normalized_note,
        }

    if looks_stage_only(normalized_note, structured_report=structured_report):
        return {
            "ok": False,
            "reasonCode": "stage_only",
            "reason": "仅包含阶段性描述，未给出最终验收结果。",
            "hardEvidence": hard_evidence,
            "softEvidence": soft_evidence,
            "normalizedText": normalized_note,
        }

    required_any = role_conf.get("requireAny")
    if isinstance(required_any, list) and required_any:
        lower = normalized_note.lower()
        wanted = [str(x).strip() for x in required_any if str(x).strip()]
        matched = [kw for kw in wanted if kw.lower() in lower]
        if not matched:
            return {
                "ok": False,
                "reasonCode": "role_policy_missing_keyword",
                "reason": f"{role} 交付缺少验收关键词（至少包含其一：{', '.join(wanted[:6])}）。",
                "hardEvidence": hard_evidence,
                "softEvidence": soft_evidence,
                "normalizedText": normalized_note,
            }

    verify_commands = _normalize_verify_commands(global_conf, role_conf)
    verify_result = _run_verify_commands(root, verify_commands) if verify_commands else {"ok": True, "results": []}
    if not verify_result.get("ok"):
        return {
            "ok": False,
            "reasonCode": "verify_command_failed",
            "reason": str(verify_result.get("reason") or "verify command failed"),
            "hardEvidence": hard_evidence,
            "softEvidence": soft_evidence,
            "normalizedText": normalized_note,
            "verify": verify_result,
        }

    return {
        "ok": True,
        "reasonCode": "accepted",
        "reason": "通过验收策略",
        "hardEvidence": hard_evidence,
        "softEvidence": soft_evidence,
        "normalizedText": normalized_note,
        "verify": verify_result,
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


def _pid_is_running(pid: Any) -> bool:
    try:
        p = int(pid)
    except Exception:
        return False
    if p <= 0:
        return False
    try:
        os.kill(p, 0)
        return True
    except Exception:
        return False


def autopilot_runtime_state_path(root: str) -> str:
    return os.path.join(root, "state", AUTOPILOT_RUNTIME_STATE_FILE)


def autopilot_runtime_lock_path(root: str) -> str:
    return os.path.join(root, "state", AUTOPILOT_RUNTIME_LOCK_FILE)


def load_autopilot_runtime_state(root: str) -> Dict[str, Any]:
    data = load_json_file(
        autopilot_runtime_state_path(root),
        {
            "running": False,
            "pid": 0,
            "startedAt": "",
            "endedAt": "",
            "updatedAt": "",
            "status": "idle",
            "maxSteps": 0,
            "mode": "",
            "spawnEnabled": False,
            "visibilityMode": "",
            "stopReason": "",
            "error": "",
            "logPath": "",
            "lastResult": {},
        },
    )
    return {
        "running": bool(data.get("running")),
        "pid": int(data.get("pid") or 0),
        "startedAt": str(data.get("startedAt") or ""),
        "endedAt": str(data.get("endedAt") or ""),
        "updatedAt": str(data.get("updatedAt") or ""),
        "status": str(data.get("status") or "idle"),
        "maxSteps": int(data.get("maxSteps") or 0),
        "mode": str(data.get("mode") or ""),
        "spawnEnabled": bool(data.get("spawnEnabled")),
        "visibilityMode": str(data.get("visibilityMode") or ""),
        "stopReason": str(data.get("stopReason") or ""),
        "error": str(data.get("error") or ""),
        "logPath": str(data.get("logPath") or ""),
        "lastResult": data.get("lastResult") if isinstance(data.get("lastResult"), dict) else {},
    }


def save_autopilot_runtime_state(root: str, state: Dict[str, Any]) -> Dict[str, Any]:
    normalized = {
        "running": bool(state.get("running")),
        "pid": int(state.get("pid") or 0),
        "startedAt": str(state.get("startedAt") or ""),
        "endedAt": str(state.get("endedAt") or ""),
        "updatedAt": now_iso(),
        "status": str(state.get("status") or "idle"),
        "maxSteps": int(state.get("maxSteps") or 0),
        "mode": str(state.get("mode") or ""),
        "spawnEnabled": bool(state.get("spawnEnabled")),
        "visibilityMode": str(state.get("visibilityMode") or ""),
        "stopReason": str(state.get("stopReason") or ""),
        "error": str(state.get("error") or ""),
        "logPath": str(state.get("logPath") or ""),
        "lastResult": state.get("lastResult") if isinstance(state.get("lastResult"), dict) else {},
    }
    save_json_file(autopilot_runtime_state_path(root), normalized)
    return normalized


@contextmanager
def autopilot_runtime_lock(root: str):
    lock_path = autopilot_runtime_lock_path(root)
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def build_autopilot_runner_cmd(
    args: argparse.Namespace,
    max_steps: int,
    spawn_enabled: bool,
    visibility_mode: str,
) -> List[str]:
    cmd = [
        "python3",
        os.path.abspath(__file__),
        "autopilot-runner",
        "--root",
        str(args.root),
        "--actor",
        "orchestrator",
        "--mode",
        "send",
        "--group-id",
        str(args.group_id),
        "--account-id",
        str(args.account_id),
        "--session-id",
        str(getattr(args, "session_id", "") or ""),
        "--timeout-sec",
        str(normalize_timeout_sec(getattr(args, "timeout_sec", 0), default=0)),
        "--max-steps",
        str(max_steps),
        "--visibility-mode",
        str(visibility_mode or DEFAULT_VISIBILITY_MODE),
    ]
    if spawn_enabled:
        cmd.append("--spawn")
    else:
        cmd.append("--no-spawn")
    spawn_cmd = str(getattr(args, "spawn_cmd", "") or "").strip()
    if spawn_cmd:
        cmd.extend(["--spawn-cmd", spawn_cmd])
    spawn_output = str(getattr(args, "spawn_output", "") or "").strip()
    if spawn_output:
        cmd.extend(["--spawn-output", spawn_output])
    return cmd


def start_autopilot_background(
    args: argparse.Namespace,
    max_steps: int,
    spawn_enabled: bool,
    visibility_mode: str,
) -> Dict[str, Any]:
    if str(getattr(args, "mode", "send") or "send") != "send":
        return {"attempted": False, "status": "skipped", "reason": "mode_not_send", "async": False}

    with autopilot_runtime_lock(args.root):
        current = load_autopilot_runtime_state(args.root)
        is_running = bool(current.get("running")) and _pid_is_running(current.get("pid"))
        if is_running:
            return {
                "attempted": True,
                "status": "already_running",
                "skipped": True,
                "async": True,
                "pid": int(current.get("pid") or 0),
                "state": current,
                "maxSteps": int(current.get("maxSteps") or max_steps),
            }

        stale_recovered = bool(current.get("running")) and not is_running
        if stale_recovered:
            current.update(
                {
                    "running": False,
                    "pid": 0,
                    "status": "stale_recovered",
                    "endedAt": now_iso(),
                    "stopReason": "stale_pid_recovered",
                }
            )
            save_autopilot_runtime_state(args.root, current)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = os.path.join(args.root, "state", f"autopilot-{max_steps}-{stamp}.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

        cmd = build_autopilot_runner_cmd(args, max_steps, spawn_enabled, visibility_mode)

        log_handle = None
        try:
            log_handle = open(log_path, "a", encoding="utf-8")
            proc = subprocess.Popen(
                cmd,
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
            )
        except Exception as err:
            if log_handle is not None and not log_handle.closed:
                log_handle.close()
            failed_state = {
                "running": False,
                "pid": 0,
                "startedAt": "",
                "endedAt": now_iso(),
                "status": "failed_to_start",
                "maxSteps": max_steps,
                "mode": "send",
                "spawnEnabled": bool(spawn_enabled),
                "visibilityMode": str(visibility_mode or DEFAULT_VISIBILITY_MODE),
                "stopReason": "spawn_failed",
                "error": clip(str(err), 240),
                "logPath": log_path,
                "lastResult": {},
            }
            saved = save_autopilot_runtime_state(args.root, failed_state)
            return {
                "attempted": True,
                "status": "failed_to_start",
                "async": True,
                "error": clip(str(err), 240),
                "state": saved,
                "maxSteps": max_steps,
            }
        finally:
            if log_handle is not None and not log_handle.closed:
                log_handle.close()

        started_state = {
            "running": True,
            "pid": int(getattr(proc, "pid", 0) or 0),
            "startedAt": now_iso(),
            "endedAt": "",
            "status": "running",
            "maxSteps": max_steps,
            "mode": "send",
            "spawnEnabled": bool(spawn_enabled),
            "visibilityMode": str(visibility_mode or DEFAULT_VISIBILITY_MODE),
            "stopReason": "",
            "error": "",
            "logPath": log_path,
            "lastResult": {},
        }
        saved = save_autopilot_runtime_state(args.root, started_state)
        return {
            "attempted": True,
            "status": "started",
            "async": True,
            "pid": int(getattr(proc, "pid", 0) or 0),
            "maxSteps": max_steps,
            "logPath": log_path,
            "staleRecovered": stale_recovered,
            "state": saved,
        }


def ensure_scheduler_daemon_running(
    args: argparse.Namespace,
    spawn_enabled: bool,
    visibility_mode: str,
    scheduler_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if str(getattr(args, "mode", "send") or "send") != "send":
        return {"attempted": False, "status": "skipped", "reason": "mode_not_send"}

    state = load_scheduler_daemon_state(args.root)
    is_running = bool(state.get("running")) and _pid_is_running(state.get("pid"))
    if is_running:
        return {
            "attempted": True,
            "status": "already_running",
            "pid": int(state.get("pid") or 0),
            "state": state,
        }

    cmd = [
        "python3",
        os.path.abspath(__file__),
        "scheduler-daemon",
        "--root",
        str(args.root),
        "--actor",
        "orchestrator",
        "--poll-sec",
        str(SCHEDULER_DAEMON_DEFAULT_POLL_SEC),
        "--mode",
        "send",
        "--group-id",
        str(args.group_id),
        "--account-id",
        str(args.account_id),
        "--session-id",
        str(getattr(args, "session_id", "") or ""),
        "--timeout-sec",
        str(normalize_timeout_sec(getattr(args, "timeout_sec", 0), default=0)),
        "--visibility-mode",
        str(visibility_mode or DEFAULT_VISIBILITY_MODE),
    ]
    if spawn_enabled:
        cmd.append("--spawn")
    else:
        cmd.append("--no-spawn")
    spawn_cmd = str(getattr(args, "spawn_cmd", "") or "").strip()
    if spawn_cmd:
        cmd.extend(["--spawn-cmd", spawn_cmd])
    spawn_output = str(getattr(args, "spawn_output", "") or "").strip()
    if spawn_output:
        cmd.extend(["--spawn-output", spawn_output])

    if isinstance(scheduler_state, dict):
        interval_sec = int(scheduler_state.get("intervalSec") or 0)
        if interval_sec > 0:
            cmd.extend(["--interval-sec", str(interval_sec)])
        max_steps = int(scheduler_state.get("maxSteps") or 0)
        if max_steps > 0:
            cmd.extend(["--max-steps", str(max_steps)])

    stale = bool(state.get("running")) and not is_running
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {
            "attempted": True,
            "status": "started",
            "pid": int(getattr(proc, "pid", 0) or 0),
            "staleRecovered": stale,
        }
    except Exception as err:
        return {
            "attempted": True,
            "status": "failed_soft",
            "error": clip(str(err), 200),
            "staleRecovered": stale,
        }


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
    visibility_mode = str(getattr(args, "visibility_mode", DEFAULT_VISIBILITY_MODE) or DEFAULT_VISIBILITY_MODE)
    if visibility_mode not in VISIBILITY_MODES:
        visibility_mode = DEFAULT_VISIBILITY_MODE

    cmd_body = norm
    if norm.lower().startswith("@orchestrator"):
        cmd_body = norm[len("@orchestrator") :].strip()

    governance_cmd = governance.parse_governance_command(cmd_body)
    if governance_cmd is not None:
        if args.actor != "orchestrator":
            result = {
                "ok": False,
                "action": "unauthorized",
                "error": "governance command requires actor=orchestrator",
            }
        else:
            result = governance.execute_governance_command(args.root, args.actor, governance_cmd)
        msg = governance.format_governance_message(result)
        out = send_group_message(args.group_id, args.account_id, msg, args.mode)
        ok = bool(result.get("ok")) and bool(out.get("ok"))
        print(
            json.dumps(
                {
                    "ok": ok,
                    "handled": True,
                    "intent": "governance",
                    "governance": result,
                    "send": out,
                },
                ensure_ascii=True,
            )
        )
        return 0 if ok else 1

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

    # Command: @orchestrator 推进一次
    if re.match(r"^(?:推进一次|advance(?:\s+once)?|tick)$", cmd_body, flags=re.IGNORECASE):
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
            max_steps=1,
            visibility_mode=visibility_mode,
        )
        run = autopilot_once(a_args)
        msg = (
            f"[TASK] 已推进一次 | stepsRun={int(run.get('stepsRun') or 0)} | "
            f"stopReason={str(run.get('stopReason') or '-')}"
        )
        out = send_group_message(args.group_id, args.account_id, msg, args.mode)
        ok = bool(run.get("ok")) and bool(out.get("ok"))
        print(
            json.dumps(
                {
                    "ok": ok,
                    "handled": True,
                    "intent": "advance_once",
                    "run": run,
                    "send": out,
                },
                ensure_ascii=True,
            )
        )
        return 0 if ok else 1

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

        scheduler_action = "status"
        scheduler_max_steps: Optional[int] = None
        if action == "on":
            scheduler_action = "enable"
            scheduler_max_steps = int(state.get("maxSteps") or AUTO_PROGRESS_DEFAULT_MAX_STEPS)
        elif action == "off":
            scheduler_action = "disable"

        s_args = argparse.Namespace(
            root=args.root,
            actor="orchestrator",
            action=scheduler_action,
            interval_sec=None,
            max_steps=scheduler_max_steps,
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
        scheduler = scheduler_run_once(s_args)

        kick = {"ok": True, "skipped": True, "reason": "autopilot not requested"}
        if action == "on" and isinstance(scheduler.get("run"), dict):
            kick = dict(scheduler.get("run") or {})

        daemon_bootstrap = {"attempted": False, "status": "skipped", "reason": "action_not_on_or_mode_not_send"}
        if action == "on":
            daemon_bootstrap = ensure_scheduler_daemon_running(
                args,
                spawn_enabled=spawn_enabled,
                visibility_mode=visibility_mode,
                scheduler_state=scheduler.get("state") if isinstance(scheduler.get("state"), dict) else None,
            )

        status_text = "开启" if state.get("enabled") else "关闭"
        msg = f"[TASK] 自动推进已{status_text} | maxSteps={state.get('maxSteps')}"
        out = send_group_message(args.group_id, args.account_id, msg, args.mode)
        ok = bool(out.get("ok")) and bool(kick.get("ok")) and bool(scheduler.get("ok"))
        print(
            json.dumps(
                {
                    "ok": ok,
                    "handled": True,
                    "intent": "auto_progress",
                    "action": action,
                    "state": state,
                    "scheduler": scheduler,
                    "daemonBootstrap": daemon_bootstrap,
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

    # Command: @orchestrator 开始xhs流程n8n <paper_id> <pdf_path>
    # Aliases:
    #   @orchestrator 开始xhs流程 n8n <paper_id> <pdf_path>
    #   @orchestrator start xhs n8n <paper_id> <pdf_path>
    m = re.match(
        r"^(?:开始xhs流程n8n|开始xhs流程\s+n8n|start\s+xhs(?:\s+workflow)?\s+n8n)\s+(.+)$",
        cmd_body,
        flags=re.IGNORECASE,
    )
    if m:
        tail = (m.group(1) or "").strip()
        try:
            parts = shlex.split(tail)
        except Exception as err:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "handled": True,
                        "intent": "xhs_n8n_trigger",
                        "error": f"invalid command syntax: {clip(str(err), 160)}",
                    },
                    ensure_ascii=True,
                )
            )
            return 1
        if len(parts) < 2:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "handled": True,
                        "intent": "xhs_n8n_trigger",
                        "error": "usage: @orchestrator 开始xhs流程n8n <paper_id> <pdf_path>",
                    },
                    ensure_ascii=True,
                )
            )
            return 1

        paper_id = str(parts[0]).strip()
        pdf_path = " ".join(parts[1:]).strip()
        n8n_args = argparse.Namespace(
            actor="orchestrator",
            paper_id=paper_id,
            pdf_path=pdf_path,
            group_id=args.group_id,
            account_id=args.account_id,
            mode=args.mode,
            timeout_sec=args.timeout_sec,
            n8n_trigger_script=DEFAULT_XHS_N8N_TRIGGER_SCRIPT,
        )
        result = trigger_xhs_n8n_once(n8n_args)
        print(json.dumps(result, ensure_ascii=True))
        return 0 if result.get("ok") else 1

    # Command: @orchestrator 开始xhs流程 <paper_id> <pdf_path>
    # Alias: @orchestrator start xhs workflow <paper_id> <pdf_path>
    m = re.match(r"^(?:开始xhs流程|start\s+xhs\s+workflow)\s+(.+)$", cmd_body, flags=re.IGNORECASE)
    if m:
        tail = (m.group(1) or "").strip()
        try:
            parts = shlex.split(tail)
        except Exception as err:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "handled": True,
                        "intent": "xhs_bootstrap",
                        "error": f"invalid command syntax: {clip(str(err), 160)}",
                    },
                    ensure_ascii=True,
                )
            )
            return 1
        if len(parts) < 2:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "handled": True,
                        "intent": "xhs_bootstrap",
                        "error": "usage: @orchestrator 开始xhs流程 <paper_id> <pdf_path>",
                    },
                    ensure_ascii=True,
                )
            )
            return 1

        paper_id = str(parts[0]).strip()
        pdf_path = " ".join(parts[1:]).strip()
        workflow_root = DEFAULT_XHS_WORKFLOW_ROOT
        if not os.path.isdir(workflow_root):
            derived_root = os.path.dirname(normalize_project_path(pdf_path))
            if derived_root:
                workflow_root = derived_root
        x_args = argparse.Namespace(
            root=args.root,
            paper_id=paper_id,
            pdf_path=pdf_path,
            workflow_root=workflow_root,
            run_dir="",
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
        result = xhs_bootstrap_once(x_args)
        print(json.dumps(result, ensure_ascii=True))
        return 0 if result.get("ok") else 1

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
        decomposition_tasks = bootstrap.get("decompositionTasks") if isinstance(bootstrap.get("decompositionTasks"), list) else []
        if not decomposition_tasks:
            decomposition_tasks = [
                {
                    "title": clip(str(item), 120),
                    "ownerHint": suggest_agent_from_title(str(item)),
                    "dependsOn": [],
                    "confidence": 0.55,
                }
                for item in bootstrap.get("tasks", [])
                if str(item).strip()
            ]

        created: List[Dict[str, Any]] = []
        created_ids: List[str] = []
        created_specs: List[Dict[str, Any]] = []
        for item in decomposition_tasks:
            title = clip(str(item.get("title") or ""), 120)
            if not title:
                continue
            assignee = governance.canonical_agent(str(item.get("ownerHint") or "")) or suggest_agent_from_title(title)
            depends_on = [str(dep).strip() for dep in (item.get("dependsOn") or []) if str(dep).strip()]
            apply_obj = board_apply(args.root, "orchestrator", f"@{assignee} create task: [{project_name}] {title}")
            if isinstance(apply_obj, dict) and apply_obj.get("ok"):
                tid = str(apply_obj.get("taskId") or "")
                if tid:
                    created_ids.append(tid)
                    bind_task_project_context(args.root, tid, project_path, project_name)
                    created_specs.append({"taskId": tid, "title": title, "dependsOn": depends_on})
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

        depends_sync = write_project_depends_on(args.root, created_specs)

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
        decomposition_count = int(bootstrap.get("decompositionCount") or len(decomposition_tasks))
        confidence_summary = bootstrap.get("confidenceSummary") if isinstance(bootstrap.get("confidenceSummary"), dict) else {}
        msg = (
            f"[TASK] 项目启动完成: {project_name} | 新建任务={len(created_ids)} | 自动拆解={decomposition_count}\n"
            f"{doc_hint}\n可用命令: @orchestrator 项目状态"
        )
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
                    "decompositionCount": decomposition_count,
                    "confidenceSummary": confidence_summary,
                    "dependsOnSync": depends_sync,
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
        selection = task.get("_prioritySelection") if isinstance(task.get("_prioritySelection"), dict) else {}
        d_args = argparse.Namespace(
            root=args.root,
            task_id=task_id,
            agent=agent,
            task="",
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
            selection=selection,
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
        if str(args.mode or "send") == "send":
            run = start_autopilot_background(
                args,
                max_steps=max_steps,
                spawn_enabled=spawn_enabled,
                visibility_mode=visibility_mode,
            )
            status = str(run.get("status") or "")
            if status == "started":
                msg = (
                    f"[TASK] autopilot 已后台启动 | maxSteps={max_steps} | "
                    f"pid={int(run.get('pid') or 0)}"
                )
            elif status == "already_running":
                pid = int(run.get("pid") or 0)
                started_at = str(((run.get("state") or {}).get("startedAt")) or "-")
                msg = f"[TASK] autopilot 已在运行 | pid={pid} | startedAt={started_at}"
            else:
                msg = f"[TASK] autopilot 启动失败 | reason={status or '-'} | error={clip(str(run.get('error') or '-'), 160)}"

            send = send_group_message(args.group_id, args.account_id, msg, args.mode)
            ok = status in {"started", "already_running"} and bool(send.get("ok"))
            print(
                json.dumps(
                    {
                        "ok": ok,
                        "handled": True,
                        "intent": "autopilot",
                        "async": True,
                        "run": run,
                        "send": send,
                    },
                    ensure_ascii=True,
                )
            )
            return 0 if ok else 1

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
        ops_summary: Dict[str, Any] = {}
        ops_metrics_error = ""
        if full_mode:
            try:
                ops_summary = ops_metrics.aggregate_metrics(args.root, days=7)
                msg = msg + "\n" + ops_metrics.format_core_summary(ops_summary, days=7)
            except Exception as exc:
                ops_summary = {}
                ops_metrics_error = f"{type(exc).__name__}: {exc}"
        out = send_group_message(args.group_id, args.account_id, msg, args.mode)
        result = {
            "ok": bool(out.get("ok")),
            "handled": True,
            "intent": "status",
            "full": full_mode,
            "counts": counts,
            "opsMetrics": ops_summary if full_mode else {},
            "send": out,
        }
        if full_mode and ops_metrics_error:
            result["opsMetricsError"] = ops_metrics_error
        print(json.dumps(result))
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
        done_task_id = ""
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
        done_cleanup: Dict[str, Any] = {"skipped": True, "reason": "not_mark_done"}
        if bool(apply_obj.get("ok")) and str(apply_obj.get("intent") or "") == "mark_done" and done_task_id:
            done_cleanup = cleanup_done_state(args.root, done_task_id)
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
                    "doneCleanup": done_cleanup,
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
            collab_relay = best_effort_wakeup_collaboration_relay(
                args.root,
                task_id,
                args.actor,
                kind,
                norm,
                args.mode,
            )
            print(
                json.dumps(
                    {
                        "ok": ok,
                        "handled": True,
                        "intent": "wakeup",
                        "kind": kind,
                        "apply": apply_obj,
                        "publish": publish,
                        "collabRelay": collab_relay,
                    }
                )
            )
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
            done_cleanup: Dict[str, Any] = {"skipped": True, "reason": "not_mark_done"}
            if bool(apply_obj.get("ok")) and str(apply_obj.get("intent") or "") == "mark_done":
                done_cleanup = cleanup_done_state(args.root, task_id)
            ok = bool(apply_obj.get("ok")) and bool(publish.get("ok"))
            collab_relay = best_effort_wakeup_collaboration_relay(
                args.root,
                task_id,
                args.actor,
                kind,
                norm,
                args.mode,
            )
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
                        "doneCleanup": done_cleanup,
                        "collabRelay": collab_relay,
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
        dispatch_result = dispatch_once(d_args)
        if not isinstance(dispatch_result, dict):
            dispatch_result = {"ok": False, "error": "dispatch_result_invalid"}
        collab_relay = best_effort_wakeup_collaboration_relay(
            args.root,
            task_id,
            args.actor,
            kind,
            norm,
            args.mode,
        )
        dispatch_result["collabRelay"] = collab_relay
        print(json.dumps(dispatch_result, ensure_ascii=True))
        return 0 if dispatch_result.get("ok") else 1

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
    p_dispatch.add_argument("--timeout-sec", type=int, default=0)
    p_dispatch.add_argument("--visibility-mode", choices=list(VISIBILITY_MODES), default=DEFAULT_VISIBILITY_MODE)
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
    p_autopilot.add_argument("--timeout-sec", type=int, default=0)
    p_autopilot.add_argument("--spawn", dest="spawn", action="store_true", default=True)
    p_autopilot.add_argument("--no-spawn", dest="spawn", action="store_false")
    p_autopilot.add_argument("--spawn-cmd", default="")
    p_autopilot.add_argument("--spawn-output", default="")
    p_autopilot.add_argument("--max-steps", type=int, default=3)
    p_autopilot.add_argument("--visibility-mode", choices=list(VISIBILITY_MODES), default=DEFAULT_VISIBILITY_MODE)
    p_autopilot.set_defaults(func=cmd_autopilot)

    p_autopilot_runner = sub.add_parser("autopilot-runner")
    p_autopilot_runner.add_argument("--root", required=True)
    p_autopilot_runner.add_argument("--actor", default="orchestrator")
    p_autopilot_runner.add_argument("--group-id", default=DEFAULT_GROUP_ID)
    p_autopilot_runner.add_argument("--account-id", default=DEFAULT_ACCOUNT_ID)
    p_autopilot_runner.add_argument("--mode", choices=["send", "dry-run"], default="send")
    p_autopilot_runner.add_argument("--session-id", default="")
    p_autopilot_runner.add_argument("--timeout-sec", type=int, default=0)
    p_autopilot_runner.add_argument("--spawn", dest="spawn", action="store_true", default=True)
    p_autopilot_runner.add_argument("--no-spawn", dest="spawn", action="store_false")
    p_autopilot_runner.add_argument("--spawn-cmd", default="")
    p_autopilot_runner.add_argument("--spawn-output", default="")
    p_autopilot_runner.add_argument("--max-steps", type=int, default=3)
    p_autopilot_runner.add_argument("--visibility-mode", choices=list(VISIBILITY_MODES), default=DEFAULT_VISIBILITY_MODE)
    p_autopilot_runner.set_defaults(func=cmd_autopilot_runner)

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
    p_scheduler.add_argument("--timeout-sec", type=int, default=0)
    p_scheduler.add_argument("--spawn", dest="spawn", action="store_true", default=True)
    p_scheduler.add_argument("--no-spawn", dest="spawn", action="store_false")
    p_scheduler.add_argument("--spawn-cmd", default="")
    p_scheduler.add_argument("--spawn-output", default="")
    p_scheduler.add_argument("--visibility-mode", choices=list(VISIBILITY_MODES), default=DEFAULT_VISIBILITY_MODE)
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
    p_scheduler_daemon.add_argument("--timeout-sec", type=int, default=0)
    p_scheduler_daemon.add_argument("--spawn", dest="spawn", action="store_true", default=True)
    p_scheduler_daemon.add_argument("--no-spawn", dest="spawn", action="store_false")
    p_scheduler_daemon.add_argument("--spawn-cmd", default="")
    p_scheduler_daemon.add_argument("--spawn-output", default="")
    p_scheduler_daemon.add_argument("--visibility-mode", choices=list(VISIBILITY_MODES), default=DEFAULT_VISIBILITY_MODE)
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

    p_xhs = sub.add_parser("xhs-bootstrap")
    p_xhs.add_argument("--root", required=True)
    p_xhs.add_argument("--paper-id", required=True)
    p_xhs.add_argument("--pdf-path", required=True)
    p_xhs.add_argument("--workflow-root", default=DEFAULT_XHS_WORKFLOW_ROOT)
    p_xhs.add_argument("--run-dir", default="")
    p_xhs.add_argument("--actor", default="orchestrator")
    p_xhs.add_argument("--session-id", default="")
    p_xhs.add_argument("--group-id", default=DEFAULT_GROUP_ID)
    p_xhs.add_argument("--account-id", default=DEFAULT_ACCOUNT_ID)
    p_xhs.add_argument("--mode", choices=["send", "dry-run"], default="send")
    p_xhs.add_argument("--timeout-sec", type=int, default=0)
    p_xhs.add_argument("--spawn", dest="spawn", action="store_true", default=False)
    p_xhs.add_argument("--no-spawn", dest="spawn", action="store_false")
    p_xhs.add_argument("--spawn-cmd", default="")
    p_xhs.add_argument("--spawn-output", default="")
    p_xhs.add_argument("--visibility-mode", choices=list(VISIBILITY_MODES), default=DEFAULT_VISIBILITY_MODE)
    p_xhs.set_defaults(func=cmd_xhs_bootstrap)

    p_feishu = sub.add_parser("feishu-router")
    p_feishu.add_argument("--root", required=True)
    p_feishu.add_argument("--actor", required=True)
    p_feishu.add_argument("--text", required=True)
    p_feishu.add_argument("--group-id", default=DEFAULT_GROUP_ID)
    p_feishu.add_argument("--account-id", default=DEFAULT_ACCOUNT_ID)
    p_feishu.add_argument("--mode", choices=["send", "dry-run", "off"], default="send")
    p_feishu.add_argument("--session-id", default="")
    p_feishu.add_argument("--timeout-sec", type=int, default=0)
    p_feishu.add_argument("--dispatch-spawn", action="store_true")
    p_feishu.add_argument("--dispatch-manual", action="store_true")
    p_feishu.add_argument("--visibility-mode", choices=list(VISIBILITY_MODES), default=DEFAULT_VISIBILITY_MODE)
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
