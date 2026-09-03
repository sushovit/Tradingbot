"""
regime_audit.py — how many regime blocks would the CORRECTED SPY filter have
let through?

    python regime_audit.py [--since 2026-07-01] [--refresh]

Background. Until 2026-09-02 the live SPY filter computed a 20-period EMA of
FIVE-MINUTE SPY bars (a ~100-minute average) and blocked entries when SPY sat
below it. The backtest that justified the filter used the 20-DAY EMA of daily
closes (backtest.spy_regime_series). Those are different indicators, so the
regime the desk enforced was never the regime the evidence was measured on.

This replays every journaled `spy_bearish` rejection against the CORRECTED
definition — 20-day EMA, last COMPLETED session — and reports how many would
not have been blocked.

READ-ONLY: it reads journal.db and cached/fetched SPY daily bars. It places
no orders and writes nothing but its own stdout.

THE HEADLINE NUMBER IS AN UPPER BOUND, not a count of lost trades. A flipped
rejection only means the regime gate would have passed it; the signal would
still have had to clear the AI gatekeeper, the R:R and sizing rules, the
max-positions cap, the daily-loss breaker and the whole-share floor. Several
of those reject most of what reaches them.
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime

import pytz

import daily_eval

EASTERN_TZ = pytz.timezone("US/Eastern")
DB_FILE = "journal.db"


def load_rejections(db_file: str = DB_FILE, since: str = None,
                    reason: str = "spy_bearish") -> list:
    """Every journaled regime block: [{date, ticker, setup, details}]."""
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    sql = ("SELECT timestamp, ticker, setup_name, context FROM decisions "
           "WHERE source='rules' "
           "AND json_extract(verdict,'$.rejection_reason')=?")
    params = [reason]
    if since:
        sql += " AND substr(timestamp,1,10) >= ?"
        params.append(since)
    sql += " ORDER BY timestamp"
    rows = []
    for r in conn.execute(sql, params):
        try:
            details = json.loads(r["context"] or "{}").get("details", "")
        except (ValueError, TypeError):
            details = ""
        rows.append({"date": str(r["timestamp"])[:10], "ticker": r["ticker"],
                     "setup": r["setup_name"], "details": details})
    conn.close()
    return rows


def load_chop_tags(db_file: str = DB_FILE, since: str = None) -> list:
    """chop_reclaim tags — entries the exemption let through in 'chop'."""
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    sql = ("SELECT timestamp, ticker, setup_name FROM decisions "
           "WHERE json_extract(verdict,'$.tag')='chop_reclaim'")
    params = []
    if since:
        sql += " AND substr(timestamp,1,10) >= ?"
        params.append(since)
    rows = [{"date": str(r["timestamp"])[:10], "ticker": r["ticker"],
             "setup": r["setup_name"]}
            for r in conn.execute(sql, params)]
    conn.close()
    return rows


def spy_regime_by_date(spy_daily, dates) -> dict:
    """{session date -> regime dict} using the CORRECTED definition.

    For a decision made on date D, the regime in force is the one computed
    from the last session COMPLETED before D — exactly what the live code
    now does, so the audit and the desk agree by construction."""
    out = {}
    for d in sorted(set(dates)):
        # Bars strictly up to and including D; spy_regime then drops D itself
        # when D is present as a partial/current bar.
        window = spy_daily[spy_daily.index.astype(str).str[:10] <= d]
        out[d] = daily_eval.spy_regime(window, today_str=d)
    return out


def audit(rejections, chop_tags, regimes) -> dict:
    flipped, upheld, unknown = [], [], []
    for row in rejections:
        regime = regimes.get(row["date"])
        if regime is None:
            unknown.append(row)
        elif regime["trending"]:
            flipped.append(dict(row, **{"spy_close": regime["spy_close"],
                                        "ema20d": regime["ema20d"],
                                        "as_of": regime["as_of"]}))
        else:
            upheld.append(row)

    chop_flipped = [t for t in chop_tags
                    if (regimes.get(t["date"]) or {}).get("trending")]
    n = len(rejections)
    return {
        "rejections": n,
        "flipped": flipped,
        "upheld": len(upheld),
        "unknown": len(unknown),
        "flip_pct": round(100.0 * len(flipped) / n, 1) if n else None,
        "chop_tags": len(chop_tags),
        "chop_tags_actually_trending": len(chop_flipped),
        "by_setup": _by_key(flipped, "setup"),
        "by_date": _by_key(flipped, "date"),
    }


def _by_key(rows, key) -> dict:
    out = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def render(result: dict, since: str) -> str:
    L = []
    L.append(f"Regime audit — journaled `spy_bearish` blocks since "
             f"{since or 'the beginning'}")
    L.append("")
    L.append(f"- Regime blocks journaled: **{result['rejections']}**")
    L.append(f"- Would NOT have been blocked under the corrected 20-day "
             f"definition: **{len(result['flipped'])}** "
             f"({result['flip_pct']}%)")
    L.append(f"- Still blocked: {result['upheld']}")
    if result["unknown"]:
        L.append(f"- Indeterminate (no SPY history for that date): "
                 f"{result['unknown']}")
    L.append(f"- chop_reclaim exemptions taken: {result['chop_tags']}, of "
             f"which {result['chop_tags_actually_trending']} were actually "
             f"in a TRENDING market — the exemption was not needed for those.")
    if result["by_setup"]:
        L.append("")
        L.append("Flipped blocks by setup: " +
                 ", ".join(f"{k} {v}" for k, v in result["by_setup"].items()))
        L.append("Flipped blocks by date: " +
                 ", ".join(f"{k} {v}" for k, v in result["by_date"].items()))
    if result["flipped"]:
        L.append("")
        L.append("| Date | Ticker | Setup | SPY close | 20d EMA | as_of |")
        L.append("|---|---|---|---|---|---|")
        for r in result["flipped"]:
            L.append(f"| {r['date']} | {r['ticker']} | {r['setup']} "
                     f"| {r['spy_close']:.2f} | {r['ema20d']:.2f} "
                     f"| {r['as_of']} |")
    L.append("")
    L.append("**These are an UPPER BOUND on lost entries, not lost trades.** "
             "A flipped row only means the regime gate would have passed it. "
             "Each would still have had to clear the AI gatekeeper, the "
             "R:R >= 1.5 and notional rules, whole-share sizing on a $2,000 "
             "cap, the max-positions cap and the daily-loss breaker. On "
             "recent evidence those reject the large majority of what reaches "
             "them, so the realised number would be materially smaller.")
    return "\n".join(L)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--since", default=None,
                   help="only audit rows on/after this date (YYYY-MM-DD)")
    p.add_argument("--refresh", action="store_true",
                   help="refetch SPY daily bars instead of using the cache")
    args = p.parse_args()

    rejections = load_rejections(since=args.since)
    chop_tags = load_chop_tags(since=args.since)
    if not rejections and not chop_tags:
        print("No regime-gated rows journaled — nothing to audit.")
        return 0

    dates = [r["date"] for r in rejections] + [t["date"] for t in chop_tags]
    import backtest
    spy = backtest.load_daily("SPY", years=3, refresh=args.refresh)
    if spy is None or len(spy) < 25:
        print("SPY daily history unavailable — cannot audit.")
        return 1

    regimes = spy_regime_by_date(spy, dates)
    result = audit(rejections, chop_tags, regimes)
    print(render(result, args.since))
    return 0


if __name__ == "__main__":
    sys.exit(main())
