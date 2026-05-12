from flask import Blueprint, jsonify

from services.data_store import load_favorites, save_favorites

fav_bp = Blueprint("favorites", __name__)


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
