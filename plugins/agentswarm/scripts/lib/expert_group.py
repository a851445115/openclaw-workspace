#!/usr/bin/env python3
import copy
import hashlib
import json
import logging
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    import fcntl
except Exception:  # pragma: no cover
    fcntl = None


EXPERT_GROUP_POLICY_CONFIG_CANDIDATES = (
    os.path.join("config", "expert-group-policy.json"),
    os.path.join("state", "expert-group-policy.json"),
)
DEFAULT_EXPERT_GROUP_POLICY: Dict[str, Any] = {
    "enabled": True,
    "blockedRetriesThreshold": 2,
    "blockedDurationMinutes": 30,
    "downstreamImpactThreshold": 2,
    "highRiskReasonCodes": ["spawn_failed", "budget_exceeded", "invalid_spawn_output"],
}
LOGGER = logging.getLogger(__name__)
EXPERT_OUTPUT_FIELDS: Tuple[str, ...] = ("hypothesis", "evidence", "confidence", "proposedFix", "risk")
EXPERT_ROLE_INVEST_ANALYST = "invest-analyst"
DEFAULT_EXPERT_ROLES: Tuple[str, ...] = ("coder", "debugger", EXPERT_ROLE_INVEST_ANALYST)
ROLE_ALIASES: Dict[str, str] = {
    "analyst": EXPERT_ROLE_INVEST_ANALYST,
    "invest_analyst": EXPERT_ROLE_INVEST_ANALYST,
    "invest analyst": EXPERT_ROLE_INVEST_ANALYST,
    "researcher": EXPERT_ROLE_INVEST_ANALYST,
}
ROLE_TASK_PREFIX: Dict[str, str] = {
    "coder": "Produce a code-level remediation plan with concrete implementation steps.",
    "debugger": "Trace the blocker path and isolate the most probable failure mechanism.",
    EXPERT_ROLE_INVEST_ANALYST: "Evaluate system impact, sequencing, and validation strategy across tasks.",
}
REASON_GUIDANCE: Dict[str, str] = {
    "retry_limit": "repeated retries indicate current fix attempts are not converging",
    "blocked_duration": "the blocker has stayed unresolved for too long",
    "downstream_impact": "multiple downstream tasks are now impacted",
    "high_risk_reason": "runtime reason code is classified as high risk by policy",
}
FIELD_GUIDANCE: Dict[str, str] = {
    "hypothesis": "Root-cause hypothesis in one sentence.",
    "evidence": "Logs, traces, metrics, or reproducible observations supporting the hypothesis.",
    "confidence": "Confidence score in range [0, 1].",
    "proposedFix": "Actionable fix plan, including owner-facing execution detail.",
    "risk": "Primary risk if proposedFix is applied and how to mitigate it.",
}

EXPERT_GROUP_LIFECYCLE_DIR = os.path.join("state", "expert-groups")
LIFECYCLE_STATUS_CREATED = "created"
LIFECYCLE_STATUS_EXECUTING = "executing"
LIFECYCLE_STATUS_CONVERGED = "converged"
LIFECYCLE_STATUS_ARCHIVED = "archived"
LIFECYCLE_STATUSES: Tuple[str, ...] = (
    LIFECYCLE_STATUS_CREATED,
    LIFECYCLE_STATUS_EXECUTING,
    LIFECYCLE_STATUS_CONVERGED,
    LIFECYCLE_STATUS_ARCHIVED,
)
ACTIVE_LIFECYCLE_STATUSES = {
    LIFECYCLE_STATUS_CREATED,
    LIFECYCLE_STATUS_EXECUTING,
    LIFECYCLE_STATUS_CONVERGED,
}
LIFECYCLE_GROUP_ID_RE = re.compile(r"^eg_[0-9a-f]{16}$")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except Exception:
        return int(default)
    return parsed


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _normalize_reason_codes(raw: Any, fallback: List[str]) -> List[str]:
    values = raw if isinstance(raw, list) else fallback
    out: List[str] = []
    seen = set()
    for item in values:
        code = str(item or "").strip().lower()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return float(default)
    if isinstance(value, str) and value.strip().endswith("%"):
        stripped = value.strip().rstrip("%")
        try:
            return max(0.0, min(1.0, float(stripped) / 100.0))
        except Exception:
            return float(default)
    try:
        return float(value)
    except Exception:
        return float(default)


