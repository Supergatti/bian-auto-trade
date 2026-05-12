import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from config import (
    BINANCE_BASE_URL, BINANCE_API_KEY, BINANCE_SECRET_KEY,
    KLINE_INTERVALS, KLINE_LIMITS, logger,
)
from utils.helpers import sign_params

_exchange_info_cache = {"data": None, "ts": 0}


def _retry_api(fn, name, max_retries=3):
    last_err = None
    for attempt in range(max_retries):
        try:
            return fn()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_err = e
            wait = (attempt + 1) * 2
            logger.warning("%s 连接失败 (attempt %d/%d), %ds 后重试: %s", name, attempt + 1, max_retries, wait, str(e)[:80])
            time.sleep(wait)
    raise last_err


def _public_request(endpoint, params=None):
    def _do():
        url = f"{BINANCE_BASE_URL}{endpoint}"
        start = time.time()
        resp = requests.get(url, params=params or {}, timeout=30)
        elapsed = (time.time() - start) * 1000
        logger.debug("PUBLIC %s → %d (%dms)", endpoint, resp.status_code, int(elapsed))
        resp.raise_for_status()
        return resp.json()
    return _retry_api(_do, f"PUBLIC {endpoint}")


def _signed_request(method, endpoint, params=None):
    if params is None:
        params = {}
    def _do():
        p = dict(params)
        p["timestamp"] = int(time.time() * 1000)
        p["signature"] = sign_params(p)
        headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
        url = f"{BINANCE_BASE_URL}{endpoint}"
        start = time.time()
        if method == "GET":
            resp = requests.get(url, headers=headers, params=p, timeout=30)
        else:
            resp = requests.post(url, headers=headers, data=p, timeout=30)
        elapsed = (time.time() - start) * 1000
        logger.info("SIGNED %s %s → %d (%dms)", method, endpoint, resp.status_code, int(elapsed))
        resp.raise_for_status()
        return resp.json()
    return _retry_api(_do, f"SIGNED {method} {endpoint}")


def check_keys():
    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        return False
    return True


def get_account():
    return _signed_request("GET", "/api/v3/account")


def get_balances():
    data = get_account()
    prices = _public_request("/api/v3/ticker/price")
    price_map = {p["symbol"]: float(p["price"]) for p in prices}
    from utils.helpers import get_cny_rate
    cny_rate = get_cny_rate()

    result, total_cny = [], 0.0
    for item in data.get("balances", []):
        free, locked = float(item["free"]), float(item["locked"])
        total = free + locked
        if total <= 0:
            continue
        asset = item["asset"]
        usdt_price = 1.0 if asset == "USDT" else (
            price_map.get(asset + "USDT", 0) or price_map.get("USDT" + asset, 0))
        usdt_value = total * usdt_price
        cny_value = usdt_value * cny_rate
        total_cny += cny_value
        result.append({
            "asset": asset, "free": free, "locked": locked, "total": total,
            "usdtPrice": usdt_price, "usdtValue": round(usdt_value, 4), "cnyValue": round(cny_value, 2),
        })
    result.sort(key=lambda x: x["cnyValue"], reverse=True)
    return {"balances": result, "totalCny": round(total_cny, 2), "cnyRate": round(cny_rate, 4)}


def get_exchange_info():
    now = time.time()
    if _exchange_info_cache["data"] and now - _exchange_info_cache["ts"] < 3600:
        return _exchange_info_cache["data"]
    data = _public_request("/api/v3/exchangeInfo")
    _exchange_info_cache["data"] = data
    _exchange_info_cache["ts"] = now
    return data


def get_symbol_filters(symbol):
    info = get_exchange_info()
    for s in info.get("symbols", []):
        if s["symbol"] == symbol:
            filters = {}
            for f in s.get("filters", []):
                filters[f["filterType"]] = f
            return filters, s
    return {}, {}


def get_trading_symbols():
    data = _public_request("/api/v3/exchangeInfo")
    return [{
        "symbol": s["symbol"], "baseAsset": s["baseAsset"], "quoteAsset": s["quoteAsset"],
    } for s in data.get("symbols", []) if s["status"] == "TRADING"]


def get_klines(symbol, interval="1h", limit=500):
    data = _public_request("/api/v3/klines", {
        "symbol": symbol.upper(), "interval": interval, "limit": limit,
    })
    return [{
        "time": k[0], "open": float(k[1]), "high": float(k[2]),
        "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
    } for k in data]


def get_current_price(symbol):
    data = _public_request("/api/v3/ticker/price", {"symbol": symbol})
    return float(data["price"])


def execute_order(symbol, side, quantity):
    params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": str(quantity)}
    return _signed_request("POST", "/api/v3/order", params)


# ===== Margin Trading (Short Selling) =====

def margin_account():
    """Get cross margin account details (borrowed, free, net asset, etc.)."""
    return _signed_request("GET", "/sapi/v1/margin/account")


def margin_max_borrowable(asset):
    """Get max borrowable amount for an asset in cross margin."""
    data = _signed_request("GET", "/sapi/v1/margin/maxBorrowable", {"asset": asset})
    return float(data.get("amount", 0))


def margin_borrow(asset, amount):
    """Borrow an asset on cross margin."""
    return _signed_request("POST", "/sapi/v1/margin/loan", {
        "asset": asset, "amount": str(amount),
    })


