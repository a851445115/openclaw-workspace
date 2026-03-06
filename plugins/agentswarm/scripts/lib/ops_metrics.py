import json
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


OPS_METRICS_REL_PATH = os.path.join("state", "ops.metrics.jsonl")
DISPATCH_METRIC_EVENTS = {"dispatch_done", "dispatch_blocked", "dispatch_continue"}
EXECUTOR_PRICE_PER_1K_TOKENS = {
    "codex_cli": 0.03,
    "claude_cli": 0.06,
    "gemini_cli": 0.005,
    "openclaw_agent": 0.0,
    "budget_guard": 0.0,
    "unknown": 0.0,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def metrics_file_path(root: str) -> str:
    return os.path.join(root, OPS_METRICS_REL_PATH)


def _as_nonneg_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except Exception:
        return None
    if not math.isfinite(parsed):
        return None
    if parsed < 0:
        return None
    return parsed


def _as_nonneg_int(value: Any, default: int = 0) -> int:
    parsed = _as_nonneg_float(value)
    if parsed is None:
        return default
    return max(0, int(parsed))


def _event_ts(row: Dict[str, Any]) -> Optional[float]:
    ts = row.get("ts")
    if ts is not None:
        parsed_ts = _as_nonneg_float(ts)
        if parsed_ts is None:
            return None
        return parsed_ts

    at = str(row.get("at") or "").strip()
    if not at:
        return None
    iso = at
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return None


def _normalize_executor(row: Dict[str, Any]) -> str:
    executor = str(row.get("executor") or "").strip()
    if executor:
        return executor
    agent = str(row.get("agent") or "").strip()
    if agent:
        return agent
    return "unknown"


def _estimate_cost(executor: str, token_usage: int) -> float:
    rate = float(EXECUTOR_PRICE_PER_1K_TOKENS.get(str(executor or "").strip(), 0.0) or 0.0)
    if token_usage <= 0 or rate <= 0:
        return 0.0
    return round((float(token_usage) / 1000.0) * rate, 6)


def append_event(root: str, event: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    evt = str(event or "").strip()
    if not evt:
        raise ValueError("event is required")

    row: Dict[str, Any] = {
        "event": evt,
        "at": now_iso(),
        "ts": int(time.time()),
    }
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in {"event", "at", "ts"}:
                continue
            row[key] = value

    path = metrics_file_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=True) + "\n")
    return row


def load_events(root: str, days: int = 7, now_ts: Optional[float] = None) -> List[Dict[str, Any]]:
    path = metrics_file_path(root)
    if not os.path.exists(path):
        return []

    if now_ts is None:
        now_ts = time.time()

    cutoff_ts: Optional[float] = None
    if int(days) > 0:
        cutoff_ts = float(now_ts) - (int(days) * 86400)

    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            if cutoff_ts is not None:
                evt_ts = _event_ts(row)
                if evt_ts is None or evt_ts < cutoff_ts:
                    continue
            out.append(row)
    return out


def top_agent_breakdown(summary: Dict[str, Any], top_k: int = 3) -> List[Dict[str, Any]]:
    breakdown = summary.get("agentBreakdown") if isinstance(summary.get("agentBreakdown"), dict) else {}
    items: List[Dict[str, Any]] = []
    for executor, raw in breakdown.items():
        bucket = raw if isinstance(raw, dict) else {}
        items.append(
            {
                "executor": str(executor),
                "count": _as_nonneg_int(bucket.get("count"), 0),
                "tokens": _as_nonneg_int(bucket.get("tokens"), 0),
                "estimatedCost": float(_as_nonneg_float(bucket.get("estimatedCost")) or 0.0),
            }
        )
    items.sort(key=lambda item: (-float(item["estimatedCost"]), -int(item["tokens"]), str(item["executor"])))
    return items[: max(0, int(top_k))]


def format_agent_breakdown_summary(summary: Dict[str, Any], top_k: int = 3) -> str:
    items = top_agent_breakdown(summary, top_k=top_k)
    if not items:
        return "-"
    return ", ".join(
        [
            f"{item['executor']}:${float(item['estimatedCost']):.3f}/{int(item['tokens'])}tok/{int(item['count'])}次"
            for item in items
        ]
    )


