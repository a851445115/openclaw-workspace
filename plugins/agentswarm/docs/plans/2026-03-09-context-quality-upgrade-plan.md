# Agentswarm 上下文质量升级 + Workflow 抽象层重构 实施计划

> 日期: 2026-03-09 (v2 修订: 增加 Workflow 结构性重构)
> 目标: 1) 解决 agent 间上下文断裂、验收门过弱、prompt 质量不足三大核心缺陷; 2) 解耦 workflow 定义与通用引擎
> 来源: Paper A2 (EHH-IES) 复现失败复盘 + 架构审计 + Workflow 兼容性分析

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
| **Workflow 与通用引擎耦合** | XHS 16 个 stage 硬编码在 milestones.py 中 | 无法添加新 workflow、环境要求泄漏到通用 prompt |
| **模板系统不灵活** | 仅支持 4 个固定占位符 | stage 无法引用上游产物路径 |
| **论文知识注入层缺失** | build_agent_prompt 无 workflow-scoped 上下文 | Stage L/M/N 无法获得论文方法论细节 |

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

### P1 — Workflow 抽象层重构（结构性重构）

#### P1-W1: Workflow 配置 Schema + 加载器

**新建文件**: `config/workflows/paper-xhs-3min.json`
**修改文件**: `scripts/lib/milestones.py` (新增 `workflow_registry.py` 功能内联或独立)

**方案**:
1. 创建 `config/workflows/paper-xhs-3min.json`:
   ```json
   {
     "name": "paper-xhs-3min",
     "description": "论文 → 小红书 + 复现 16 阶段 workflow",
     "defaultWorkflowRoot": "~/.openclaw/projects/paper-xhs-3min-workflow",
     "defaultOutputRoot": "~/xhs-share",
     "contextMarkerFile": "orchestrator-bootstrap.json",
     "placeholders": ["paper_id", "workflow_root", "run_dir", "pdf_path"],
     "envRequirements": [
       "必须使用 conda 环境 'workplace'（包含 python + gurobi 优化求解器）",
       "对于优化问题（SDP/DRO等），必须调用真实求解器",
       "所有 Python 命令必须在 workplace 环境中执行：conda run -n workplace python ..."
     ],
     "stages": [ ... 当前 XHS_STAGE_DEFINITIONS 内容搬到这里 ... ]
   }
   ```
2. 新增加载函数 `load_workflow_config(root, workflow_name) -> Dict`:
   - 从 `config/workflows/{workflow_name}.json` 加载
   - 验证 schema 完整性
3. 新增注册查找函数 `list_workflows(root) -> List[str]`

#### P1-W2: 从 milestones.py 提取 XHS 硬编码

**修改文件**: `scripts/lib/milestones.py`

**方案**:
1. 删除 `DEFAULT_XHS_WORKFLOW_ROOT`, `DEFAULT_XHS_OUTPUT_ROOT`, `DEFAULT_XHS_N8N_TRIGGER_SCRIPT`, `XHS_WORKFLOW_NAME`, `XHS_TEMPLATE_DIR`, `XHS_CONTEXT_MARKER_FILE`, `XHS_ALLOWED_PLACEHOLDERS`, `XHS_STAGE_DEFINITIONS` 等硬编码常量
2. 改为从 `load_workflow_config()` 动态加载
3. `xhs_bootstrap_once()` 泛化为 `workflow_bootstrap_once(args, workflow_name)`
4. `read_xhs_stage_template()` 泛化为 `read_stage_template(template_dir, template_file)`
5. `render_xhs_stage_prompt()` 泛化为 `render_stage_prompt(template_text, allowed_placeholders, values)`
6. 保留 XHS 常量作为兼容别名（指向 config 加载结果），避免破坏现有测试

#### P1-W3: 移除 build_agent_prompt 中的硬编码 workflow 判断

**修改文件**: `scripts/lib/milestones.py`

**方案**:
1. 删除 `if project_path and "paper-xhs-3min-workflow" in project_path` 分支
2. 改为从 task 的 project context 中读取 workflow_name → 加载 workflow config → 读取 `envRequirements`
3. 使 `build_agent_prompt()` 对所有 workflow 统一处理环境要求

#### P1-W4: 论文知识注入（Workflow-scoped）

**新建文件**: `scripts/lib/paper_context_injector.py`

**方案**:
- `load_paper_context(run_dir)`: 从 workflow run_dir 读取已提取的论文结构化信息
  - `{run_dir}/artifacts/a0-extract/raw-text.md` → 核心方法论
  - `{run_dir}/repro/j-scope/method-mapping.md` → 方法映射
