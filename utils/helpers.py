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