def _canonical_role(role: Any) -> str:
    role_text = str(role or "").strip().lower()
    if not role_text:
        return ""
    alias = ROLE_ALIASES.get(role_text)
    if alias:
        return alias
    role_dash = role_text.replace("_", "-")
    alias = ROLE_ALIASES.get(role_dash)
    if alias:
        return alias
    return role_dash


def _normalize_roles(raw_roles: Any) -> List[str]:
    values = raw_roles if isinstance(raw_roles, list) else list(DEFAULT_EXPERT_ROLES)
    out: List[str] = []
    seen = set()
    for item in values:
        role = _canonical_role(item)
        if not role or role in seen:
            continue
        seen.add(role)
        out.append(role)
    if not out:
        return list(DEFAULT_EXPERT_ROLES)
    return out


def _reason_context_lines(reasons: List[str]) -> List[str]:
    lines: List[str] = []
    for reason in _normalize_reason_codes(reasons, []):
        guidance = REASON_GUIDANCE.get(reason, "unclassified trigger reason; assess impact and mitigations")
        lines.append(f"{reason}: {guidance}")
    return lines


def _normalize_string_list(raw: Any, limit: int = 64) -> List[str]:
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    seen = set()
    for item in raw:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
        if len(out) >= max(1, int(limit)):
            break
    return out


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_lifecycle_status(status: Any) -> str:
    value = str(status or "").strip().lower()
    if value in LIFECYCLE_STATUSES:
        return value
    return ""


