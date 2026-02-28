# Multi Agent Orchestrator Plugin (Milestone A MVP)

This plugin provides a file-backed orchestrator for Feishu group collaboration.

Implemented in this MVP:
- Plugin skeleton and manifest (`openclaw.plugin.json` + `plugin.json`).
- File task board: append-only `state/tasks.jsonl` + `state/tasks.snapshot.json`.
- Atomic board lock (`state/locks/task-board.lock`) and strict status transitions.
- Feishu command parser for:
  - `@orchestrator 帮助`
  - `@orchestrator 开始项目 <绝对路径>`
  - `@orchestrator 项目状态`
  - `@orchestrator 自动推进 开 [N] | 关 | 状态`
  - `@orchestrator create project ...`
  - `@orchestrator run`
  - `@orchestrator autopilot [N]` (连续推进最多 N 步，默认 3)
  - `@orchestrator status` (默认短摘要，含计数 + 阻塞Top + 待推进Top，目标<500字)
  - `@orchestrator status all` / `@orchestrator status full` (扩展调试视图)
- Acceptance policy gate:
  - `done` 状态会经过角色化验收策略（`config/acceptance-policy.json`）
  - coder 需包含更强验收信号（测试/验证/日志等关键词）才可自动完结
- Structured agent prompt:
  - orchestrator 派发给子 agent 的 prompt 采用结构化模板，包含 `TASK_CONTEXT`、`BOARD_SNAPSHOT`、`TASK_RECENT_HISTORY`、`OUTPUT_SCHEMA`
  - `OUTPUT_SCHEMA` 统一为 `status/summary/changes/evidence/risks/nextActions`，便于自动验收和下一步调度
- Hybrid worker execution:
  - orchestrator 保持任务管理与验收闭环
  - `coder` 默认通过 Codex CLI bridge 执行（`scripts/lib/codex_worker_bridge.py`）
  - `debugger/invest-analyst/broadcaster` 默认保持 OpenClaw agent 执行
  - coder 回传仍按统一结构化 JSON 合约写回看板并触发 `[DONE]/[BLOCKED]`
- Visibility modes:
  - `milestone_only`（默认）仅里程碑广播
  - `handoff_visible` / `full_visible` 会额外发送 agent -> orchestrator 的可见交接 @mention
- Simple Wake-up v1:
  - `@orchestrator run` / `dispatch` 会发布 `[CLAIM]` + `[TASK]`（默认手动协作模式）
  - spawn 完成后自动解析子代理输出，回写看板为 `done` / `blocked`，并发布 `[DONE]` / `[BLOCKED]` 中文里程碑
  - 被指派成员也可通过 `@orchestrator` 主动汇报完成/阻塞，仍会更新看板并发布低噪里程碑
  - bot 对 bot 派发模板自动插入 Feishu API mention 标签（`<at user_id="...">name</at>`），确保真实@到 orchestrator
  - 内置 bot 回环保护与 clarify 节流，避免群内噪声和循环触发。
- Feishu inbound wiring helper for orchestrator agent:
  - `scripts/feishu-inbound-router` parses OpenClaw Feishu wrapper text
  - routes `@orchestrator` mentions into `scripts/orchestrator-router`
  - keeps milestone posting in group chat via existing message path

## Layout

- `openclaw.plugin.json`: plugin manifest and config schema.
- `scripts/lib/task_board.py`: board engine (route/apply/status) with lock discipline.
- `scripts/lib/milestones.py`: Feishu parser, wake-up flow, milestone publishing, dispatch spawn 闭环。
- `scripts/lib/codex_worker_bridge.py`: coder -> Codex CLI bridge (schema-constrained JSON output).
- `scripts/orchestrator-router`: unified command entrypoint.
- `scripts/feishu-inbound-router`: parse Feishu inbound wrapper and call router.
- `scripts/dispatch-task`: direct dispatch/clarify wrapper.
- `scripts/dry-run-mvp`: minimal dry-run verification flow.
- `config/feishu-bot-openids.json`: bot role/accountId -> Feishu open_id 映射（用于 API mention 标签）。
- `config/acceptance-policy.json`: done 验收策略（全局证据要求 + 角色关键词门禁）。

## Quick Start

```bash
cd ~/.openclaw/workspace/plugins/multi-agent-orchestrator
./scripts/init-task-board --root .
./scripts/orchestrator-router --root . --actor orchestrator --text "@orchestrator 帮助"
./scripts/orchestrator-router --root . --actor orchestrator --text "@orchestrator 开始项目 /absolute/path/to/project"
./scripts/orchestrator-router --root . --actor orchestrator --text "@orchestrator 项目状态"
./scripts/orchestrator-router --root . --actor orchestrator --text "@orchestrator 自动推进 开 2"
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

1. In group chat, mention bot and check help:
   - `@orchestrator 帮助`
2. 一键启动项目：
   - `@orchestrator 开始项目 /absolute/path/to/project`
3. 查看项目状态：
   - `@orchestrator 项目状态`
4. 打开自动推进：
   - `@orchestrator 自动推进 开 2`
5. 查看自动推进状态：
   - `@orchestrator 自动推进 状态`
6. 如需停用自动推进：
   - `@orchestrator 自动推进 关`
7. Confirm board update locally:
   - `tail -n 20 state/tasks.jsonl`
8. Send wake-up completion report from team member (or test account):
   - 人工输入可直接用 `@orchestrator T-001 已完成，证据: docs/protocol.md`
   - bot 回报应使用 API mention 标签（由调度模板自动生成），Feishu UI 中 orchestrator 会被高亮@到
9. Send blocked report:
   - `@orchestrator T-001 阻塞，错误日志在 tmp/error.log`
10. 验证派发闭环（默认 `run` 为手动模式；可使用 `@orchestrator autopilot` 或 `--dispatch-spawn` 触发自动闭环）。
11. Validate chat output style:
   - `@orchestrator status` returns compact Chinese board summary (counts + blocked/pending top items)
   - `@orchestrator status full` returns a longer but capped list for debugging
   - milestone messages remain concise with `[TASK]`, `[DONE]`, `[BLOCKED]` (no raw CLI logs)

## Hello World Example

Run `python3 examples/hello_world.py`.

## Coder Auto-Task Autopilot (Simulation)

Simple Wake-up v1 默认使用 dispatch spawn 闭环；如需兼容旧流程，可用 `--dispatch-manual` 仅发布 `[TASK]` 模板。

Visibility mode simulation command:

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

## Visibility Mode Usage

默认是 `milestone_only`。如果希望群里看到可见交接 @mention，可切换：

```bash
./scripts/orchestrator-router \
  --root . \
  --actor orchestrator \
  --milestones dry-run \
  --visibility-mode handoff_visible \
  --text "dispatch T-1007 coder: 修复任务派发后的自动回报"
```

## Structured Report Contract

子 agent 推荐输出（JSON object only）：

```json
{
  "taskId": "T-123",
  "agent": "coder",
  "status": "done",
  "summary": "修复完成，测试通过",
  "changes": [{"path": "src/a.py", "summary": "修复索引越界"}],
  "evidence": ["pytest -q passed", "logs/run.log"],
  "risks": [],
  "nextActions": []
}
```

说明：
- `status=done` 时必须带 `evidence`，否则会被 acceptance policy 拦截为 `blocked`。
- 非结构化输出仍兼容，但稳定性低于结构化输出。
