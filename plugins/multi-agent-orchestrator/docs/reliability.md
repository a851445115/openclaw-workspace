# Reliability Hardening Design (Milestone C)

## Goals

- Make orchestration resilient to transient failures.
- Ensure lock safety for concurrent operators/processes.
- Support deterministic rebuild of snapshot state from append-only events.

## 1) Timeout and Retry Policy

Apply policy per external call and critical local operation.

### Timeout Defaults

- command routing: 5s
- local state read/write: 3s
- external message send: 10s
- synthesis step: 15s

### Retry Defaults

- max attempts: 3
- backoff: exponential (base 500ms, multiplier 2)
- jitter: +/-20%
- retryable errors:
  - network timeout
  - temporary API 5xx
  - lock contention timeout

Do not retry on:
- schema/validation failure
- missing credentials
- permission denied

## 2) Stale Lock Recovery Strategy

Lock directory baseline: `state/locks/`.

Proposed lock file structure:

```json
{
  "owner": "<process-or-agent-id>",
  "pid": 12345,
  "createdAt": "2026-02-27T00:00:00Z",
  "expiresAt": "2026-02-27T00:00:30Z",
  "resource": "tasks.snapshot.json"
}
```

Recovery rules:
- lock is stale when current time > `expiresAt` + grace window.
- if owner process is not alive, stale confidence increases.
- recovery script must support dry-run and explicit apply mode.
- every reclaimed lock should be logged with timestamp and old owner.

Safety guardrails:
- never remove lock younger than TTL unless `--force` is used.
- refuse bulk apply without operator confirmation flag.

## 3) Task Replay/Rebuild from `tasks.jsonl`

Rebuild objective:
- reconstruct canonical `tasks.snapshot.json` by replaying events in order.

Algorithm outline:
1. read each line from `state/tasks.jsonl`.
2. parse JSON event and validate required keys.
3. apply event reducer into in-memory task map.
4. compute meta (`version`, `updatedAt`, replay info).
5. compare rebuilt snapshot hash to live snapshot hash.
6. write only when `--apply` is set.

Validation checks during replay:
- duplicate `eventId`
- unknown event type
- invalid state transitions
- missing task for non-create events

## 4) Operational Recommendations

- run stale lock dry-run every 15 minutes in active migration windows.
- run snapshot rebuild dry-run at least daily.
- keep last 7 snapshots with timestamp suffix for quick restore.
- alert on replay mismatch and repeated lock contention.

## 5) Implemented Tooling

- `scripts/recover-stale-locks`
  - dry-run/apply both supported
  - stale判断基于 `expiresAtTs + grace` 与 `pid` 存活
  - apply 模式写入 `state/locks/recovery.audit.jsonl` 审计记录
- `scripts/rebuild-snapshot`
  - 回放 `tasks.jsonl` 重建 snapshot（含错误统计与 diff 摘要）
  - apply 模式原子写出 rebuilt snapshot
  - 可选 `--compact-jsonl` 输出去重压缩后的事件流

## 6) Failure Auto-Recovery & Escalation Loop (Batch 2)

- 策略文件：`config/recovery-policy.json`
  - 默认恢复链：`coder -> debugger -> invest-analyst -> human`
  - 支持按 `reasonCode` 覆盖 `maxAttempts` 与 `cooldownSec`
  - 支持在任务根目录 `config/recovery-policy.json` 覆盖仓库默认策略
- 状态文件：`state/recovery.state.json`
  - 维度：`taskId + reasonCode`
  - 记录字段：`attempt`、`nextAssignee`、`action`、`recoveryState`、`cooldownUntilTs`
  - 冷却未到期时复用上一决策，不递增 attempt
- 触发条件（当前支持）
  - `spawn_failed`
  - `incomplete_output`
  - `blocked_signal`
- 输出字段（dispatch/autopilot 的 `spawn`）
  - `reasonCode`
  - `attempt`
  - `nextAssignee`
  - `action`（`retry` | `escalate` | `human`）
  - 附加：`recoveryState`、`cooldownActive`、`cooldownUntil`
- 行为约定
  - 可恢复时输出下一跳负责人与尝试次数
  - 超预算进入 `escalated_to_human`
  - `incomplete_output` 的 `retry` 默认保持 `blocked` 并附带 `recovery_pending:<nextAssignee>`，兼容既有阻塞门禁

## 7) Cost/Budget Governance (Batch 6)

- 策略文件：`config/budget-policy.json`
  - `global.maxTaskTokens`：单任务累计 token 上限
  - `global.maxTaskWallTimeSec`：单任务累计执行时长上限（秒）
  - `global.maxTaskRetries`：单任务累计 spawn 重试/执行次数上限
  - `global.degradePolicy`：降级动作序列（支持 `reduced_context` / `manual_handoff` / `stop_run`）
  - `global.onExceeded`：超预算时默认降级动作
  - `agents.coder`：可按 agent 覆盖上述字段
- 状态文件：`state/budget.state.json`
  - 维度：`taskId + agent`
  - 记录字段：`tokenUsage`、`elapsedMs`、`retryCount`、`updatedAt`
- 执行时机
  - spawn 前：`precheck_budget`，若已耗尽预算则直接 `blocked`
  - spawn 后：`record_and_check_budget`，累积 `token/time/retry` 并判断是否超限
- 超限行为
  - 统一 `reasonCode=budget_exceeded`
  - `nextAssignee=human`
  - `action=escalate`
  - 输出 `degradeAction` 与 `exceededKeys` 便于观测
- 派发输出增强
  - `spawn.metrics.elapsedMs`
  - `spawn.metrics.tokenUsage`
