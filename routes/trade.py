import json
import time
import threading
import collections
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Blueprint, jsonify, request, Response, stream_with_context

from config import (
    KLINE_INTERVALS, KLINE_LIMITS, MAX_WEB_SEARCH_ROUNDS,
    DEEPSEEK_API_KEY, logger,
)
from services.data_store import (
    load_trade_pairs, load_trade_history, save_trade_history,
    append_trade_record, record_balance_snapshot,
)
from utils.files import load_json, save_json
from utils.helpers import strip_markdown_code, parse_flash_search_response, enrich_decisions
from services.binance import (
    collect_all_pairs_data, get_account, get_balances,
    get_symbol_filters, get_current_price, execute_order,
    collect_pair_data, _public_request,
    margin_account, margin_max_borrowable, margin_borrow, margin_repay, margin_order,
)
from services.deepseek import ask_flash, ask_pro
from services.web_search import search_web
from services.prompts import (
    SEARCH_SYSTEM, SEARCH_TEMPLATE,
    SUMMARIZE_SYSTEM, SUMMARIZE_TEMPLATE,
    DECISION_SYSTEM, DECISION_TEMPLATE,
    FLASH_DECISION_SYSTEM, FLASH_DECISION_TEMPLATE,
)

trade_bp = Blueprint("trade", __name__)

# Escalate to Pro when Flash suggests a trade above this USD threshold
ESCALATE_USDT_THRESHOLD = 100
# Keywords that trigger Pro escalation regardless of trade size
ESCALATE_KEYWORDS = ["crash", "暴跌", "崩盘", "hack", "黑客", "regulation", "监管",
                     "ban", "禁止", "delist", "下架", "sec", "lawsuit", "诉讼"]

# ===== Auto-Trading State =====
_auto_state = {
    "running": False,
    "round": 0,
    "last_time": None,
    "status": "idle",
    "interval": 600,
    "search_interval": 7200,
    "last_search_time": 0,
    "cached_search_context": "",
    "decisions": [],
    "last_pnl": None,
    "positions": {},
}
_auto_events = collections.deque(maxlen=500)
_auto_lock = threading.Lock()


def _auto_broadcast(event_type, **kwargs):
    evt = {"event": event_type}
    evt.update(kwargs)
    with _auto_lock:
        _auto_events.append(evt)


def _check_positions(market_data):
    """Check tracked positions for stop-loss, take-profit, and flash-crash triggers.
    Returns list of exit trades executed."""
    exits = []
    positions = _auto_state.get("positions", {})
    if not positions:
        return exits

    for symbol, pos in list(positions.items()):
        try:
            current_price = get_current_price(symbol)
        except Exception:
            continue

        entry = pos.get("entry_price", 0)
        qty = pos.get("quantity", 0)
        sl = pos.get("stop_loss", 0)
        tp = pos.get("take_profit", 0)

        if entry <= 0 or qty <= 0:
            continue

        pnl_pct = (current_price - entry) / entry * 100
        reason = None

        # 1. Stop-loss hit
        if sl > 0 and current_price <= sl:
            reason = f"🛑 止损触发: 当前价 {current_price} <= 止损价 {sl} (亏损 {pnl_pct:.1f}%)"

        # 2. Take-profit hit
        elif tp > 0 and current_price >= tp:
            reason = f"🎯 止盈触发: 当前价 {current_price} >= 止盈价 {tp} (盈利 {pnl_pct:.1f}%)"

        # 3. Flash crash: >5% drop from entry in a short period
        elif pnl_pct <= -5:
            reason = f"⚠ 风控平仓: 跌幅 {pnl_pct:.1f}% 超过 5% 阈值"

        if reason:
            logger.info(reason)
            _auto_broadcast("log", msg=reason)
            try:
                r = _execute_one_trade(symbol, "SELL", qty)
                trade_info = r.get("trade", {})
                if r.get("status") == "success":
                    _auto_broadcast("log", msg=f"  ✅ 自动平仓 {symbol} x {qty} @ {current_price}")
                    _auto_broadcast("trade_result", symbol=symbol, action="SELL",
                                  status="success", price=current_price, quantity=qty,
                                  error=f"自动: {reason}")
                    exits.append({"symbol": symbol, "action": "SELL", "qty": qty, "price": current_price, "reason": reason})
                    del _auto_state["positions"][symbol]
                else:
                    _auto_broadcast("log", msg=f"  ❌ 自动平仓失败 {symbol}: {r.get('error', '?')}")
            except Exception as e:
                _auto_broadcast("log", msg=f"  ❌ 自动平仓异常 {symbol}: {str(e)[:80]}")
                logger.error("自动平仓异常 %s: %s", symbol, str(e))

    return exits


