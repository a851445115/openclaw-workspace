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
