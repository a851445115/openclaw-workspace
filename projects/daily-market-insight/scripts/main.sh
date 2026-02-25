#!/bin/bash
# 每日市场洞察 - 完整运行脚本
# 协调所有Agent按顺序执行

PROJECT_DIR="$HOME/.openclaw/workspace/projects/daily-market-insight"
DATA_DIR="$PROJECT_DIR/data"
REPORT_DIR="$PROJECT_DIR/reports"
LOG_DIR="$PROJECT_DIR/logs"
DATE_TAG=$(date +%Y-%m-%d)

mkdir -p "$DATA_DIR" "$REPORT_DIR" "$LOG_DIR"

LOG_FILE="$LOG_DIR/run_${DATE_TAG}.log"

log() {
    echo "[$(date '+%H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "========================================"
log "  每日市场洞察 - 多Agent系统启动"
log "  日期: ${DATE_TAG}"
log "========================================"

# ============================================
# Step 1: 新闻采集
# ============================================
log "[Step 1/4] 开始新闻采集..."

# 搜索关键词
QUERIES=(
    "宏观经济 政策 利率 通胀 中国 今日"
    "科技行业 AI芯片 新能源汽车 最新动态"
    "A股 港股 美股 大盘行情 今日"
    "美联储 央行 财政政策 经济数据"
    "地缘政治 国际贸易 原油 黄金"
)

# 新闻内容收集（这个步骤需要 OpenClaw 的 web_search）
# 这里生成任务文件供 OpenClaw 读取
TASK_FILE="$DATA_DIR/search_tasks_${DATE_TAG}.json"
echo '{"date": "'$DATE_TAG'", "tasks": [' > "$TASK_FILE"
first=true
for q in "${QUERIES[@]}"; do
    if [ "$first" = true ]; then
        first=false
    else
        echo "," >> "$TASK_FILE"
    fi
    echo "{\"query\": \"$q\"}" >> "$TASK_FILE"
done
echo ']}' >> "$TASK_FILE"

log "搜索任务已生成: $TASK_FILE"
log "[Step 1/4] 完成 ✓"

# ============================================
# Step 2 & 3: 新闻分析和市场预测
# 这些步骤由 OpenClaw 调用 opencode sisyphus 执行
# ============================================

log "[Step 2/4] 等待 OpenClaw 执行新闻分析..."
log "[Step 3/4] 等待 OpenClaw 执行市场预测..."

log "========================================"
log "  预处理完成，等待 Agent 调用"
log "========================================"
