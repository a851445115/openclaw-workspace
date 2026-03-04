#!/usr/bin/env python3
import copy
import hashlib
import json
import logging
import os
from typing import Any, Dict, List


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
