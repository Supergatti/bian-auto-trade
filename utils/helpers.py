import json
import hmac
import hashlib
import time
from urllib.parse import urlencode

import requests

from config import BINANCE_SECRET_KEY, logger

_cny_rate_cache = {"rate": 0, "ts": 0}


def get_cny_rate():
    now = time.time()
    if now - _cny_rate_cache["ts"] < 3600 and _cny_rate_cache["rate"] > 0:
        return _cny_rate_cache["rate"]
    try:
        resp = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=10)
        rate = resp.json()["rates"].get("CNY", 7.25)
        _cny_rate_cache["rate"] = rate
        _cny_rate_cache["ts"] = now
        return rate
    except Exception:
        return 7.25


def sign_params(params):
    qs = urlencode(params)
    return hmac.new(BINANCE_SECRET_KEY.encode(), qs.encode(), hashlib.sha256).hexdigest()


def strip_markdown_code(text):
    """Remove markdown code block fences from a response string."""
    t = text.strip()
    if t.startswith("```"):
        parts = t.split("\n", 1)
        if len(parts) > 1:
            t = parts[1]
        else:
            t = t[3:]
        # Find last ``` and remove it + anything after
        idx = t.rfind("```")
        if idx >= 0:
            t = t[:idx]
        t = t.strip()
    return t


def repair_json(text):
    """Try to fix common JSON errors from LLM output."""
    import re
    t = text.strip()
    fixes = [
        t,  # original
        re.sub(r',\s*([}\]])', r'\1', t),  # trailing comma before ] or }
    ]
    # Try closing unclosed braces/brackets
    opens = sum(1 for c in t if c in "[{")
    closes = sum(1 for c in t if c in "]}")
    if opens > closes:
        missing = "]" * (t.count("[") - t.count("]")) + "}" * (t.count("{") - t.count("}"))
        fixes.append(t.rstrip() + missing)
    for attempt in fixes:
        try:
            return json.loads(attempt)
        except (json.JSONDecodeError, ValueError):
            continue
    raise ValueError(f"JSON repair failed. Raw: {t[:200]}")



def parse_flash_search_response(flash_resp):
    """Parse Flash's search query response, returning list of search strings.
    Returns empty list if no search is needed."""
    try:
        resp = strip_markdown_code(flash_resp)
        req = json.loads(resp)
        return req.get("searches", [])
    except (json.JSONDecodeError, ValueError):
        if "不需要" in flash_resp or "no need" in flash_resp.lower():
            return []
        return []


def enrich_decisions(decisions, market_data):
    """Fill in estimatedUsdt, lastPrice, detail, and stopLossPrice defaults."""
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
