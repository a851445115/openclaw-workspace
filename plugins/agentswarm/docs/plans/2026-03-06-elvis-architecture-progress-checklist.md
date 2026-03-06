# Elvis Architecture Integration Progress Checklist

基线计划文件：`docs/plans/2026-03-05-elvis-architecture-integration-plan.md`

## 结论

- 已完成：0 / 9
- 部分完成：7 / 9
- 未开始：2 / 9

> 说明：这里按原路线图中的“文件 + 集成 + 验收标准”口径统计；只有模块骨架、测试桩、未接主流程的任务，一律记为“部分完成”。

## 已完成

- [ ] 当前无已完全收口事项

## 部分完成

### P0-1 Worktree 隔离机制
- [x] 已有 `scripts/lib/worktree_manager.py`
- [x] 已在 dispatch 流程中调用 `ensure_task_worktree`
- [x] 已实现 `cleanup_task_worktree`
- [x] 将 `cleanup_task_worktree` 接入任务 done/blocked 完成闭环
- [x] 已启用并核对 `config/worktree-policy.json` 默认策略
- [ ] 跑真实并发 smoke，验证 5+ agents 独立工作目录
- [ ] 验证 bootstrap commands 与依赖隔离行为

### P0-2 任务注册表增强
- [x] 已新增 `state/active-sessions.json` 结构与 API
- [x] 已记录 `worktreePath` / `pid` / `tmuxSession` / `startTime` / `lastHeartbeat` / `status`
- [x] 已在 dispatch 中写入 upsert / heartbeat / status
- [x] 增加进程存活检查与自动回收闭环
- [x] 增加 heartbeat timeout / “无输出超时”检测
- [x] 将超时/僵尸会话处理接入 scheduler tick
- [ ] 补充对 scheduler 周期抖动 / heartbeat 质量波动的兜底验证与调参

### P1-1 主动工作发现
- [x] 已新增 `scripts/lib/proactive_scanner.py`
- [x] 已实现 arXiv / Feishu / pytest / TODO 扫描函数
- [x] 已新增 `config/scanner-policy.json`
- [x] 接入 `scheduler-daemon` / autopilot 定时运行
- [x] 将扫描结果自动转换为任务写入任务板
- [ ] 补强 `pytest` / `feishu` 文件喂数质量
- [ ] 为飞书需求变更增加优先级更新闭环

### P1-2 多模型代码审查
- [x] 已新增 `scripts/lib/multi_reviewer.py`
- [x] 已实现 reviewer 权重聚合逻辑
- [x] 已新增 `config/multi-reviewer-policy.json`
- [x] 接入 done 验收主流程
- [x] 将 reviewer 结果写入 acceptance/gate 输出
- [x] 支持 `disabled` / `dryRun` / `enabled` 与 fake outputs 测试通道
- [ ] 让 reviewer 评估完整代码 diff，而不是仅评估 acceptance payload 摘要

### P2-1 实时干预能力
- [x] 已新增 `scripts/intervene-task`
- [x] 已选择并实现文件注入方案（state + prompt injection）
- [x] 已将干预信号接入 worker prompt 运行时
- [x] 已支持 `intervene` / `intervention` / `clear intervention` 指令与消息回执
- [ ] 尚未实现 tmux/长会话内的在线中断式干预
- [ ] 尚未完成真实 worker 执行中的中途纠偏验收

### P2-2 业务上下文存储
- [x] 已新增 `scripts/lib/context_store.py`
- [x] 已支持自动创建 `state/business_context.db`
- [x] 已建表：`customers` / `papers` / `reproduction_history`
- [x] 已通过 `BUSINESS_CONTEXT` 将上下文注入 agent prompt
- [ ] 尚未扩展 orchestrator / workflow 的显式上下文管理命令
- [ ] 尚未完成更高层业务流的上下文回写闭环

### P3-1 成本优化仪表盘
- [x] 已在 `scripts/lib/ops_metrics.py` 增加 `dailyCost`
- [x] 已增加 `costPerCommit`
- [x] 已增加 executor 维度的 `agentBreakdown`
- [x] 已在 manager report / `status full` 中暴露成本摘要
- [ ] 当前仍是内置估算价目表，不代表真实账单成本
- [ ] 尚未提供独立成本看板或更细粒度账单归因

## 未开始

### P3-2 智能失败分类
- [ ] 新增 `scripts/lib/failure_classifier.py`
- [ ] 在 `scripts/lib/recovery_loop.py` 接入失败类型识别
- [ ] 为不同失败类型选择不同恢复策略
- [ ] 增加对应单元测试与回归样例

### P3-3 可视化证据要求
- [ ] 在 `config/acceptance-policy.json` 增加 `requireTypes`
- [ ] 增加 `minScreenshots`
- [ ] 视论文复现场景增加 `requireComparison` / `minPlots`
- [ ] 将可视化证据校验接入 done 验收闭环

## 建议推进顺序

1. 收口 P0-1：worktree 创建、启用、清理、并发验证闭环
2. 收口 P0-2：active session 存活检测与无输出超时闭环
3. 接入 P1-1：scanner → task board 自动建任务
4. 接入 P1-2：multi reviewer → acceptance gate
5. 推进 P2-1：file-backed intervention → prompt/runtime 闭环
6. 再推进 P2-2 / P3 能力型增强
