"""CEO 2026-10-31 (agenda 38): replays start from the LIVE initial stop and
the RULE replay takes the live time stop and earnings exit."""

import sqlite3

import pandas as pd

import lock_replay
import position_mgmt as pm
import scoreboard
import trade_table


def _bars(day, rows, start="09:30"):
    idx = pd.date_range(f"{day} {start}", periods=len(rows), freq="5min", tz="America/New_York")
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])


def _journal(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "j.db"))
    conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, timestamp TEXT, ticker TEXT, action TEXT, "
                 "qty REAL, price REAL, pnl_usd REAL, reason TEXT, decision_id INTEGER, "
                 "exit_target REAL, exit_stop REAL)")
    conn.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY, timestamp TEXT, ticker TEXT, "
                 "setup_name TEXT, source TEXT, verdict TEXT, context TEXT)")
    conn.execute("INSERT INTO decisions VALUES (1, '2026-10-28 09:33:00', 'TXN', 'momentum_continuation', "
                 "'rules', '{}', '{\"stop\": 293.77, \"target\": 349.31}')")
    conn.execute("INSERT INTO decisions VALUES (2, '2026-10-28 09:34:53', 'TXN', 'stop_reanchored', 'ops', "
                 "'{\"reasoning\": \"TXN: initial stop re-anchored to the fill 308.8: 293.77 -> 294.92 "
                 "(same 1.5-2.5 ATR clamp, same shares)\"}', '')")
    conn.execute("INSERT INTO trades VALUES (52, '2026-10-28 09:33:54', 'TXN', 'BUY', 3, 308.8, 0, "
                 "'momentum_continuation', 1, NULL, NULL)")
    conn.commit()
    return conn


def test_replay_starts_from_the_reanchored_live_stop(tmp_path):
    (t,) = trade_table.load_trades(_journal(tmp_path))
    assert (t["signal_stop"], t["orig_stop"]) == (293.77, 294.92)


def test_the_reanchor_message_format_is_the_one_position_mgmt_writes():
    src = open("position_mgmt.py", encoding="utf-8").read()
    assert 'initial stop re-anchored to the fill' in src
    m = trade_table._REANCHOR.search("TXN: initial stop re-anchored to the fill 308.8: 293.77 -> 294.92 (x)")
    assert m and float(m.group(2)) == 294.92


def test_txn_1030_the_live_stop_hits_on_the_bar_low_the_signal_stop_does_not():
    # IEX 10-30 14:35 bar low 294.825: below the live 294.92, above the signal 293.77
    b = pd.concat([_bars("2026-10-28", [(308.8, 309.5, 307.0, 308.0)] * 3, start="09:35"),
                   _bars("2026-10-30", [(295.72, 295.72, 295.07, 295.21),
                                        (294.84, 295.62, 294.825, 295.62)], start="14:30")])
    params = pm.r_atr_params({})
    live = lock_replay.replay_r_atr(b, "2026-10-28 09:33:54", 308.8, 294.92, 322.68, params, 9.25)
    sig = lock_replay.replay_r_atr(b, "2026-10-28 09:33:54", 308.8, 293.77, 322.68, params, 9.25)
    # the 14:35 bar OPENED below the stop (294.84): fill at min(stop, open) = the open
    assert live[0] == 294.84 and live[2] == "stop (gap)" and str(live[1]).startswith("2026-10-30 14:35")
    assert sig[1] is None                                          # "still holding"


def _days(n, px=100.0, start="2026-10-01"):
    out = []
    for d in pd.bdate_range(start, periods=n):
        out.append(_bars(d.date(), [(px, px + 0.2, px - 0.2, px)] * 78))
    return pd.concat(out)


def test_rule_replay_takes_the_day_5_time_stop_like_live():
    b = _days(6)
    params, xp = pm.r_atr_params({}), pm.daily_exit_params({})
    px, ts, why = lock_replay.replay_r_atr(b, "2026-10-01 09:31:00", 100.0, 95.0, 115.0, params,
                                           2.0, exit_params=xp)
    assert why.startswith("Time stop (day 5") and ts.strftime("%Y-%m-%d %H:%M") == "2026-10-07 15:55"
    # without exit_params the old behaviour (still open) is unchanged
    assert lock_replay.replay_r_atr(b, "2026-10-01 09:31:00", 100.0, 95.0, 115.0, params, 2.0)[1] is None


def test_rule_replay_takes_the_earnings_exit_and_ignores_an_unknown_calendar():
    b = _days(3)
    xp = pm.daily_exit_params({})
    rp = pm.r_atr_params({})
    soon = {pd.Timestamp("2026-10-02").date(): True}
    px, ts, why = lock_replay.replay_r_atr(b, "2026-10-01 09:31:00", 100.0, 95.0, 115.0, rp, 2.0,
                                           exit_params=xp, earnings_soon=lambda d: soon.get(d, False))
    assert why.startswith("Earnings exit") and ts.strftime("%Y-%m-%d %H:%M") == "2026-10-02 15:50"
    unknown = lock_replay.replay_r_atr(b, "2026-10-01 09:31:00", 100.0, 95.0, 115.0, rp, 2.0,
                                       exit_params=xp, earnings_soon=lambda d: None)
    assert unknown[1] is None


def test_shadow_no_counts_toward_20():
    split = {"no": {"n": 4, "pnl": -75.61}, "yes": {"n": 1, "pnl": 3.0}, "none": {"n": 0, "pnl": 0}}
    assert "  Shadow NO: 4/20 toward review" in scoreboard.shadow_lines(split)
    split["no"]["n"] = 20
    assert "  Shadow NO review: DUE (20 trades)" in scoreboard.shadow_lines(split)


def test_no_replay_triggers_stops_on_closes():
    """Agenda 38 audit: every replay fires its stop on the bar LOW."""
    import re
    for f in ("lock_replay.py", "study_exit_grid.py", "study_common.py", "backtest.py",
              "outcomes.py", "study_adx_band.py", "study_trade_funnel.py", "study_atr_early_step.py"):
        src = open(f, encoding="utf-8").read()
        assert not re.search(r"\b(c|close)\s*<=?\s*(live_stop|live|stop)\b", src), f
