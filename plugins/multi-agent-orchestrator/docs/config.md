# Config Notes

Runtime config schema v2 is defined in `openclaw.plugin.json`, with load/normalize logic in `scripts/lib/config_runtime.py`.

## Keys

- `projectId`: logical project identifier.
- `channel.provider`: fixed to `feishu` for this workflow.
- `channel.groupId`: visible control group.
- `channel.milestoneOnly`: whether only milestone messages are posted.
- `orchestrator.maxConcurrentSpawns`: cap for subagent fanout.
- `orchestrator.retryPolicy.maxAttempts`: retry budget per execution path.
- `orchestrator.retryPolicy.backoff`: retry backoff model (`fixed` / `linear` / `exponential`) and timing controls.
- `orchestrator.budgetPolicy.guardrails`: budget guardrails (`maxTaskTokens` / `maxTaskWallTimeSec` / `maxTaskRetries`).
- `agents`: supports backward-compatible string items and v2 object items (`{id, capabilities[]}`).

## Default Behavior

- Router scripts are production-facing for Feishu group mentions (`@orchestrator ...`).
- Task board source of truth remains local files (`state/tasks.jsonl` + snapshot).
- Milestone broadcasts are concise Chinese status messages from orchestrator router.
- `config/feishu-bot-openids.json` maps bot role/accountId to Feishu open_id, used to generate real `<at user_id="...">name</at>` mention tags in bot dispatch templates.
- `config/acceptance-policy.json` controls done gate policy (hard evidence gate + role keyword requirements + optional verify commands).
- Validation remains conservative and status-driven.

## Acceptance Policy v2

`config/acceptance-policy.json` now supports:

- `global.requireEvidence`:
  - `true` means done reports must contain **hard evidence** (URL / file path or filename / test-passed traces).
- `global.evidenceMode`:
  - default `hard` (reserved for future mode extension; current gate enforces hard evidence when `requireEvidence=true`).
- `global.evidenceTimeoutSec`:
  - default timeout hint for evidence-related validation.
- `global.verifyTimeoutSec`:
  - default timeout for verify command execution.
- `global.verifyCommands` and `roles.<role>.verifyCommands`:
  - merged during acceptance (`global + role`).
  - supports `string` command (default `expectExitCode=0`) or object:
    - `{ "cmd": "...", "expectExitCode": 0, "timeoutSec": 20 }`

Reason code semantics in spawn acceptance:

- `spawn.reasonCode` remains `incomplete_output` for acceptance rejection to keep recovery-loop compatibility.
- `spawn.acceptanceReasonCode` carries fine-grained cause:
  - `missing_hard_evidence`
  - `verify_command_failed`
  - `stage_only`
  - `role_policy_missing_keyword`

## Runtime Policy v2

Runtime policy files:

- `config/runtime-policy.json`: baseline policy loaded by default.
- `config/runtime-policy.example.json`: editable template for new deployments.

Minimal template:

```json
{
  "agents": ["coder", "debugger"],
  "orchestrator": {
    "maxConcurrentSpawns": 3,
    "retryPolicy": {
      "maxAttempts": 2,
      "backoff": {
        "mode": "exponential",
        "baseMs": 500,
        "maxMs": 8000,
        "multiplier": 2.0,
        "jitterPct": 20
      }
    },
    "budgetPolicy": {
      "guardrails": {
        "maxTaskTokens": 12000,
        "maxTaskWallTimeSec": 1200,
        "maxTaskRetries": 3
      }
    }
  }
}
```

Compatibility strategy (`config_runtime.load_runtime_config`):

- Merge order: built-in defaults -> repo/runtime policy files -> caller override.
- Old agent config (`agents: ["coder"]`) auto-normalizes to `[{ "id": "coder", "capabilities": [] }]`.
- Mixed config (string + object agents, partial retry/budget fields) is accepted and completed with safe defaults.
- Legacy budget policy (`config/budget-policy.json`) is used as fallback source when v2 budget guardrails are missing.

## Orchestrator Agent Wiring

- Orchestrator workspace instructions should call `scripts/feishu-inbound-router` on inbound Feishu mention wrappers.
- `feishu-inbound-router` extracts group/sender/text and forwards to `scripts/orchestrator-router`.
- Keep `channel.groupId` aligned with bound Feishu group for correct milestone routing.

- `feishu-router` now supports group command intents through orchestrator entry: create/claim/done/block/status/synthesize/escalate/dispatch/clarify.
- Runtime guardrails include bot-to-bot milestone echo suppression and clarify global cooldown throttle.
- Visibility mode switch:
  - `handoff_visible`: default worker->orchestrator visible handoff report
  - `milestone_only`: low-noise milestones only
  - `full_visible`: reserved for richer visible collaboration signals
- `autopilot` command:
  - `@orchestrator autopilot [N]` loops runnable tasks with a max-step cap (default 3).
