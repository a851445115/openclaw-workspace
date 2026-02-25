#!/usr/bin/env python3
"""
每日市场洞察 - 多Agent系统
完整实现版本
"""

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path.home() / ".openclaw/workspace/projects/daily-market-insight"
DATA_DIR = PROJECT_DIR / "data"
REPORT_DIR = PROJECT_DIR / "reports"
LOG_DIR = PROJECT_DIR / "logs"

def ensure_dirs():
    """确保目录存在"""
    for d in [DATA_DIR, REPORT_DIR, LOG_DIR]:
        d.mkdir(parents=True, exist_ok=True)

def log(message: str):
    """记录日志"""
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_line = f"[{timestamp}] {message}"
    print(log_line)
    
    log_file = LOG_DIR / f"run_{datetime.now().strftime('%Y-%m-%d')}.log"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(log_line + "\n")

def search_news() -> list:
    """
    新闻采集 - 返回搜索关键词列表
    实际搜索由 OpenClaw 的 web_search 执行
    """
    queries = [
        "宏观经济 政策 利率 通胀 中国 今日新闻",
        "科技行业 AI芯片 新能源汽车 最新动态",
        "A股 港股 美股 大盘行情 今日",
        "美联储 央行 财政政策 经济数据 最新",
        "地缘政治 国际贸易 原油 黄金 今日",
    ]
    
    date_tag = datetime.now().strftime("%Y-%m-%d")
    task_file = DATA_DIR / f"search_tasks_{date_tag}.json"
    
    tasks = {"date": date_tag, "tasks": [{"query": q} for q in queries]}
    with open(task_file, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)
    
    log(f"搜索任务已生成: {task_file}")
    return queries

def analyze_news(news_content: str) -> str:
    """
    新闻分析 - 调用 opencode sisyphus
    """
    prompt = f"""你是一个专业的财经新闻分析师。请分析以下新闻内容，并输出结构化的分析结果。

## 分析要求：
1. 提取关键事件和核心信息
2. 按领域分类（宏观经济/科技/其他）
3. 评估新闻的重要程度（高/中/低）
4. 分析情感倾向（正面/负面/中性）
5. 识别可能受影响的行业

## 新闻内容：
{news_content}

## 请以Markdown格式输出分析结果，包含：
- 新闻摘要
- 分类和重要程度
- 情感分析
- 受影响行业
- 关键要点列表"""

    log("调用 opencode sisyphus 进行新闻分析...")
    
    result = subprocess.run(
        ["opencode", "run", "--agent", "sisyphus", prompt],
        capture_output=True,
        text=True,
        timeout=300
    )
    
    if result.returncode != 0:
        log(f"分析出错: {result.stderr}")
        return f"分析失败: {result.stderr}"
    
    return result.stdout

def predict_market(analysis_content: str) -> str:
    """
    市场趋势预测 - 调用 opencode sisyphus
    """
    prompt = f"""你是一个专业的投资市场分析师。请基于以下新闻分析结果，对A股、港股、美股的投资趋势进行预测。

## 新闻分析结果：
{analysis_content}

## 请预测以下市场：
1. **A股市场**：趋势判断、受影响板块、机会与风险
2. **港股市场**：趋势判断、受影响板块、机会与风险
3. **美股市场**：趋势判断、受影响板块、机会与风险

## 输出格式（Markdown）：
- 整体市场情绪
- 各市场趋势分析
- 投资建议
- 风险提示"""

    log("调用 opencode sisyphus 进行市场预测...")
    
    result = subprocess.run(
        ["opencode", "run", "--agent", "sisyphus", prompt],
        capture_output=True,
        text=True,
        timeout=300
    )
    
    if result.returncode != 0:
        log(f"预测出错: {result.stderr}")
        return f"预测失败: {result.stderr}"
    
    return result.stdout

def generate_report(news_analysis: str, market_prediction: str) -> str:
    """
    生成最终报告
    """
    date_tag = datetime.now().strftime("%Y-%m-%d")
    time_tag = datetime.now().strftime("%H:%M:%S")
    
    report = f"""# 📊 每日市场洞察报告
## {date_tag}

---

## 📰 今日重点新闻分析

{news_analysis}

---

## 📈 市场趋势预测

{market_prediction}

---

## ⚠️ 免责声明

本报告由AI自动生成，仅供参考，不构成投资建议。投资有风险，入市需谨慎。

---
*报告生成时间: {date_tag} {time_tag}*
*Powered by 钢镚儿多Agent系统* 🐱
"""
    
    report_file = REPORT_DIR / f"daily_insight_{date_tag}.md"
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(report)
    
    log(f"报告已保存: {report_file}")
    return report

def main():
    """主入口"""
    ensure_dirs()
    
    log("=" * 40)
    log("  每日市场洞察 - 多Agent系统启动")
    log(f"  日期: {datetime.now().strftime('%Y-%m-%d')}")
    log("=" * 40)
    
    # Step 1: 新闻采集（返回关键词，实际搜索由外部执行）
    log("[Step 1/4] 新闻采集...")
    queries = search_news()
    
    log("=" * 40)
    log("  预处理完成")
    log("  需要执行的搜索关键词:")
    for q in queries:
        log(f"  - {q}")
    log("=" * 40)
    
    return queries

if __name__ == "__main__":
    main()
