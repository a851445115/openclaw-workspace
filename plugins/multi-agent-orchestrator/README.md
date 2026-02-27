# Multi Agent Orchestrator Plugin (Milestone B)

This plugin provides a minimal runnable file-backed orchestrator for local task board flows.

Implemented in Milestone B:
- Protocol message types: `[TASK] [CLAIM] [DONE] [BLOCKED] [REVIEW] [DIAG]`
- Command router intents: create task, claim task, mark done, block task, escalate task, status
- Direct `@agent` override parsing (routing metadata only)
- Synthesis stub pipeline over task board entries
- Existing scripts upgraded from stubs to functional wrappers
- Milestone announcements to Feishu control group (low-noise summary format)
- Internal dispatch helper (`/subagents spawn`) with visible pre/post assignment summary
- Controlled clarify command with role gate + cooldown throttle

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
- `scripts/dispatch-task`: controlled dispatch/clarify helper.
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

### Milestone Broadcast

`orchestrator-router` now publishes concise 1-3 line milestones after task-board mutations (`create_task`, `claim_task`, `mark_done`, `block_task`, `escalate_task -> [DIAG]`):

```bash
./scripts/orchestrator-router --root . --actor orchestrator --text "create task T-001: implement parser"
./scripts/orchestrator-router --root . --actor coder --text "claim task T-001"
./scripts/orchestrator-router --root . --actor coder --text "mark done T-001: parser merged"
```

Modes:

- `--milestones send` (default): send to control group `oc_041146c92a9ccb403a7f4f48fb59701d`
- `--milestones dry-run`: skip send and only keep local execution
- `--milestones off`: disable milestone publish

Active broadcaster gate:

- Default: only `orchestrator` can proactively broadcast.
- Optional: `--allow-broadcaster` also allows `broadcaster`.

### Internal Dispatch (Still sessions_spawn Path)

```bash
./scripts/dispatch-task dispatch --root . --task-id T-001 --agent coder
```

Behavior:

- Sends pre-dispatch assignment summary (`[CLAIM]`).
- Triggers internal spawn through orchestrator session using `/subagents spawn ...`.
- Sends post-dispatch summary (`[CLAIM]`, submitted/failed).

### Controlled Clarify (Pointed, Throttled)

```bash
./scripts/dispatch-task clarify --root . --task-id T-001 --role debugger --question "Need stack trace location?"
```

Guardrails:

- Restricted to `actor=orchestrator`.
- Role must be one of `coder|invest-analyst|debugger|broadcaster`.
- Cooldown (default 180s per `group+role`) stored in `state/clarify.cooldown.json`.
