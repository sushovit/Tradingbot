"""
desk_config.py — small, dependency-free switches read from bot_config.json.

    intern_enabled()   bot_config.json "intern": {"enabled": ...}
                       CEO 2026-10-14: the intern desk is OFF. Default when
                       the key is missing is OFF too - an intern run must be
                       switched on deliberately, never by omission.
    in_worker()        True inside the trading worker process: run_worker
                       sets TRADINGBOT_WORKER=1 before it imports
                       streamlit_app, whose page code then runs in bare mode.
"""

import json
import os

CONFIG_FILE = "bot_config.json"
WORKER_ENV = "TRADINGBOT_WORKER"


def _config(config=None):
    if config is not None:
        return config
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def intern_enabled(config: dict = None) -> bool:
    return bool(((_config(config) or {}).get("intern") or {}).get("enabled", False))


def in_worker() -> bool:
    return os.environ.get(WORKER_ENV) == "1"