def aggregate_metrics(root: str, days: int = 7, now_ts: Optional[float] = None) -> Dict[str, Any]:
    now_value = float(now_ts if now_ts is not None else time.time())
    safe_days = int(days)
    if safe_days <= 0:
        safe_days = 7

    rows = load_events(root, days=safe_days, now_ts=now_value)

    done_count = 0
    blocked_count = 0
    blocked_reasons: Dict[str, int] = {}
    recovery_scheduled = 0
    recovery_escalated = 0
    scheduler_tick = 0

    cycle_total = 0.0
    cycle_count = 0
    total_estimated_cost = 0.0
    agent_breakdown: Dict[str, Dict[str, Any]] = {}

    for row in rows:
        event = str(row.get("event") or "").strip()
        if event == "dispatch_done":
            done_count += 1
        elif event == "dispatch_blocked":
            blocked_count += 1
            reason = str(row.get("reasonCode") or "unknown").strip() or "unknown"
            blocked_reasons[reason] = blocked_reasons.get(reason, 0) + 1
        elif event == "recovery_scheduled":
            recovery_scheduled += 1
        elif event == "recovery_escalated":
            recovery_escalated += 1
        elif event == "scheduler_tick":
            scheduler_tick += 1

        if event in {"dispatch_done", "dispatch_blocked"}:
            cycle_ms = _as_nonneg_float(row.get("cycleMs"))
            if cycle_ms is not None:
                cycle_total += cycle_ms
                cycle_count += 1

        if event in DISPATCH_METRIC_EVENTS:
            executor = _normalize_executor(row)
            token_usage = _as_nonneg_int(row.get("tokenUsage"), 0)
            estimated_cost = _estimate_cost(executor, token_usage)
            bucket = agent_breakdown.setdefault(
                executor,
                {
                    "count": 0,
                    "tokens": 0,
                    "estimatedCost": 0.0,
                },
            )
            bucket["count"] = _as_nonneg_int(bucket.get("count"), 0) + 1
            bucket["tokens"] = _as_nonneg_int(bucket.get("tokens"), 0) + token_usage
            bucket["estimatedCost"] = round(float(_as_nonneg_float(bucket.get("estimatedCost")) or 0.0) + estimated_cost, 6)
            total_estimated_cost += estimated_cost

    resolved_total = done_count + blocked_count
    success_rate = (float(done_count) / float(resolved_total)) if resolved_total > 0 else 0.0

    recovery_total = recovery_scheduled + recovery_escalated
    recovery_rate = (float(recovery_scheduled) / float(recovery_total)) if recovery_total > 0 else 0.0

    avg_cycle_ms = (cycle_total / float(cycle_count)) if cycle_count > 0 else 0.0
    total_estimated_cost = round(total_estimated_cost, 6)
    daily_cost = round(total_estimated_cost / float(safe_days), 6) if safe_days > 0 else 0.0
    cost_per_commit = round(total_estimated_cost / float(done_count), 6) if done_count > 0 else 0.0

    normalized_breakdown: Dict[str, Dict[str, Any]] = {}
    for executor in sorted(agent_breakdown.keys()):
        bucket = agent_breakdown[executor]
        normalized_breakdown[executor] = {
            "count": _as_nonneg_int(bucket.get("count"), 0),
            "tokens": _as_nonneg_int(bucket.get("tokens"), 0),
            "estimatedCost": round(float(_as_nonneg_float(bucket.get("estimatedCost")) or 0.0), 6),
        }

    return {
        "windowDays": safe_days,
        "eventsConsidered": len(rows),
        "throughputCompleted": done_count,
        "successRate": success_rate,
        "blockedReasonDistribution": blocked_reasons,
        "recoveryRate": recovery_rate,
        "averageCycleMs": avg_cycle_ms,
        "dailyCost": daily_cost,
        "costPerCommit": cost_per_commit,
        "totalEstimatedCost": total_estimated_cost,
        "agentBreakdown": normalized_breakdown,
        "counts": {
            "dispatchDone": done_count,
            "dispatchBlocked": blocked_count,
            "recoveryScheduled": recovery_scheduled,
            "recoveryEscalated": recovery_escalated,
            "schedulerTick": scheduler_tick,
            "resolved": resolved_total,
        },
    }


def format_core_summary(summary: Dict[str, Any], days: int = 7) -> str:
    done = int(summary.get("throughputCompleted") or 0)
    success_pct = float(summary.get("successRate") or 0.0) * 100.0
    recovery_pct = float(summary.get("recoveryRate") or 0.0) * 100.0
    avg_ms = float(summary.get("averageCycleMs") or 0.0)
    daily_cost = float(summary.get("dailyCost") or 0.0)
    cost_per_commit = float(summary.get("costPerCommit") or 0.0)
    breakdown_text = format_agent_breakdown_summary(summary, top_k=2)

    blocked = summary.get("blockedReasonDistribution")
    blocked_text = "-"
    if isinstance(blocked, dict) and blocked:
        items = sorted(blocked.items(), key=lambda item: (-int(item[1]), str(item[0])))
        blocked_text = ", ".join([f"{k}:{v}" for k, v in items[:3]])

    return (
        f"[OPS] 最近{int(days)}天 | 完成={done} | 成功率={success_pct:.1f}% | "
        f"恢复率={recovery_pct:.1f}% | 平均cycle={avg_ms:.0f}ms | 阻塞={blocked_text} | "
        f"日均成本=${daily_cost:.3f} | 单完成成本=${cost_per_commit:.3f} | 执行器成本Top={breakdown_text}"
    )
