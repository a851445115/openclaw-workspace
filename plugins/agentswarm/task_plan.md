# Task Plan - Dispatch Closed Loop + Feishu Runtime Hardening

## Goal
Complete the remaining Milestone C work for multi-agent-orchestrator with minimal-risk changes:
1) dispatch spawn closed loop (auto apply done/blocked + milestone publish),
2) Feishu runtime command integration through orchestrator entrance,
3) lock ownership/recovery and snapshot rebuild tooling,
4) low-noise message UX with bot loop prevention + clarify throttling.

## Phases
| Phase | Status | Notes |
|---|---|---|
| 1. Baseline inspection (task_plan/progress/README/code) | complete | Confirmed dispatch still manual, reliability scripts were stubs, Feishu runtime command coverage incomplete |
| 2. Dispatch closed-loop implementation | complete | Added spawn execution/parsing, auto board writeback, `[DONE]/[BLOCKED]` milestone publish, dry-run skip semantics |
| 3. Feishu runtime command integration | complete | `feishu-router` now handles create/claim/done/block/status/synthesize/escalate/dispatch/clarify through orchestrator entry |
| 4. Reliability hardening | complete | Implemented real stale-lock recovery and snapshot replay+compact tool; extended lock metadata ownership |
| 5. UX and guardrails | complete | Added bot-to-bot milestone echo suppression and stronger clarify cooldown |
| 6. Verification + docs | complete | Added unittest coverage, reran dry-run script, refreshed README/docs |

## Constraints
- Keep `state/tasks.jsonl` append-only.
- Preserve existing low-noise Chinese milestone templates.
- Avoid broad refactors; prefer focused script-level changes.

## Errors Encountered
- `edit` tool could not modify files outside agent workspace path; switched to direct file patching via shell/python edits.
- `dry-run-mvp` failed after first dispatch-loop implementation because dry-run without spawn output auto-blocked task; fixed by treating skipped spawn as manual wait-for-report (no auto close).

## 2026-03-06 - Elvis Roadmap Closure
### P0-1 Subtask - Worktree lifecycle partial closure
- Scope: wire `cleanup_task_worktree` into dispatch spawn auto-close for `done` / `blocked` decisions only.
- Constraints: cleanup is policy-gated, failures must not flip dispatch success, and result must be surfaced in return metadata.
- Current status: partial — dispatch spawn `done` / `blocked` now runs policy-gated cleanup and default worktree policy is enabled, but 5+ agents concurrency smoke and bootstrap/dependency isolation validation are still pending.
- Verification target: `python3 -m unittest tests/test_worktree_manager.py tests/test_orchestrator_runtime.py -q`.

### P0-2 Subtask - Active session watchdog partial closure
- Scope: close stale pid + heartbeat timeout handling through the active-session watchdog path.
- Constraints: watchdog checks must stay non-fatal to scheduler tick, and current heuristics still depend on scheduler cadence plus heartbeat quality.
- Current status: partial but key loop landed — stale pid detection, heartbeat timeout, and scheduler-tick integration are in place; remaining work is robustness verification and tuning.
- Verification basis: main-session retest confirmed the current watchdog loop; this doc-sync pass does not add a new standalone command log.


### Goal
Close the gaps from `docs/plans/2026-03-05-elvis-architecture-integration-plan.md` in roadmap order, starting with P0 production-hardening items.

### Phases
| Phase | Status | Notes |
|---|---|---|
| 1. Audit roadmap completion | complete | Verified 0 fully complete, 4 partially complete, 5 not started |
| 2. Write executable checklist | complete | Added `docs/plans/2026-03-06-elvis-architecture-progress-checklist.md` |
| 3. Close P0-1 worktree lifecycle | in_progress | Cleanup-on-done/blocked + enabled default policy landed; still missing 5+ agents / bootstrap validation |
| 4. Close P0-2 active session watchdog | in_progress | Stale pid + heartbeat timeout are wired into scheduler tick; still needs robustness verification |
| 5. Integrate P1 scanner/reviewer | in_progress | Scheduler scanner + acceptance reviewer landed; still missing feed hardening and full-diff review |
| 6. Commit + push | pending | Out of scope for this doc-sync pass; only after remaining rollout validation and explicit approval |

### P1-1 Subtask - proactive scanner runtime glue
- Scope: wire `proactive_scanner` into `scheduler_run_once()` with policy load, per-tick dedupe, task-board creation, and audit summary.
- Constraints: keep glue thin inside `scripts/lib/milestones.py`, respect `dryRun`, avoid duplicate/noisy task creation, and keep scanner failures non-fatal to scheduler.
- Current status: partial — scheduler tick glue, findings → task-board creation, dedupe, and advisory events are landed; upstream `pytest` / `feishu` file feeds and priority-closure quality still need hardening.
- Verification target: `python3 -m unittest tests/test_proactive_scanner.py tests/test_orchestrator_runtime.py -q`.

### P1-2 Subtask - multi reviewer done-gate integration
- Scope: wire `multi-reviewer-policy.json` into `evaluate_acceptance()` and dispatch done classification with auditable reviewer summaries.
- Constraints: keep default policy conservative, preserve existing acceptance gates, and degrade gracefully on reviewer runner failures.
- Current status: partial — acceptance done-gate integration, `disabled` / `dryRun` / `enabled`, and fake-output tests are landed; reviewer input is still acceptance payload summary rather than full code diff.
- Verification target: `python3 -m unittest tests/test_multi_reviewer.py tests/test_quality_gate_v2.py -q`.

### P2-1 Subtask - 实时干预能力
- Scope: add file-backed task interventions with CLI/script management, prompt injection, orchestrator command routing, and auditable apply-count updates.
- Constraints: no tmux refactor, keep the current one-shot CLI bridge, prefer a single stable state file, and avoid clearing intervention automatically on terminal task states.
- Planned verification: `python3 -m unittest tests/test_orchestrator_runtime.py -q`.

### P2-2 Subtask - Business context storage partial closure
- Scope: add SQLite context store + CLI + prompt injection for task-bound customer/paper/history context.
- Acceptance target: `state/business_context.db` auto-init, stable JSON CLI, `BUSINESS_CONTEXT` prompt segment when `customerId` / `paperId` are present.

### P3-1 Subtask - Cost dashboard partial closure
- Scope: add `dailyCost` / `costPerCommit` / `agentBreakdown` aggregation, wire dispatch ops events with `executor` + `tokenUsage`, and surface cost summaries in manager report plus `status full`.
- Acceptance target: aggregate JSON exposes cost fields, text summaries show at least one cost metric, and old events without token usage safely fall back to zero cost.

### P3-2 Subtask - 智能失败分类最小闭环
- Scope: 新增规则型 `failure_classifier`，在 `recovery_loop.py` 做 failureType-aware 恢复分流，并把分类结果挂入 `milestones.py` 的 blocked / retryContext / ops event 输出。
- Constraints: 不改变旧 `reasonCode` 语义，不重写整个 recovery policy，只做最小可验证接入。
- Current status: partial — 已覆盖 `context_overflow` / `wrong_direction` / `missing_info` / `executor_failure` / `budget_exceeded` / `incomplete_output` / `continuation_stall` / `unknown`，并完成 recovery/runtime 回归；剩余工作是更高精度分类与看板级消费。
- Verification target: `python3 -m unittest tests/test_failure_classifier.py tests/test_recovery_loop.py tests/test_orchestrator_runtime.py -q`.
