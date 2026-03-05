# Scheme C: Checkpoint Continuation Protocol (Design + Implementation)

**Date:** 2026-03-05  
**Scope:** Fix false blocked classification for long-running `codex` / `claude` tasks.

## 1) Problem Statement

Current long-running progress is misclassified as blocked:

1. `scripts/lib/codex_worker_bridge.py` and `scripts/lib/claude_worker_bridge.py` normalize worker output to `status in {done, blocked, progress}`.
2. `scripts/lib/milestones.py::classify_spawn_result(...)` has only terminal orchestrator decisions (`done` / `blocked`).
3. When output is neither done nor blocked, it falls through to:
   - `decision = blocked`
   - `reasonCode = no_completion_signal`
4. Dispatch path treats blocked as recovery candidate; `scripts/lib/recovery_loop.py` includes `no_completion_signal` in `RECOVERY_REASON_CODES`, so long-running tasks are retried/escalated prematurely.

**Impact:** normal in-flight work is interpreted as failure, causing false blocked state, unnecessary retries, and context churn.

---

## 2) Scheme C Protocol: Checkpoint Payload

Add a structured `checkpoint` object (optional) in worker reports when `status=progress`.

```json
{
  "status": "progress",
  "summary": "Indexed 42/120 files",
  "checkpoint": {
    "progressPercent": 35,
    "completed": ["Scanned routing layer", "Mapped failing tests"],
    "remaining": ["Patch retries", "Run targeted tests"],
    "nextAction": "Patch retry classifier in milestones.py",
    "continueHint": "continue",
    "stallSignal": "none",
    "evidenceDelta": ["Found fallthrough to no_completion_signal", "2 files identified for patch"]
  }
}
```

### Required checkpoint fields

- `progressPercent` (`int`, `0..100`): coarse completion indicator.
- `completed` (`string[]`): newly finished sub-steps since task start.
- `remaining` (`string[]`): pending sub-steps.
- `nextAction` (`string`): deterministic next step for orchestrator continuation prompting.
- `continueHint` (`string enum`): `continue | need_input | handoff_suggested`.
- `stallSignal` (`string enum`): `none | soft_stall | hard_block`.
- `evidenceDelta` (`string[]`): incremental evidence since previous checkpoint.

Validation rules:
- For `status=progress`, treat missing `checkpoint` as legacy mode (compatible), but prefer continuation only if minimum fields exist (`progressPercent`, `nextAction`, `stallSignal`).
- Unknown fields are allowed (forward-compatible).

---

## 3) Orchestrator Decision State Machine (continue / done / blocked)

Introduce non-terminal decision `continue` in orchestrator classification.

### Decision thresholds (explicit)

Defaults in runtime policy:

- `maxContinuationRounds = 6`
- `noProgressWindowRounds = 2` (consecutive)
- `minProgressDeltaPct = 3`
- `minEvidenceDeltaItems = 1`
- `maxContinuationWallTimeSec = 1800` (or min with budget guardrail if stricter)

Derived per round:

- `progressDelta = checkpoint.progressPercent - last.progressPercent`
- `evidenceDeltaCount = new_unique(checkpoint.evidenceDelta).length`
- `noProgressRound = (progressDelta < minProgressDeltaPct AND evidenceDeltaCount < minEvidenceDeltaItems)`

### State machine

1. **DONE**
   - If status indicates done (`done|completed|success|succeeded`) **and** acceptance passes.
2. **BLOCKED**
   - Worker explicit blocked/failed/error, or `stallSignal=hard_block`.
   - Or `round > maxContinuationRounds`.
   - Or consecutive `noProgressRound >= noProgressWindowRounds`.
   - Or elapsed continuation wall time exceeds `maxContinuationWallTimeSec`.
3. **CONTINUE**
   - `status=progress` or legacy fallthrough previously mapped to `no_completion_signal`.
   - `stallSignal in {none, soft_stall}`.
   - Within rounds/time/progress budgets above.

Result mapping:
- `continue -> decision=continue`, `reasonCode=checkpoint_continue`.
- `blocked due anti-stall -> reasonCode in {continuation_round_limit, continuation_no_progress, continuation_timeout}`.
- Keep existing blocked reason codes for explicit hard failures.

---

## 4) Anti-Stall Policy

Policy goal: allow legitimate long-running execution without infinite looping.

- **Max continuation rounds:** hard cap (`maxContinuationRounds`), then escalate blocked.
- **No-progress window:** block when `noProgressWindowRounds` consecutive checkpoints show insufficient delta.
- **Time budget guardrails:** stop continuation if `maxContinuationWallTimeSec` exceeded; if `budgetPolicy.guardrails.maxTaskWallTimeSec > 0`, effective limit is `min(continuationLimit, budgetLimit)`.
- **Soft stall handling:** `stallSignal=soft_stall` allows one continuation window; if still no progress next round, block with `continuation_no_progress`.
- **Need-input handling:** `continueHint=need_input` becomes blocked with reason `continuation_need_input` (human action required).

