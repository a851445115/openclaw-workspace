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
- P2-1 intervention 方案选择：采用 `state/interventions.json` 单文件，而不是按任务分文件；原因是这轮更容易做原子写、dry-run 拷贝、脚本测试与统一审计。
- P2-1 生命周期策略：任务 `done` / `blocked` 时默认保留 intervention，直到显式 `clear`；这样最利于审计，也避免 retry / reopen / continuation 丢失人为纠偏信息。
- P2-1 prompt 消费语义：不做“一次性消费”，而是在每次构造真实 agent prompt 时递增 `applyCount` 并更新时间，满足后续升级到更复杂消费策略的兼容性。
- `orchestrator-router` 无需额外专门分支即可支持 intervention：其现有 fallback 会把未知 orchestrator 指令交给 `feishu-router`，而后者现在已识别 `intervene` / `intervention` / `clear intervention`。
- 为保持审计可读性，本次额外记录 `lastAppliedAt`，从而不需要复用 `updatedAt` 表达“最后一次 prompt 注入”。

- P2-2 最小闭环选择为“SQLite store + CLI + prompt injection”，先不扩飞书/控制台命令，避免把业务上下文能力和命令面耦合。
- `task-context-map.json` 已天然可承载 `customerId` / `paperId`，因此只需在读取侧做 helper，兼容旧任务上下文格式。

- P3-1 采用内置 executor 价目表而不是新增配置文件：这轮只做低风险最小闭环，后续如果要接真实账单再把价目表外移。
- `agentBreakdown` 实际按 executor 聚合更稳定，因为 dispatch ops event 已稳定持有 `executor`，而 agent 名称可能跨模型复用。
- 对无 `tokenUsage` 的旧事件统一回落到 0 成本，但仍保留在 `agentBreakdown` 的 `count` 中，便于观察执行量与成本视角并存。

- P3-2 finding: 直接用旧 `reasonCode` 做恢复会把“缺上下文”“方向跑偏”“上下文溢出”都压成同一种 blocked 语义，恢复动作不够精细。
- P3-2 finding: 低风险方案是保留旧 `reasonCode` 作为兼容主键，再额外挂 `failureType` / `normalizedReason` / `recoveryStrategy` / `signals`，让现有 JSON 消费方不被破坏。
- P3-2 finding: `no_completion_signal` 不能一刀切视作 `continuation_stall`，否则会破坏既有自动重试链；更稳妥的是只在文本显式出现 stalled/continue-midway 信号时再升级为 `continuation_stall`。
- P3-2 finding: `blocked_signal` 也不能默认归类为 `missing_info`，否则会把原本的人类升级分支误降级成自动重试；需要更具体的 secret/schema/clarify 信号再触发。

- Advanced P3-3 visual evidence requirements to partial closure.
- Extended `config/acceptance-policy.json` with compatible defaults for `requireTypes`, `minScreenshots`, `requireComparison`, and `minPlots`.
- Wired visual evidence checks into `scripts/lib/milestones.py::evaluate_acceptance()` using low-risk helpers layered on top of existing normalized evidence + structured report data.
- Landed explicit acceptance reason codes for missing required evidence types, insufficient screenshots, missing comparison evidence, and insufficient plot evidence.
- Fixed two counting pitfalls during audit: ignored synthetic `test:` evidence wrappers for visual classification, and prevented `截图` from being miscounted as generic plot evidence.
- Extended `tests/test_quality_gate_v2.py` with compatibility, require-types, min-screenshots, require-comparison, min-plots, and all-green coverage.
- Verification passed: `python3 -m unittest tests/test_quality_gate_v2.py tests/test_orchestrator_runtime.py -q` (132 tests, OK).
- Gemini/Codex 路由切换 finding: 现有 `runtime-policy.json` 默认值本身已经是 `codex_cli`，真正还会把规划类任务送去 `claude_cli` 的点在 `scripts/lib/milestones.py::resolve_spawn_plan()` 的高智能覆盖逻辑。
- Gemini/Codex 路由切换 finding: 写作/文字任务的 `gemini_cli` override 应继续保留，因为这是用户新策略里唯一的非 codex 特例。
- Codex bridge finding: `codex exec --help` 能明确确认 `--model` 与 `-c key=value` 配置覆盖路径，因此可以安全显式传 `--model gpt-4.5` 与 `-c model_reasoning_effort=...`。
- Main-session verification finding: `codex exec --model gpt-4.5 -c 'model_reasoning_effort="xhigh"'` 在本机可真实运行，并显示 `reasoning effort: xhigh`，因此默认值可以直接使用 `xhigh`，无需保守降级到 `high`。
- Host blocker finding: 本轮后续验证与真实 smoke run 并非被插件逻辑阻塞，而是被宿主机全局进程 / `fork` 资源耗尽阻塞；错误表现为 `fork: Resource temporarily unavailable` 与 `Failed to create unified exec process: Resource temporarily unavailable (os error 35)`。
