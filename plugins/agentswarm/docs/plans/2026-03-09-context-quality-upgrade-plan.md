# Agentswarm 上下文质量升级实施计划

> 日期: 2026-03-09
> 目标: 解决 agent 间上下文断裂、验收门过弱、prompt 质量不足三大核心缺陷
> 来源: Paper A2 (EHH-IES) 复现失败复盘 + 架构审计

---

## 问题诊断摘要

| 缺陷 | 根因 | 影响 |
|------|------|------|
| Agent 输出被截断到 120 字符 | `compact_event_payload()` 和 `clip()` | 重试时 agent 丢失前轮完整上下文 |
| Worker bridge 无 system prompt | 三个 bridge 直接传 `-p task` | Agent 缺乏角色定位和完整性约束 |
| verifyCommands 为空数组 | acceptance-policy.json 未填充 | 验收门不执行任何自动化测试 |
| Agent 输出不持久化 | stdout 处理完即丢弃 | 后续 stage/retry 无法引用完整结果 |
| 任务拆解纯正则 | task_decomposer.py 无 LLM 回退 | 无法从论文正文推断实现任务 |
| Stage 间无输出验证 | dispatch_once 不检查上游产物 | 错误向下游传播 |
| 审计反馈无闭环 | Stage N 失败不自动触发 Stage L 修复 | 发现伪造后需人工介入 |

---

## 实施优先级

### P0 — 最小可行修复（阻断伪造输出）

#### P0-1: 持久化完整 spawn 输出

**修改文件**: `scripts/lib/milestones.py`

**方案**: 在 `run_dispatch_spawn()` 返回后、`dispatch_once()` 处理结果前，将完整 stdout 写入文件。

```
state/spawn-outputs/{task_id}-{agent}-{round}.json
```

**具体改动**:
1. 新增函数 `save_spawn_output(root, task_id, agent, spawn_result)`:
   - 写入 `state/spawn-outputs/` 目录
   - 文件名: `{task_id}-{agent}-{timestamp}.json`
   - 内容: 完整 spawn stdout + stderr + decision + normalizedReport
   - 保留最近 5 轮（按 task_id+agent 分组），旧文件自动清理
2. 在 `dispatch_once()` 中调用 `save_spawn_output()`
3. 修改 `build_agent_prompt()`:
   - 新增 `PREVIOUS_OUTPUT` 段
   - 从 `state/spawn-outputs/` 读取该 task 最近一次完整输出
   - 注入到 prompt 中（上限 4000 字符）

#### P0-2: 增加 Worker Bridge System Prompt

**修改文件**: `scripts/lib/claude_worker_bridge.py`, `scripts/lib/codex_worker_bridge.py`, `scripts/lib/gemini_worker_bridge.py`

**方案**: 为每个 bridge 增加 `--system-prompt` / 系统级指令。

**Claude bridge 改动**:
- 在 cmd 列表中增加 `--system-prompt` 参数
- System prompt 内容:
  1. 角色定位: "You are a specialist execution agent..."
  2. 完整性约束: 不允许伪造证据/不允许用启发式替代论文方法
  3. 输出规范: 必须返回 JSON schema 中定义的结构

**Codex bridge 改动**:
- 将 system prompt 作为 stdin 输入的前缀（codex exec 通过 stdin 接收）
- 格式: `SYSTEM:\n{system_prompt}\n\nTASK:\n{task}`

**Gemini bridge 改动**:
- 在 `--prompt` 参数中拼接 system prompt 前缀
- 格式: `{system_prompt}\n\n---\n\n{task}`

**System prompt 内容** (三个 bridge 共用模板):
```
You are a specialist execution agent in a multi-agent project team.

CRITICAL RULES:
1. Implement the EXACT algorithm/method described in the task, not approximations.
2. For optimization problems, use real solvers (CVXPY/Gurobi/MOSEK), never heuristics.
3. Never fabricate evidence, metrics, or completion claims.
4. If you cannot complete a component, report status=blocked with clear explanation.
5. All test assertions must verify behavioral correctness, not just syntax.
6. Run real commands and capture actual outputs as evidence.
```

