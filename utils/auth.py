from flask import request, jsonify
from config import ACCESS_TOKEN

EXCLUDED_PATHS = ["/", "/static/"]


def check_auth():
    if not ACCESS_TOKEN:
        return None
    path = request.path
    for excluded in EXCLUDED_PATHS:
        if path.startswith(excluded):
            return None
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {ACCESS_TOKEN}":
        return jsonify({"error": "Unauthorized"}), 401
    return None
