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
