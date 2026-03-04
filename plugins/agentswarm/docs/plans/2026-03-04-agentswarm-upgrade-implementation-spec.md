# Agentswarm 升级完整实施方案（方案2：会话优先 + 文件兜底）

## 1. 架构目标

将现有插件升级为三层协作架构：
1. **总经理层（Orchestrator）**：全局调度、验收、汇报、风险治理。
2. **执行层（Workers）**：本地 CLI 执行（codex/claude），支持上下文连续。
3. **应急层（Expert Group）**：高风险问题的临时多 agent 会诊。

最终效果：
- 常规任务自动推进；
- blocked 可持续恢复；
- 员工间可见且可追溯协作；
- 高风险问题自动升级并收敛。

---

## 2. 关键设计原则

1. **上下文优先**：任何 retry 必须附带上轮关键信息，不允许“裸重试”。
2. **结构化优先**：协作消息与执行回报必须 JSON 结构化。
3. **可审计优先**：所有决策（为何重试/为何建组）必须落盘可回放。
4. **渐进增强**：失败时回退到文件重放，不阻断主流程。

---

## 3. 方案2核心：会话优先 + 文件兜底

## 3.1 会话优先
新增 `session_registry` 管理每个任务/agent 的会话：
- 键：`taskId + agent + executor`
- 值：`sessionId, startedAt, lastActiveAt, status, workspace, model`

执行策略：
1. dispatch/retry 时先查可复用会话。
2. 若会话健康，则在该会话续跑（最大化语义连续）。
3. 若会话失效（超时/崩溃/不可达），切换到文件兜底恢复。

## 3.2 文件兜底（context pack）
新增 `context_pack` 生成重试上下文包：
- `lastPromptDigest`: 上次核心指令摘要
- `lastOutputDigest`: 上次输出摘要（含 status/reasonCode）
- `blockedReason`: 阻塞根因与分类
- `artifactIndex`: 产物路径索引（日志/代码/报告）
- `unfinishedChecklist`: 未完成项清单
- `recentDecisions`: 最近协作/决策结论

注入策略：
- 重试 prompt 固定包含 `RETRY_CONTEXT_PACK` 节；
- 明细内容超长时仅保留摘要 + 文件索引路径。

---

## 4. 员工协作（Agent-to-Agent）实现

## 4.1 协作协议（Collaboration Protocol v1）
统一消息类型：
- `handoff`
- `consult`
- `question`
- `answer`
- `decision`

每条消息字段：
- `taskId`
- `threadId`
- `fromAgent`
- `toAgent`
- `messageType`
- `summary`
- `evidence[]`
- `request`
- `deadline`
- `createdAt`

持久化位置：
- `state/collab.messages.jsonl`
- `state/collab.threads.json`

## 4.2 协作流程
1. agent 输出 `consult/question` 请求。
2. orchestrator 校验后中继给目标 agent（群里可见@）。
3. 目标 agent 回答 `answer/decision`。
4. orchestrator 将线程摘要写入任务上下文，下轮 prompt 自动注入。

## 4.3 治理规则
- 每个 thread 最大 3 轮往返；
- 超时自动升级给 orchestrator；
- 重复问题去重（`taskId + normalized_question_hash`）。

---

## 5. 临时专家组（Expert Group）实现

## 5.1 触发规则（Rule-first）
满足任一条件即触发：
1. 同任务连续 blocked >= 2 次；
2. blocked 持续时长 >= policy 阈值（如 30 分钟）；
3. 影响下游未完成任务数 >= N；
4. reasonCode 属于高危集合（核心算法失败、数据一致性问题、关键验收失败）。

配置文件：
- `config/expert-group-policy.json`

## 5.2 建组与分工
- 组 ID：`EG-<taskId>-<ts>`
- 建议角色：`coder + debugger + invest-analyst`（必要时加入 broadcaster/knowledge-curator）
- 分工模板：
  - coder：实现可行性与最小修复路径
  - debugger：根因链路与复现最短路径
  - analyst：风险评估与方案权衡

## 5.3 收敛输出
每位专家统一输出：
- `hypothesis`
- `evidence`
- `confidence`
- `proposedFix`
- `risk`

orchestrator 聚合后输出：
- `consensusPlan`
- `owner`
- `executionChecklist`
- `acceptanceGate`

落盘：
- `state/expert-groups/<groupId>.json`

---

## 6. Orchestrator 作为“项目总经理”

## 6.1 定期汇报
新增汇报生成器（日报/周报）：
- 完成进度（done/pending/blocked）
- 风险TOP
- 专家组状态
- 下周期计划

输出：
- 飞书消息 + `state/reports/*.md`

## 6.2 指挥与治理
- 自动推进主流程不变（autopilot/scheduler）；
- 新增两类治理动作：
  1) `trigger_collab`
  2) `trigger_expert_group`

---

## 7. 兼容与回滚

1. 默认兼容旧任务板格式。
2. 新增字段均可选，不破坏旧 JSON 解析。
3. 提供开关：
- `collaboration.enabled`
- `expertGroup.enabled`
- `sessionPriority.enabled`
4. 任一异常可降级到：
- 无会话 + context pack
- 无专家组 + 传统 recovery

---

## 8. 测试方案

## 8.1 单测
- 会话复用/失效回退
- context pack 生成与裁剪
- 协作协议入库/去重/超时
- 专家组触发规则

## 8.2 集成测试
- blocked -> retry -> done 全链路
- blocked -> expert group -> consensus -> done
- 会话断开后恢复执行

## 8.3 真实群验收
- 单任务协作
- 多任务串行交接
- 高风险场景自动建组
- 对人汇报质量检查

---

## 9. 里程碑与工期（建议）

1. P0 上下文韧性：2-3 天
2. P1 员工协作：2-3 天
3. P2 专家组：3-5 天
4. P3 汇报体验：1-2 天

总计建议：8-13 天（含验收与回归）。

---

## 10. 成功指标（KPI）

1. blocked 自动恢复成功率 >= 80%
2. 专家组问题闭环时长中位数下降 >= 30%
3. 任务完成率（done/总任务）稳定提升
4. 人工介入频次逐月下降

