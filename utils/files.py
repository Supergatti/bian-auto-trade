import json
import os
from config import DATA_DIR, logger


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_json(path, default_factory):
    try:
        _ensure_data_dir()
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default_factory()


def save_json(path, data):
    _ensure_data_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, default=str)