def _normalize_history(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        to_status = _normalize_lifecycle_status(item.get("to"))
        if not to_status:
            continue
        out.append(
            {
                "at": str(item.get("at") or "").strip(),
                "from": _normalize_lifecycle_status(item.get("from")),
                "to": to_status,
                "event": str(item.get("event") or "").strip(),
            }
        )
    return out


def _compact_template_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    role = _canonical_role(entry.get("role"))
    task = str(entry.get("task") or "").strip()
    required_fields = _normalize_string_list(entry.get("requiredFields"), limit=16)
    out: Dict[str, Any] = {"role": role, "task": task}
    if required_fields:
        out["requiredFields"] = required_fields
    return out


def _compact_templates(raw_templates: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_templates, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw_templates:
        if not isinstance(item, dict):
            continue
        compact = _compact_template_entry(item)
        if not compact.get("role") and not compact.get("task"):
            continue
        out.append(compact)
        if len(out) >= 12:
            break
    return out


def _normalize_consensus(consensus: Any, fallback_owner: str = "orchestrator") -> Dict[str, Any]:
    raw = consensus if isinstance(consensus, dict) else {}
    owner_text = _canonical_role(raw.get("owner")) or str(raw.get("owner") or "").strip() or fallback_owner
    return {
        "consensusPlan": str(raw.get("consensusPlan") or "").strip(),
        "owner": owner_text or fallback_owner,
        "executionChecklist": _normalize_string_list(raw.get("executionChecklist"), limit=64),
        "acceptanceGate": _normalize_string_list(raw.get("acceptanceGate"), limit=64),
    }


def build_lifecycle_group_id(task_id: str) -> str:
    normalized_task_id = str(task_id or "").strip() or "unknown-task"
    digest = hashlib.sha256(normalized_task_id.encode("utf-8")).hexdigest()[:16]
    return f"eg_{digest}"


def is_valid_lifecycle_group_id(group_id: Any) -> bool:
    value = str(group_id or "").strip().lower()
    return bool(LIFECYCLE_GROUP_ID_RE.fullmatch(value))


def resolve_lifecycle_group_id(task_id: str = "", group_id: str = "") -> str:
    task_key = str(task_id or "").strip()
    candidate = str(group_id or "").strip().lower()
    if is_valid_lifecycle_group_id(candidate):
        return candidate
    if task_key:
        return build_lifecycle_group_id(task_key)
    return ""


def lifecycle_state_path(root: str, group_id: str) -> str:
    group_key = resolve_lifecycle_group_id(group_id=group_id)
    if not group_key:
        return ""
    return os.path.join(root, EXPERT_GROUP_LIFECYCLE_DIR, f"{group_key}.json")


def _normalize_lifecycle_record(loaded: Dict[str, Any], task_key: str, group_key: str) -> Dict[str, Any]:
    task_value = str(loaded.get("taskId") or task_key).strip() or task_key
    safe_group = resolve_lifecycle_group_id(task_value, loaded.get("groupId")) or group_key
    return {
        "groupId": safe_group,
        "taskId": task_value,
        "status": _normalize_lifecycle_status(loaded.get("status")),
        "createdAt": str(loaded.get("createdAt") or "").strip(),
        "updatedAt": str(loaded.get("updatedAt") or "").strip(),
        "history": _normalize_history(loaded.get("history")),
        "reasons": _normalize_reason_codes(loaded.get("reasons"), []),
        "templates": _compact_templates(loaded.get("templates")),
        "consensus": _normalize_consensus(loaded.get("consensus")),
    }


def _load_lifecycle_state_from_path(path: str, task_key: str, group_key: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except Exception as err:
        LOGGER.warning(
            "failed to load expert-group lifecycle: path=%s error=%s",
            path,
            str(err),
        )
        return {}
    if not isinstance(loaded, dict):
        return {}
    return _normalize_lifecycle_record(loaded, task_key, group_key)


@contextmanager
def lifecycle_file_lock(path: str):
    lock_path = f"{path}.lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
        os.close(fd)


def _write_lifecycle_state_atomic(path: str, record: Dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd: Optional[int] = None
    temp_path = ""
    try:
        fd, temp_path = tempfile.mkstemp(prefix=".expert-group-", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            json.dump(record, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        if temp_path:
            try:
                os.unlink(temp_path)
            except Exception:
                pass
        raise


def load_lifecycle_state(root: str, task_id: str = "", group_id: str = "") -> Dict[str, Any]:
    task_key = str(task_id or "").strip()
    group_key = resolve_lifecycle_group_id(task_key, group_id)
    if not group_key:
        return {}
    path = lifecycle_state_path(root, group_key)
    return _load_lifecycle_state_from_path(path, task_key, group_key)


def is_lifecycle_active(record: Dict[str, Any]) -> bool:
    if not isinstance(record, dict):
        return False
    status = _normalize_lifecycle_status(record.get("status"))
    return status in ACTIVE_LIFECYCLE_STATUSES


def lifecycle_summary(
    record: Dict[str, Any],
    root: str = "",
    task_id: str = "",
    group_id: str = "",
) -> Dict[str, Any]:
    task_key = str(task_id or "").strip()
    rec = record if isinstance(record, dict) else {}
    record_task = str(rec.get("taskId") or task_key).strip() or task_key
    group_key = resolve_lifecycle_group_id(record_task, str(rec.get("groupId") or group_id or "").strip())
    status = _normalize_lifecycle_status(rec.get("status")) or "inactive"
    history_count = len(rec.get("history") or []) if isinstance(rec.get("history"), list) else 0
    path = lifecycle_state_path(root, group_key) if root and group_key else ""
    return {
        "groupId": group_key,
        "taskId": record_task,
        "status": status,
        "path": path,
        "historyCount": max(0, int(history_count)),
        "updatedAt": str(rec.get("updatedAt") or "").strip(),
    }


def transition_lifecycle_state(
    root: str,
    task_id: str,
    target_status: str,
    reasons: Optional[List[str]] = None,
    templates: Optional[List[Dict[str, Any]]] = None,
    consensus: Optional[Dict[str, Any]] = None,
    group_id: str = "",
    event: str = "status_transition",
) -> Dict[str, Any]:
    task_key = str(task_id or "").strip()
    status_target = _normalize_lifecycle_status(target_status) or LIFECYCLE_STATUS_CREATED
    group_key = resolve_lifecycle_group_id(task_key, group_id)
    if not group_key:
        return {}
    path = lifecycle_state_path(root, group_key)
    if not path:
        return {}

    with lifecycle_file_lock(path):
        existing = _load_lifecycle_state_from_path(path, task_key, group_key)
        now_iso = _utc_now_iso()
        previous_status = _normalize_lifecycle_status(existing.get("status"))
        history = _normalize_history(existing.get("history"))
        if previous_status != status_target:
            history.append(
                {
                    "at": now_iso,
                    "from": previous_status,
                    "to": status_target,
                    "event": str(event or "status_transition").strip() or "status_transition",
                }
            )

        owner_fallback = str((existing.get("consensus") or {}).get("owner") or "orchestrator")
        record: Dict[str, Any] = {
            "groupId": group_key,
            "taskId": task_key or str(existing.get("taskId") or "").strip(),
            "status": status_target,
            "createdAt": str(existing.get("createdAt") or now_iso).strip() or now_iso,
            "updatedAt": now_iso,
            "history": history,
            "reasons": _normalize_reason_codes(reasons, existing.get("reasons") if reasons is None else []),
            "templates": _compact_templates(templates if templates is not None else existing.get("templates")),
            "consensus": _normalize_consensus(
                consensus if consensus is not None else existing.get("consensus"),
                fallback_owner=owner_fallback,
            ),
        }
        _write_lifecycle_state_atomic(path, record)
    return record


def neutral_consensus(fallback_owner: str = "orchestrator") -> Dict[str, Any]:
    owner_fallback = _canonical_role(fallback_owner) or str(fallback_owner or "").strip() or "orchestrator"
    return {
        "consensusPlan": "",
        "owner": owner_fallback,
        "executionChecklist": [],
        "acceptanceGate": [],
        "inactive": True,
    }


def normalize_expert_group_policy(raw: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    defaults = fallback if isinstance(fallback, dict) else DEFAULT_EXPERT_GROUP_POLICY
    blocked_retries_threshold = _safe_int(source.get("blockedRetriesThreshold"), _safe_int(defaults.get("blockedRetriesThreshold"), 2))
    blocked_duration_minutes = _safe_int(source.get("blockedDurationMinutes"), _safe_int(defaults.get("blockedDurationMinutes"), 30))
    downstream_impact_threshold = _safe_int(source.get("downstreamImpactThreshold"), _safe_int(defaults.get("downstreamImpactThreshold"), 2))
    return {
        "enabled": _as_bool(source.get("enabled"), _as_bool(defaults.get("enabled"), True)),
        "blockedRetriesThreshold": max(1, blocked_retries_threshold),
        "blockedDurationMinutes": max(1, blocked_duration_minutes),
        "downstreamImpactThreshold": max(1, downstream_impact_threshold),
        "highRiskReasonCodes": _normalize_reason_codes(
            source.get("highRiskReasonCodes"),
            _normalize_reason_codes(defaults.get("highRiskReasonCodes"), []),
        ),
    }


def merge_expert_group_policy(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base) if isinstance(base, dict) else copy.deepcopy(DEFAULT_EXPERT_GROUP_POLICY)
    if not isinstance(override, dict):
        return merged
    for key in (
        "enabled",
        "blockedRetriesThreshold",
        "blockedDurationMinutes",
        "downstreamImpactThreshold",
        "highRiskReasonCodes",
    ):
        if key in override:
            merged[key] = override.get(key)
    return merged


def load_expert_group_policy(root: str, override: Dict[str, Any] = None) -> Dict[str, Any]:
    script_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    search_roots = [script_root, root]
    merged = copy.deepcopy(DEFAULT_EXPERT_GROUP_POLICY)

    for base in search_roots:
        for rel in EXPERT_GROUP_POLICY_CONFIG_CANDIDATES:
            path = os.path.join(base, rel)
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    merged = merge_expert_group_policy(merged, loaded)
            except Exception as err:
                LOGGER.warning(
                    "failed to load expert-group policy: path=%s error=%s",
                    path,
                    str(err),
                )
                continue

    if isinstance(override, dict):
        merged = merge_expert_group_policy(merged, override)
    return normalize_expert_group_policy(merged, DEFAULT_EXPERT_GROUP_POLICY)


def policy_digest(policy: Dict[str, Any]) -> str:
    normalized = normalize_expert_group_policy(policy if isinstance(policy, dict) else {}, DEFAULT_EXPERT_GROUP_POLICY)
    encoded = json.dumps(normalized, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _extract_int(snapshot: Dict[str, Any], keys: List[str]) -> int:
    if not isinstance(snapshot, dict):
        return 0
    for key in keys:
        if key not in snapshot:
            continue
        value = _safe_int(snapshot.get(key), 0)
        if value >= 0:
            return value
    return 0


def evaluate_trigger(task_snapshot: Dict[str, Any], runtime_snapshot: Dict[str, Any], policy: Dict[str, Any]) -> Dict[str, Any]:
    task = task_snapshot if isinstance(task_snapshot, dict) else {}
    runtime = runtime_snapshot if isinstance(runtime_snapshot, dict) else {}
    normalized_policy = normalize_expert_group_policy(policy if isinstance(policy, dict) else {}, DEFAULT_EXPERT_GROUP_POLICY)

    reasons: List[str] = []
    if not normalized_policy.get("enabled"):
        return {"triggered": False, "reasons": reasons, "score": 0}

    retry_count = max(
        _extract_int(runtime, ["retryCount", "attempt", "blockedRetries"]),
        _extract_int(task, ["retryCount", "blockedRetries"]),
    )
    if retry_count >= _safe_int(normalized_policy.get("blockedRetriesThreshold"), 2):
        reasons.append("retry_limit")

    blocked_duration_minutes = max(
        _extract_int(task, ["blockedDurationMinutes", "blockedMinutes"]),
        _extract_int(runtime, ["blockedDurationMinutes", "blockedMinutes"]),
    )
    if blocked_duration_minutes >= _safe_int(normalized_policy.get("blockedDurationMinutes"), 30):
        reasons.append("blocked_duration")

    downstream_impact = max(
        _extract_int(task, ["downstreamImpact", "blockedDependents", "impactedDownstreamTasks"]),
        _extract_int(runtime, ["downstreamImpact", "blockedDependents", "impactedDownstreamTasks"]),
    )
    if downstream_impact >= _safe_int(normalized_policy.get("downstreamImpactThreshold"), 2):
        reasons.append("downstream_impact")

    reason_code = str(runtime.get("reasonCode") or task.get("reasonCode") or "").strip().lower()
    high_risk_codes = set(_normalize_reason_codes(normalized_policy.get("highRiskReasonCodes"), []))
    if reason_code and reason_code in high_risk_codes:
        reasons.append("high_risk_reason")

    score = len(reasons)
    return {"triggered": bool(score > 0), "reasons": reasons, "score": score}


def build_expert_templates(
    reasons: List[str],
    task_snapshot: Dict[str, Any] = None,
    runtime_snapshot: Dict[str, Any] = None,
    roles: List[str] = None,
) -> List[Dict[str, Any]]:
    task = task_snapshot if isinstance(task_snapshot, dict) else {}
    runtime = runtime_snapshot if isinstance(runtime_snapshot, dict) else {}
    normalized_roles = _normalize_roles(roles)
    normalized_reasons = _normalize_reason_codes(reasons, [])

    reason_lines = _reason_context_lines(normalized_reasons)
    if not reason_lines:
        reason_lines = ["generic_blocker: no explicit trigger reason, perform broad blocker triage"]
    reason_text = "; ".join(reason_lines)
    task_id = str(task.get("taskId") or "").strip() or "unknown-task"
    status = str(task.get("status") or "").strip().lower() or "blocked"
    runtime_reason_code = str(runtime.get("reasonCode") or "").strip().lower()

    templates: List[Dict[str, Any]] = []
    for role in normalized_roles:
        role_focus = ROLE_TASK_PREFIX.get(role, "Perform role-specific blocker analysis and propose an actionable plan.")
        runtime_hint = f" Runtime reasonCode={runtime_reason_code}." if runtime_reason_code else ""
        task_description = (
            f"[{role}] Task {task_id} is currently {status}. {role_focus} "
            f"Trigger reasons: {reason_text}.{runtime_hint}"
        ).strip()
        templates.append(
            {
                "role": role,
                "task": task_description,
                "requiredFields": list(EXPERT_OUTPUT_FIELDS),
                "fieldGuidance": dict(FIELD_GUIDANCE),
            }
        )
    return templates


def _default_consensus_plan(reasons: List[str]) -> str:
    normalized_reasons = _normalize_reason_codes(reasons, [])
    if normalized_reasons:
        return f"Run expert triage and execute the highest-confidence fix for reasons: {', '.join(normalized_reasons)}."
    return "Run expert triage, gather evidence, and execute the highest-confidence fix."


def _default_checklist(reasons: List[str]) -> List[str]:
    normalized_reasons = _normalize_reason_codes(reasons, [])
    if normalized_reasons:
        return [
            f"Validate trigger reasons: {', '.join(normalized_reasons)}.",
            "Collect hypothesis, evidence, confidence, proposedFix, and risk from each expert.",
            "Select the highest-confidence proposedFix and schedule execution ownership.",
        ]
    return [
        "Collect hypothesis, evidence, confidence, proposedFix, and risk from each expert.",
        "Select the highest-confidence proposedFix and schedule execution ownership.",
        "Track risk mitigation and confirm blocker resolution evidence.",
    ]


def _default_acceptance_gate() -> List[str]:
    return [
        "Consensus plan references verifiable evidence.",
        "Owner is explicitly assigned for execution.",
        "Risks are documented with mitigation actions.",
    ]


def _extract_confidence(entry: Dict[str, Any]) -> float:
    raw = entry.get("confidence")
    confidence = _safe_float(raw, 0.0)
    if confidence > 1.0:
        confidence = confidence / 100.0
    return max(0.0, min(1.0, confidence))


def is_valid_expert_output(entry: Any, min_confidence: float = 0.01) -> bool:
    if not isinstance(entry, dict):
        return False
    proposed_fix = str(entry.get("proposedFix") or "").strip()
    confidence = _extract_confidence(entry)
    has_analysis = bool(
        str(entry.get("hypothesis") or "").strip()
        or str(entry.get("evidence") or "").strip()
        or str(entry.get("risk") or "").strip()
    )
    return bool(proposed_fix and confidence >= max(0.0, float(min_confidence)) and has_analysis)


def filter_valid_expert_outputs(expert_outputs: Any) -> List[Dict[str, Any]]:
    if not isinstance(expert_outputs, list):
        return []
    out: List[Dict[str, Any]] = []
    for raw in expert_outputs:
        if not isinstance(raw, dict):
            continue
        if is_valid_expert_output(raw):
            out.append(raw)
    return out


def converge_expert_conclusions(
    expert_outputs: List[Dict[str, Any]],
    reasons: List[str] = None,
    fallback_owner: str = "orchestrator",
    active: bool = True,
) -> Dict[str, Any]:
    owner_fallback = _canonical_role(fallback_owner) or str(fallback_owner or "").strip() or "orchestrator"
    if not active:
        return neutral_consensus(owner_fallback)

    base_plan = _default_consensus_plan(reasons or [])
    out: Dict[str, Any] = {
        "consensusPlan": base_plan,
        "owner": owner_fallback,
        "executionChecklist": _default_checklist(reasons or []),
        "acceptanceGate": _default_acceptance_gate(),
        "inactive": False,
    }

    if not isinstance(expert_outputs, list) or not expert_outputs:
        return out
    valid_outputs = filter_valid_expert_outputs(expert_outputs)
    if not valid_outputs:
        return out

    best_entry: Dict[str, Any] = {}
    best_score = -1.0
    extra_checklist: List[str] = []
    extra_gates: List[str] = []

    for raw in valid_outputs:
        entry = raw if isinstance(raw, dict) else {}
        hypothesis = str(entry.get("hypothesis") or "").strip()
        evidence = str(entry.get("evidence") or "").strip()
        proposed_fix = str(entry.get("proposedFix") or "").strip()
        risk = str(entry.get("risk") or "").strip()
        confidence = _extract_confidence(entry)

        score = confidence
        if proposed_fix:
            score += 1.0
        if evidence:
            score += 0.5
        if hypothesis:
            score += 0.25
        if score > best_score:
            best_score = score
            best_entry = entry

        if risk:
            extra_checklist.append(f"Mitigate stated risk: {risk}")
            extra_gates.append(f"Risk mitigated: {risk}")
        if evidence:
            extra_gates.append(f"Evidence verified: {evidence}")

    if not best_entry:
        return out

    best_owner = _canonical_role(best_entry.get("role")) or _canonical_role(best_entry.get("owner")) or str(best_entry.get("owner") or "").strip()
    best_hypothesis = str(best_entry.get("hypothesis") or "").strip()
    best_fix = str(best_entry.get("proposedFix") or "").strip()
    best_evidence = str(best_entry.get("evidence") or "").strip()

    if best_fix:
        out["consensusPlan"] = best_fix
    elif best_hypothesis:
        out["consensusPlan"] = best_hypothesis

    if best_owner:
        out["owner"] = best_owner

    checklist: List[str] = []
    if best_fix:
        checklist.append(f"Execute fix: {best_fix}")
    if best_hypothesis:
        checklist.append(f"Validate hypothesis: {best_hypothesis}")
    if best_evidence:
        checklist.append(f"Confirm evidence: {best_evidence}")
    checklist.extend(extra_checklist)
    if checklist:
        out["executionChecklist"] = checklist

    gates: List[str] = []
    if best_evidence:
        gates.append(f"Primary evidence reviewed: {best_evidence}")
    gates.extend(extra_gates)
    if gates:
        deduped: List[str] = []
        seen = set()
        for item in gates:
            normalized = str(item).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        if deduped:
            out["acceptanceGate"] = deduped

    return out
