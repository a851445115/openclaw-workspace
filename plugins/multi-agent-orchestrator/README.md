# Multi Agent Orchestrator Plugin (Milestone A MVP)

This plugin provides a file-backed orchestrator for Feishu group collaboration.

Implemented in this MVP:
- Plugin skeleton and manifest (`openclaw.plugin.json` + `plugin.json`).
- File task board: append-only `state/tasks.jsonl` + `state/tasks.snapshot.json`.
- Atomic board lock (`state/locks/task-board.lock`) and strict status transitions.
- Feishu command parser for:
  - `@orchestrator create project ...`
  - `@orchestrator run`
  - `@orchestrator status`
- Simple Wake-up v1:
  - team member report must include `@orchestrator`
  - orchestrator self-checks or dispatches `debugger`
  - orchestrator updates board and posts Chinese `[DONE]/[BLOCKED]` milestone messages
- Dispatch loop closure:
  - after `/subagents spawn`, parse result hints
  - auto write back `mark done` or `block task`
  - publish concise Chinese milestone summary (no raw logs)

## Layout

- `openclaw.plugin.json`: plugin manifest and config schema.
- `scripts/lib/task_board.py`: board engine (route/apply/status) with lock discipline.
- `scripts/lib/milestones.py`: Feishu parser, wake-up flow, milestone publishing, dispatch close-loop.
- `scripts/orchestrator-router`: unified command entrypoint.
- `scripts/dispatch-task`: direct dispatch/clarify wrapper.
- `scripts/dry-run-mvp`: minimal dry-run verification flow.

## Quick Start

```bash
cd ~/.openclaw/workspace/plugins/multi-agent-orchestrator
./scripts/init-task-board --root .
./scripts/orchestrator-router --root . --actor orchestrator --text "@orchestrator create project Alpha: 完成解析器; 编写测试"
./scripts/orchestrator-router --root . --actor orchestrator --text "@orchestrator run"
./scripts/orchestrator-router --root . --actor orchestrator --text "@orchestrator status"
```

Team wake-up report example:

```bash
./scripts/orchestrator-router --root . --actor coder --text "@orchestrator T-001 已完成，证据: scripts/lib/task_board.py"
```

## Dry Run Script

```bash
./scripts/dry-run-mvp
```

## Enable in OpenClaw

1. Ensure plugin folder exists at `~/.openclaw/workspace/plugins/multi-agent-orchestrator`.
2. Ensure `openclaw.plugin.json` contains:
   - `"id": "multi-agent-orchestrator"`
   - `"skills": ["."]`
3. Load/reload OpenClaw plugin discovery (restart gateway if needed):
   - `openclaw gateway restart`
4. Bind orchestrator runtime to the same Feishu group as `channel.groupId` in plugin config.
