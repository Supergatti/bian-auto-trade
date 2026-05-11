import json
import time
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request, Response, stream_with_context

from config import (
    TRADE_PAIRS_FILE, TRADE_HISTORY_FILE,
    KLINE_INTERVALS, KLINE_LIMITS, MAX_WEB_SEARCH_ROUNDS,
    DEEPSEEK_API_KEY, logger,
)
from routes.favorites import load_trade_pairs
from utils.files import load_json, save_json
from services.binance import (
    collect_all_pairs_data, get_account, get_balances,
    get_symbol_filters, get_current_price, execute_order,
    _public_request,
)
from services.deepseek import ask_flash, ask_pro
from services.web_search import search_web

trade_bp = Blueprint("trade", __name__)


def load_trade_history():
    return load_json(TRADE_HISTORY_FILE, list)


def save_trade_history(records):
    save_json(TRADE_HISTORY_FILE, records)


def _calculate_pnl(records, specific_symbol=None):
    trades = records
    if specific_symbol:
        trades = [r for r in records if r["symbol"] == specific_symbol]
    pnl_by_symbol = {}
    total_pnl = 0
    for symbol in set(r["symbol"] for r in trades):
        symbol_trades = [r for r in trades if r["symbol"] == symbol]
        buys = []
        i = 0
        while i < len(symbol_trades):
            t = symbol_trades[i]
            if t["side"] == "BUY":
                buys.append(t)
            elif t["side"] == "SELL" and buys:
                buy = buys.pop(0)
                buy_qty = float(buy["quantity"])
                sell_qty = float(t["quantity"])
                buy_price = float(buy["price"])
                sell_price = float(t["price"])
                ratio = min(sell_qty / buy_qty, 1.0)
                pnl = (sell_price - buy_price) * buy_qty * ratio - float(t.get("commission", 0))
                total_pnl += pnl
            i += 1
        unrealized = 0
        for b in buys:
            try:
                cp = get_current_price(symbol)
                unrealized += (cp - float(b["price"])) * float(b["quantity"])
            except Exception:
                pass
        pnl_by_symbol[symbol] = {
            "realizedPnl": round(total_pnl, 4),
            "unrealizedPnl": round(unrealized, 4),
            "totalPnl": round(total_pnl + unrealized, 4),
        }
    return {"bySymbol": pnl_by_symbol, "totalRealizedPnl": round(total_pnl, 4)}


def _make_market_summary(market_data, balances):
    pairs = list(market_data.keys())
    summary = f"当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC+8\n\n## 账户余额\n"
    for b in balances:
        summary += f"- {b['asset']}: 可用 {b['free']} 冻结 {b['locked']} 总计 {b['total']} (约 {b['cnyValue']} CNY)\n"
    summary += "\n## 各交易对市场数据\n"
    for pair in pairs:
        md = market_data[pair]
        summary += f"\n### {pair}\n"
        tk = md.get("ticker", {})
        if "error" not in tk:
            summary += f"- 最新价: {tk['lastPrice']}  24h涨跌: {tk['priceChangePercent']}%  24h最高: {tk['high']}  24h最低: {tk['low']}  24h成交量: {tk['volume']}\n"
        for interval in KLINE_INTERVALS:
            kls = md.get("klines", {}).get(interval, [])
            if isinstance(kls, list) and kls:
                closes = [k["close"] for k in kls]
                summary += f"- {interval}K线({len(kls)}根): 开 {kls[0]['open']} 高 {max(closes)} 低 {min(closes)} 收 {kls[-1]['close']}\n"
        ob = md.get("orderBook", {})
        if "error" not in ob:
            bids = ob.get("bids", [])[:3]
            asks = ob.get("asks", [])[:3]
            summary += f"- 买盘前3: {[(b[0], b[1]) for b in bids]}  卖盘前3: {[(a[0], a[1]) for a in asks]}\n"
        trades = md.get("recentTrades", [])
        if isinstance(trades, list) and trades:
            buy_vol = sum(t["qty"] for t in trades if not t["isBuyerMaker"])
            sell_vol = sum(t["qty"] for t in trades if t["isBuyerMaker"])
            summary += f"- 最近20笔成交: 主动买 {buy_vol:.4f} 主动卖 {sell_vol:.4f}\n"
    return summary


