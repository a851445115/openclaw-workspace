# Elvis Sun 架构集成计划

> 基于 Elvis Sun 的 OpenClaw + Agent Swarm 架构分析，制定 agentswarm 升级路线图。
> 目标：结合 Elvis 的实战经验与我们的系统化架构，打造高效的论文复现自动化平台。

## 核心对标分析

### 已实现的相似功能 ✅
- 双层架构（orchestrator + worker agents）
- 执行器路由（coder→claude_cli, debugger→codex_cli）
- 任务状态追踪（append-only event log）
- 自动推进（autopilot）
- 监控循环（scheduler-daemon）
- 失败恢复（recovery_loop.py）
- 预算控制（budget_policy.py）
- 验收门禁（acceptance-policy.json）
- 任务分解（task_decomposer.py）

### 关键差异点（Elvis 有但我们缺少的）
1. **Worktree 物理隔离** - 支持 5+ agents 并发
2. **tmux 会话管理** - 实时干预能力
3. **主动工作发现** - 扫描 Sentry/会议/git log
4. **多模型代码审查** - Codex + Gemini + Claude
5. **业务上下文存储** - 结构化客户数据库

### 我们的独特优势 💎
- 治理控制系统（pause/freeze/abort）
- 协作中心（multi-agent coordination）
- 专家组咨询（expert_group.py）
- 知识反馈循环（knowledge_adapter.py）
- 角色策略库（role-strategies.json）

---

## 实施路线图

### P0 - 立即实施（1-2 周）

#### P0-1: Worktree 隔离机制
**文件**：
- 新增：`scripts/lib/worktree_manager.py`
- 修改：`scripts/lib/claude_worker_bridge.py`
- 修改：`scripts/lib/codex_worker_bridge.py`

**实现要点**：
```python
# worktree_manager.py
class WorktreeManager:
    def create_worktree(self, task_id):
        """为每个任务创建独立 worktree"""
        worktree_path = f"../task-{task_id}"
        subprocess.run(["git", "worktree", "add", worktree_path, "-b", f"task/{task_id}"])
        return worktree_path

    def cleanup_worktree(self, task_id):
        """任务完成后清理 worktree"""
        subprocess.run(["git", "worktree", "remove", f"../task-{task_id}"])
```

**验收标准**：
- 支持 5+ agents 并发工作
- 每个 agent 独立依赖环境
- 一个 agent 崩溃不影响其他

#### P0-2: 任务注册表增强
**文件**：
- 修改：`scripts/lib/session_registry.py`
- 新增：`state/active-sessions.json`

**数据结构**：
```json
{
  "T-123": {
    "worktreePath": "/path/to/task-T-123",
    "pid": 12345,
    "tmuxSession": "agent-T-123",
    "startTime": "2026-03-05T20:00:00Z",
    "lastHeartbeat": "2026-03-05T20:15:00Z",
    "status": "running"
  }
}
```

**验收标准**：
- 记录 PID、worktree 路径、启动时间
- 监控进程存活状态
- 检测"无输出超时"（5 分钟无新 commit）

---

### P1 - 短期目标（2-4 周）

#### P1-1: 主动工作发现
**文件**：
- 新增：`scripts/lib/proactive_scanner.py`
- 新增：`config/scanner-policy.json`

**实现要点**：
```python
class ProactiveScanner:
    def scan_arxiv_rss(self):
        """扫描 arXiv 新论文 → 评估复现难度 → 生成任务"""

    def scan_feishu_messages(self):
        """扫描飞书群 → 提取需求变更 → 更新优先级"""

    def scan_pytest_failures(self):
        """扫描测试失败 → 生成调试任务"""

    def scan_todo_comments(self):
        """扫描 TODO 注释 → 生成技术债务清单"""
```

**触发方式**：
- cron 每小时运行
- 或集成到 scheduler-daemon

**验收标准**：
- 自动发现新论文并生成任务
- 自动提取飞书群需求变更
- 自动生成调试任务

#### P1-2: 多模型代码审查
**文件**：
- 新增：`scripts/lib/multi_reviewer.py`
- 修改：`config/acceptance-policy.json`

**实现要点**：
```python
REVIEWERS = [
    {"model": "codex", "focus": "logic,edge-cases", "weight": 0.4},
    {"model": "gemini", "focus": "security,scalability", "weight": 0.3},
    {"model": "claude", "focus": "readability", "weight": 0.3}
]

def review_pr(task_id, changes):
    scores = []
    for reviewer in REVIEWERS:
        score = call_reviewer(reviewer["model"], changes)
        scores.append(score * reviewer["weight"])

    final_score = sum(scores)
    return final_score >= 0.7  # 通过阈值
```