def _do_full_search(market_summary, pairs_str):
    """Single-round search: Flash suggests queries → parallel Tavily → one summary.
    LLM calls: 2 (direction + summary). Tavily calls: up to 5 (parallel)."""
    search_context = ""

    # Step 1: Flash suggests what to search (1 LLM call)
    q_prompt = SEARCH_TEMPLATE.format(market_summary=market_summary, pairs_str=pairs_str)
    _auto_broadcast("search_round", round=1, status="asking")

    try:
        flash_resp = ask_flash([
            {"role": "system", "content": SEARCH_SYSTEM},
            {"role": "user", "content": q_prompt},
        ], max_tokens=512)
    except Exception as e:
        _auto_broadcast("log", msg=f"⚠ Flash 搜索方向失败: {str(e)[:80]}")
        return search_context

    searches = parse_flash_search_response(flash_resp)
    if not searches:
        _auto_broadcast("log", msg="📋 Flash 判断无需搜索")
        return search_context

    _auto_broadcast("search_query", round=1, queries=searches)

    # Step 2: Parallel Tavily searches (up to 5, all at once)
    all_results = []
    with ThreadPoolExecutor(max_workers=min(len(searches), 5)) as pool:
        futures = {pool.submit(search_web, q, 4): q for q in searches[:5]}
        for future in as_completed(futures):
            query = futures[future]
            try:
                results = future.result()
                all_results.extend(results)
                _auto_broadcast("search_found", round=1, query=query, count=len(results))
            except Exception as e:
                _auto_broadcast("log", msg=f"  ⚠ 搜索 '{query[:30]}' 失败: {str(e)[:60]}")

    if not all_results:
        _auto_broadcast("log", msg="📋 所有搜索无结果")
        return search_context

    # Step 3: Flash summarizes all results in one go (1 LLM call)
    results_text = "\n---\n".join(
        f"[{r['title']}] {r['snippet']}" for r in all_results[:15]
    )
    try:
        summary = ask_flash([
            {"role": "system", "content": SUMMARIZE_SYSTEM},
            {"role": "user", "content": SUMMARIZE_TEMPLATE.format(results_text=results_text)},
        ], max_tokens=512)
        search_context = f"\n{summary}\n"
        _auto_broadcast("search_summary", round=1, summary=summary)
    except Exception as e:
        _auto_broadcast("log", msg=f"⚠ 总结失败: {str(e)[:80]}")

    return search_context


def _call_flash_decision(market_summary, search_context, positions_block):
    """Call DeepSeek Flash for a quick trading decision."""
    search_section = f"## 联网搜索情报{search_context}" if search_context else "## 联网搜索情报\n(暂无搜索数据)"
    prompt = FLASH_DECISION_TEMPLATE.format(
        market_summary=market_summary,
        search_context=search_section,
        open_positions=positions_block,
    )
    content = ask_flash([
        {"role": "system", "content": FLASH_DECISION_SYSTEM},
        {"role": "user", "content": prompt},
    ], max_tokens=1024)
    return json.loads(strip_markdown_code(content))


def _should_escalate_to_pro(flash_analysis):
    """Check if Flash's decisions warrant escalation to Pro."""
    decisions = flash_analysis.get("decisions", [])
    search_context = _auto_state.get("cached_search_context", "")

    # Check search context for alarming keywords
    ctx_lower = search_context.lower()
    for kw in ESCALATE_KEYWORDS:
        if kw.lower() in ctx_lower:
            logger.info("⚠ 搜索情报含关键词 '%s', 升级到 Pro", kw)
            return True

    for d in decisions:
        action = d.get("action", "HOLD")
        est = float(d.get("estimatedUsdt", 0))

        # SELL decision → escalate
        if action == "SELL" and est > 0:
            logger.info("⚠ Flash 建议卖出 %s ≈$%.2f, 升级到 Pro", d.get("symbol"), est)
            return True

        # BUY above threshold → escalate
        if action == "BUY" and est > ESCALATE_USDT_THRESHOLD:
            logger.info("⚠ Flash 建议买入 %s ≈$%.2f > $%d, 升级到 Pro", d.get("symbol"), est, ESCALATE_USDT_THRESHOLD)
            return True

    return False