def _run_deepseek_analysis(market_data, balances):
    market_summary = _make_market_summary(market_data, balances)
    pairs = list(market_data.keys())
    pairs_str = ", ".join(pairs)

    search_context = ""
    for round_num in range(MAX_WEB_SEARCH_ROUNDS):
        q_prompt = f"""你是一个加密货币交易分析师。以下是当前市场数据：

{market_summary}

现在你需要做交易决策。但在决策之前，你可以联网搜索以下方面的最新信息来辅助判断：
- 这些币种的最新新闻（政策、事件、大机构动向）
- 整体加密市场情绪和趋势
- 任何可能影响短期价格的重要因素

交易对: {pairs_str}

请列出你想要搜索的内容（最多3个关键词或短语），用JSON格式返回，如果不需要搜索就直接说"不需要"。
格式: {{"searches": ["关键词1", "关键词2", "关键词3"]}} 或 {{"searches": []}}"""

        try:
            flash_resp = ask_flash([
                {"role": "system", "content": "你是加密货币研究助手。返回纯JSON格式回答。"},
                {"role": "user", "content": q_prompt},
            ], max_tokens=512)
        except Exception as e:
            logger.warning("Flash round %d failed: %s", round_num + 1, str(e))
            break

        try:
            flash_resp = flash_resp.strip()
            if flash_resp.startswith("```"):
                flash_resp = flash_resp.split("\n", 1)[-1].rstrip("```").strip()
            req = json.loads(flash_resp)
            searches = req.get("searches", [])
        except json.JSONDecodeError:
            if "不需要" in flash_resp or "no need" in flash_resp.lower():
                break
            searches = []

        if not searches:
            break

        logger.info("🔍 第 %d 轮联网搜索: %s", round_num + 1, searches)
        round_results = []
        for query in searches:
            round_results.extend(search_web(query, num_results=3))

        if round_results:
            results_text = "\n".join(
                f"[{r['title']}] {r['snippet']}" for r in round_results[:10]
            )
            summarize_prompt = f"""请用简洁中文总结以下搜索结果的要点（150字以内），聚焦于对加密市场的影响：

{results_text}"""

            try:
                summary = ask_flash([
                    {"role": "system", "content": "你是加密货币信息摘要助手。回答简洁，不超过150字。"},
                    {"role": "user", "content": summarize_prompt},
                ], max_tokens=512)
                search_context += f"\n## 第{round_num + 1}轮搜索结果\n{summary}\n"
                logger.info("📝 Flash 总结搜索结果: %s", summary[:80])
            except Exception as e:
                logger.warning("Flash summary failed: %s", str(e))

    decision_prompt = f"""你是一个专业的加密货币量化交易分析师。请根据以下数据给出交易建议。

{market_summary}

{f"## 联网搜索情报{search_context}" if search_context else ""}

请对每个交易对给出以下详细分析，返回纯 JSON 格式（不要 markdown 代码块标记）：
{{
  "overall_analysis": "整体市场分析，200字以内",
  "research_summary": "基于联网搜索的宏观分析摘要",
  "decisions": [
    {{
      "symbol": "BTCUSDT",
      "trend": "上升",
      "action": "BUY",
      "quantity": 0.001,
      "reason": "买入理由，一句话",
      "detail": "详细技术分析，包括支撑位阻力位、量价关系、指标信号等，100字内",
      "estimatedUsdt": 85.50,
      "stopLossPrice": 82000,
      "risk": "中"
    }}
  ]
}}

注意事项：
- HOLD 时 quantity 为 0，reason 写观望理由
- estimatedUsdt = 建议数量 × 最新价格
- stopLossPrice 仅在 BUY/SELL 时给出建议止损价
- 确保总买入金额不超过账户可用 USDT 余额"""

    try:
        content = ask_pro([
            {"role": "system", "content": "你是一个加密货币交易分析师。始终返回纯 JSON，不要使用 markdown 代码块。"},
            {"role": "user", "content": decision_prompt},
        ])
    except Exception as e:
        logger.error("DeepSeek Pro 调用失败: %s", str(e))
        raise

    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
    return json.loads(content)


