# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Agentswarm is a file-backed multi-agent orchestration plugin for OpenClaw. It provides task board management, agent dispatch, and Feishu group integration for collaborative AI workflows.

**Core Capabilities:**
- File-based task board with append-only event log (`state/tasks.jsonl`) and materialized snapshot (`state/tasks.snapshot.json`)
- Multi-agent orchestration with role-based dispatch (coder, debugger, invest-analyst, broadcaster, knowledge-curator, paper-ingestor, paper-summarizer)
- Feishu group integration for milestone visibility and command routing
- Hybrid worker execution: claude_cli, codex_cli, and openclaw_agent bridges
- Built-in scheduler kernel for autonomous task progression
- Governance controls (pause/resume/freeze/abort/approval)
- Recovery loop with automatic escalation chains
- Budget tracking and guardrails (token/time/retry limits)
- Checkpoint continuation for long-running tasks
- Collaboration hub for multi-agent coordination
- Expert group consultation for complex decisions

## Architecture

**State Management:**
- `state/tasks.jsonl` - append-only event stream (source of truth)
- `state/tasks.snapshot.json` - materialized task map (derived state)
- `state/budget.state.json` - per-task budget tracking
- `state/recovery.state.json` - recovery attempt tracking
- `state/governance.control.json` - governance state
- `state/governance.audit.jsonl` - governance audit trail (append-only hash chain)
- `state/scheduler.kernel.json` - scheduler state
- `state/scheduler.daemon.json` - scheduler daemon loop state
- `state/ops.metrics.jsonl` - operational metrics

**Core Modules:**
- `scripts/lib/task_board.py` - task board engine with lock discipline
- `scripts/lib/milestones.py` - orchestration runtime, dispatch, autopilot, scheduler
- `scripts/lib/recovery_loop.py` - failure recovery and escalation
- `scripts/lib/budget_policy.py` - cost governance
- `scripts/lib/priority_engine.py` - task prioritization and dependency resolution
- `scripts/lib/strategy_library.py` - role-specific execution strategies
- `scripts/lib/knowledge_adapter.py` - knowledge feedback integration
- `scripts/lib/collaboration_hub.py` - multi-agent collaboration coordination
- `scripts/lib/expert_group.py` - expert consultation for complex decisions
- `scripts/lib/session_registry.py` - session tracking and management
- `scripts/lib/context_pack.py` - context packaging for agent dispatch
- `scripts/lib/claude_worker_bridge.py` - claude_cli executor bridge
- `scripts/lib/codex_worker_bridge.py` - codex_cli executor bridge
- `scripts/lib/config_runtime.py` - runtime config loader with v2 schema support
- `scripts/lib/governance.py` - governance control and audit
- `scripts/lib/evidence_normalizer.py` - evidence validation and normalization
- `scripts/lib/task_decomposer.py` - automatic task decomposition from PRD/README
- `scripts/lib/ops_metrics.py` - operational metrics collection

**Configuration:**
- `openclaw.plugin.json` - plugin manifest and config schema
- `config/runtime-policy.json` - runtime policy (agents, retry, budget, continuation)
- `config/acceptance-policy.json` - done gate policy (evidence requirements, verify commands)
- `config/recovery-policy.json` - recovery escalation chains
- `config/budget-policy.json` - budget guardrails (legacy fallback)
- `config/role-strategies.json` - role-specific execution strategies
- `config/feishu-bot-openids.json` - Feishu bot mention mapping
- `config/decomposition-policy.json` - project auto-decomposition policy
- `config/collaboration-policy.json` - collaboration hub configuration
- `config/expert-group-policy.json` - expert group consultation rules

## Common Commands

### Testing
```bash
# Run all tests
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v

# Run specific test file
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests/test_orchestrator_runtime.py -v

# Run specific test case
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_orchestrator_runtime.TestOrchestratorRuntime.test_dispatch_spawn_done -v
```

### Task Board Operations
```bash
# Initialize task board
./scripts/init-task-board --root .

# Rebuild snapshot from event log
./scripts/rebuild-snapshot --root . --dry-run
./scripts/rebuild-snapshot --root . --apply

# Recover stale locks
./scripts/recover-stale-locks --root . --dry-run
./scripts/recover-stale-locks --root . --apply

# Router commands (orchestrator entrypoint)
./scripts/orchestrator-router --root . --actor orchestrator --text "@orchestrator 帮助"
./scripts/orchestrator-router --root . --actor orchestrator --text "@orchestrator status"
./scripts/orchestrator-router --root . --actor orchestrator --text "@orchestrator autopilot 3"
./scripts/orchestrator-router --root . --actor orchestrator --text "@orchestrator 开始项目 /absolute/path/to/project"
```

### Feishu Integration
```bash
# Parse Feishu inbound wrapper and route to orchestrator
cat inbound.txt | ./scripts/feishu-inbound-router --root .

# Scheduler control
python3 scripts/lib/milestones.py scheduler-run --root . --action enable --interval-sec 300
python3 scripts/lib/milestones.py scheduler-daemon --root . --mode dry-run --max-loops 3 --poll-sec 1
```

### Governance Commands
```bash
# Governance status
./scripts/orchestrator-router --root . --actor orchestrator --text "@orchestrator 治理 状态"

# Pause/resume operations
./scripts/orchestrator-router --root . --actor orchestrator --text "@orchestrator 治理 暂停"
./scripts/orchestrator-router --root . --actor orchestrator --text "@orchestrator 治理 恢复"

# Freeze/unfreeze operations
./scripts/orchestrator-router --root . --actor orchestrator --text "@orchestrator 治理 冻结"
./scripts/orchestrator-router --root . --actor orchestrator --text "@orchestrator 治理 解冻"

# Abort operations
./scripts/orchestrator-router --root . --actor orchestrator --text "@orchestrator 治理 中止 全部"
./scripts/orchestrator-router --root . --actor orchestrator --text "@orchestrator 治理 中止 T-xxxx"
```

