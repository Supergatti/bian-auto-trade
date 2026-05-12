import time
from datetime import datetime

import requests
from flask import Blueprint, jsonify, request

from config import logger
from services.data_store import (
    load_trade_pairs, load_trade_history, save_trade_history,
    _load_balance_history, record_balance_snapshot, append_trade_record,
)
from services.binance import (
    check_keys, get_account, get_balances,
    get_depth, get_recent_trades, get_ticker_24h,
    get_symbol_filters, get_current_price, execute_order,
)

manual_bp = Blueprint("manual", __name__)


@manual_bp.route("/api/balance/history")
def balance_history():
    history = _load_balance_history()
    return jsonify(history)


@manual_bp.route("/api/manual/pair-info")
def pair_info():
    if not check_keys():
        return jsonify({"error": "请设置 API 密钥"}), 400
    symbol = request.args.get("symbol", "").upper().strip()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400

    try:
        ticker = get_ticker_24h(symbol)
        depth = get_depth(symbol, 8)
        trades = get_recent_trades(symbol, 30)
        filters, info = get_symbol_filters(symbol)

        base_asset = info.get("baseAsset", symbol.replace("USDT", ""))
        quote_asset = info.get("quoteAsset", "USDT")

        step_size = filters.get("LOT_SIZE", {}).get("stepSize", "0.00000001")
        min_qty = float(filters.get("LOT_SIZE", {}).get("minQty", "0"))
        prec = 0
        if "." in str(step_size):
            prec = len(str(step_size).split(".")[1].rstrip("0"))

        account = get_account()
        base_free = 0
        base_locked = 0
        quote_free = 0
        quote_locked = 0
        for b in account.get("balances", []):
            if b["asset"] == base_asset:
                base_free = float(b["free"])
                base_locked = float(b["locked"])
            if b["asset"] == quote_asset:
                quote_free = float(b["free"])
                quote_locked = float(b["locked"])

        return jsonify({
            "symbol": symbol,
            "baseAsset": base_asset,
            "quoteAsset": quote_asset,
            "ticker": ticker,
            "depth": depth,
            "recentTrades": trades,
            "precision": prec,
            "minQty": min_qty,
            "balance": {
                "baseFree": base_free,
                "baseLocked": base_locked,
                "baseTotal": base_free + base_locked,
                "quoteFree": quote_free,
                "quoteLocked": quote_locked,
                "quoteTotal": quote_free + quote_locked,
            },
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@manual_bp.route("/api/manual/order", methods=["POST"])
def manual_order():
    if not check_keys():
        return jsonify({"error": "请设置 API 密钥"}), 400

    data = request.get_json(silent=True) or {}
    symbol = data.get("symbol", "").upper().strip()
    side = data.get("side", "").upper().strip()
    order_type = data.get("orderType", "MARKET").upper().strip()
    quantity = data.get("quantity", 0)

    if not symbol or side not in ("BUY", "SELL") or float(quantity) <= 0:
        return jsonify({"error": "需要 symbol/side/quantity"}), 400

    try:
        qty = float(quantity)
        filters, info = get_symbol_filters(symbol)
        step_size = filters.get("LOT_SIZE", {}).get("stepSize", "0.00000001")
        min_qty_val = float(filters.get("LOT_SIZE", {}).get("minQty", "0"))
        prec = 0
        if "." in str(step_size):
            prec = len(str(step_size).split(".")[1].rstrip("0"))
        qty = round(max(qty, min_qty_val), prec)

        if side == "SELL":
            base_asset = info.get("baseAsset", symbol.replace("USDT", ""))
            acc = get_account()
            for b in acc.get("balances", []):
                if b["asset"] == base_asset:
                    free = float(b["free"])
                    if free < qty:
                        qty = round(free, prec)
                    break

        if qty <= 0:
            return jsonify({"error": "余额不足"}), 400

        params = {"symbol": symbol, "side": side, "type": order_type, "quantity": qty}
        result = execute_order(symbol, side, qty)
        trade_record = append_trade_record(result, get_current_price(symbol))

        logger.info("💱 手动交易: %s %s %s @ %.4f", symbol, side, trade_record["quantity"], trade_record["price"])

        return jsonify({"status": "success", "trade": trade_record})
    except requests.exceptions.HTTPError as e:
        body = ""
        try:
            body = e.response.text[:500] if e.response else ""
        except Exception:
            pass
        logger.error("手动交易 HTTP 错误: %s %s", str(e), body)
        return jsonify({"error": f"交易失败 ({e.response.status_code if e.response else '?'}): {body}"}), 500
    except Exception as e:
        logger.error("手动交易失败: %s", str(e))
        return jsonify({"error": f"交易失败: {str(e)}"}), 500
