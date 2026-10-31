"""
credit.py — CEO 2026-10-24: the Claude credit report.

2026-10-23 the Anthropic balance ran out at 09:36 ET and the gatekeeper
blocked every signal for the rest of the day. This module makes the spend
visible before that happens again:

  * record(): every Anthropic call (gatekeeper, review, pre-flight, analysis
    tools) is journaled to journal.db table api_usage with its model, tokens
    and $ cost. Prices come from bot_config.json `anthropic_prices`
    ($ per million tokens). It never raises: a usage row must never cost a
    verdict.
  * status(): remaining = anthropic_credit.balance_usd - spend since
    anthropic_credit.as_of; today's spend; 7-day average $/day; days left.
    The CEO sets balance_usd/as_of from the owner's figure.
  * lines(): the two report lines, used by Discord (session start, 16:20 ET)
    and by the top of the dashboard.
  * alerts(): ⚠️ when remaining < 7 days of the 7-day average, or when
    credit_preflight failed.

    python credit.py              # print + post the two lines (16:20 job)
    python credit.py --no-post    # print only
"""

import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta

import pytz

logger = logging.getLogger(__name__)

ET = pytz.timezone("US/Eastern")
CONFIG_FILE = "bot_config.json"


def _db() -> str:
    """journal.db, through journal.DB_FILE so tests' isolation applies."""
    import journal
    return journal.DB_FILE
AVG_DAYS = 7
LOW_DAYS = 7
TOPUP_REMIND_DAYS = 30           # CEO 2026-10-31: no balance increase in 30 days -> remind

SCHEMA = """
    CREATE TABLE IF NOT EXISTS api_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        date TEXT NOT NULL,
        purpose TEXT NOT NULL,
        model TEXT NOT NULL,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cache_write_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        priced INTEGER NOT NULL DEFAULT 1
    )
"""


def _config(config=None) -> dict:
    if config is not None:
        return config
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def ensure_table(conn):
    conn.execute(SCHEMA)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_api_usage_date ON api_usage (date)")


# ================================================================ pricing

def price_for(model: str, prices: dict):
    """$/MTok row for a model: exact name, else the longest configured name
    the model starts with (a dated id like claude-sonnet-5-20260801). None
    when the model is not in the table."""
    rows = {k: v for k, v in (prices or {}).items() if isinstance(v, dict)}
    if model in rows:
        return rows[model]
    hits = [k for k in rows if str(model).startswith(k)]
    return rows[max(hits, key=len)] if hits else None


def usage_of(response) -> dict:
    """Token counts from an SDK response's .usage (missing fields are 0)."""
    u = getattr(response, "usage", None)
    get = (u.get if isinstance(u, dict) else lambda k, d=0: getattr(u, k, d))
    return {
        "input_tokens": int(get("input_tokens", 0) or 0),
        "output_tokens": int(get("output_tokens", 0) or 0),
        "cache_write_tokens": int(get("cache_creation_input_tokens", 0) or 0),
        "cache_read_tokens": int(get("cache_read_input_tokens", 0) or 0),
    }


def cost_usd(model: str, usage: dict, prices: dict):
    """(cost, priced). An unknown model is priced at the DEAREST configured
    row (priced=False) so the credit estimate errs towards running out
    early, never late."""
    row = price_for(model, prices)
    priced = row is not None
    if row is None:
        rows = [v for v in (prices or {}).values() if isinstance(v, dict)]
        if not rows:
            return 0.0, False
        row = max(rows, key=lambda r: float(r.get("output", 0)))
    c = (usage.get("input_tokens", 0) * float(row.get("input", 0))
         + usage.get("output_tokens", 0) * float(row.get("output", 0))
         + usage.get("cache_write_tokens", 0) * float(row.get("cache_write", row.get("input", 0)))
         + usage.get("cache_read_tokens", 0) * float(row.get("cache_read", 0))) / 1e6
    return round(c, 6), priced


# ================================================================ record

def record(purpose: str, model: str, response=None, usage: dict = None,
           config: dict = None, db: str = None, now: datetime = None):
    """Journal one billed call. Returns its cost, or None if it could not be
    written. Never raises."""
    try:
        usage = usage if usage is not None else usage_of(response)
        cost, priced = cost_usd(model, usage, _config(config).get("anthropic_prices"))
        now = (now or datetime.now(ET)).astimezone(ET)
        conn = sqlite3.connect(db or _db(), timeout=15)
        try:
            ensure_table(conn)
            conn.execute(
                "INSERT INTO api_usage (timestamp, date, purpose, model, "
                "input_tokens, output_tokens, cache_write_tokens, "
                "cache_read_tokens, cost_usd, priced) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d"),
                 purpose, str(model), usage["input_tokens"], usage["output_tokens"],
                 usage["cache_write_tokens"], usage["cache_read_tokens"],
                 cost, int(priced)))
            conn.commit()
        finally:
            conn.close()
        if not priced:
            logger.warning(f"api_usage: no price for model {model!r} - "
                           f"costed at the dearest configured row")
        return cost
    except Exception as e:
        logger.error(f"api_usage: could not record {purpose} call: {e}")
        return None


# ================================================================ status