def _execute_one_trade(symbol, action, quantity):
    filters, sinfo = get_symbol_filters(symbol)
    if not filters:
        return {"symbol": symbol, "action": action, "status": "failed", "error": f"找不到 {symbol} 交易规则"}

    step_size = float(filters.get("LOT_SIZE", {}).get("stepSize", "0.00000001"))
    min_qty = float(filters.get("LOT_SIZE", {}).get("minQty", "0"))
    min_notional = float(filters.get("MIN_NOTIONAL", {}).get("minNotional", "10"))

    qty = float(quantity)
    prec = 0
    if "." in str(step_size):
        prec = len(str(step_size).split(".")[1].rstrip("0"))
    qty = round(max(qty, min_qty), prec)

    try:
        current_price = get_current_price(symbol)
    except Exception:
        current_price = 0

    if qty * current_price < min_notional:
        return {"symbol": symbol, "action": action, "status": "failed",
                "error": f"金额 {qty * current_price:.2f} USDT 低于最小 {min_notional} USDT"}

    if action == "SELL":
        base_asset = sinfo.get("baseAsset", symbol.replace("USDT", ""))
        bal_data = get_account()
        for b in bal_data.get("balances", []):
            if b["asset"] == base_asset:
                free = float(b["free"])
                if free < qty:
                    qty = round(free, prec)
                break

    if qty <= 0:
        return {"symbol": symbol, "action": action, "status": "skipped", "error": "余额不足"}

    side = "BUY" if action == "BUY" else "SELL"

    try:
        result = execute_order(symbol, side, qty)
    except Exception as e:
        return {"symbol": symbol, "action": action, "status": "failed", "error": str(e)}

    fills = result.get("fills", [])
    exec_price = 0; exec_qty = 0; commission = 0
    if fills:
        total_quote = sum(float(f["price"]) * float(f["qty"]) for f in fills)
        exec_qty = sum(float(f["qty"]) for f in fills)
        exec_price = total_quote / exec_qty if exec_qty > 0 else 0
        commission = sum(float(f.get("commission", 0)) for f in fills)

    trade_record = {
        "id": str(uuid.uuid4())[:8],
        "symbol": symbol, "side": side,
        "quantity": exec_qty or qty,
        "price": exec_price or current_price,
        "commission": commission,
        "totalUsdt": (exec_qty or qty) * (exec_price or current_price),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "orderId": result.get("orderId", ""),
    }

    logger.info("✅ 交易: %s %s %s @ %.4f USDT", trade_record["id"], side, trade_record["quantity"], trade_record["price"])

    records = load_trade_history()
    records.append(trade_record)
    save_trade_history(records)

    return {"symbol": symbol, "action": action, "status": "success", "trade": trade_record}


@trade_bp.route("/api/trade/pairs")
def trade_pairs():
    return jsonify(load_trade_pairs())


@trade_bp.route("/api/trade/market-data")
def market_data():
    pairs = load_trade_pairs()
    return jsonify(collect_all_pairs_data(pairs))


def _sse(data):
    return (json.dumps(data, ensure_ascii=False, default=str) + "\n").encode()