#### P0-3: 启用 verifyCommands 并执行验证

**修改文件**: `config/acceptance-policy.json`, `scripts/lib/milestones.py`

**方案**:
1. 在 `acceptance-policy.json` 中填充 `verifyCommands`:
   ```json
   {
     "roles": {
       "coder": {
         "verifyCommands": [
           "cd {projectPath} && python -m pytest tests/ -x -q 2>&1 | tail -50"
         ],
         "verifyPassPattern": "passed|ok|success",
         "verifyFailPattern": "FAILED|ERROR|error|failed"
       }
     }
   }
   ```
2. 确认 `_run_verify_commands()` 在 `evaluate_acceptance()` 中被正确调用
3. 新增 `verifyPassPattern` / `verifyFailPattern` 字段解析逻辑

---

### P1 — 上下文质量提升

#### P1-1: 论文知识注入层 (Paper Context Injector)

**新建文件**: `scripts/lib/paper_context_injector.py`

**功能**:
- `load_paper_context(root, task_id)`: 从 task-context-map 读取 paperId → 查找论文工作目录下的结构化信息
- `extract_paper_sections(paper_dir)`: 读取 normalized-paper.md / claims.jsonl 等已提取的论文信息
- `build_paper_prompt_section(paper_context)`: 格式化为 prompt 注入段

**集成到 `build_agent_prompt()`**:
- 在 TASK_CONTEXT 之后注入 `PAPER_CONTEXT` 段
- 包含: 核心方法论描述、数学公式、基准指标、参考实现提示

#### P1-2: 正确性测试生成器

**新建文件**: `scripts/lib/correctness_test_generator.py`

**功能**:
- `generate_tests_for_task(root, task_id, paper_context)`:
  - 优化问题: 小规模验证解、约束可行性检查、目标值范围检查
  - 算法行为: 收敛性检查、论文 Table 数据点验证
- 输出 `tests/test_reproduction_correctness.py` 到工作区
- 在 Stage J 完成后自动调用，为 Stage L 准备测试

**集成方式**:
- 在 `dispatch_once()` 中、Stage L 派发前调用
- 生成的测试文件路径注入到 Stage L 的 prompt 中
- Stage L 的 `verifyCommands` 指向这些测试

#### P1-3: LLM 驱动的任务拆解

**修改文件**: `scripts/lib/task_decomposer.py`

**方案**:
- 新增 `llm_decompose_project(root, project_path, doc_text)`:
  - 调用 claude_worker_bridge 执行 LLM 分析
  - 使用专用 JSON schema 要求返回结构化任务列表
  - 每个任务包含: title, objective, methodologyRef, acceptanceTest, ownerHint, dependsOn, complexity
- 修改 `decompose_project()`:
  - 先尝试正则拆解
  - 如果产出任务数 < 3 或平均 confidence < 0.6，回退到 `llm_decompose_project()`
  - 合并两种结果，去重

---

### P2 — 闭环机制

#### P2-1: 审计反馈自动闭环

**修改文件**: `scripts/lib/milestones.py`

**方案**:
- 新增 `handle_audit_feedback(root, audit_task_id, impl_task_id, audit_result)`:
  - 当 Stage N (审计) 输出包含 "FAILED" 或 "fabricated" 信号时触发
  - 将 impl_task_id (Stage L) 重新标记为 pending
  - 在 retry_context 中注入 audit_findings（审计的完整失败报告）
  - 在 collaborated_hub 中创建 debugger → coder 的 handoff 消息
- 在 `classify_spawn_result()` 中检测审计失败信号
- 自动升级 executor（如从 codex → claude）以获得更强推理能力

#### P2-2: Stage 间输出 Gate Check

**修改文件**: `scripts/lib/milestones.py`

**方案**:
- 新增 `verify_upstream_outputs(root, task_id)`:
  - 检查 dependsOn 中所有前置任务的 status
  - 对于 paper-xhs-3min workflow，检查 stage 模板中定义的 "Required Outputs" 文件是否存在
  - 若缺失则阻断后续 dispatch 并返回具体缺失项
