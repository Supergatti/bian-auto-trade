from flask import Blueprint, jsonify

from services.binance import check_keys, get_account, get_balances, get_trading_symbols, get_klines
from routes.favorites import load_favorites, save_favorites
from config import TOP_HOT_PAIRS

account_bp = Blueprint("account", __name__)
market_bp = Blueprint("market", __name__)


@account_bp.route("/api/account")
def account_info():
    if not check_keys():
        return jsonify({"error": "请设置 BINANCE_API_KEY 和 BINANCE_SECRET_KEY"}), 400
    try:
        return jsonify(get_account())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@account_bp.route("/api/balance")
def balance():
    if not check_keys():
        return jsonify({"error": "请设置 BINANCE_API_KEY 和 BINANCE_SECRET_KEY"}), 400
    try:
        data = get_balances()

        from services.binance import _public_request
        prices = _public_request("/api/v3/ticker/price")
        price_map = {p["symbol"]: float(p["price"]) for p in prices}

        favs = load_favorites()
        added = False
        for pair in TOP_HOT_PAIRS:
            if pair not in favs:
                favs.append(pair); added = True
        for item in data["balances"]:
            pair = item["asset"] + "USDT"
            if item["asset"] != "USDT" and pair not in favs and pair in price_map:
                favs.append(pair); added = True
        if added:
            save_favorites(favs)

        from routes.manual_trade import _record_balance_snapshot
        _record_balance_snapshot()

        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@market_bp.route("/api/symbols")
def symbols():
    try:
        return jsonify(get_trading_symbols())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@market_bp.route("/api/klines")
def klines():
    from flask import request
    symbol = request.args.get("symbol", "BTCUSDT")
    interval = request.args.get("interval", "1h")
    limit = request.args.get("limit", 500, type=int)
    try:
        return jsonify(get_klines(symbol, interval, limit))
    except Exception as e:
        return jsonify({"error": str(e)}), 500
