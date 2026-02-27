# Config Notes (Milestone A)

The scaffold exposes a minimal schema in `openclaw.plugin.json`.

## Keys

- `projectId`: logical project identifier.
- `channel.provider`: fixed to `feishu` for this workflow.
- `channel.groupId`: visible control group.
- `channel.milestoneOnly`: whether only milestone messages are posted.
- `orchestrator.maxConcurrentSpawns`: cap for subagent fanout.
- `agents`: agent IDs allowed to claim tasks (e.g. include `debugger` for diagnostic workflow).

## Default Behavior

- No runtime orchestration implementation yet.
- Scripts are local board utilities only.
- Validation is conservative and status-driven.

## TODO Milestone B/C

- Add strict schema for agent capabilities.
- Add retry/backoff policy fields.
- Add budget and token guardrail configuration.