def _sum(conn, where: str, args) -> float:
    return float(conn.execute(f"SELECT COALESCE(SUM(cost_usd), 0) FROM api_usage "
                              f"WHERE {where}", args).fetchone()[0])


def status(config: dict = None, db: str = None, now: datetime = None) -> dict:
    """{balance, as_of, spent_since, remaining, today, avg7, days_left}.
    remaining/days_left are None when no balance is configured; days_left is
    None when nothing has been spent in the last 7 days."""
    cfg = _config(config)
    now = (now or datetime.now(ET)).astimezone(ET)
    credit = cfg.get("anthropic_credit") or {}
    balance = credit.get("balance_usd")
    as_of = str(credit.get("as_of") or "")
    today = now.strftime("%Y-%m-%d")
    start7 = (now - timedelta(days=AVG_DAYS - 1)).strftime("%Y-%m-%d")
    out = {"balance": balance, "as_of": as_of, "spent_since": 0.0,
           "remaining": None, "today": 0.0, "avg7": 0.0, "days_left": None,
           "topup_age_days": None}
    try:
        out["topup_age_days"] = (now.date() - datetime.fromisoformat(as_of[:10]).date()).days
    except (TypeError, ValueError):
        pass
    try:
        conn = sqlite3.connect(db or _db(), timeout=15)
        try:
            ensure_table(conn)
            out["today"] = _sum(conn, "date = ?", (today,))
            out["avg7"] = _sum(conn, "date >= ? AND date <= ?", (start7, today)) / AVG_DAYS
            if balance is not None and as_of:
                # as_of may be a date ("2026-10-24") or a timestamp; a bare
                # date means "the balance at the start of that day".
                key = as_of if len(as_of) > 10 else as_of + " 00:00:00"
                out["spent_since"] = _sum(conn, "timestamp >= ?", (key.replace("T", " "),))
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"api_usage: could not read spend: {e}")
    if balance is not None:
        out["remaining"] = float(balance) - out["spent_since"]
        if out["avg7"] > 0:
            out["days_left"] = max(0.0, out["remaining"]) / out["avg7"]
    return out


# ================================================================ text

def credit_line(s: dict) -> str:
    left = (f"~${s['remaining']:,.2f} left" if s["remaining"] is not None
            else "balance not set (anthropic_credit)")
    days = (f"~{s['days_left']:,.0f} days" if s["days_left"] is not None
            else "~n/a days")
    return (f"💳 Claude credit: {left} · today ${s['today']:,.2f} · "
            f"7-day avg ${s['avg7']:,.2f}/day → {days}")


def account_line(broker) -> str:
    try:
        a = broker.trading.get_account()
        return (f"💵 Account: equity ${float(a.equity):,.2f} · cash "
                f"${float(a.cash):,.2f} · buying power ${float(a.buying_power):,.2f}")
    except Exception as e:
        return f"💵 Account: unavailable ({type(e).__name__})"


def lines(broker, config: dict = None, db: str = None, now: datetime = None,
          s: dict = None) -> list:
    s = s or status(config, db, now)
    return [credit_line(s), account_line(broker)]


def alerts(s: dict, preflight: dict = None) -> list:
    """⚠️ messages: low credit (remaining < 7 days of the 7-day average, or
    used up) and a failed credit pre-flight."""
    out = []
    rem = s.get("remaining")
    if rem is not None and (rem <= 0 or (s.get("avg7", 0) > 0
                                         and rem < LOW_DAYS * s["avg7"])):
        days = s.get("days_left")
        out.append(f"⚠️ Claude credit low: ~${max(rem, 0):,.2f} left"
                   + (f" ≈ {days:,.1f} days at ${s['avg7']:,.2f}/day" if days is not None else "")
                   + " — top up the Anthropic API balance, or the gatekeeper "
                     "blocks every signal when it runs out.")
    age = s.get("topup_age_days")
    if age is not None and age >= TOPUP_REMIND_DAYS:
        out.append(f"⚠️ No Anthropic top-up recorded in {age} days (balance set {s['as_of'][:10]}) - "
                   f"check the console; send the new balance when you top up.")
    if preflight is not None and not preflight.get("ok"):
        out.append(f"⚠️ Claude credit pre-flight FAILED ({preflight.get('reason')}): "
                   f"{str(preflight.get('detail') or '')[:160]} — the gatekeeper "
                   f"cannot approve trades until this is fixed.")
    return out


def report(broker, preflight: dict = None, config: dict = None, db: str = None,
           now: datetime = None) -> list:
    """The Discord messages: the two lines, then any ⚠️ alerts."""
    s = status(config, db, now)
    return ["\n".join(lines(broker, config, db, now, s=s))] + alerts(s, preflight)


def post(messages, config: dict = None):
    import alerts as _alerts
    import trade_alerts
    if not trade_alerts.enabled("credit_report", _config(config)):
        return 0
    for m in messages:
        _alerts.post_async(m)
    return len(messages)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    from dotenv import load_dotenv
    load_dotenv(".env")
    from broker import Broker
    import claude_integration
    preflight = claude_integration.credit_preflight()
    msgs = report(Broker(), preflight)
    print("\n".join(msgs))
    if "--no-post" not in argv:
        post(msgs)
        import alerts as _alerts
        _alerts.drain()
    return 0


if __name__ == "__main__":
    sys.exit(main())
