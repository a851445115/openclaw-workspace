#!/bin/bash
# 新闻采集脚本 - 每日市场洞察系统
# 使用 Brave Search API 搜索各领域最新新闻

set -e

PROJECT_DIR="$HOME/.openclaw/workspace/projects/daily-market-insight"
DATA_DIR="$PROJECT_DIR/data"
LOG_DIR="$PROJECT_DIR/logs"
DATE_TAG=$(date +%Y-%m-%d)
OUTPUT_FILE="$DATA_DIR/news_${DATE_TAG}.json"

mkdir -p "$DATA_DIR" "$LOG_DIR"

echo "[$(date)] 开始新闻采集..." | tee -a "$LOG_DIR/collector.log"

# 搜索关键词配置（宏观经济 + 科技 + 其他领域）
declare -a SEARCH_QUERIES=(
    "宏观经济 政策 利率 通胀 今日新闻"
    "科技行业 AI 芯片 新能源 最新动态"
    "A股 港股 美股 市场行情 今日"
    "央行 财政政策 经济数据 最新"
    "地缘政治 国际贸易 能源 今日"
)

# 这个脚本会被 OpenClaw 的 web_search 工具调用
# 这里只负责准备搜索任务列表
echo '{"date": "'$DATE_TAG'", "queries": [' > "$OUTPUT_FILE"

first=true
for query in "${SEARCH_QUERIES[@]}"; do
    if [ "$first" = true ]; then
        first=false
    else
        echo "," >> "$OUTPUT_FILE"
    fi
    echo "\"$query\"" >> "$OUTPUT_FILE"
done

echo ']}' >> "$OUTPUT_FILE"

echo "[$(date)] 搜索任务列表已生成: $OUTPUT_FILE" | tee -a "$LOG_DIR/collector.log"
echo "$OUTPUT_FILE"