def margin_repay(asset, amount):
    """Repay a borrowed asset on cross margin."""
    return _signed_request("POST", "/sapi/v1/margin/repay", {
        "asset": asset, "amount": str(amount),
    })


def margin_order(symbol, side, quantity):
    """Place a margin trade order (side=SELL to short, side=BUY to cover)."""
    params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": str(quantity)}
    return _signed_request("POST", "/sapi/v1/margin/order", params)


def stop_loss_order(symbol, quantity, stop_price):
    """Place a STOP_LOSS market order (triggers market sell when stopPrice is hit)."""
    params = {
        "symbol": symbol, "side": "SELL", "type": "STOP_LOSS",
        "quantity": str(quantity), "stopPrice": str(stop_price),
    }
    return _signed_request("POST", "/api/v3/order", params)


def oco_order(symbol, quantity, stop_price, take_profit_price):
    """Place an OCO (one-cancels-other) order: stop-loss + limit take-profit.
    When one triggers, the other is automatically cancelled."""
    stop_limit_price = round(stop_price * 0.998, 2)  # slightly below stop for execution
    params = {
        "symbol": symbol,
        "side": "SELL",
        "quantity": str(quantity),
        "price": str(take_profit_price),  # limit price for take-profit
        "stopPrice": str(stop_price),  # stop trigger
        "stopLimitPrice": str(stop_limit_price),  # limit price after stop triggers
        "stopLimitTimeInForce": "GTC",
    }
    return _signed_request("POST", "/api/v3/order/oco", params)


def cancel_order(symbol, order_id):
    """Cancel an open order by ID."""
    return _signed_request("DELETE", "/api/v3/order", {
        "symbol": symbol, "orderId": order_id,
    })


def cancel_oco_order(symbol, order_list_id):
    """Cancel an OCO (one-cancels-other) order by orderListId."""
    return _signed_request("DELETE", "/api/v3/orderList", {
        "symbol": symbol, "orderListId": order_list_id,
    })


def get_open_orders(symbol=None):
    """Get all open orders, optionally filtered by symbol."""
    params = {}
    if symbol:
        params["symbol"] = symbol
    return _signed_request("GET", "/api/v3/openOrders", params or None)


def get_depth(symbol, limit=10):
    data = _public_request("/api/v3/depth", {"symbol": symbol.upper(), "limit": limit})
    return {
        "bids": [[float(b[0]), float(b[1])] for b in data["bids"]],
        "asks": [[float(a[0]), float(a[1])] for a in data["asks"]],
    }


def get_recent_trades(symbol, limit=20):
    data = _public_request("/api/v3/trades", {"symbol": symbol.upper(), "limit": limit})
    return [{
        "price": float(t["price"]), "qty": float(t["qty"]),
        "time": t["time"], "isBuyerMaker": t["isBuyerMaker"],
    } for t in data]


def get_ticker_24h(symbol):
    data = _public_request("/api/v3/ticker/24hr", {"symbol": symbol.upper()})
    return {
        "lastPrice": float(data["lastPrice"]),
        "priceChange": float(data["priceChange"]),
        "priceChangePercent": float(data["priceChangePercent"]),
        "high": float(data["highPrice"]),
        "low": float(data["lowPrice"]),
        "volume": float(data["volume"]),
        "quoteVolume": float(data["quoteVolume"]),
    }


def collect_pair_data(symbol):
    start = time.time()
    logger.info("  采集 %s 数据...", symbol)
    data = {"symbol": symbol}
    try:
        tk = _public_request("/api/v3/ticker/24hr", {"symbol": symbol})
        data["ticker"] = {
            "lastPrice": float(tk["lastPrice"]),
            "priceChange": float(tk["priceChange"]),
            "priceChangePercent": float(tk["priceChangePercent"]),
            "high": float(tk["highPrice"]),
            "low": float(tk["lowPrice"]),
            "volume": float(tk["volume"]),
            "quoteVolume": float(tk["quoteVolume"]),
        }
    except Exception as e:
        data["ticker"] = {"error": str(e)}

    data["klines"] = {}
    for interval, limit in KLINE_LIMITS.items():
        try:
            kl = _public_request("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
            data["klines"][interval] = [{
                "time": k[0], "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
            } for k in kl]
        except Exception as e:
            data["klines"][interval] = {"error": str(e)}

    try:
        ob = _public_request("/api/v3/depth", {"symbol": symbol, "limit": 10})
        data["orderBook"] = {
            "bids": [[float(b[0]), float(b[1])] for b in ob["bids"]],
            "asks": [[float(a[0]), float(a[1])] for a in ob["asks"]],
        }
    except Exception as e:
        data["orderBook"] = {"error": str(e)}

    try:
        trades = _public_request("/api/v3/trades", {"symbol": symbol, "limit": 20})
        data["recentTrades"] = [{
            "price": float(t["price"]), "qty": float(t["qty"]),
            "time": t["time"], "isBuyerMaker": t["isBuyerMaker"],
        } for t in trades]
    except Exception as e:
        data["recentTrades"] = {"error": str(e)}

    elapsed = (time.time() - start) * 1000
    logger.info("  %s 采集完成 (%dms)", symbol, int(elapsed))
    return data


def collect_all_pairs_data(pairs):
    result = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(collect_pair_data, p): p for p in pairs}
        for future in as_completed(futures):
            pair = futures[future]
            result[pair] = future.result()
    ordered = {}
    for p in pairs:
        if p in result:
            ordered[p] = result[p]
    return ordered
