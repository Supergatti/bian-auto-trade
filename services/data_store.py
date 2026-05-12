import uuid
from datetime import datetime

from config import (
    FAVORITES_FILE, TRADE_PAIRS_FILE, TRADE_HISTORY_FILE,
    BALANCE_HISTORY_FILE, logger,
)
from utils.files import load_json, save_json
from services.binance import get_balances


def load_favorites():
    return load_json(FAVORITES_FILE, list)


def save_favorites(favs):
    save_json(FAVORITES_FILE, favs)


def load_trade_pairs():
    pairs = load_json(TRADE_PAIRS_FILE, lambda: ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"])
    return [p.upper().strip() for p in pairs if isinstance(p, str)]


def load_trade_history():
    return load_json(TRADE_HISTORY_FILE, list)


def save_trade_history(records):
    save_json(TRADE_HISTORY_FILE, records)


def _load_balance_history():
    return load_json(BALANCE_HISTORY_FILE, list)


def _save_balance_history(records):
    max_entries = 1000
    if len(records) > max_entries:
        records = records[-max_entries:]
    save_json(BALANCE_HISTORY_FILE, records)


def record_balance_snapshot():
    try:
        data = get_balances()
        total_cny = data.get("totalCny", 0)
        history = _load_balance_history()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if history and history[-1]["time"] == now:
            history[-1] = {"time": now, "totalCny": total_cny}
        else:
            history.append({"time": now, "totalCny": total_cny})
            if len(history) > 1:
                prev_time = datetime.strptime(history[-2]["time"], "%Y-%m-%d %H:%M:%S")
                curr_time = datetime.strptime(now, "%Y-%m-%d %H:%M:%S")
                if (curr_time - prev_time).total_seconds() < 600:
                    history.pop(-2)
        _save_balance_history(history)
    except Exception as e:
        logger.warning("记录余额快照失败: %s", str(e)[:80])


def append_trade_record(result, current_price=0):
    fills = result.get("fills", [])
    exec_price = 0
    exec_qty = 0
    commission = 0
    if fills:
        total_quote = sum(float(f["price"]) * float(f["qty"]) for f in fills)
        exec_qty = sum(float(f["qty"]) for f in fills)
        exec_price = total_quote / exec_qty if exec_qty > 0 else 0
        commission = sum(float(f.get("commission", 0)) for f in fills)

    trade_record = {
        "id": str(uuid.uuid4())[:8],
        "symbol": result.get("symbol", ""),
        "side": result.get("side", ""),
        "quantity": exec_qty or result.get("quantity", 0),
        "price": exec_price or current_price,
        "totalUsdt": (exec_qty or result.get("quantity", 0)) * (exec_price or current_price),
        "commission": commission,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "orderId": result.get("orderId", ""),
    }

    records = load_trade_history()
    records.append(trade_record)
    save_trade_history(records)

    return trade_record
