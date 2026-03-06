# Findings

## 2026-02-28
- `cmd_dispatch` previously only sent `[CLAIM]/[TASK]` and waited for report; no spawn result ingestion or board writeback.
- `feishu-router` only handled create/run/status + wake-up report; explicit command intents (`claim/done/block/synthesize/escalate/dispatch/clarify`) were not wired from group messages.
- `recover-stale-locks` and `rebuild-snapshot` were pure TODO stubs.
- Dry-run semantics required special handling: when spawn is skipped in dry-run, dispatch should remain manual (wait-for-report) instead of auto-blocking.
- Bot loop risk exists when orchestrator/bot milestones are re-ingested from Feishu wrappers; guard needed before routing.

## 2026-03-06
- Roadmap audit against `2026-03-05-elvis-architecture-integration-plan.md` still shows 4 partial items: P0-1, P0-2, P1-1, P1-2; none is fully closed yet.
- P0-1 current state: dispatch spawn terminal decisions now run policy-gated `cleanup_task_worktree`, and the default `config/worktree-policy.json` policy is enabled; remaining gap is 5+ agents parallel smoke plus bootstrap/dependency-isolation validation.
- P0-2 current state: `session_registry.py` now closes the stale-session loop with stale pid detection + heartbeat timeout and is invoked from scheduler tick; remaining risk is dependence on scheduler cadence and heartbeat quality.
- P1-1 current state: `proactive_scanner.py` is now wired into `scheduler_run_once()` and supports findings → task board, same-tick/cross-tick dedupe, and advisory ops events, but upstream `pytest` / `feishu` file feeds remain weak.
- P1-2 current state: multi reviewer is now wired after `evaluate_acceptance()` verify commands, supports `disabled` / `dryRun` / `enabled`, and has `AGENTSWARM_MULTI_REVIEW_FAKE_OUTPUT` for deterministic tests; reviewer input is still acceptance payload summary rather than full code diff.
- Current executor routing work (code→codex, writing→gemini, planning→claude) is roadmap-adjacent progress, but not one of the nine roadmap closure items.
- Reused existing `cleanupOnDone` as the conservative terminal-cleanup gate for both done and blocked auto-close decisions; cleanup metadata now lives under `worktree.cleanup` and `spawn.worktreeCleanup`.
- P1-1 scanner 接线当前落在 `scheduler_run_once()`：先做 watchdog，再按 scheduler gate 判断是否真正 tick；只有允许 tick 时才跑 scanner，这样新建任务可被同一轮 autopilot 感知。
- 为避免跨 tick 重复建同类任务，最小实现可同时使用两层去重：同 tick 内按 finding 指纹去重，跨 tick 结合 `state/scanner.registry.json` 与任务标题归一化去重。
- `feishu progress_push` 更适合作为 advisory/ops event 而非默认建任务；否则 scheduler 周期运行会持续制造低价值噪声。
- P1-2 的当前接线点在 `evaluate_acceptance()` 的 verify-commands 之后：这样 reviewer 只给“基础 done 证据已满足”的候选结果做二次判定，不会放大现有误报面。
- 将 reviewer 摘要挂到 `acceptance.multiReviewer`，并再透传到 `spawn.acceptance`，比直接改动主 `spawn.detail` 语义更稳妥，同时满足审计可见性。
- `AGENTSWARM_MULTI_REVIEW_FAKE_OUTPUT` 走真实 `dispatch` 子进程即可稳定复现 reviewer 分数/缺席场景，无需 monkeypatch runner。
