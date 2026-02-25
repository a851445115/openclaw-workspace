#!/bin/bash
# 新闻分析 Agent - 使用 opencode sisyphus
# 输入: 原始新闻文本
# 输出: 结构化分析结果

NEWS_CONTENT="$1"
OUTPUT_FILE="$2"

if [ -z "$NEWS_CONTENT" ]; then
    echo "Usage: $0 <news_content> [output_file]"
    exit 1
fi

PROMPT='你是一个专业的财经新闻分析师。请分析以下新闻内容，并输出结构化的分析结果。

## 分析要求：
1. 提取关键事件和核心信息
2. 按领域分类（宏观经济/科技/其他）
3. 评估新闻的重要程度（高/中/低）
4. 分析情感倾向（正面/负面/中性）
5. 识别可能受影响的行业和公司

## 新闻内容：
'"$NEWS_CONTENT"'

## 请以以下JSON格式输出：
```json
{
  "summary": "新闻摘要",
  "category": "宏观经济|科技|其他",
  "importance": "高|中|低",
  "sentiment": "正面|负面|中性",
  "affected_sectors": ["行业1", "行业2"],
  "key_points": ["要点1", "要点2", "要点3"]
}
```'

if [ -n "$OUTPUT_FILE" ]; then
    opencode run --agent sisyphus "$PROMPT" > "$OUTPUT_FILE" 2>&1
else
    opencode run --agent sisyphus "$PROMPT"
fi