**验收标准**：
- 三个模型交叉验证
- 加权评分机制
- 集成到 done 验收流程

---

### P2 - 中期目标（1-2 月）

#### P2-1: 实时干预能力
**文件**：
- 新增：`scripts/intervene-task`
- 修改：`scripts/lib/milestones.py`

**实现方案**：
```bash
# 方案 A：tmux 集成
./scripts/intervene-task --task-id T-123 --message "Stop. Focus on API first."

# 方案 B：文件监控
echo "Focus on API first" > state/interventions/T-123.txt
# agent 定期读取并调整
```

**验收标准**：
- 支持中途纠正方向
- 节省 token 消耗
- 提高任务成功率

#### P2-2: 业务上下文存储
**文件**：
- 新增：`state/business_context.db`
- 新增：`scripts/lib/context_store.py`

**数据库结构**：
```sql
CREATE TABLE customers (
    id TEXT PRIMARY KEY,
    name TEXT,
    requirements TEXT,
    tech_stack TEXT
);

CREATE TABLE papers (
    id TEXT PRIMARY KEY,
    title TEXT,
    authors TEXT,
    arxiv_id TEXT,
    difficulty_score REAL
);

CREATE TABLE reproduction_history (
    paper_id TEXT,
    success BOOLEAN,
    issues TEXT,
    lessons_learned TEXT
);
```

**验收标准**：
- 结构化存储客户数据
- 论文元数据索引
- 复现历史追踪

---

### P3 - 长期优化（3+ 月）

#### P3-1: 成本优化仪表盘
**文件**：
- 修改：`scripts/lib/ops_metrics.py`

**指标**：
```json
{
  "daily_cost": 6.33,
  "cost_per_commit": 0.127,
  "agent_breakdown": {
    "coder": {"tokens": 1.2M, "cost": 4.80},
    "debugger": {"tokens": 0.8M, "cost": 1.53}
  }
}
```

#### P3-2: 智能失败分类
**文件**：
- 新增：`scripts/lib/failure_classifier.py`
- 修改：`scripts/lib/recovery_loop.py`

**失败模式**：
```python
FAILURE_PATTERNS = {
    "context_overflow": ["context length", "token limit"],
    "wrong_direction": ["not what", "incorrect"],
    "missing_info": ["unclear", "need more"]
}
```

#### P3-3: 可视化证据要求
**文件**：
- 修改：`scripts/lib/evidence_normalizer.py`
- 修改：`config/acceptance-policy.json`

**证据类型**：
```json
{
  "roles": {
    "paper-ingestor": {
      "requireTypes": ["plot", "data"],
      "minScreenshots": 2
    },
    "coder": {
      "requireTypes": ["log", "screenshot"],
      "minScreenshots": 1
    }
  }
}
```

---

## 论文复现业务特殊场景

### 场景 1: 论文监控自动化
```python
def morning_routine():
    papers = scan_arxiv(category="eess.SP")
    for paper in papers:
        difficulty = estimate_difficulty(paper)
        if difficulty < 0.7:
            create_task(
                title=f"复现论文：{paper.title}",
                owner="paper-ingestor",
                priority=paper.citations * 10
            )
```

### 场景 2: 客户沟通自动化
```python
def monitor_feishu():
    messages = get_new_messages(group_id="customer_group")
    for msg in messages:
        if "催进度" in msg.text:
            report = generate_progress_report(customer_id)
            send_to_feishu(report)
        elif "需求变更" in msg.text:
            update_task_priority(extract_task_id(msg))
```

### 场景 3: 实验结果验证
```python
ACCEPTANCE_POLICY = {
    "paper-ingestor": {
        "requireTypes": ["plot"],
        "requireComparison": True,
        "minPlots": 3
    }
}
```

---

## 实施优先级建议

1. **先做 P0**（Worktree + 注册表）- 高并发基础
2. **快速验证 P1**（主动发现 + 多模型审查）- 效率提升关键
3. **逐步完善 P2/P3** - 根据实际反馈调整

---

## 成功指标

- [ ] 支持 5+ agents 并发工作
- [ ] 自动发现新论文并生成任务
- [ ] 多模型交叉验证通过率 > 90%
- [ ] 平均任务成功率 > 85%
- [ ] 成本效率 < $0.15/commit
- [ ] 中途干预能力可用
