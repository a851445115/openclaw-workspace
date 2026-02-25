#!/bin/bash
#
# 每日市场洞察 - 主调度脚本
# 钢镚儿多Agent系统
#

set -e

PROJECT_DIR="$HOME/.openclaw/workspace/projects/daily-market-insight"
DATA_DIR="$PROJECT_DIR/data"
LOGS_DIR="$PROJECT_DIR/logs"
DATE_STR=$(date +"%Y-%m-%d")
LOG_FILE="$LOGS_DIR/run_${DATE_STR}.log"

# 确保目录存在
mkdir -p "$DATA_DIR" "$LOGS_DIR"

echo "========================================" | tee -a "$LOG_FILE"
echo "🚀 每日市场洞察报告系统" | tee -a "$LOG_FILE"
echo "   日期: $DATE_STR" | tee -a "$LOG_FILE"
echo "   时间: $(date)" | tee -a "$LOG_FILE"
echo "========================================" | tee -a "$LOG_FILE"

# Step 1: 新闻采集提示
echo "" | tee -a "$LOG_FILE"
echo "📡 Step 1: 新闻采集" | tee -a "$LOG_FILE"
echo "   需要通过 web_search 工具采集新闻" | tee -a "$LOG_FILE"

# Step 2: 运行分析 Agent
echo "" | tee -a "$LOG_FILE"
echo "📊 Step 2: 启动新闻分析 Agent (sisyphus)..." | tee -a "$LOG_FILE"

# Step 3: 运行预测 Agent
echo "" | tee -a "$LOG_FILE"
echo "🔮 Step 3: 启动市场趋势 Agent (sisyphus)..." | tee -a "$LOG_FILE"

# Step 4: 生成报告
echo "" | tee -a "$LOG_FILE"
echo "📝 Step 4: 生成报告..." | tee -a "$LOG_FILE"

echo "" | tee -a "$LOG_FILE"
echo "✅ 系统准备就绪！" | tee -a "$LOG_FILE"
echo "   实际执行需要通过 OpenClaw 调度" | tee -a "$LOG_FILE"
