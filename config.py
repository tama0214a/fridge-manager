"""設定ファイル (data/config.json) の読み書き。"""
from __future__ import annotations

import json
import threading

from db import DATA_DIR

CONFIG_PATH = DATA_DIR / "config.json"

DEFAULTS: dict = {
    "host": "0.0.0.0",
    "port": 8341,
    "default_storage_days": 30,
    "warn_days": 7,
    "notify_enabled": False,
    "notify_time": "09:00",
    "admin_email": "",
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_security": "starttls",
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
}

_lock = threading.Lock()


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            for key in DEFAULTS:
                if key in stored:
                    cfg[key] = stored[key]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    data = {key: cfg.get(key, DEFAULTS[key]) for key in DEFAULTS}
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(CONFIG_PATH)