- 在 `dispatch_once()` 的 spawn 前调用

#### P2-3: Claude `--resume` 会话持续性

**修改文件**: `scripts/lib/claude_worker_bridge.py`, `scripts/lib/session_registry.py`

**方案**:
- 在 `session_registry` 中新增 `claude_session_id` 字段
- Claude bridge 执行成功后，从 stderr 中解析 session-id
- retry 时在 cmd 中增加 `--resume <session-id>` 参数
- 失败时回退到无 session 模式

---

### P3 — 测试覆盖

#### P3-1: 为所有改动编写测试

- `tests/test_spawn_output_persistence.py`: 验证完整输出保存/读取/清理
- `tests/test_worker_system_prompt.py`: 验证三个 bridge 的 system prompt 注入
- `tests/test_verify_commands.py`: 验证 acceptance gate 的 verifyCommands 执行
- `tests/test_paper_context_injector.py`: 验证论文上下文提取和注入
- `tests/test_upstream_gate_check.py`: 验证 stage 间输出验证
- 更新已有测试以适配新逻辑

---

## 文件变更清单

| 操作 | 文件路径 | 改动范围 |
|------|----------|---------|
| 修改 | `scripts/lib/milestones.py` | save_spawn_output, build_agent_prompt 注入 PREVIOUS_OUTPUT + PAPER_CONTEXT, verify_upstream_outputs, handle_audit_feedback |
| 修改 | `scripts/lib/claude_worker_bridge.py` | 增加 --system-prompt 参数 |
| 修改 | `scripts/lib/codex_worker_bridge.py` | stdin 前缀注入 system prompt |
| 修改 | `scripts/lib/gemini_worker_bridge.py` | prompt 前缀注入 system prompt |
| 修改 | `config/acceptance-policy.json` | 填充 verifyCommands, verifyPassPattern, verifyFailPattern |
| 修改 | `scripts/lib/context_pack.py` | compact_event_payload result 截断限制放宽 |
| 修改 | `scripts/lib/task_decomposer.py` | 新增 llm_decompose_project 回退 |
| 修改 | `scripts/lib/session_registry.py` | 新增 claude_session_id 字段 |
| 新建 | `scripts/lib/paper_context_injector.py` | 论文知识注入模块 |
| 新建 | `scripts/lib/correctness_test_generator.py` | 正确性测试生成模块 |
| 新建 | `tests/test_spawn_output_persistence.py` | spawn 输出持久化测试 |
| 新建 | `tests/test_worker_system_prompt.py` | bridge system prompt 测试 |
| 新建 | `tests/test_verify_commands.py` | 验收命令执行测试 |
| 新建 | `tests/test_paper_context_injector.py` | 论文知识注入测试 |
| 新建 | `tests/test_upstream_gate_check.py` | stage 间 gate check 测试 |

---

## 执行顺序

```
Phase 1 (P0): 最小可行修复
  1. P0-1: save_spawn_output + build_agent_prompt PREVIOUS_OUTPUT
  2. P0-2: 三个 worker bridge system prompt
  3. P0-3: acceptance-policy verifyCommands
  4. 对应测试编写

Phase 2 (P1): 上下文质量提升
  5. P1-1: paper_context_injector.py + 集成到 build_agent_prompt
  6. P1-2: correctness_test_generator.py + 集成到 dispatch_once
  7. P1-3: task_decomposer LLM 回退
  8. 对应测试编写

Phase 3 (P2): 闭环机制
  9. P2-1: audit feedback loop
  10. P2-2: upstream gate check
  11. P2-3: claude --resume session
  12. 对应测试编写
```

---

## 验收标准

- [ ] 所有改动通过 `python -m pytest tests/ -x` 
- [ ] dry-run-mvp 脚本通过
- [ ] Paper A2 workflow 重新执行时，Stage L 的 coder agent 能看到完整的前轮输出
- [ ] verifyCommands 在 acceptance gate 中实际执行
- [ ] System prompt 在三个 bridge 的 subprocess 调用中可见（通过 fake output 测试验证）
