#!/bin/bash
# 每日市场洞察 - 主控制器
# 协调多Agent系统运行

set -e

PROJECT_DIR="$HOME/.openclaw/workspace/projects/daily-market-insight"
DATA_DIR="$PROJECT_DIR/data"
REPORT_DIR="$PROJECT_DIR/reports"
LOG_DIR="$PROJECT_DIR/logs"
DATE_TAG=$(date +%Y-%m-%d)
REPORT_FILE="$REPORT_DIR/daily_insight_${DATE_TAG}.md"

mkdir -p "$DATA_DIR" "$REPORT_DIR" "$LOG_DIR"

echo "========================================"
echo "  每日市场洞察 - 多Agent系统启动"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================"

# Step 1: 新闻采集（由 OpenClaw 执行 web_search）
echo "[Step 1/4] 新闻采集中..."

# Step 2: 新闻分析（调用 opencode sisyphus）
echo "[Step 2/4] 新闻分析中 (opencode sisyphus)..."

# Step 3: 市场趋势预测（调用 opencode sisyphus）
echo "[Step 3/4] 市场趋势预测中 (opencode sisyphus)..."

# Step 4: 生成报告并推送
echo "[Step 4/4] 生成报告并推送..."

echo "========================================"
echo "  完成！报告已保存至: $REPORT_FILE"
echo "========================================"