def _run_deepseek_analysis_stream(market_data, balances):
    market_summary = _make_market_summary(market_data, balances)
    pairs = list(market_data.keys())
    pairs_str = ", ".join(pairs)

    yield _sse({"event": "balance", "data": [{"asset": b["asset"], "total": b["total"], "cnyValue": b["cnyValue"]} for b in balances]})

    search_context = ""
    for round_num in range(MAX_WEB_SEARCH_ROUNDS):
        yield _sse({"event": "search_round", "round": round_num + 1, "status": "asking"})

        q_prompt = f"""你是一个加密货币交易分析师。以下是当前市场数据：

{market_summary}

现在你需要做交易决策。但在决策之前，你可以联网搜索以下方面的最新信息来辅助判断：
- 这些币种的最新新闻（政策、事件、大机构动向）
- 整体加密市场情绪和趋势
- 任何可能影响短期价格的重要因素

交易对: {pairs_str}

请列出你想要搜索的内容（最多3个关键词或短语），用JSON格式返回，如果不需要搜索就直接说"不需要"。
格式: {{"searches": ["关键词1", "关键词2", "关键词3"]}} 或 {{"searches": []}}"""

        try:
            flash_resp = ask_flash([
                {"role": "system", "content": "你是加密货币研究助手。返回纯JSON格式回答。"},
                {"role": "user", "content": q_prompt},
            ], max_tokens=512)
        except Exception as e:
            logger.warning("Flash round %d failed: %s", round_num + 1, str(e))
            yield _sse({"event": "search_error", "round": round_num + 1, "error": str(e)[:100]})
            break

        try:
            flash_resp = flash_resp.strip()
            if flash_resp.startswith("```"):
                flash_resp = flash_resp.split("\n", 1)[-1].rstrip("```").strip()
            req = json.loads(flash_resp)
            searches = req.get("searches", [])
        except json.JSONDecodeError:
            if "不需要" in flash_resp or "no need" in flash_resp.lower():
                yield _sse({"event": "search_round", "round": round_num + 1, "status": "skip", "msg": "DeepSeek 判断无需额外搜索"})
                break
            searches = []

        if not searches:
            yield _sse({"event": "search_round", "round": round_num + 1, "status": "skip", "msg": "无搜索关键词"})
            break

        yield _sse({"event": "search_query", "round": round_num + 1, "queries": searches})

        round_results = []
        for query in searches:
            yield _sse({"event": "searching", "round": round_num + 1, "query": query})
            results = search_web(query, num_results=3)
            if results:
                yield _sse({"event": "search_found", "round": round_num + 1, "query": query, "count": len(results),
                            "preview": results[0]["snippet"][:100] if results else ""})
            round_results.extend(results)

        if round_results:
            results_text = "\n".join(f"[{r['title']}] {r['snippet']}" for r in round_results[:10])
            yield _sse({"event": "summarizing", "round": round_num + 1, "count": len(round_results)})

            try:
                summary = ask_flash([
                    {"role": "system", "content": "你是加密货币信息摘要助手。回答简洁，不超过150字。"},
                    {"role": "user", "content": f"请用简洁中文总结以下搜索结果要点（150字内），聚焦加密市场影响：\n\n{results_text}"},
                ], max_tokens=512)
                search_context += f"\n## 第{round_num + 1}轮搜索结果\n{summary}\n"
                yield _sse({"event": "search_summary", "round": round_num + 1, "summary": summary})
            except Exception as e:
                logger.warning("Flash summary failed: %s", str(e))
                yield _sse({"event": "search_error", "round": round_num + 1, "error": str(e)[:100]})
        else:
            yield _sse({"event": "search_round", "round": round_num + 1, "status": "no_results", "msg": f"搜索 '{' '.join(searches)}' 无结果"})

    yield _sse({"event": "pro_start", "msg": "发送完整数据到 DeepSeek-V4-Pro 进行最终决策..."})

    decision_prompt = f"""你是一个专业的加密货币量化交易分析师。请根据以下数据给出交易建议。

{market_summary}

{f"## 联网搜索情报{search_context}" if search_context else ""}

请对每个交易对给出以下详细分析，返回纯 JSON 格式（不要 markdown 代码块标记）：
{{
  "overall_analysis": "整体市场分析，200字以内",
  "research_summary": "基于联网搜索的宏观分析摘要",
  "decisions": [
    {{
      "symbol": "BTCUSDT",
      "trend": "上升",
      "action": "BUY",
      "quantity": 0.001,
      "reason": "买入理由，一句话",
      "detail": "详细技术分析，包括支撑位阻力位、量价关系、指标信号等，100字内",
      "estimatedUsdt": 85.50,
      "stopLossPrice": 82000,
      "risk": "中"
    }}
  ]
}}

注意事项：
- HOLD 时 quantity 为 0，reason 写观望理由
- estimatedUsdt = 建议数量 × 最新价格
- stopLossPrice 仅在 BUY/SELL 时给出建议止损价
- 确保总买入金额不超过账户可用 USDT 余额"""

    try:
        content = ask_pro([
            {"role": "system", "content": "你是一个加密货币交易分析师。始终返回纯 JSON，不要使用 markdown 代码块。"},
            {"role": "user", "content": decision_prompt},
        ])
        yield _sse({"event": "pro_done", "length": len(content)})
    except Exception as e:
        logger.error("DeepSeek Pro 调用失败: %s", str(e))
        yield _sse({"event": "error", "error": f"DeepSeek Pro 决策失败: {str(e)}"})
        return

    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

    try:
        analysis = json.loads(content)
    except json.JSONDecodeError as e:
        yield _sse({"event": "error", "error": f"JSON 解析失败: {str(e)}"})
        return

    decisions = analysis.get("decisions", [])
    for d in decisions:
        sym = d.get("symbol", "")
        if sym in market_data and "ticker" in market_data[sym]:
            tk = market_data[sym]["ticker"]
            if "error" not in tk:
                if "estimatedUsdt" not in d:
                    d["estimatedUsdt"] = round(float(d.get("quantity", 0)) * tk["lastPrice"], 2)
                    d["lastPrice"] = tk["lastPrice"]
        d.setdefault("detail", d.get("reason", ""))
        d.setdefault("stopLossPrice", None)

    yield _sse({"event": "decisions", "decisions": decisions, "research_summary": analysis.get("research_summary", ""), "overall_analysis": analysis.get("overall_analysis", "")})

    yield _sse({"event": "done", "marketData": market_data, "analysis": analysis})


