# Task Board Protocol (Milestone B)

This protocol is file-backed and runnable in local mode.

## Message Types

- `[TASK]`: task creation events.
- `[CLAIM]`: ownership updates and claim transitions.
- `[DONE]`: completion events.
- `[BLOCKED]`: blocker events.
- `[REVIEW]`: reserved for Milestone C review workflow.

Protocol defaults live in `docs/protocol-config.json` and are mirrored in plugin schema.

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
- `status [task-id]` -> `status`
- `synthesize [task-id]` -> `synthesize`

## Direct @agent Override

Prefix command text with `@agent-name` to attach routing metadata:

- Example: `@coder claim task T-001`
- Router behavior in Milestone B: records `overrideAgent` metadata only.
- Transport layer behavior is intentionally unchanged.

## Synthesis Pipeline (Stub)

`synthesize` aggregates task board entries with status `done`, `review`, or `blocked` and returns a report string:

- Includes task id, status, owner, and result/review/block reason.
- Designed as a Milestone B bridge until cross-agent output collection is added.

## Milestone C Hooks

- Real lock ownership + stale lock recovery.
- Review assignment and `[REVIEW]` state transitions.
- Event replay and snapshot compaction tooling.
