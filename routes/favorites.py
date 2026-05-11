import json
from flask import Blueprint, jsonify

from config import TOP_HOT_PAIRS, FAVORITES_FILE, TRADE_PAIRS_FILE
from utils.files import load_json, save_json

fav_bp = Blueprint("favorites", __name__)


def load_favorites():
    return load_json(FAVORITES_FILE, list)


def save_favorites(favs):
    save_json(FAVORITES_FILE, favs)


def load_trade_pairs():
    pairs = load_json(TRADE_PAIRS_FILE, lambda: ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"])
    return [p.upper().strip() for p in pairs if isinstance(p, str)]


@fav_bp.route("/api/favorites")
def favorites():
    return jsonify(load_favorites())


@fav_bp.route("/api/favorites/toggle", methods=["POST"])
def toggle_favorite():
    from flask import request
    data = request.get_json(silent=True) or {}
    symbol = data.get("symbol", "").upper().strip()
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400
    favs = load_favorites()
    action = "removed" if symbol in favs else "added"
    if symbol in favs:
        favs.remove(symbol)
    else:
        favs.append(symbol)
    save_favorites(favs)
    return jsonify({"action": action, "symbol": symbol, "favorites": favs})