@trade_bp.route("/api/trade/analyze-stream", methods=["GET"])
def trade_analyze_stream():
    if not DEEPSEEK_API_KEY:
        return jsonify({"error": "请设置 DEEPSEEK_API_KEY"}), 400

    def generate():
        logger.info("=" * 50)
        logger.info("🚀 开始自动交易分析 (SSE流式)")
        pairs = load_trade_pairs()
        logger.info("交易对: %s", pairs)

        t_start = time.time()

        yield _sse({"event": "log", "msg": f"📡 开始采集 {len(pairs)} 个交易对数据..."})
        yield _sse({"event": "log", "msg": f"   交易对: {', '.join(pairs)}"})

        from concurrent.futures import ThreadPoolExecutor, as_completed
        from services.binance import collect_pair_data
        result = {}
        submitted = set()
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(collect_pair_data, p): p for p in pairs}
            submitted = set(pairs)
            for future in as_completed(futures):
                pair = futures[future]
                try:
                    data = future.result()
                    result[pair] = data
                    tk = data.get("ticker", {})
                    price_str = f" @ {tk['lastPrice']}" if isinstance(tk, dict) and "lastPrice" in tk else ""
                    yield _sse({"event": "collect_pair", "pair": pair, "price": tk.get("lastPrice") if isinstance(tk, dict) else None,
                                "change": tk.get("priceChangePercent") if isinstance(tk, dict) else None})
                except Exception as e:
                    yield _sse({"event": "log", "msg": f"   ⚠ {pair} 采集失败: {str(e)[:80]}"})

        market_data = {}
        for p in pairs:
            if p in result:
                market_data[p] = result[p]

        collect_time = time.time() - t_start
        yield _sse({"event": "log", "msg": f"✅ 数据采集完成 (耗时 {collect_time:.1f}s)"})

        bal_result = get_balances()
        bal_list = bal_result["balances"]
        yield _sse({"event": "log", "msg": f"💰 账户余额: {', '.join(f'{b['asset']}:{b['total']}' for b in bal_list)}"})

        yield _sse({"event": "log", "msg": "🌐 开始联网搜索 + DeepSeek 分析..."})

        for line in _run_deepseek_analysis_stream(market_data, bal_list):
            yield line

        total_elapsed = time.time() - t_start
        logger.info("⏱ 分析总耗时 %.1fs", total_elapsed)
        logger.info("=" * 50)
        yield _sse({"event": "log", "msg": f"⏱ 分析总耗时 {total_elapsed:.1f}s"})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@trade_bp.route("/api/trade/analyze", methods=["POST"])
def trade_analyze():
    if not DEEPSEEK_API_KEY:
        return jsonify({"error": "请设置 DEEPSEEK_API_KEY"}), 400

    logger.info("=" * 50)
    logger.info("🚀 开始自动交易分析 (Flash搜索 + Pro决策)")
    pairs = load_trade_pairs()
    logger.info("交易对: %s", pairs)

    t_start = time.time()
    logger.info("📡 采集 %d 个交易对市场数据...", len(pairs))
    market_data = collect_all_pairs_data(pairs)
    collect_time = time.time() - t_start
    logger.info("📡 数据采集完成，耗时 %.1fs", collect_time)

    bal_result = get_balances()
    bal_list = bal_result["balances"]
    logger.info("💰 账户余额: %s", [f"{b['asset']}:{b['total']}" for b in bal_list])

    try:
        logger.info("🌐 开始联网搜索 + DeepSeek 分析...")
        analysis = _run_deepseek_analysis(market_data, bal_list)
    except Exception as e:
        logger.error("DeepSeek 分析失败: %s", str(e))
        return jsonify({"error": f"DeepSeek 分析失败: {str(e)}"}), 500

    decisions = analysis.get("decisions", [])
    for d in decisions:
        sym = d.get("symbol", "")
        if sym in market_data and "ticker" in market_data[sym]:
            tk = market_data[sym]["ticker"]
            if "error" not in tk and "estimatedUsdt" not in d:
                d["estimatedUsdt"] = round(float(d.get("quantity", 0)) * tk["lastPrice"], 2)
                d["lastPrice"] = tk["lastPrice"]
        d.setdefault("detail", d.get("reason", ""))
        d.setdefault("stopLossPrice", None)

    logger.info("📊 分析结果: %d 个决策", len(decisions))
    for d in decisions:
        logger.info("  %s → %s (趋势:%s 风险:%s) qty=%.6f est=%.2f USDT | %s",
                   d.get("symbol"), d.get("action"), d.get("trend"),
                   d.get("risk"), d.get("quantity", 0), d.get("estimatedUsdt", 0), d.get("reason", ""))

    total_elapsed = time.time() - t_start
    logger.info("⏱ 分析总耗时 %.1fs", total_elapsed)
    logger.info("=" * 50)

    return jsonify({"marketData": market_data, "analysis": analysis})


