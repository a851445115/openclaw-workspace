---
name: multi-agent-orchestrator
description: File-backed multi-agent orchestration for Feishu-visible milestones and silent subagent execution. Use when operating the shared task board, routing orchestration commands, and synthesizing board output.
---

# Multi Agent Orchestrator

Use local scripts as stable interfaces for task board operations.

## Constraints

- Treat `state/tasks.jsonl` as append-only.
- Treat `state/tasks.snapshot.json` as derived state.
- Keep `@agent` override as routing metadata only in Milestone B.
- Keep execution internal (`dispatch` triggers spawn closed-loop by default); Feishu group remains concise milestone visibility.
- Default broadcast authority: `orchestrator` only (optionally `broadcaster`).
- Do not let non-orchestrator roles initiate proactive cross-bot mentions.

## Script Interfaces

- `scripts/init-task-board --root <path> [--dry-run]`
- `scripts/claim-task --root <path> --task-id <id> --agent <name> [--dry-run]`
- `scripts/update-task --root <path> --task-id <id> --from <status> --to <status> [--actor <name>] [--dry-run]`
- `scripts/orchestrator-router --root <path> --actor <name> --text "<command>" [--mode route|apply] [--milestones send|dry-run|off]`
- `scripts/feishu-inbound-router --root <path> [--milestones send|dry-run|off]` (read full Feishu inbound wrapper from stdin)
- `scripts/dispatch-task dispatch --root <path> --task-id <id> --agent <role> [--task <text>] [--mode send|dry-run]`
- `scripts/dispatch-task clarify --root <path> --task-id <id> --role <coder|invest-analyst|debugger|broadcaster> --question <text> [--cooldown-sec <n>] [--mode send|dry-run]`
- `scripts/synthesize-board --root <path> [--task-id <id>] [--actor <name>]`

## Intents

- `create task`
- `claim task`
- `mark done`
- `block task`
- `escalate task` (blocks original + creates `[DIAG]` follow-up task for `debugger`)
- `status`
- `synthesize`

## Milestone C Follow-up

- `[REVIEW]` event authoring and reviewer assignment workflow.
