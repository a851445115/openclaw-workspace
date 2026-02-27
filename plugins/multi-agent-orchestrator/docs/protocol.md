# Task Board Protocol (Milestone B)

This protocol is file-backed and runnable in local mode.

## Message Types

- `[TASK]`: task creation events.
- `[CLAIM]`: ownership updates and claim transitions.
- `[DONE]`: completion events.
- `[BLOCKED]`: blocker events.
- `[REVIEW]`: reserved for Milestone C review workflow.
- `[DIAG]`: diagnostic follow-up tasks (debugger role).

Protocol defaults live in `docs/protocol-config.json` and are mirrored in plugin schema.

Milestone publishing format is intentionally low-noise:

- 1-3 lines only.
- Must include `taskId`, status, owner/assignee hint, and one key detail.
- Prefix constrained to protocol tags (`[TASK]/[CLAIM]/[DONE]/[BLOCKED]/[DIAG]`).

## State Files

- `state/tasks.jsonl`: append-only event stream.
- `state/tasks.snapshot.json`: materialized task map used by router/status.
- `state/locks/`: reserved for future lock hardening.

## Command Intents

The command router maps plain text (or direct override text) to intents:

- `create task [task-id]: <title>` -> `create_task`
- `claim task <task-id>` -> `claim_task`
- `mark done <task-id>: <result>` -> `mark_done`
- `block task <task-id>: <reason>` -> `block_task`
- `escalate task <task-id>: <reason>` -> `escalate_task`
- `status [task-id]` -> `status`
- `synthesize [task-id]` -> `synthesize`

### Escalate to Debugger

`escalate task` is the built-in hook to integrate the `debugger` role into the workflow:

- It blocks the original task with `[BLOCKED]`.
- It creates a new diagnostic follow-up task with `[DIAG]`.
- The follow-up task includes `assigneeHint=debugger` and `relatedTo=<originalTaskId>`.

Example:

- `escalate task T-101: feishu webhook auth failing`

## Direct @agent Override

Prefix command text with `@agent-name` to attach routing metadata:

- Example: `@debugger create task T-900: investigate stale locks`
- Router behavior in Milestone B: records `overrideAgent` metadata only.
- Transport layer behavior is intentionally unchanged.

## Visibility vs Execution

- Source of truth remains local task board files (`tasks.jsonl` + snapshot).
- Default execution is manual dispatch: orchestrator sends `[CLAIM]`/`[TASK]` and waits for report-based completion.
- Optional subagent execution can be enabled with `--dispatch-mode subagent`.
- Group messages are milestone summaries for human observability; they are not task state.

## Broadcast Guardrails

- Default active broadcaster: `orchestrator`.
- Optional secondary broadcaster: `broadcaster` (explicit opt-in).
- Clarify requests are orchestrator-only and role-targeted with cooldown throttle.

## Synthesis Pipeline (Stub)

`synthesize` aggregates task board entries with status `done`, `review`, or `blocked` and returns a report string:

- Includes task id, status, owner, optional `relatedTo`, and result/review/block reason.
- Designed as a Milestone B bridge until cross-agent output collection is added.

## Milestone C Hooks

- Real lock ownership + stale lock recovery.
- Review assignment and `[REVIEW]` state transitions.
- Event replay and snapshot compaction tooling.


## Feishu Orchestrator Commands (MVP)

- `@orchestrator create project <name>: <task1>; <task2>; ...`
- `@orchestrator run [taskId]`
- `@orchestrator status [taskId]`
  - 无 taskId: 返回中文摘要（状态计数 + 阻塞Top + 待推进Top）
  - `status all` / `status full`: 返回扩展列表（仍有上限）

Wake-up v1: team members report progress/completion with `@orchestrator` (include task id like `T-001`).
`@orchestrator run [taskId]` 默认只做认领+派发，不会自动完结。成员回报后由 orchestrator 更新为 `[DONE]` / `[BLOCKED]`。
若指定已完成任务（`done`），会返回幂等提示：`[DONE] T-xxx 已完成，无需重复执行`，且不改状态。

Inbound wrapper parsing helper: `scripts/feishu-inbound-router` (used by orchestrator agent runtime).