- `build_paper_prompt_section(paper_context) -> str`: 格式化为 prompt 段
- **集成方式**: 在 workflow config 中声明 `contextInjectors` 字段，`build_agent_prompt()` 根据 workflow config 决定是否注入
- **仅影响** paper-xhs-3min workflow 的 Stage J-O（论文复现阶段）

#### P1-W5: 扩展模板占位符系统

**修改文件**: `scripts/lib/milestones.py` (泛化后的 `render_stage_prompt`)

**方案**:
1. 占位符不再硬编码为 4 个固定值
2. 从 workflow config 的 `placeholders` 字段读取允许的占位符列表
3. 支持 `{upstream_output_dir}` 等动态占位符（由 bootstrap 时根据前序 stage 的 Required Outputs 推算）

---

### P2 — 剩余增强项

#### P2-1: LLM 驱动的任务拆解

**修改文件**: `scripts/lib/task_decomposer.py`

**方案**:
- 新增 `llm_decompose_project(root, project_path, doc_text)`:
  - 调用 claude_worker_bridge 执行 LLM 分析
  - 使用专用 JSON schema 要求返回结构化任务列表
- 修改 `decompose_project()`:
  - 先尝试正则拆解
  - 如果产出任务数 < 3 或平均 confidence < 0.6，回退到 LLM

---

### P3 — 测试覆盖

#### P3-1: 为所有改动编写测试

- `tests/test_workflow_registry.py`: workflow config 加载、schema 验证、泛化 bootstrap
- `tests/test_paper_context_injector.py`: 论文上下文提取（workflow-scoped）
- 更新已有测试以适配重构后的接口

---

## 文件变更清单

| 操作 | 文件路径 | 改动范围 |
|------|----------|---------|
| 修改 | `scripts/lib/milestones.py` | 提取 XHS 硬编码 → workflow config 加载, 泛化 bootstrap/template/prompt |
| 新建 | `config/workflows/paper-xhs-3min.json` | workflow 声明式配置 |
| 新建 | `scripts/lib/paper_context_injector.py` | 论文知识注入（workflow-scoped） |
| 修改 | `scripts/lib/task_decomposer.py` | LLM 回退 |
| 新建 | `tests/test_workflow_registry.py` | workflow 加载 + bootstrap 测试 |
| 新建 | `tests/test_paper_context_injector.py` | 论文上下文测试 |

---

## 执行顺序

```
Phase 1 (P0): 最小可行修复 ✅ 已完成
  1. P0-1: spawn output 持久化
  2. P0-2: build_agent_prompt PREVIOUS_OUTPUT 注入
  3. P0-3: worker bridge system prompt
  4. P0-4: verifyCommands 填充
  + Claude --resume / payload 扩容 / stage gate / audit feedback

Phase 2 (P1): Workflow 抽象层重构
  5. P1-W1: workflow config schema + 加载器
  6. P1-W2: 提取 XHS 硬编码到 config
  7. P1-W3: build_agent_prompt 去硬编码
  8. P1-W4: paper_context_injector (workflow-scoped)
  9. P1-W5: 扩展模板占位符

Phase 3 (P2): 剩余增强
  10. P2-1: LLM task_decomposer 回退

Phase 4 (P3): 测试覆盖
  11. workflow registry + injector 测试
```

---

## 验收标准

- [x] P0-1: spawn output 持久化 (save_spawn_output / load_latest_spawn_output / _prune_spawn_outputs)
- [x] P0-2: build_agent_prompt PREVIOUS_OUTPUT 段注入
- [x] P0-3: 三个 worker bridge system prompt (claude/codex/gemini)
- [x] P0-4: acceptance-policy.json verifyCommands 填充 (coder + debugger)
- [x] P1(旧): Claude --resume session 支持
- [x] P1(旧): compact_event_payload result 截断限制从 120 提升到 500 字符
- [x] P1(旧): dispatch_once stage gate check
- [x] P2(旧): 审计反馈闭环 (handle_audit_feedback)
- [x] 桥接测试已更新适配 system prompt 变更
- [x] P1-W1: workflow config schema + 加载器
- [x] P1-W2: XHS 硬编码提取到 config (泛化 bootstrap/template/render + 兼容别名)
- [x] P1-W3: build_agent_prompt 去掉 workflow 名称判断 (detect_workflow_from_project)
- [x] P1-W4: paper_context_injector (workflow-scoped, stage J-O)
- [x] P1-W5: 模板占位符扩展 (upstream_output_dir)
- [x] P2-1: LLM task_decomposer 回退 (llm_decompose_project)
- [x] P3: workflow 相关测试 (35 tests: test_workflow_registry + test_paper_context_injector)

> 最后更新: 2026-03-09 v3 — 全部任务完成