### Config Migration
```bash
# Migrate to v2 config schema
./scripts/migrate-config-v2 --root . --dry-run
./scripts/migrate-config-v2 --root . --apply
```

### Ops Metrics
```bash
# Export weekly ops report
./scripts/export-weekly-ops-report --root . --days 7
```

## Development Guidelines

### State Immutability
- **NEVER** modify `state/tasks.jsonl` directly - it is append-only
- **NEVER** manually edit `state/tasks.snapshot.json` - it is derived state
- **NEVER** manually edit `state/governance.audit.jsonl` - it is append-only with hash chain
- Use `scripts/rebuild-snapshot` to reconstruct snapshot from event log

### Lock Discipline
- All task board mutations must acquire `state/locks/task-board.lock`
- Lock TTL is 45 seconds with 8-second wait timeout
- Use `scripts/recover-stale-locks` to clean up stale locks

### Status Transitions
Valid transitions (enforced by task_board.py):
- `pending` → `claimed`, `blocked`
- `claimed` → `in_progress`, `done`, `blocked`
- `in_progress` → `review`, `done`, `blocked`, `failed`
- `review` → `done`, `in_progress`, `blocked`
- `blocked` → `in_progress`, `claimed`
- `failed` → `in_progress`
- `done` → (terminal)

### Task Priority Fields
Tasks support scheduling fields for dependency management:
- `dependsOn: string[]` - prerequisite task IDs (only `done` tasks satisfy)
- `blockedBy: string[]` - explicit blockers (cleared when referenced task is `done`)
- `priority: number` - task priority score
- `impact: number` - task impact score

Scheduler selects from ready queue (satisfied dependencies) using deterministic scoring.

### Acceptance Policy
- All `done` transitions are gated by `config/acceptance-policy.json`
- Must include hard evidence (file paths, URLs, test output)
- Role-specific keyword requirements (e.g., coder requires test/verify/log signals)
- Verify commands can be configured globally or per-role
- Acceptance reason codes: `missing_hard_evidence`, `verify_command_failed`, `stage_only`, `role_policy_missing_keyword`

### Checkpoint Continuation
For long-running tasks with `status=progress`:
- Continuation policy in `config/runtime-policy.json` controls max rounds and timeout
- Reason codes: `checkpoint_continue`, `continuation_round_limit`, `continuation_no_progress`, `continuation_timeout`, `continuation_need_input`
- Task remains `in_progress` during continuation rounds

### Worker Bridges
When modifying worker bridges (`claude_worker_bridge.py`, `codex_worker_bridge.py`):
- Maintain structured output contract: `{taskId, agent, status, summary, changes, evidence, risks, nextActions}`
- Handle both JSON and free-text responses for backward compatibility
- Always return `reasonCode` for non-done outcomes
- Support `status=progress` for checkpoint continuation

### Testing Patterns
- Use `tempfile.TemporaryDirectory()` for isolated test environments
- Initialize task board with `scripts/init-task-board` in test setup
- Mock external calls (Feishu API, worker spawns) in unit tests
- Use `PYTHONDONTWRITEBYTECODE=1` to avoid `.pyc` pollution

### Config Schema v2
- Runtime policy uses v2 schema with `agents[]` as objects: `{id, capabilities[]}`
- Backward compatible with string-based agent lists
- Use `scripts/migrate-config-v2` to upgrade old configs
- Retry policy supports `fixed`, `linear`, `exponential` backoff modes
- Budget guardrails: `maxTaskTokens`, `maxTaskWallTimeSec`, `maxTaskRetries`
- Continuation policy: `enabled`, `maxContinuationRounds`, `noProgressWindowRounds`

## Key Constraints

1. **Append-Only Event Log**: `state/tasks.jsonl` is the source of truth and must never be edited manually
2. **Lock Safety**: All board mutations require lock acquisition with TTL enforcement
3. **Status Transition Validation**: Invalid transitions are rejected by task_board.py
4. **Evidence Gate**: Done transitions without evidence are auto-blocked
5. **Broadcast Authority**: Only orchestrator (and optionally broadcaster) can send milestones
6. **Governance Respect**: Frozen/paused states block dispatch/autopilot/scheduler operations
7. **Hash Chain Integrity**: Governance audit log maintains hash chain for tamper detection

## Troubleshooting

**Lock contention:**
```bash
./scripts/recover-stale-locks --root . --dry-run
```

**Snapshot drift:**
```bash
./scripts/rebuild-snapshot --root . --dry-run
# Review diff, then apply if safe
./scripts/rebuild-snapshot --root . --apply
```

**Config migration issues:**
```bash
./scripts/migrate-config-v2 --root . --dry-run
# Check diff.sampleRemoved for deprecated fields
```

**Test failures:**
- Ensure `PYTHONDONTWRITEBYTECODE=1` is set
- Check for stale locks in test temp directories
- Verify test isolation (each test should use fresh temp directory)

**Governance state issues:**
- Check `state/governance.control.json` for current state
- Review `state/governance.audit.jsonl` for audit trail
- Use governance status command to inspect current controls

## Protocol Messages

Milestone format (low-noise, 1-3 lines):
- `[TASK]` - task assignment with owner hint
- `[CLAIM]` - ownership claim
- `[DONE]` - completion with evidence
- `[BLOCKED]` - blocker with reason
- `[DIAG]` - diagnostic follow-up (debugger role)
- `[REVIEW]` - review request (reserved for Milestone C)