def _call_pro_decision_wrapper(market_summary, search_context, positions_block):
    """Call DeepSeek Pro for a final trading decision (same signature as _call_flash_decision)."""
    search_section = f"## 联网搜索情报{search_context}" if search_context else "## 联网搜索情报\n(暂无搜索数据)"
    prompt = DECISION_TEMPLATE.format(
        market_summary=market_summary,
        search_context=search_section,
        open_positions=positions_block,
    )
    content = ask_pro([
        {"role": "system", "content": DECISION_SYSTEM},
        {"role": "user", "content": prompt},
    ])
    return json.loads(strip_markdown_code(content))


def _auto_trade_loop(interval):
    global _auto_state
    logger.info("🤖 全自动交易循环启动, 间隔 %ds (搜索间隔 %ds)", interval, _auto_state["search_interval"])

    while _auto_state["running"]:
        _auto_state["round"] += 1
        rnd = _auto_state["round"]
        _auto_state["status"] = f"第{rnd}轮: 采集数据..."
        _auto_broadcast("log", msg=f"🔄 第 {rnd} 轮自动交易开始")
        _auto_broadcast("round_start", round=rnd, time=datetime.now().strftime("%H:%M:%S"))

        try:
            # ===== Step 0: Collect market data =====
            pairs = load_trade_pairs()
            _auto_broadcast("log", msg=f"📡 采集 {len(pairs)} 个交易对市场数据...")

            result = {}
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = {pool.submit(collect_pair_data, p): p for p in pairs}
                for future in as_completed(futures):
                    pair = futures[future]
                    try:
                        data = future.result()
                        result[pair] = data
                        tk = data.get("ticker", {})
                        if isinstance(tk, dict) and "lastPrice" in tk:
                            _auto_broadcast("collect_pair", pair=pair, price=tk["lastPrice"],
                                          change=tk.get("priceChangePercent"))
                    except Exception as e:
                        _auto_broadcast("log", msg=f"  ⚠ {pair} 采集失败: {str(e)[:80]}")

            market_data = {p: result[p] for p in pairs if p in result}

            # ===== Step 1: Check positions for stop-loss / take-profit / volatility =====
            positions = _auto_state.get("positions", {})
            if positions:
                _auto_broadcast("log", msg=f"🔍 检查 {len(positions)} 个持仓: {', '.join(positions.keys())}")
                exits = _check_positions(market_data)
                if exits:
                    _auto_broadcast("log", msg=f"💥 {len(exits)} 个持仓触发自动平仓")
                    pnl = _calculate_pnl(load_trade_history())
                    _auto_state["last_pnl"] = pnl
                    _auto_broadcast("pnl", pnl=pnl)
                    try:
                        record_balance_snapshot()
                    except Exception:
                        pass
                remaining = _auto_state.get("positions", {})
                if remaining:
                    pos_list = ", ".join(f"{s}(入场{pos['entry_price']})" for s, pos in remaining.items())
                    _auto_broadcast("log", msg=f"📌 剩余持仓: {pos_list}")

            # ===== Step 2: Get balances =====
            bal_result = get_balances()
            bal_list = bal_result["balances"]
            _auto_broadcast("balance", balances=[
                {"asset": b["asset"], "total": b["total"], "cnyValue": b["cnyValue"]}
                for b in bal_list
            ])
            _auto_broadcast("log", msg=f"💰 总资产约 ¥{bal_result['totalCny']:.2f}")

            # ===== Step 3: Search (only every 2 hours) =====
            market_summary = _make_market_summary(market_data, bal_list)
            pairs_str = ", ".join(market_data.keys())

            now_ts = time.time()
            search_interval = _auto_state.get("search_interval", 7200)
            last_search = _auto_state.get("last_search_time", 0)
            need_search = (now_ts - last_search) >= search_interval

            if need_search:
                _auto_state["status"] = f"第{rnd}轮: 联网搜索中..."
                _auto_broadcast("log", msg="🌐 触发联网搜索 (距上次搜索超过2小时)")
                search_context = _do_full_search(market_summary, pairs_str)
                _auto_state["last_search_time"] = now_ts
                _auto_state["cached_search_context"] = search_context
            else:
                search_context = _auto_state.get("cached_search_context", "")
                since = int((now_ts - last_search) // 60)
                _auto_broadcast("log", msg=f"📋 使用缓存搜索情报 (上次搜索 {since} 分钟前)")
                if search_context:
                    _auto_broadcast("log", msg=f"📋 缓存内容: {search_context[:120]}...")

            # ===== Step 4: Build open positions summary for Pro =====
            positions_block = ""
            current_positions = _auto_state.get("positions", {})
            if current_positions:
                positions_block = "## 当前持仓\n"
                for sym, pos in current_positions.items():
                    try:
                        cp = get_current_price(sym)
                        pnl_pct = (cp - pos["entry_price"]) / pos["entry_price"] * 100
                    except Exception:
                        cp = 0
                        pnl_pct = 0
                    positions_block += (
                        f"- {sym}: 入场价 {pos['entry_price']} 数量 {pos['quantity']} "
                        f"止损 {pos.get('stop_loss', '-')} 止盈 {pos.get('take_profit', '-')} "
                        f"当前 {cp} (盈亏 {pnl_pct:+.1f}%)\n"
                    )
            else:
                positions_block = "## 当前持仓\n(空仓，无持仓)"

            # ===== Step 5: AI decision (Flash first, Pro on escalation) =====
            _auto_state["status"] = f"第{rnd}轮: Flash 快速分析..."
            _auto_broadcast("log", msg="🧠 Flash 快速决策中...")
            used_model = "flash"

            try:
                analysis = _call_flash_decision(market_summary, search_context, positions_block)
            except Exception as e:
                _auto_broadcast("error", error=f"Flash 决策失败: {str(e)}")
                raise

            # Check if escalation to Pro is needed
            if _should_escalate_to_pro(analysis):
                _auto_state["status"] = f"第{rnd}轮: Pro 深度分析..."
                _auto_broadcast("pro_start", msg="⚠ 触发升级条件 → DeepSeek Pro 深度分析中...")
                try:
                    analysis = _call_pro_decision_wrapper(market_summary, search_context, positions_block)
                    used_model = "pro"
                    _auto_broadcast("log", msg="✅ Pro 深度分析完成")
                except Exception as e:
                    _auto_broadcast("log", msg=f"⚠ Pro 调用失败, 使用 Flash 结果: {str(e)[:60]}")
            else:
                _auto_broadcast("log", msg="📋 Flash 决策足够 (无需Pro)")

            decisions = analysis.get("decisions", [])
            enrich_decisions(decisions, market_data)
            _auto_state["decisions"] = decisions
            _auto_broadcast("decisions", decisions=decisions,
                          research_summary=analysis.get("research_summary", ""),
                          overall_analysis=analysis.get("overall_analysis", ""),
                          model=used_model)

            # ===== Step 6: Execute trades & track positions =====
            actionable = [d for d in decisions if d.get("action") in ("BUY", "SELL")
                          and float(d.get("quantity", 0)) > 0]
            if actionable:
                _auto_broadcast("log", msg=f"💱 执行 {len(actionable)} 笔交易...")
                for d in actionable:
                    sym = d.get("symbol", "").upper().strip()
                    act = d.get("action", "").upper().strip()
                    qty = float(d.get("quantity", 0))
                    try:
                        r = _execute_one_trade(sym, act, qty)
                        trade_info = r.get("trade", {})
                        exec_price = trade_info.get("price", 0)
                        exec_qty = trade_info.get("quantity", qty)
                        _auto_broadcast("trade_result", symbol=sym, action=act,
                                      status=r.get("status", "?"),
                                      price=exec_price if r.get("status") == "success" else 0,
                                      quantity=exec_qty,
                                      error=r.get("error", ""))

                        if r.get("status") == "success":
                            _auto_broadcast("log", msg=f"  ✅ {act} {sym} {exec_qty} @ {exec_price}")

                            # Track new BUY position with stop-loss and take-profit
                            if act == "BUY":
                                sl = float(d.get("stopLossPrice", 0))
                                tp = float(d.get("takeProfitPrice", 0))
                                _auto_state["positions"][sym] = {
                                    "entry_price": exec_price,
                                    "quantity": exec_qty,
                                    "stop_loss": sl,
                                    "take_profit": tp,
                                    "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                }
                                _auto_broadcast("log", msg=f"  📌 跟踪持仓 {sym}: 止损 {sl} 止盈 {tp}")

                            # Remove from tracking when SELL succeeds
                            elif act == "SELL":
                                if sym in _auto_state["positions"]:
                                    del _auto_state["positions"][sym]
                                    _auto_broadcast("log", msg=f"  📌 移除持仓 {sym}")
                        else:
                            _auto_broadcast("log", msg=f"  ⚠ {act} {sym}: {r.get('error', '?')}")
                    except Exception as e:
                        _auto_broadcast("log", msg=f"  ❌ {act} {sym}: {str(e)[:80]}")
            else:
                _auto_broadcast("log", msg="📊 本轮无交易建议 (全部 HOLD)")

            # ===== Step 7: Update PnL =====
            pnl = _calculate_pnl(load_trade_history())
            _auto_state["last_pnl"] = pnl
            _auto_broadcast("pnl", pnl=pnl)

            try:
                record_balance_snapshot()
            except Exception:
                pass

        except Exception as e:
            logger.error("第 %d 轮自动交易异常: %s", rnd, str(e))
            _auto_broadcast("error", error=f"第{rnd}轮异常: {str(e)}")

        _auto_state["last_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _auto_broadcast("round_done", round=rnd, next_round_in=interval,
                      positions=_auto_state.get("positions", {}))

        _auto_state["status"] = f"等待 {interval // 60} 分钟后下一轮..."
        wait_start = time.time()
        while _auto_state["running"] and (time.time() - wait_start) < interval:
            remaining = interval - int(time.time() - wait_start)
            if remaining % 30 == 0:
                pos_count = len(_auto_state.get("positions", {}))
                status = f"等待中... {remaining // 60}分{remaining % 60}秒"
                if pos_count > 0:
                    status += f" (持仓{pos_count}个)"
                _auto_broadcast("waiting", remaining=remaining, positions=pos_count)
            time.sleep(1)

    _auto_state["status"] = "已停止"
    _auto_state["running"] = False
    _auto_broadcast("auto_stopped", msg="全自动交易已停止", total_rounds=_auto_state["round"])
    logger.info("🤖 全自动交易循环结束, 共 %d 轮", _auto_state["round"])


def _calculate_pnl(records, specific_symbol=None):
    trades = records
    if specific_symbol:
        trades = [r for r in records if r["symbol"] == specific_symbol]
    pnl_by_symbol = {}
    total_pnl = 0
    for symbol in set(r["symbol"] for r in trades):
        symbol_trades = [r for r in trades if r["symbol"] == symbol]
        buys = []
        symbol_realized = 0
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
                symbol_realized += pnl
            i += 1
        total_pnl += symbol_realized
        unrealized = 0
        for b in buys:
            try:
                cp = get_current_price(symbol)
                unrealized += (cp - float(b["price"])) * float(b["quantity"])
            except Exception:
                pass
        pnl_by_symbol[symbol] = {
            "realizedPnl": round(symbol_realized, 4),
            "unrealizedPnl": round(unrealized, 4),
            "totalPnl": round(symbol_realized + unrealized, 4),
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


def _search_loop(market_summary, pairs_str):
    """Single-round search: Flash direction → parallel Tavily → one summary. Optimized for minimal API calls."""
    search_context = ""

    q_prompt = SEARCH_TEMPLATE.format(market_summary=market_summary, pairs_str=pairs_str)
    try:
        flash_resp = ask_flash([
            {"role": "system", "content": SEARCH_SYSTEM},
            {"role": "user", "content": q_prompt},
        ], max_tokens=512)
    except Exception as e:
        logger.warning("Flash search direction failed: %s", str(e))
        return search_context

    searches = parse_flash_search_response(flash_resp)
    if not searches:
        return search_context

    logger.info("🔍 联网搜索 (%d queries): %s", len(searches), searches)

    # Parallel search
    all_results = []
    with ThreadPoolExecutor(max_workers=min(len(searches), 5)) as pool:
        futures = {pool.submit(search_web, q, 3): q for q in searches[:5]}
        for future in as_completed(futures):
            query = futures[future]
            try:
                all_results.extend(future.result())
            except Exception as e:
                logger.warning("Search '%s' failed: %s", query[:30], str(e)[:60])

    if all_results:
        results_text = "\n---\n".join(f"[{r['title']}] {r['snippet']}" for r in all_results[:12])
        try:
            summary = ask_flash([
                {"role": "system", "content": SUMMARIZE_SYSTEM},
                {"role": "user", "content": SUMMARIZE_TEMPLATE.format(results_text=results_text)},
            ], max_tokens=512)
            search_context = f"\n{summary}\n"
            logger.info("📝 搜索总结: %s", summary[:80])
        except Exception as e:
            logger.warning("Flash summary failed: %s", str(e))

    return search_context


def _call_pro_decision(market_summary, search_context):
    search_section = f"## 联网搜索情报{search_context}" if search_context else ""
    decision_prompt = DECISION_TEMPLATE.format(
        market_summary=market_summary,
        search_context=search_section,
        open_positions="(手动分析模式，无持仓追踪)",
    )

    content = ask_pro([
        {"role": "system", "content": DECISION_SYSTEM},
        {"role": "user", "content": decision_prompt},
    ])
    return json.loads(strip_markdown_code(content))


def _run_deepseek_analysis(market_data, balances):
    market_summary = _make_market_summary(market_data, balances)
    pairs_str = ", ".join(market_data.keys())

    search_context = _search_loop(market_summary, pairs_str)

    try:
        return _call_pro_decision(market_summary, search_context)
    except Exception as e:
        logger.error("DeepSeek Pro 调用失败: %s", str(e))
        raise


def _execute_one_trade(symbol, action, quantity):
    filters, sinfo = get_symbol_filters(symbol)
    if not filters:
        return {"symbol": symbol, "action": action, "status": "failed", "error": f"找不到 {symbol} 交易规则"}

    step_size_str = filters.get("LOT_SIZE", {}).get("stepSize", "0.00000001")
    step_size = float(step_size_str)
    min_qty = float(filters.get("LOT_SIZE", {}).get("minQty", "0"))
    min_notional = float(filters.get("MIN_NOTIONAL", {}).get("minNotional", "10"))

    qty = float(quantity)
    prec = 0
    if "." in step_size_str:
        prec = len(step_size_str.split(".")[1].rstrip("0"))
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

    current_price = get_current_price(symbol)
    trade_record = append_trade_record(result, current_price)

    logger.info("✅ 交易: %s %s %s @ %.4f USDT", trade_record["id"], side, trade_record["quantity"], trade_record["price"])

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
    pairs_str = ", ".join(market_data.keys())

    yield _sse({"event": "balance", "data": [{"asset": b["asset"], "total": b["total"], "cnyValue": b["cnyValue"]} for b in balances]})

    search_context = ""

    # Single-round search: Flash direction → parallel Tavily → one summary
    yield _sse({"event": "search_round", "round": 1, "status": "asking"})
    q_prompt = SEARCH_TEMPLATE.format(market_summary=market_summary, pairs_str=pairs_str)

    try:
        flash_resp = ask_flash([
            {"role": "system", "content": SEARCH_SYSTEM},
            {"role": "user", "content": q_prompt},
        ], max_tokens=512)
    except Exception as e:
        logger.warning("Flash search direction failed: %s", str(e))
        yield _sse({"event": "search_error", "round": 1, "error": str(e)[:100]})
        flash_resp = ""

    searches = parse_flash_search_response(flash_resp) if flash_resp else []
    if not searches:
        yield _sse({"event": "search_round", "round": 1, "status": "skip", "msg": "无需搜索"})
    else:
        yield _sse({"event": "search_query", "round": 1, "queries": searches})

        # Parallel search
        all_results = []
        with ThreadPoolExecutor(max_workers=min(len(searches), 5)) as pool:
            futures = {pool.submit(search_web, q, 3): q for q in searches[:5]}
            for future in as_completed(futures):
                query = futures[future]
                try:
                    results = future.result()
                    all_results.extend(results)
                    yield _sse({"event": "search_found", "round": 1, "query": query, "count": len(results),
                              "preview": results[0]["snippet"][:100] if results else ""})
                except Exception as e:
                    yield _sse({"event": "search_error", "round": 1, "error": str(e)[:80]})

        if all_results:
            results_text = "\n---\n".join(f"[{r['title']}] {r['snippet']}" for r in all_results[:12])
            yield _sse({"event": "summarizing", "round": 1, "count": len(all_results)})
            try:
                summary = ask_flash([
                    {"role": "system", "content": SUMMARIZE_SYSTEM},
                    {"role": "user", "content": SUMMARIZE_TEMPLATE.format(results_text=results_text)},
                ], max_tokens=512)
                search_context = f"\n{summary}\n"
                yield _sse({"event": "search_summary", "round": 1, "summary": summary})
            except Exception as e:
                yield _sse({"event": "search_error", "round": 1, "error": str(e)[:80]})
        else:
            yield _sse({"event": "search_round", "round": 1, "status": "no_results", "msg": "搜索无结果"})

    yield _sse({"event": "pro_start", "msg": "发送完整数据到 DeepSeek-V4-Pro 进行最终决策..."})

    try:
        search_section = f"## 联网搜索情报{search_context}" if search_context else ""
        decision_prompt = DECISION_TEMPLATE.format(
            market_summary=market_summary,
            search_context=search_section,
            open_positions="(手动分析模式，无持仓追踪)",
        )
        content = ask_pro([
            {"role": "system", "content": DECISION_SYSTEM},
            {"role": "user", "content": decision_prompt},
        ])
        yield _sse({"event": "pro_done", "length": len(content)})
        analysis = json.loads(strip_markdown_code(content))
    except json.JSONDecodeError as e:
        yield _sse({"event": "error", "error": f"JSON 解析失败: {str(e)}"})
        return
    except Exception as e:
        logger.error("DeepSeek Pro 调用失败: %s", str(e))
        yield _sse({"event": "error", "error": f"DeepSeek Pro 决策失败: {str(e)}"})
        return

    decisions = analysis.get("decisions", [])
    enrich_decisions(decisions, market_data)

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
    enrich_decisions(decisions, market_data)

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


# ===== Auto-Trading Endpoints =====

@trade_bp.route("/api/trade/auto-start", methods=["POST"])
def auto_trade_start():
    if _auto_state["running"]:
        return jsonify({"error": "自动交易已在运行中"}), 400
    if not DEEPSEEK_API_KEY:
        return jsonify({"error": "请设置 DEEPSEEK_API_KEY"}), 400

    data = request.get_json(silent=True) or {}
    interval = int(data.get("interval", 600))
    interval = max(60, min(interval, 86400))

    _auto_state["running"] = True
    _auto_state["round"] = 0
    _auto_state["status"] = "启动中"
    _auto_state["interval"] = interval
    _auto_state["decisions"] = []
    _auto_state["last_pnl"] = None
    _auto_state["last_time"] = None
    _auto_state["last_search_time"] = 0
    _auto_state["cached_search_context"] = ""
    _auto_state["positions"] = {}

    with _auto_lock:
        _auto_events.clear()

    _auto_broadcast("auto_started", interval=interval, search_interval=_auto_state["search_interval"],
                  positions={})
    logger.info("🚀 全自动交易启动, 间隔 %ds, 搜索间隔 %ds", interval, _auto_state["search_interval"])

    thread = threading.Thread(target=_auto_trade_loop, args=(interval,), daemon=True)
    thread.start()
    _auto_state["thread"] = thread

    return jsonify({"status": "started", "interval": interval, "search_interval": _auto_state["search_interval"]})


@trade_bp.route("/api/trade/auto-stop", methods=["POST"])
def auto_trade_stop():
    if not _auto_state["running"]:
        return jsonify({"error": "自动交易未在运行"}), 400
    _auto_state["running"] = False
    _auto_state["status"] = "stopping"
    _auto_broadcast("auto_stopping", msg="正在停止...")
    logger.info("🛑 全自动交易停止信号已发送")
    return jsonify({"status": "stopping"})


@trade_bp.route("/api/trade/auto-status")
def auto_trade_status():
    return jsonify({
        "running": _auto_state["running"],
        "round": _auto_state["round"],
        "lastTime": _auto_state["last_time"],
        "status": _auto_state["status"],
        "interval": _auto_state["interval"],
        "search_interval": _auto_state.get("search_interval", 7200),
        "last_search_time": _auto_state.get("last_search_time", 0),
        "decisions": _auto_state["decisions"],
        "pnl": _auto_state.get("last_pnl"),
        "positions": _auto_state.get("positions", {}),
    })


@trade_bp.route("/api/trade/auto-stream")
def auto_trade_stream():
    def generate():
        idx = 0
        while True:
            with _auto_lock:
                new_events = list(_auto_events)[idx:]
                idx = len(_auto_events)
            for evt in new_events:
                yield (json.dumps(evt, ensure_ascii=False, default=str) + "\n").encode()
            if not _auto_state["running"] and idx > 0:
                break
            time.sleep(1)
    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
