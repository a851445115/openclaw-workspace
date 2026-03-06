#!/usr/bin/env python3
import re
from typing import Any, Dict, List


INCOMPLETE_REASON_CODES = {
    "incomplete_output",
    "missing_evidence",
    "stage_only",
    "role_policy_missing_keyword",
    "missing_hard_evidence",
}
EXECUTOR_REASON_CODES = {
    "spawn_failed",
    "invalid_spawn_output",
    "spawn_command_empty",
    "verify_command_failed",
}
CONTINUATION_REASON_CODES = {
    "continuation_round_limit",
    "continuation_no_progress",
    "continuation_timeout",
}


CONTEXT_OVERFLOW_PATTERNS = (
    ("context_length", re.compile(r"context length exceeded|maximum context length|context window|token limit exceeded|too many tokens|prompt is too long|超出上下文|上下文过长", re.IGNORECASE)),
)
WRONG_DIRECTION_PATTERNS = (
    ("direction_mismatch", re.compile(r"not what (?:the )?(?:task|request|prompt) asked|does not match the requested|not aligned with the requested|wrong direction|misaligned deliverable|instead of (?:implementing|adding|writing)", re.IGNORECASE)),
)
MISSING_INFO_PATTERNS = (
    ("missing_secret", re.compile(r"secret[_\s-]*key|api[_\s-]*key|credential|token missing|env var|environment variable", re.IGNORECASE)),
    ("missing_schema", re.compile(r"api schema|schema missing|missing schema|contract missing|interface missing", re.IGNORECASE)),
    ("requirements_unclear", re.compile(r"unclear requirement|need more context|missing requirement|need user choice|input required|clarify", re.IGNORECASE)),
)
CONTINUATION_STALL_PATTERNS = (
    ("continuation_stall", re.compile(r"continue was required|continuation stalled|checkpoint stalled|stopped midway|stalled midway|partial progress.*stalled|需要继续|继续.*卡住", re.IGNORECASE)),
)
EXECUTOR_FAILURE_PATTERNS = (
    ("executor_runtime", re.compile(r"traceback|runtime error|worker crashed|process exited|segmentation fault|command failed|exception", re.IGNORECASE)),
)
INCOMPLETE_OUTPUT_PATTERNS = (
    ("missing_evidence", re.compile(r"missing evidence|未通过验收|missing required signal|缺少验收|缺少证据|need evidence", re.IGNORECASE)),
)


FAILURE_TYPE_TO_REASON = {
    "context_overflow": "context_length_exceeded",
    "wrong_direction": "misaligned_deliverable",
    "missing_info": "missing_upstream_information",
    "executor_failure": "executor_runtime_failure",
    "budget_exceeded": "budget_limit_reached",
    "incomplete_output": "output_missing_required_signal",
    "continuation_stall": "continuation_stalled",
    "unknown": "unknown",
}
FAILURE_TYPE_TO_STRATEGY = {
    "context_overflow": "retry_same_assignee_shrink_scope",
    "wrong_direction": "escalate_for_replan",
    "missing_info": "retry_with_clarification",
    "executor_failure": "retry_or_switch_executor",
    "budget_exceeded": "escalate_budget_review",
    "incomplete_output": "retry_with_stricter_evidence",
    "continuation_stall": "escalate_after_stall",
    "unknown": "manual_triage",
}


def normalize_reason(reason_code: Any) -> str:
    return str(reason_code or "").strip().lower()


def _normalize_signal_list(values: List[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        token = str(value or "").strip()
        if token and token not in out:
            out.append(token)
    return out


def _join_text(*parts: Any) -> str:
    return "\n".join(str(part or "") for part in parts if str(part or "").strip())


def _match_patterns(text: str, pattern_specs) -> List[str]:
    matches: List[str] = []
    for signal, pattern in pattern_specs:
        if pattern.search(text):
            matches.append(signal)
    return matches


def classify_failure(
    reason_code: Any,
    detail: Any = "",
    output_text: Any = "",
    stderr: Any = "",
    current_assignee: Any = "",
    executor: Any = "",
) -> Dict[str, Any]:
    reason = normalize_reason(reason_code)
    executor_name = normalize_reason(executor)
    current = normalize_reason(current_assignee)
    text = _join_text(detail, output_text, stderr)

    signals: List[str] = []
    if reason:
        signals.append(f"reason:{reason}")
    if executor_name:
        signals.append(f"executor:{executor_name}")
    if current:
        signals.append(f"assignee:{current}")

    context_hits = _match_patterns(text, CONTEXT_OVERFLOW_PATTERNS)
    wrong_hits = _match_patterns(text, WRONG_DIRECTION_PATTERNS)
    missing_hits = _match_patterns(text, MISSING_INFO_PATTERNS)
    continuation_hits = _match_patterns(text, CONTINUATION_STALL_PATTERNS)
    executor_hits = _match_patterns(text, EXECUTOR_FAILURE_PATTERNS)
    incomplete_hits = _match_patterns(text, INCOMPLETE_OUTPUT_PATTERNS)

    failure_type = "unknown"
    if reason == "budget_exceeded":
        failure_type = "budget_exceeded"
        signals.append("budget_limit")
    elif context_hits:
        failure_type = "context_overflow"
        signals.extend(context_hits)
    elif reason in CONTINUATION_REASON_CODES or continuation_hits:
        failure_type = "continuation_stall"
        signals.extend(continuation_hits or ["continuation_reason"])
    elif wrong_hits:
        failure_type = "wrong_direction"
        signals.extend(wrong_hits)
    elif reason == "continuation_need_input" or missing_hits:
        failure_type = "missing_info"
        signals.extend(missing_hits or ["continuation_need_input"])
    elif reason in INCOMPLETE_REASON_CODES or incomplete_hits:
        failure_type = "incomplete_output"
        signals.extend(incomplete_hits or ["incomplete_reason"])
    elif reason in EXECUTOR_REASON_CODES or executor_hits:
        failure_type = "executor_failure"
        signals.extend(executor_hits or ["executor_reason"])

    normalized_reason = FAILURE_TYPE_TO_REASON.get(failure_type, reason or "unknown")
    if failure_type == "unknown" and reason:
        normalized_reason = reason
    recovery_strategy = FAILURE_TYPE_TO_STRATEGY.get(failure_type, "manual_triage")

    return {
        "failureType": failure_type,
        "normalizedReason": normalized_reason,
        "recoveryStrategy": recovery_strategy,
        "signals": _normalize_signal_list(signals),
    }