---

## 5) Required Code Touch Points

## Milestone A — Protocol ingestion (bridges)

- `scripts/lib/codex_worker_bridge.py`
  - Extend `build_schema()` with `checkpoint` object + required subfields/types.
  - Extend `normalize_result()` to preserve normalized checkpoint.
- `scripts/lib/claude_worker_bridge.py`
  - Same schema + normalize logic as codex bridge.
- Tests:
  - `tests/test_claude_worker_bridge.py`
  - Add `tests/test_codex_worker_bridge.py` (or extend existing bridge coverage) for checkpoint normalization.

## Milestone B — Orchestrator classification/state machine

- `scripts/lib/milestones.py`
  - Add `decision=continue` path in `classify_spawn_result(...)`.
  - Add checkpoint normalization helper and continuation-threshold evaluator.
  - Persist per-task continuation state (`progressPercent`, last evidence hash, round count, timestamps) under `state/continuation.state.json`.
  - In dispatch flow, branch `continue` separately (do not mark task blocked; do not trigger recovery loop escalation).
- `scripts/lib/recovery_loop.py`
  - Gate `no_completion_signal` recovery trigger behind continuation policy, or convert to `checkpoint_continue` path before recovery decision.

## Milestone C — Config wiring

- `scripts/lib/config_runtime.py`
  - Add `orchestrator.continuationPolicy` normalization with defaults and bounds.
- `config/runtime-policy.json` + `config/runtime-policy.example.json`
  - Add continuation policy section.
- `docs/config.md`
  - Document new continuation keys and reasonCode semantics.

Suggested config shape:

```json
{
  "orchestrator": {
    "continuationPolicy": {
      "enabled": true,
      "maxContinuationRounds": 6,
      "noProgressWindowRounds": 2,
      "minProgressDeltaPct": 3,
      "minEvidenceDeltaItems": 1,
      "maxContinuationWallTimeSec": 1800
    }
  }
}
```

## Milestone D — Test coverage

- `tests/test_recovery_loop.py`
  - Update `no_completion_signal` expectations under continuation-enabled mode.
- `tests/test_orchestrator_runtime.py`
  - Add end-to-end continuation rounds and anti-stall transitions.
- `tests/test_quality_gate_v2.py`
  - Ensure done-acceptance behavior unchanged.
- New: `tests/test_checkpoint_continuation.py`
  - Focused unit tests for state machine thresholds and reason codes.

---

## 6) Test Matrix

| Case | Input | Expected |
|---|---|---|
| C1 progress + valid checkpoint + delta | `status=progress`, `stallSignal=none`, progress/evidence grows | `decision=continue`, `reasonCode=checkpoint_continue` |
| C2 progress but no delta within window | consecutive no-progress rounds >= threshold | `decision=blocked`, `reasonCode=continuation_no_progress` |
| C3 round limit reached | round > `maxContinuationRounds` | `blocked`, `continuation_round_limit` |
| C4 continuation timeout | elapsed > `maxContinuationWallTimeSec` | `blocked`, `continuation_timeout` |
| C5 explicit hard blocker | `stallSignal=hard_block` or `status=blocked` | `blocked`, existing hard-failure code |
| C6 done + evidence valid | done + acceptance pass | `done`, `done_with_evidence` |
| C7 done + acceptance fail | done + missing hard evidence | `blocked`, `incomplete_output` (compat preserved) |
| C8 legacy worker no checkpoint | old payload with progress-like output | compatible: continue if heuristics pass, else existing fallback |

---

## 7) Rollout / Rollback

### Rollout

1. **Phase 0 (shadow):** `continuationPolicy.enabled=true` with `decision` shadow logging only (no behavior change), emit metrics:
   - `continuation_entered_total`
   - `continuation_blocked_total` by reason
   - `false_blocked_no_completion_signal_total`
2. **Phase 1 (canary):** enable active `continue` only for codex/claude routed tasks.
3. **Phase 2 (default-on):** enable for all supported worker executors.

### Rollback

- Immediate: set `orchestrator.continuationPolicy.enabled=false` in runtime policy.
- Behavior reverts to current done/blocked-only path without schema break.
- Keep checkpoint payload optional; no bridge rollback required unless schema regression appears.

---

## 8) Acceptance Criteria

- Long-running tasks producing progress checkpoints are not marked blocked solely for missing completion signal.
- `no_completion_signal` false positives drop significantly in ops metrics.
- Existing done acceptance and explicit blocked behaviors remain backward compatible.
- Recovery loop still handles true failures; continuation handles in-flight progress.
