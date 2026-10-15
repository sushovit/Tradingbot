"""
dashboard_data — the exact numbers the dashboard shows. Read-only helpers.
"""

import sqlite3
from types import SimpleNamespace as NS

import dashboard_data as dd

LADDER = {"daily_trailing": "ladder",
          "daily_lock": {"start_pct": 0.5, "cushion_pct": 0.5}}
CONFIG = {"ticker_profiles": {}, "risk_profiles": {"Moderate": LADDER}}


def pos(symbol="TXN", qty="4", entry="280.25", last="279.59"):
    return NS(symbol=symbol, qty=qty, avg_entry_price=entry, current_price=last)


def leg(symbol, kind, price, side="OrderSide.SELL"):
    return NS(symbol=symbol, side=side, order_type=f"OrderType.{kind}",
              stop_price=price if kind == "STOP" else None,
              limit_price=price if kind == "LIMIT" else None)


LOCAL = {"TXN": {"in_position": True, "source": "bot", "timeframe": "daily",
                 "setup": "mean_reversion_reclaim", "entry_price": 282.815,
                 "initial_stop": 278.0, "trailing_stop_price": 278.0,
                 "profit_target_price": 298.69}}


def test_txn_row_uses_the_broker_fill_and_the_live_stop():
    rows = dd.position_rows([pos()], LOCAL,
                            [leg("TXN", "STOP", "278"), leg("TXN", "LIMIT", "298.69")],
                            CONFIG)
    r = rows[0]
    assert r["Entry (fill)"] == 280.25                   # not the 282.815 record
    assert r["Stop (live)"] == 278.0 and r["Stop is"] == "original"
    assert round(r["P&L $"], 2) == -2.64
    assert round(r["If stopped $"], 2) == -9.0           # 4 x (278 - 280.25)
    assert round(r["R now"], 3) == -0.293
    assert r["Target"] == 298.69
    assert r["Next stop move"] == "Breakeven at a 5-min close ≥ $281.66"


def test_the_live_leg_wins_over_the_local_record():
    rows = dd.position_rows([pos(last="284.0")], LOCAL,
                            [leg("TXN", "STOP", "282.58")], CONFIG)
    r = rows[0]
    assert r["Stop (live)"] == 282.58 and r["Stop is"] == "locked profit"
    assert round(r["If stopped $"], 2) == round(4 * (282.58 - 280.25), 2)


def test_stop_states():
    def state(stop):
        return dd.position_rows([pos()], LOCAL, [leg("TXN", "STOP", stop)],
                                CONFIG)[0]["Stop is"]
    assert state("278") == "original"
    assert state("280.25") == "breakeven"
    assert state("279.13") == "raised (still below entry)"
    assert state("281") == "locked profit"
    assert dd.position_rows([pos()], {}, [], CONFIG)[0]["Stop is"] == "no stop visible"


def test_next_trigger_once_trailing():
    assert dd.next_trigger(280.25, 278.0, 280.25, 284.0, LADDER).startswith(
        "Trailing: stop follows the high close (now $284.00) at −0.5% → $282.58")


def test_intraday_rows_say_so():
    local = {"X": dict(LOCAL["TXN"], timeframe="intraday")}
    r = dd.position_rows([pos("X")], local, [], CONFIG)[0]
    assert r["Next stop move"] == "intraday ATR trail"


def test_status_summary_counts_a_cycle_line():
    line = ("[2026-10-14 10:32:42] Cycle #76 | Monitoring Live (Alpaca paper): "
            "TXN: In Pos [bot] (+1.13%) | NVDA: Waiting (No Signal) | "
            "AMD: Waiting (No Signal) | CAT: Pass (momentum_continuation: x) | "
            "MU: Gatekeeper blocked (conviction=62) | WBD: No Data")
    s = dd.status_summary(line)
    assert s["cycle"] == 76 and s["time"] == "2026-10-14 10:32:42"
    assert s["counts"] == {"In position": 1, "Waiting": 2, "Passed (filter)": 1,
                           "Blocked by gatekeeper": 1, "No data": 1}


def test_status_summary_plain_line():
    s = dd.status_summary("[2026-10-14 16:15:02] Session ended 16:15 ET")
    assert s["cycle"] is None and s["headline"] == "Session ended 16:15 ET"


def test_decisions_mark_approvals_below_threshold_as_not_traded():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY, timestamp TEXT,"
                 " ticker TEXT, setup_name TEXT, source TEXT, approved INTEGER,"
                 " conviction_score INTEGER, verdict TEXT)")
    rows = [("2026-10-14 09:31:43", "TXN", 1, 72, '{"reasoning": "RSI turned up"}'),
            ("2026-10-14 09:33:30", "META", 1, 58, '{"reasoning": "met"}'),
            ("2026-10-14 09:34:43", "PSX", 0, 25, '{"rejection_reason": "RSI 81.5"}'),
            ("2026-10-14 09:35:00", "OXY", 0, None, '{"error": "timeout"}'),
            ("2026-10-13 10:00:00", "OLD", 1, 90, '{}')]
    for ts, t, ok, conv, v in rows:
        conn.execute("INSERT INTO decisions (timestamp, ticker, setup_name, "
                     "source, approved, conviction_score, verdict) VALUES "
                     "(?,?,?,?,?,?,?)", (ts, t, "s", "claude", ok, conv, v))
    out = {r["Ticker"]: r for r in dd.decision_rows(conn, "2026-10-14")}
    assert set(out) == {"TXN", "META", "PSX", "OXY"}
    assert out["TXN"]["Verdict"] == "approved"
    assert out["META"]["Verdict"] == "approved, below 70 — not traded"
    assert out["PSX"]["Verdict"] == "rejected" and out["PSX"]["Why"] == "RSI 81.5"
    assert out["OXY"]["Verdict"] == "error"


def test_desk_status_reads_lock_and_heartbeat(tmp_path):
    lock, status = tmp_path / "bot.run", tmp_path / "bot_status.log"
    lock.write_text("4242")
    status.write_text("[2026-10-14 10:32:42] Cycle #76 | Monitoring Live: "
                      "A: Waiting (No Signal)\n")
    d = dd.desk_status(str(lock), str(status))
    assert d["running"] and d["pid"] == 4242 and d["cycle"] == 76
    assert d["heartbeat_age"] is not None and d["heartbeat_age"] < 60
    assert dd.desk_status(str(tmp_path / "none"), str(status))["running"] is False
