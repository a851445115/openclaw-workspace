#!/usr/bin/env python3
from typing import Any, Callable, Dict, List, Optional


DEFAULT_REVIEWERS: List[Dict[str, Any]] = [
    {"model": "codex", "weight": 0.4, "enabled": True},
    {"model": "claude", "weight": 0.3, "enabled": True},
    {"model": "gemini", "weight": 0.3, "enabled": True},
]
DEFAULT_POLICY: Dict[str, Any] = {
    "enabled": False,
    "dryRun": True,
    "passThreshold": 0.7,
    "allowDegradedPass": True,
    "minSuccessfulReviewers": 1,
    "reviewers": DEFAULT_REVIEWERS,
}


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"1", "true", "yes", "on"}:
            return True
        if token in {"0", "false", "no", "off"}:
            return False
    return bool(default)


def _safe_float(value: Any, default: float) -> float:
    try:
        out = float(value)
        return out
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int) -> int:
    try:
        out = int(value)
        return out
    except Exception:
        return int(default)


def _normalize_reviewers(raw_reviewers: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen: set = set()
    source = raw_reviewers if isinstance(raw_reviewers, list) else list(DEFAULT_REVIEWERS)

    for item in source:
        if isinstance(item, str):
            model = _as_text(item).lower()
            weight = 1.0
            enabled = True
        elif isinstance(item, dict):
            model = _as_text(item.get("model") or item.get("name")).lower()
            weight = max(0.0, _safe_float(item.get("weight"), 0.0))
            enabled = _safe_bool(item.get("enabled"), True)
        else:
            continue
        if not model or model in seen:
            continue
        seen.add(model)
        rows.append({"model": model, "weight": weight, "enabled": enabled})

    if not rows:
        rows = [dict(item) for item in DEFAULT_REVIEWERS]

    enabled_rows = [row for row in rows if bool(row.get("enabled"))]
    if enabled_rows:
        total = sum(max(0.0, _safe_float(row.get("weight"), 0.0)) for row in enabled_rows)
        if total <= 0:
            even = 1.0 / float(len(enabled_rows))
            for row in enabled_rows:
                row["weight"] = even
        else:
            for row in enabled_rows:
                row["weight"] = max(0.0, _safe_float(row.get("weight"), 0.0)) / total
    for row in rows:
        if not bool(row.get("enabled")):
            row["weight"] = 0.0
    return rows


def normalize_reviewer_policy(raw_policy: Any) -> Dict[str, Any]:
    source = raw_policy if isinstance(raw_policy, dict) else {}
    merged: Dict[str, Any] = dict(DEFAULT_POLICY)
    for key in ("enabled", "dryRun", "passThreshold", "allowDegradedPass", "minSuccessfulReviewers", "reviewers"):
        if key in source:
            merged[key] = source.get(key)

    pass_threshold = _safe_float(merged.get("passThreshold"), float(DEFAULT_POLICY["passThreshold"]))
    min_successful = max(1, _safe_int(merged.get("minSuccessfulReviewers"), int(DEFAULT_POLICY["minSuccessfulReviewers"])))
    return {
        "enabled": _safe_bool(merged.get("enabled"), bool(DEFAULT_POLICY["enabled"])),
        "dryRun": _safe_bool(merged.get("dryRun"), bool(DEFAULT_POLICY["dryRun"])),
        "passThreshold": min(1.0, max(0.0, pass_threshold)),
        "allowDegradedPass": _safe_bool(merged.get("allowDegradedPass"), bool(DEFAULT_POLICY["allowDegradedPass"])),
        "minSuccessfulReviewers": min_successful,
        "reviewers": _normalize_reviewers(merged.get("reviewers")),
    }


def _score_from_output(raw: Any) -> Optional[float]:
    if isinstance(raw, (int, float)):
        return min(1.0, max(0.0, float(raw)))
    if not isinstance(raw, dict):
        return None
    for key in ("score", "overallScore", "totalScore"):
        if key in raw:
            return min(1.0, max(0.0, _safe_float(raw.get(key), 0.0)))
    return None


def aggregate_review_scores(policy: Any, reviewer_outputs: Any) -> Dict[str, Any]:
    normalized_policy = normalize_reviewer_policy(policy)
    outputs = reviewer_outputs if isinstance(reviewer_outputs, dict) else {}

    breakdown: List[Dict[str, Any]] = []
    weighted_total = 0.0
    successful = 0
    requested = 0
    missing_models: List[str] = []

    for reviewer in normalized_policy.get("reviewers") or []:
        model = _as_text(reviewer.get("model")).lower()
        enabled = bool(reviewer.get("enabled"))
        weight = _safe_float(reviewer.get("weight"), 0.0)
        if enabled:
            requested += 1

        raw_output = outputs.get(model) if isinstance(outputs, dict) else None
        score = _score_from_output(raw_output)
        if not enabled:
            breakdown.append(
                {
                    "model": model,
                    "enabled": False,
                    "weight": 0.0,
                    "score": None,
                    "ok": True,
                    "reason": "reviewer_disabled",
                }
            )
            continue

        if score is None:
            missing_models.append(model)
            breakdown.append(
                {
                    "model": model,
                    "enabled": True,
                    "weight": weight,
                    "score": None,
                    "ok": False,
                    "reason": "missing_or_invalid_score",
                }
            )
            continue

        successful += 1
        weighted_total += max(0.0, weight) * score
        notes = _as_text((raw_output or {}).get("notes") if isinstance(raw_output, dict) else "")
        breakdown.append(
            {
                "model": model,
                "enabled": True,
                "weight": weight,
                "score": score,
                "ok": True,
                "notes": notes,
                "reason": "",
            }
        )

    total_score = min(1.0, max(0.0, weighted_total))
    degraded = bool(missing_models)
    reason = ""
    if degraded:
        reason = "missing_review_outputs:" + ",".join(missing_models)

    return {
        "totalScore": total_score,
        "breakdown": breakdown,
        "successfulReviewers": successful,
        "requestedReviewers": requested,
        "degraded": degraded,
        "reason": reason,
    }


def decide_review_pass(
    total_score: Any,
    policy: Any,
    degraded: bool = False,
    successful_reviewers: Optional[int] = None,
) -> Dict[str, Any]:
    normalized_policy = normalize_reviewer_policy(policy)
    threshold = _safe_float(normalized_policy.get("passThreshold"), float(DEFAULT_POLICY["passThreshold"]))
    min_successful = _safe_int(
        normalized_policy.get("minSuccessfulReviewers"),
        int(DEFAULT_POLICY["minSuccessfulReviewers"]),
    )
    score = min(1.0, max(0.0, _safe_float(total_score, 0.0)))

    if bool(degraded):
        if bool(normalized_policy.get("allowDegradedPass")):
            return {"pass": True, "decision": "pass_degraded", "reason": "degraded_but_allowed", "threshold": threshold}
        return {"pass": False, "decision": "blocked_degraded", "reason": "degraded_not_allowed", "threshold": threshold}

    if successful_reviewers is not None and int(successful_reviewers) < max(1, min_successful):
        return {
            "pass": False,
            "decision": "blocked_insufficient_reviewers",
            "reason": f"successful_reviewers<{max(1, min_successful)}",
            "threshold": threshold,
        }

    if score >= threshold:
        return {"pass": True, "decision": "pass_threshold_met", "reason": "", "threshold": threshold}
    return {"pass": False, "decision": "blocked_threshold", "reason": "score_below_threshold", "threshold": threshold}


def run_multi_review(
    changes: Any,
    policy: Any = None,
    runner: Optional[Callable[[Dict[str, Any], Any, Optional[Dict[str, Any]]], Any]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    normalized_policy = normalize_reviewer_policy(policy or {})
    base: Dict[str, Any] = {
        "ok": True,
        "totalScore": 0.0,
        "breakdown": [],
        "conclusion": {},
        "degraded": False,
        "reason": "",
        "policy": normalized_policy,
    }

    if not bool(normalized_policy.get("enabled")):
        base["degraded"] = True
        base["reason"] = "review_disabled_by_policy"
        base["conclusion"] = {
            "pass": True,
            "decision": "skipped_disabled",
            "reason": "review_disabled_by_policy",
        }
        return base

    if bool(normalized_policy.get("dryRun")):
        base["degraded"] = True
        base["reason"] = "review_dry_run"
        base["conclusion"] = {"pass": True, "decision": "skipped_dry_run", "reason": "review_dry_run"}
        return base

    if runner is None:
        base["degraded"] = True
        base["reason"] = "runner_unavailable"
        base["conclusion"] = decide_review_pass(0.0, normalized_policy, degraded=True, successful_reviewers=0)
        return base

    outputs: Dict[str, Any] = {}
    runner_errors: List[str] = []
    for reviewer in normalized_policy.get("reviewers") or []:
        if not bool(reviewer.get("enabled")):
            continue
        model = _as_text(reviewer.get("model")).lower()
        try:
            outputs[model] = runner(reviewer, changes, context or {})
        except Exception as err:
            runner_errors.append(f"{model}:{err.__class__.__name__}")

    aggregate = aggregate_review_scores(normalized_policy, outputs)
    degraded = bool(aggregate.get("degraded")) or bool(runner_errors)
    reason_tokens: List[str] = []
    agg_reason = _as_text(aggregate.get("reason"))
    if agg_reason:
        reason_tokens.append(agg_reason)
    if runner_errors:
        reason_tokens.append("runner_errors:" + ",".join(runner_errors))
    reason = "; ".join(reason_tokens)
    conclusion = decide_review_pass(
        aggregate.get("totalScore"),
        normalized_policy,
        degraded=degraded,
        successful_reviewers=aggregate.get("successfulReviewers"),
    )
    return {
        "ok": True,
        "totalScore": aggregate.get("totalScore"),
        "breakdown": aggregate.get("breakdown") or [],
        "conclusion": conclusion,
        "degraded": degraded,
        "reason": reason,
        "policy": normalized_policy,
    }
