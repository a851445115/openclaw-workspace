# Ops Dashboard v2

## 数据来源

- 事件文件: `state/ops.metrics.jsonl`
- 写入入口:
  - `scripts/lib/milestones.py dispatch` 路径写入:
    - `dispatch_done`
    - `dispatch_blocked`
    - `recovery_scheduled`
    - `recovery_escalated`
  - `scripts/lib/milestones.py autopilot` 路径写入:
    - `autopilot_cycle`
  - `scripts/lib/milestones.py scheduler-run` 路径写入:
    - `scheduler_tick`

每条事件为一行 JSON（JSONL），包含统一字段:
- `event`: 事件名
- `at`: UTC 时间（ISO-8601）
- `ts`: Unix 秒级时间戳

## 指标口径

聚合由 `scripts/lib/ops_metrics.py` 提供，默认按最近 7 天过滤。

- 吞吐（`throughputCompleted`）:
  - 口径: `dispatch_done` 事件数
- 成功率（`successRate`）:
  - 口径: `dispatch_done / (dispatch_done + dispatch_blocked)`
  - 无样本时为 `0.0`
- 阻塞原因分布（`blockedReasonDistribution`）:
  - 口径: `dispatch_blocked.reasonCode` 的计数分布
  - 缺失 reason 时记为 `unknown`
- 恢复率（`recoveryRate`）:
  - 口径: `recovery_scheduled / (recovery_scheduled + recovery_escalated)`
  - 无恢复样本时为 `0.0`
- 平均 cycle 时长（`averageCycleMs`）:
  - 口径: `dispatch_done` + `dispatch_blocked` 的 `cycleMs` 均值（毫秒）
  - 无样本时为 `0.0`

## Timeframe 过滤

`aggregate_metrics(root, days=N)` 会仅统计 `N` 天窗口内事件：
- 保留 `event_ts >= now - N * 86400`
- `event_ts` 优先取 `ts`，其次解析 `at`

## 导出方式

使用导出脚本：

```bash
scripts/export-weekly-ops-report --root <workspace_root> --days 7
```

输出为 JSON，可直接用于脚本消费：

```json
{
  "ok": true,
  "root": "...",
  "days": 7,
  "report": {
    "throughputCompleted": 0,
    "successRate": 0.0,
    "blockedReasonDistribution": {},
    "recoveryRate": 0.0,
    "averageCycleMs": 0.0
  }
}
```

## Status 入口

`@orchestrator status full` 会在状态消息附加 7 天核心指标摘要（`[OPS] ...`），并在 JSON 响应中附加 `opsMetrics` 字段。
