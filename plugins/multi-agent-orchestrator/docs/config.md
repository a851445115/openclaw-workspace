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

- Router scripts are production-facing for Feishu group mentions (`@orchestrator ...`).
- Task board source of truth remains local files (`state/tasks.jsonl` + snapshot).
- Milestone broadcasts are concise Chinese status messages from orchestrator router.
- `config/feishu-bot-openids.json` maps bot role/accountId to Feishu open_id, used to generate real `<at user_id="...">name</at>` mention tags in bot dispatch templates.
- `config/acceptance-policy.json` controls done gate policy (global evidence requirement + role keyword requirements).
- Validation remains conservative and status-driven.

## TODO Milestone B/C

- Add strict schema for agent capabilities.
- Add retry/backoff policy fields.
- Add budget and token guardrail configuration.

## Orchestrator Agent Wiring

- Orchestrator workspace instructions should call `scripts/feishu-inbound-router` on inbound Feishu mention wrappers.
- `feishu-inbound-router` extracts group/sender/text and forwards to `scripts/orchestrator-router`.
- Keep `channel.groupId` aligned with bound Feishu group for correct milestone routing.

- `feishu-router` now supports group command intents through orchestrator entry: create/claim/done/block/status/synthesize/escalate/dispatch/clarify.
- Runtime guardrails include bot-to-bot milestone echo suppression and clarify global cooldown throttle.
- Visibility mode switch:
  - `milestone_only`: default low-noise milestones
  - `handoff_visible`: show worker->orchestrator handoff report
  - `full_visible`: reserved for richer visible collaboration signals
- `autopilot` command:
  - `@orchestrator autopilot [N]` loops runnable tasks with a max-step cap (default 3).
