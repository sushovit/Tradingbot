"""
sell_pin.py — CEO 2026-10-23: the dashboard's SELL needs a PIN, because the
dashboard is reachable from any device on the home network (the owner's
phone: http://<mac>:8501).

  * The PIN is stored only as a salted SHA-256 hash in .env
    (SELL_PIN_HASH = "<salt>$<hex>"), never in git.
  * No hash configured -> SELL is disabled (fail closed).
  * 5 wrong PINs lock SELL for 15 minutes. The counter lives in a small
    state file, so reloading the page or using another device does not
    reset it.
"""

import hashlib
import hmac
import json
import os
import secrets
import time

ENV_KEY = "SELL_PIN_HASH"
STATE_FILE = "sell_pin_state.json"
MAX_TRIES = 5
LOCK_S = 15 * 60


def make_hash(pin: str, salt: str = None) -> str:
    salt = salt or secrets.token_hex(8)
    return f"{salt}${hashlib.sha256((salt + str(pin)).encode()).hexdigest()}"


def configured(env=os.environ) -> bool:
    return "$" in (env.get(ENV_KEY) or "")


def _state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"fails": 0, "locked_until": 0}


def _save(path, st):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, path)


def locked_for(path=STATE_FILE, now=None) -> int:
    """Seconds left on the lockout (0 = not locked)."""
    now = now if now is not None else time.time()
    return max(0, int(_state(path).get("locked_until", 0) - now))


def check(pin: str, env=os.environ, path=STATE_FILE, now=None):
    """(ok: bool, message: str). Counts failures and locks after MAX_TRIES."""
    now = now if now is not None else time.time()
    if not configured(env):
        return False, "Selling is disabled: no PIN is set up."
    left = locked_for(path, now)
    if left:
        return False, f"Too many wrong PINs. Selling is locked for {left // 60 + 1} more min."
    salt, digest = env[ENV_KEY].split("$", 1)
    ok = hmac.compare_digest(make_hash(pin or "", salt).split("$", 1)[1], digest)
    st = _state(path)
    if ok:
        _save(path, {"fails": 0, "locked_until": 0})
        return True, ""
    st["fails"] = int(st.get("fails", 0)) + 1
    if st["fails"] >= MAX_TRIES:
        st = {"fails": 0, "locked_until": now + LOCK_S}
        _save(path, st)
        return False, "Wrong PIN. Selling is now locked for 15 minutes."
    _save(path, st)
    return False, f"Wrong PIN ({MAX_TRIES - st['fails']} tries left)."