@trade_bp.route("/api/trade/execute", methods=["POST"])
def trade_execute():
    data = request.get_json(silent=True) or {}
    symbol = data.get("symbol", "").upper().strip()
    action = data.get("action", "").upper().strip()
    quantity = data.get("quantity", 0)

    logger.info("💱 执行交易: %s %s 数量=%s", symbol, action, quantity)
    if not symbol or action not in ("BUY", "SELL"):
        return jsonify({"error": "需要 symbol 和 action (BUY/SELL)"}), 400

    result = _execute_one_trade(symbol, action, quantity)
    if result["status"] == "success":
        pnl = _calculate_pnl(load_trade_history(), symbol)
        return jsonify({"trade": result["trade"], "pnl": pnl})
    return jsonify(result), 400 if result["status"] == "failed" else 200


@trade_bp.route("/api/trade/execute-all", methods=["POST"])
def trade_execute_all():
    data = request.get_json(silent=True) or {}
    decisions = data.get("decisions", [])
    if not decisions:
        return jsonify({"error": "请提供 decisions 列表"}), 400

    logger.info("=" * 50)
    logger.info("💱 批量执行交易 %d 笔", len(decisions))

    results = []; success = 0; failed = 0; skipped = 0
    for i, d in enumerate(decisions):
        symbol = d.get("symbol", "").upper().strip()
        action = d.get("action", "").upper().strip()
        qty = d.get("quantity", 0)
        if not symbol or action not in ("BUY", "SELL") or float(qty) <= 0:
            results.append({"symbol": symbol, "action": action, "status": "skipped", "error": "HOLD 不执行"})
            skipped += 1; continue
        logger.info("  [%d/%d] %s %s %.6f", i + 1, len(decisions), symbol, action, float(qty))
        r = _execute_one_trade(symbol, action, float(qty))
        results.append(r)
        if r["status"] == "success": success += 1
        elif r["status"] == "skipped": skipped += 1
        else: failed += 1

    pnl = _calculate_pnl(load_trade_history())
    logger.info("📊 批量交易完成: 成功%d 失败%d 跳过%d", success, failed, skipped)
    logger.info("=" * 50)
    return jsonify({"results": results, "summary": {"total": len(decisions), "success": success, "failed": failed, "skipped": skipped}, "pnl": pnl})


@trade_bp.route("/api/trade/history")
def trade_history():
    return jsonify({"trades": load_trade_history(), "pnl": _calculate_pnl(load_trade_history())})


@trade_bp.route("/api/trade/positions")
def positions():
    records = load_trade_history()
    held = {}
    for r in sorted(records, key=lambda x: x["time"]):
        symbol = r["symbol"]
        if symbol not in held:
            held[symbol] = {"buys": [], "quantity": 0}
        if r["side"] == "BUY":
            held[symbol]["buys"].append(r)
            held[symbol]["quantity"] += float(r["quantity"])
        elif r["side"] == "SELL":
            qty = float(r["quantity"]); remaining = qty
            while remaining > 0 and held[symbol]["buys"]:
                b = held[symbol]["buys"][0]; bq = float(b["quantity"])
                if bq <= remaining:
                    remaining -= bq; held[symbol]["buys"].pop(0)
                else:
                    b["quantity"] = bq - remaining; remaining = 0
            held[symbol]["quantity"] -= qty
    result = []
    for symbol, h in held.items():
        if h["quantity"] > 1e-10:
            try: cp = get_current_price(symbol)
            except Exception: cp = 0
            avg_cost = sum(float(b["price"]) * float(b["quantity"]) for b in h["buys"]) / h["quantity"] if h["buys"] and h["quantity"] > 0 else 0
            result.append({
                "symbol": symbol, "quantity": round(h["quantity"], 8),
                "avgPrice": round(avg_cost, 4), "currentPrice": cp,
                "unrealizedPnl": round((cp - avg_cost) * h["quantity"], 4) if avg_cost > 0 else 0,
            })
    return jsonify(result)
