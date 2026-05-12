SEARCH_SYSTEM = "You are a crypto research assistant. Always respond with pure JSON only. Generate search queries in English for best results."

SEARCH_TEMPLATE = """You are a cryptocurrency trading analyst. Here is the current market data:

{market_summary}

Before making trading decisions, you can search the web for the latest information:
- Latest news about these coins (policy, events, institutional moves)
- Overall crypto market sentiment and trends
- Any important factors that may affect short-term prices

Trading pairs: {pairs_str}

List what you want to search (up to 5 English keywords or phrases). Return pure JSON format. If no search is needed, return empty searches array.
Format: {{"searches": ["keyword1 in English", "keyword2 in English", "keyword3 in English"]}} or {{"searches": []}}"""

SUMMARIZE_SYSTEM = "You are a crypto information summarizer. Summarize concisely in Chinese, under 150 characters. Focus on market impact."

SUMMARIZE_TEMPLATE = "Summarize the following search results in concise Chinese (under 150 chars), focusing on crypto market impact:\n\n{results_text}"

DECISION_SYSTEM = "你是加密货币现货交易分析师。只做现货买卖，不碰杠杆。返回纯JSON。"

DECISION_TEMPLATE = """你是加密货币现货交易分析师。只做现货，不借币不做空。

{market_summary}

{search_context}

{open_positions}

{risk_rules}

{last_execution}

请对每个交易对给出分析，返回纯JSON：
{{
  "overall_analysis": "整体市场分析，400字以内",
  "research_summary": "基于搜索情报的宏观分析摘要",
  "decisions": [
    {{
      "symbol": "BTCUSDT",
      "trend": "上升/下降/震荡",
      "action": "BUY/SELL/HOLD",
      "quantity": 0.001,
      "reason": "理由，一句话",
      "detail": "技术分析，支撑阻力、量价关系、指标信号，400字内",
      "estimatedUsdt": 85.50,
      "stopLossPrice": 82000,
      "takeProfitPrice": 88000,
      "risk": "低/中/高",
      "mode": "spot"
    }}
  ]
}}

铁律：
- {risk_rules}
- 上升趋势→BUY。下降趋势→SELL卖出持仓。震荡→HOLD
- BUY/SELL必须给stopLossPrice和takeProfitPrice
- SELL只能卖已有持仓，不能借币做空
- 已有持仓优先管理（加仓/减仓/清仓）"""

# Flash analysis prompt — text only, no JSON. Pro handles all JSON output.
FLASH_DECISION_SYSTEM = "用中文总结以下行情数据，400-600字。"

FLASH_DECISION_TEMPLATE = """{market_summary}

{risk_rules}

{last_execution}

中文总结(400-600字)："""
