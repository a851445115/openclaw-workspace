#!/bin/bash
# 市场趋势预测 Agent - 使用 opencode sisyphus
# 输入: 新闻分析结果
# 输出: 投资市场趋势预测

ANALYSIS_CONTENT="$1"
OUTPUT_FILE="$2"

if [ -z "$ANALYSIS_CONTENT" ]; then
    echo "Usage: $0 <analysis_content> [output_file]"
    exit 1
fi

PROMPT='你是一个专业的投资市场分析师，擅长从时事新闻中推断市场趋势。

## 分析要求：
请基于以下新闻分析结果，对A股、港股、美股的投资趋势进行预测：

### 需要分析的市场：
1. **A股市场**：受影响的主要板块、个股推荐（如有）、风险提示
2. **港股市场**：受影响的主要板块、个股推荐（如有）、风险提示  
3. **美股市场**：受影响的主要板块、个股推荐（如有）、风险提示

### 输出格式：
```json
{
  "overall_sentiment": "看多|看空|中性",
  "confidence": "高|中|低",
  "markets": {
    "a_share": {
      "trend": "看多|看空|中性",
      "sectors": ["板块1", "板块2"],
      "opportunities": ["机会1"],
      "risks": ["风险1"]
    },
    "hong_kong": {
      "trend": "看多|看空|中性",
      "sectors": ["板块1"],
      "opportunities": ["机会1"],
      "risks": ["风险1"]
    },
    "us_market": {
      "trend": "看多|看空|中性",
      "sectors": ["板块1"],
      "opportunities": ["机会1"],
      "risks": ["风险1"]
    }
  },
  "investment_advice": "综合投资建议"
}
```

## 新闻分析结果：
'"$ANALYSIS_CONTENT"'

## 请输出你的市场趋势预测（仅输出JSON，不要其他内容）：'

if [ -n "$OUTPUT_FILE" ]; then
    opencode run --agent sisyphus "$PROMPT" > "$OUTPUT_FILE" 2>&1
else
    opencode run --agent sisyphus "$PROMPT"
fi
