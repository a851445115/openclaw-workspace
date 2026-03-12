# agentswarm Frontend Dashboard PRD

## Goal
Build a local, read-only frontend dashboard for the `agentswarm` OpenClaw plugin so a single operator can visually inspect real-time project execution status, agent activity, task flow, and task input/output history.

## Scope (V1)
The first version is strictly read-only and runs as a local web app.

### Must-have capabilities
1. Dashboard overview
   - task counts by status
   - recent event stream
   - blocked tasks
   - recently active agents
   - runtime cards for scheduler / autopilot / governance
   - key metrics summary
2. Tasks page
   - searchable/filterable task table
   - filters: status, owner, assigneeHint, time
3. Task detail page
   - title, taskId, status, owner, assigneeHint
   - result
   - blockedReason
   - dependsOn / blockedBy
   - history timeline
   - event payload details
   - evidence/log/file path references when available
4. Agents page
   - per-agent recent task counts
   - recent done / blocked counts
   - recent active time
   - derived state: active / idle / blocked / unknown
5. Runtime page
   - scheduler status
   - autopilot status
   - governance status
   - budget / recovery / worker sessions / metrics summaries
   - recent log file entry points

## Constraints
- local-only web app for now
- polling refresh every 3-5 seconds
- real data first; do not rely on mock data unless necessary
- no control actions in V1
- no edit/write UI in V1
- no websocket/SSE in V1

## Preferred stack
- Next.js + TypeScript
- Tailwind CSS + shadcn/ui
- SWR for polling
- optional Recharts for lightweight visual summaries

## Real data sources
Read from the agentswarm plugin state directory:
- ~/.openclaw/workspace/plugins/agentswarm/state/tasks.snapshot.json
- ~/.openclaw/workspace/plugins/agentswarm/state/tasks.jsonl
- ~/.openclaw/workspace/plugins/agentswarm/state/scheduler.kernel.json
- ~/.openclaw/workspace/plugins/agentswarm/state/autopilot.runtime.json
- ~/.openclaw/workspace/plugins/agentswarm/state/governance.control.json
- ~/.openclaw/workspace/plugins/agentswarm/state/budget.state.json
- ~/.openclaw/workspace/plugins/agentswarm/state/recovery.state.json
- ~/.openclaw/workspace/plugins/agentswarm/state/worker-sessions.json
- ~/.openclaw/workspace/plugins/agentswarm/state/ops.metrics.jsonl

## Suggested API endpoints
- GET /api/overview
- GET /api/tasks
- GET /api/tasks/:taskId
- GET /api/tasks/:taskId/events
- GET /api/agents
- GET /api/runtime
- GET /api/logs
- GET /api/health

## Agent state derivation
- active: recent events within N minutes or current claimed/in_progress tasks
- blocked: recent relevant task is blocked
- idle: previously active but currently no active tasks
- unknown: insufficient data

## Acceptance criteria
1. App runs locally.
2. Data is sourced from real agentswarm state files.
3. Polling refresh works.
4. Agent states are visible.
5. Tasks and details are visible.
6. Event timeline and payloads are inspectable.
7. Result / blockedReason / history are visible.
8. UI is clean and clearly read-only.
9. Missing or malformed data fails gracefully.

## Team split
- Codex: project setup, API layer, data normalization, core pages, local run path
- Gemini: information architecture review, UI polish suggestions, naming/copy, UX review, acceptance checklist
