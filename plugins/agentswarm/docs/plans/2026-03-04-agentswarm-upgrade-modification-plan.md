# Agentswarm 升级修改计划（方案2）

> 目标：在现有 orchestrator 基础上升级为“总经理型 orchestrator + 员工协作 + 临时专家组”，并采用“会话优先 + 文件兜底”的上下文策略，保证 blocked 可持续恢复。

## 1. 目标与范围

### 1.1 核心目标
1. Orchestrator 持续掌握全局上下文并定期对人汇报。
2. 每个本地 CLI worker（codex/claude）具备可恢复上下文：优先续会话，断会话后自动回放上下文包。
3. Agent 间协作从“自由文本”升级为“结构化协作协议”。
4. 当任务风险超阈值时，自动拉起临时专家组协同分析并收敛方案。

### 1.2 非目标（本批次不做）
1. 不做 UI 重构（保留现有飞书控制台风格）。
2. 不做外部数据库依赖（继续文件态存储，便于本地稳定运行）。
3. 不改变既有任务板状态机语义（仅增强 retry/collab/expert 子流程）。

## 2. 当前问题（需要修复）
1. CLI worker 每次 spawn 为新进程，天然无会话记忆。
2. blocked 后虽有 recovery chain，但上下文传递不完整，retry 信息损失。
3. 缺少 agent-to-agent 标准协作通道，问题排查效率受限。
4. 缺少“是否建专家组”的可解释决策机制。

## 3. 实施批次与优先级

## Batch P0（最高优先）上下文韧性
- P0-1: 引入 worker 会话注册表（session registry）
- P0-2: 引入 retry 上下文包（失败摘要/产物索引/未完成清单/关键日志）
- P0-3: 升级 dispatch/autopilot，使 retry 自动注入 context pack
- P0-4: 扩展 reasonCode 映射（包含 no_completion_signal）并纳入 recovery

验收标准：
- 同任务连续 blocked 的第2轮 prompt 必含上轮失败根因 + 未完成项；
- 会话失效时可无人工介入继续执行；
- recovery 自动链路不中断。

## Batch P1 员工协作协议
- P1-1: 新增协作事件协议（handoff/consult/question/answer/decision）
- P1-2: 新增协作日志文件与 thread 关联
- P1-3: orchestrator 负责协作中继与超时升级
- P1-4: prompt 注入协作线程摘要

验收标准：
- agent 间交互可追溯；
- 同一问题的问答可在后续 prompt 自动继承。

## Batch P2 临时专家组
- P2-1: 增加专家组触发规则引擎（重试次数、阻塞时长、影响面、风险）
- P2-2: 专家组任务模板（根因/复现/方案评估分工）
- P2-3: 结论收敛器（共识输出 + 最终执行任务回写）
- P2-4: 专家组生命周期（创建-执行-收敛-归档）

验收标准：
- 高风险任务自动建组；
- 输出可执行修复计划并回写主看板。

## Batch P3 管理者体验
- P3-1: 定期汇报模板（日报/周报）
- P3-2: 飞书卡片新增“协作线程摘要/专家组状态”
- P3-3: 关键 KPI（blocked 恢复率、专家组闭环时长、任务完工率）

## 4. 关键文件改造清单（预计）

- 修改：`/Users/chengren17/.openclaw/workspace/plugins/agentswarm/scripts/lib/milestones.py`
- 修改：`/Users/chengren17/.openclaw/workspace/plugins/agentswarm/scripts/lib/recovery_loop.py`
- 修改：`/Users/chengren17/.openclaw/workspace/plugins/agentswarm/scripts/lib/codex_worker_bridge.py`
- 修改：`/Users/chengren17/.openclaw/workspace/plugins/agentswarm/scripts/lib/claude_worker_bridge.py`
- 新增：`/Users/chengren17/.openclaw/workspace/plugins/agentswarm/scripts/lib/session_registry.py`
- 新增：`/Users/chengren17/.openclaw/workspace/plugins/agentswarm/scripts/lib/context_pack.py`
- 新增：`/Users/chengren17/.openclaw/workspace/plugins/agentswarm/scripts/lib/collaboration_hub.py`
- 新增：`/Users/chengren17/.openclaw/workspace/plugins/agentswarm/scripts/lib/expert_group.py`
- 修改：`/Users/chengren17/.openclaw/workspace/plugins/agentswarm/config/recovery-policy.json`
- 新增：`/Users/chengren17/.openclaw/workspace/plugins/agentswarm/config/collaboration-policy.json`
- 新增：`/Users/chengren17/.openclaw/workspace/plugins/agentswarm/config/expert-group-policy.json`
- 新增/修改测试：`/Users/chengren17/.openclaw/workspace/plugins/agentswarm/tests/*`

## 5. 回归与发布策略
1. 先 dry-run 全流程（单任务、多任务、连续 blocked、会话失效恢复）。
2. 再在真实飞书群执行 canary（仅部分任务启用协作/专家组）。
3. 达标后全量启用，并保留一键回滚开关（禁用专家组/禁用会话优先）。

## 6. 风险与应对
1. 会话泄漏风险 -> 增加 TTL + 心跳回收。
2. 协作噪声过高 -> 限制并发线程数与往返轮数。
3. 误触发专家组 -> 使用硬规则 + 阈值双重门禁。
4. Token 成本上升 -> context pack 分层裁剪（摘要优先，明细按需读取）。

## 7. 完成定义（DoD）
1. blocked 任务可自动重试并保留完整上下文语义。
2. agent 间协作有结构化日志与可见链路。
3. 专家组可自动触发并产出可执行修复方案。
4. 对人汇报可稳定输出进度、风险、下一步计划。
