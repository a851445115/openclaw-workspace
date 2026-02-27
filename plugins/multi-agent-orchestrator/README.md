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
  - bot 对 bot 汇报会自动插入 Feishu API mention 标签（`<at user_id="...">name</at>`），确保真实@到 orchestrator
  - orchestrator 根据汇报更新看板并发布中文里程碑消息
- 子代理自动派发在 v1 中默认关闭（后续版本再引入），当前避免 `/subagents spawn ...` 链路风险。
- Feishu inbound wiring helper for orchestrator agent:
  - `scripts/feishu-inbound-router` parses OpenClaw Feishu wrapper text
  - routes `@orchestrator` mentions into `scripts/orchestrator-router`
  - keeps milestone posting in group chat via existing message path

## Layout

- `openclaw.plugin.json`: plugin manifest and config schema.
- `scripts/lib/task_board.py`: board engine (route/apply/status) with lock discipline.
- `scripts/lib/milestones.py`: Feishu parser, wake-up flow, milestone publishing, manual dispatch logic.
- `scripts/orchestrator-router`: unified command entrypoint.
- `scripts/feishu-inbound-router`: parse Feishu inbound wrapper and call router.
- `scripts/dispatch-task`: direct dispatch/clarify wrapper.
- `scripts/dry-run-mvp`: minimal dry-run verification flow.
- `config/feishu-bot-openids.json`: bot role/accountId -> Feishu open_id 映射（用于 API mention 标签）。

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
   - 人工输入可直接用 `@orchestrator T-001 已完成，证据: docs/protocol.md`
   - bot 回报应使用 API mention 标签（由调度模板自动生成），Feishu UI 中 orchestrator 会被高亮@到
5. Send blocked report:
   - `@orchestrator T-001 阻塞，错误日志在 tmp/error.log`
6. 子代理自动派发暂未启用（Simple Wake-up v1 采用手动派发 + 汇报闭环）。
6. Validate chat output style:
   - `@orchestrator status` returns compact Chinese board summary (counts + blocked/pending top items)
   - `@orchestrator status full` returns a longer but capped list for debugging
   - milestone messages remain concise with `[TASK]`, `[DONE]`, `[BLOCKED]` (no raw CLI logs)

## Hello World Example

Run `python3 examples/hello_world.py`.

## Coder Auto-Task Autopilot (Simulation)

Simple Wake-up v1 still uses manual dispatch by orchestrator, but coder can auto-start when it receives a clear `[TASK]` assignment.

Local simulation command:

```bash
./scripts/simulate-coder-autopilot.py \
  --group-id oc_REPLACE_ME \
  --actor orchestrator \
  --message '[TASK] T-1007 | 负责人=coder
任务: 修复任务派发后的自动回报
请 <at user_id="ou_0991106b8c19b021e1c9af96e869f3fc">coder</at> 完成后回报：<at user_id="ou_f938eaffd79cf1837c3c7c6cd5089235">orchestrator</at> T-1007 已完成，证据: 日志/截图/链接。'
```

Expected result:

- `shouldAct=true`
- `taskId=T-1007`
- `reportPreview` contains a real Feishu mention tag to orchestrator:
  `<at user_id="ou_f938eaffd79cf1837c3c7c6cd5089235">orchestrator</at>`

Safety checks implemented by simulator:

- only allowlisted group `oc_REPLACE_ME`
- ignore self messages (`actor=coder`)
- ignore orchestrator milestone messages (`[DONE]`, `[BLOCKED]`, `[CLAIM]`)
- only trigger when assignment is explicit: `[TASK]` + `T-xxxx` + (`负责人=coder` or `<at ...>coder</at>`)

## Feishu E2E Dry-Run (run T-1007)

Use this local dry-run to verify the full chain without posting to Feishu:

```bash
TASK_MSG=$(python3 scripts/lib/milestones.py feishu-router \
  --root . \
  --actor orchestrator \
  --group-id oc_REPLACE_ME \
  --mode dry-run \
  --text '@orchestrator run T-1007' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["taskSend"]["payload"]["text"])')

./scripts/simulate-coder-autopilot.py \
  --group-id oc_REPLACE_ME \
  --actor orchestrator \
  --message "$TASK_MSG"
```

Pass criteria:

- first command emits a `[TASK] T-1007` assignment with coder mention tag
- simulator returns `shouldAct=true`
- simulator `reportPreview` includes real orchestrator mention tag and `T-1007 已完成`

For live Feishu validation, run the same `@orchestrator run T-1007` in the allowlisted group and check coder bot replies without human `@coder` prompting.
