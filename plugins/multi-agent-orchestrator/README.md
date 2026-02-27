# Multi Agent Orchestrator Plugin (Milestone A MVP)

This plugin provides a file-backed orchestrator for Feishu group collaboration.

Implemented in this MVP:
- Plugin skeleton and manifest (`openclaw.plugin.json` + `plugin.json`).
- File task board: append-only `state/tasks.jsonl` + `state/tasks.snapshot.json`.
- Atomic board lock (`state/locks/task-board.lock`) and strict status transitions.
- Feishu command parser for:
  - `@orchestrator create project ...`
  - `@orchestrator run`
  - `@orchestrator status` (默认短摘要，含计数 + 阻塞Top + 待推进Top，目标<500字)
  - `@orchestrator status all` / `@orchestrator status full` (扩展调试视图)
- Simple Wake-up v1:
  - 默认手动派发（manual dispatch）：orchestrator 发布 `[CLAIM]` + `[TASK]` 分配任务
  - 被指派成员执行后，需通过 `@orchestrator` 汇报进展/完成/阻塞
  - orchestrator 根据汇报更新看板并发布中文里程碑消息
- 可选子代理派发（`--dispatch-mode subagent`，默认关闭）：
  - 仅在显式开启时尝试 `/subagents spawn ...`
  - 失败时仅在允许状态（claimed/in_progress/review）转为 blocked，避免 `done -> blocked`
- Feishu inbound wiring helper for orchestrator agent:
  - `scripts/feishu-inbound-router` parses OpenClaw Feishu wrapper text
  - routes `@orchestrator` mentions into `scripts/orchestrator-router`
  - keeps milestone posting in group chat via existing message path

## Layout

- `openclaw.plugin.json`: plugin manifest and config schema.
- `scripts/lib/task_board.py`: board engine (route/apply/status) with lock discipline.
- `scripts/lib/milestones.py`: Feishu parser, wake-up flow, milestone publishing, manual/subagent dispatch logic.
- `scripts/orchestrator-router`: unified command entrypoint.
- `scripts/feishu-inbound-router`: parse Feishu inbound wrapper and call router.
- `scripts/dispatch-task`: direct dispatch/clarify wrapper.
- `scripts/dry-run-mvp`: minimal dry-run verification flow.

## Quick Start

```bash
cd ~/.openclaw/workspace/plugins/multi-agent-orchestrator
./scripts/init-task-board --root .
./scripts/orchestrator-router --root . --actor orchestrator --text "@orchestrator create project Alpha: 完成解析器; 编写测试"
./scripts/orchestrator-router --root . --actor orchestrator --text "@orchestrator run"
./scripts/orchestrator-router --root . --actor orchestrator --text "@orchestrator status"
./scripts/orchestrator-router --root . --actor orchestrator --text "@orchestrator status full"
```

Team wake-up report example:

```bash
./scripts/orchestrator-router --root . --actor coder --text "@orchestrator T-001 已完成，证据: scripts/lib/task_board.py"
```

Feishu wrapper routing example (what orchestrator agent should run on inbound mention):

```bash
cat inbound.txt | ./scripts/feishu-inbound-router --root .
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

## How To Test In Feishu Group

1. In group chat, mention bot and create project:
   - `@orchestrator create project WakeupV1: 写一个最小任务; 给出状态命令`
2. Confirm board update locally:
   - `tail -n 20 state/tasks.jsonl`
3. Trigger run:
   - `@orchestrator run`
4. Send wake-up completion report from team member (or test account):
   - `@orchestrator T-001 已完成，证据: docs/protocol.md`
5. Send blocked report:
   - `@orchestrator T-001 阻塞，错误日志在 tmp/error.log`
6. Optional subagent dispatch (diagnostic only):
   - `./scripts/dispatch-task dispatch --root . --task-id T-001 --agent coder --dispatch-mode subagent --mode dry-run`
6. Validate chat output style:
   - `@orchestrator status` returns compact Chinese board summary (counts + blocked/pending top items)
   - `@orchestrator status full` returns a longer but capped list for debugging
   - milestone messages remain concise with `[TASK]`, `[DONE]`, `[BLOCKED]` (no raw CLI logs)

## Hello World Example

Run `python3 examples/hello_world.py`.
