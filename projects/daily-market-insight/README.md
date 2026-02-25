# 每日市场洞察 - 多Agent系统

## 📁 项目结构

```
daily-market-insight/
├── scripts/
│   ├── daily_report.py    # Python主逻辑
│   └── run.sh             # Shell启动脚本
├── data/                  # 数据存储
├── logs/                  # 日志文件
└── README.md              # 本文件
```

## 🤖 Agent架构

```
钢镚儿 (协调者)
    │
    ├── 新闻采集 Agent (web_search + web_fetch)
    │       ↓
    ├── 新闻分析 Agent (opencode sisyphus)
    │       ↓
    ├── 市场趋势 Agent (opencode sisyphus)
    │       ↓
    └── 报告生成 Agent (feishu_doc)
```

## 🕒 执行时间

每天早上 10:00 (GMT+8)

## 📋 执行流程

1. **新闻采集** - 搜索宏观经济、科技等领域最新新闻
2. **新闻分析** - 提取重点、情感分析、影响评估
3. **市场预测** - A股/港股/美股趋势预测
4. **报告生成** - 创建飞书文档 + 群消息通知

## 📝 新闻搜索关键词

### 宏观经济
- 中国经济 GDP 通胀
- 美联储 利率 货币政策
- 央行 降准 LPR

### 科技行业
- 人工智能 AI 大模型
- 芯片 半导体
- 新能源 电动车

### 市场
- A股 港股 美股
- 股市 行情
