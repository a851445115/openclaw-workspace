# Multi Agent Orchestrator Plugin (Milestone B)

This plugin provides a minimal runnable file-backed orchestrator for local task board flows.

Implemented in Milestone B:
- Protocol message types: `[TASK] [CLAIM] [DONE] [BLOCKED] [REVIEW] [DIAG]`
- Command router intents: create task, claim task, mark done, block task, escalate task, status
- Direct `@agent` override parsing (routing metadata only)
- Synthesis stub pipeline over task board entries
- Existing scripts upgraded from stubs to functional wrappers

## Layout

- `openclaw.plugin.json`: plugin manifest + protocol schema keys.
- `docs/protocol.md`: protocol behavior and command grammar.
- `docs/protocol-config.json`: protocol defaults for message types/intents.
- `scripts/lib/task_board.py`: local board engine (route, apply, synthesize).
- `scripts/init-task-board`: initialize board files.
- `scripts/claim-task`: claim helper wrapper.
- `scripts/update-task`: transition validator + apply wrapper.
- `scripts/orchestrator-router`: plain text command router.
- `scripts/synthesize-board`: synthesis report helper.
- `state/`: runtime state placeholders.

## Quick Start

```bash
cd ~/.openclaw/workspace/plugins/multi-agent-orchestrator
./scripts/init-task-board --root .
./scripts/orchestrator-router --root . --actor lead --text "create task T-001: implement parser"
./scripts/claim-task --root . --task-id T-001 --agent coder
./scripts/orchestrator-router --root . --actor coder --text "mark done T-001: parser merged"
./scripts/orchestrator-router --root . --actor lead --text "status"
./scripts/synthesize-board --root .
```

### Debugger Integration (Diagnostic Role)

Use `escalate task` to attach a diagnostic follow-up task for `debugger`:

```bash
./scripts/orchestrator-router --root . --actor lead --text "escalate task T-001: flaky token refresh"
```

This blocks `T-001` and creates a new `[DIAG]` task with `assigneeHint=debugger` and `relatedTo=T-001`.
