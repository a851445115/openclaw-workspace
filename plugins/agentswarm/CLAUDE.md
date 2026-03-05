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
- Automatic task decomposition from PRD/README files

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
