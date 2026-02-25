#!/usr/bin/env python3
"""
每日市场洞察报告生成系统
钢镚儿 - 多Agent协作实现
"""

import os
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 项目根目录
PROJECT_DIR = Path(__file__).parent.parent
DATA_DIR = PROJECT_DIR / "data"
LOGS_DIR = PROJECT_DIR / "logs"

def log(message: str):
    """打印带时间戳的日志"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")
    sys.stdout.flush()

def run_opencode_agent(agent: str, prompt: str, timeout: int = 300) -> str:
    """
    运行 opencode agent
    """
    log(f"启动 {agent} agent...")
    
    # 创建临时目录存放结果
    result_file = DATA_DIR / f"temp_{agent}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    
    # 构建 prompt，要求输出到文件
    full_prompt = f"""{prompt}

请将你的分析结果直接输出，不要使用任何工具写入文件。我会自动捕获你的输出。"""
    
    try:
        result = subprocess.run(
            ["opencode", "run", "--agent", agent, full_prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(PROJECT_DIR)
        )
        
        output = result.stdout
        if result.stderr:
            log(f"{agent} stderr: {result.stderr[:500]}")
        
        log(f"{agent} 完成")
        return output
        
    except subprocess.TimeoutExpired:
        log(f"⚠️ {agent} 超时 ({timeout}s)")
        return f"Error: Agent {agent} timed out"
    except Exception as e:
        log(f"❌ {agent} 错误: {e}")
        return f"Error: {str(e)}"

def search_news() -> dict:
    """
    新闻采集 - 使用 Brave Search
    这个函数会在外部通过 web_search 工具调用
    """
    return {
        "macro": [],  # 宏观经济新闻
        "tech": [],   # 科技新闻
        "other": []   # 其他新闻
    }

def analyze_news(news_data: dict) -> str:
    """
    新闻分析 Agent - 使用 opencode sisyphus
    """
    prompt = f"""你是一位专业的财经分析师。请分析以下新闻，提取重点内容，并进行情感分析。

今日新闻数据（JSON格式）：
{json.dumps(news_data, ensure_ascii=False, indent=2)}

请从以下维度分析：
1. **宏观经济**：利率、通胀、GDP、政策等
2. **科技行业**：AI、芯片、新能源等
3. **其他重要事件**：地缘政治、大宗商品等

对每条新闻：
- 提取核心信息
- 判断市场情感（正面/中性/负面）
- 评估对市场的影响程度（高/中/低）

请以结构化格式输出分析结果。"""

    return run_opencode_agent("sisyphus", prompt, timeout=600)

def predict_trend(analysis: str) -> str:
    """
    市场趋势预测 Agent - 使用 opencode sisyphus
    """
    prompt = f"""你是一位资深的投资分析师，专注于A股、港股和美股市场。

基于以下新闻分析结果，请预测各市场的短期（1-2周）和中期（1-3个月）趋势：

{analysis}

请针对以下市场分别分析：
1. **A股市场**：预测走势、重点关注板块、风险提示
2. **港股市场**：预测走势、重点关注板块、风险提示  
3. **美股市场**：预测走势、重点关注板块、风险提示

输出格式：
- 市场整体判断（看涨/看跌/震荡）
- 关键驱动因素
- 建议关注的方向
- 风险因素
- 投资建议"""

    return run_opencode_agent("sisyphus", prompt, timeout=600)

def generate_report(date_str: str, news_analysis: str, market_trend: str) -> str:
    """
    生成完整的 Markdown 报告
    """
    report = f"""# 每日市场洞察

**日期**: {date_str}

---

## 📰 今日新闻分析

{news_analysis}

---

## 📈 市场趋势预测

{market_trend}

---

## 📊 数据来源

- Brave Search 新闻搜索
- 免费财经媒体渠道

---

*本报告由钢镚儿多Agent系统自动生成*
*生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}*
"""
    return report

def save_report(report: str, date_str: str) -> Path:
    """
    保存报告到本地
    """
    report_file = DATA_DIR / f"report_{date_str.replace('-', '')}.md"
    report_file.write_text(report, encoding='utf-8')
    log(f"报告已保存: {report_file}")
    return report_file

def main():
    """主流程"""
    log("=" * 50)
    log("🚀 每日市场洞察报告生成系统启动")
    log("=" * 50)
    
    # 确保目录存在
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    
    date_str = datetime.now().strftime("%Y-%m-%d")
    
    # Step 1: 新闻采集 (这里需要外部调用 web_search)
    log("📡 Step 1: 新闻采集...")
    log("⚠️ 新闻采集需要通过 web_search 工具完成")
    
    # Step 2: 新闻分析
    log("📊 Step 2: 新闻分析...")
    # news_analysis = analyze_news(news_data)
    
    # Step 3: 市场趋势预测
    log("🔮 Step 3: 市场趋势预测...")
    # market_trend = predict_trend(news_analysis)
    
    # Step 4: 生成报告
    log("📝 Step 4: 生成报告...")
    # report = generate_report(date_str, news_analysis, market_trend)
    
    # Step 5: 保存报告
    # save_report(report, date_str)
    
    log("✅ 完成！")
    
    return {
        "status": "ready",
        "message": "系统准备就绪，等待新闻数据输入"
    }

if __name__ == "__main__":
    result = main()
    print(json.dumps(result, ensure_ascii=False))
