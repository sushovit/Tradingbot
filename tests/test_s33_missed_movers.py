"""
S33 — missed_movers.py: the day's top universe gainers and why each was not
traded. Fake fixtures only: a throwaway journal, a fake universe file and a
fake bot_status.log in tmp_path. No webhook URL, no network.
"""

import json
import os
import plistlib
import socket
import sqlite3

import pytest

import missed_movers as mm
import trade_table

DAY = "2026-10-16"


# ============================================================ fixtures

def _universe(root, moves, day=DAY, at="15:01:53"):
    payload = {"generated_at": f"{day}T{at}.000000-04:00", "date": day,
               "candidates": [{"symbol": s, "price": 10.0, "change_pct": c,
                               "source": "movers"} for s, c in moves.items()]}
    (root / "universe_today.json").write_text(json.dumps(payload))


def _status(root, entries, day=DAY):
    body = " | ".join(f"{t}: {s}" for t, s in entries.items())
    (root / "bot_status.log").write_text(
        f"[{day} 16:15:12] Session ended 16:15 ET\n"
        f"[{day} 15:59:38] Cycle #296 | Monitoring Live (Alpaca paper): "
        f"{body}\n")


def _decision(db, ticker, setup, source, approved=0, conviction=None,
              context=None, verdict=None, ts=f"{DAY} 10:04:01"):
    with sqlite3.connect(db) as c:
        cur = c.execute(
            "INSERT INTO decisions (timestamp, ticker, setup_name, context, "
            "verdict, approved, conviction_score, source) VALUES "
            "(?,?,?,?,?,?,?,?)",
            (ts, ticker, setup, json.dumps(context or {}),
             json.dumps(verdict or {}), approved, conviction, source))
        return cur.lastrowid


def _trade(db, ticker, action, price, decision_id, ts, setup="momentum_continuation"):
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO trades (timestamp, ticker, action, qty, price, "
                  "pnl_usd, pnl_pct, reason, decision_id) VALUES "
                  "(?,?,?,?,?,?,?,?,?)",
                  (ts, ticker, action, 1, price, 0, 0, setup, decision_id))


@pytest.fixture
def desk(tmp_path, temp_journal):
    """A day: 12 universe names, every funnel outcome represented."""
    db = temp_journal.DB_FILE
    moves = {"AAA": 9.5, "BBB": 8.0, "CCC": 7.25, "DDD": 6.0, "EEE": 5.5,
             "FFF": 5.0, "GGG": 4.0, "HHH": 3.0, "III": 2.0, "JJJ": 1.0,
             "KKK": 0.5, "LLL": -4.0}
    _universe(tmp_path, moves)
    _status(tmp_path, {t: "Waiting (No Signal)" for t in moves})
    # AAA: rules filter (adx_low)
    _decision(db, "AAA", "trend_continuation", "rules",
              context={"details": "ADX 19.2 < 30"},
              verdict={"approved": False, "rejection_reason": "adx_low",
                       "source": "rules"})
    # BBB: rules filter first, then gatekeeper -> furthest = gatekeeper
    _decision(db, "BBB", "mean_reversion_reclaim", "rules",
              verdict={"approved": False, "rejection_reason": "volume_low"})
    _decision(db, "BBB", "momentum_continuation", "claude", approved=0,
              conviction=25, ts=f"{DAY} 10:07:00",
              context={"entry": 10, "stop": 9, "target": 13},
              verdict={"approved": False, "conviction_score": 25,
                       "rejection_reason": "RSI is 81, overextended"})
    # CCC: traded
    did = _decision(db, "CCC", "momentum_continuation", "claude", approved=1,
                    conviction=78, context={"entry": 20, "stop": 19,
                                            "target": 23},
                    verdict={"approved": True, "conviction_score": 78})
    _trade(db, "CCC", "BUY", 20.05, did, f"{DAY} 10:05:00")
    # DDD: nothing journaled -> no signal (cycle said Waiting)
    # EEE: held from an earlier day
    _trade(db, "EEE", "BUY", 50.0, None, "2026-10-14 09:31:00",
           setup="mean_reversion_reclaim")
    # FFF: another day's row only -> no signal on DAY
    _decision(db, "FFF", "trend_continuation", "rules", ts="2026-10-15 10:00:00",
              verdict={"approved": False, "rejection_reason": "adx_low"})
    # GGG: approved below the conviction threshold -> gatekeeper
    _decision(db, "GGG", "mean_reversion_reclaim", "claude", approved=1,
              conviction=64, verdict={"approved": True, "conviction_score": 64})
    return tmp_path


def _report(root):
    return mm.build(DAY, str(root), config={"claude_conviction_threshold": 70})


def _by_ticker(report):
    return {r["ticker"]: r for r in report["rows"]}


# ============================================================ ranking

def test_top_ten_by_percent_change(desk):
    rows = _report(desk)["rows"]
    assert [r["ticker"] for r in rows] == ["AAA", "BBB", "CCC", "DDD", "EEE",
                                           "FFF", "GGG", "HHH", "III", "JJJ"]
    assert [r["pct"] for r in rows][:3] == [9.5, 8.0, 7.25]


def test_top_gainers_sorts_descending_and_caps():
    moves = {f"T{i}": float(i) for i in range(15)}
    top = mm.top_gainers(moves)
    assert len(top) == 10 and top[0] == ("T14", 14.0) and top[-1][0] == "T5"


def test_another_days_universe_file_is_not_used(tmp_path, temp_journal):
    _universe(tmp_path, {"AAA": 5.0}, day="2026-10-19")
    report = _report(tmp_path)
    assert report["source"] is None and report["rows"] == []
    assert "No local % change data" in "\n".join(mm.table_lines(report))


def test_past_day_falls_back_to_the_scan_report_universe_table(tmp_path,
                                                               temp_journal):
    (tmp_path / "reports").mkdir()
    table = ("# Universe — 342 scanned, 163 qualified, top 20 shown (19.5s)\n"
             "\n| # | Ticker | Price | Avg $ vol (20d) | Move % | Source |\n"
             "|---|---|---|---|---|---|\n"
             "| 1 | MU | $877.44 | $646M | -5.8% | movers |\n"
             "| 2 | BRK.B | $500.00 | $90M | +1.2% | movers |\n"
             "\nAll 163 qualifying names are scanned.\n# Floor status\n")
    (tmp_path / "reports" / f"scan_ET{DAY}_1000.md").write_text(
        table.replace("+1.2%", "+0.1%"))
    (tmp_path / "reports" / f"scan_ET{DAY}_1800.md").write_text(table)
    report = _report(tmp_path)
    assert report["source"]["partial"] is True
    assert "1800" in report["source"]["file"]           # latest scan wins
    assert [(r["ticker"], r["pct"]) for r in report["rows"]] == [
        ("BRK.B", 1.2), ("MU", -5.8)]


# ============================================================ stage / reason

def test_stage_and_reason_per_ticker(desk):
    rows = _by_ticker(_report(desk))
    assert (rows["AAA"]["stage"], rows["AAA"]["reason"]) == ("rules filter",
                                                             "adx_low")
    assert rows["AAA"]["details"] == "ADX 19.2 < 30"
    assert rows["BBB"]["stage"] == "gatekeeper"            # furthest stage
    assert rows["BBB"]["reason"] == "gk_rsi c25"
    assert rows["BBB"]["rows"] == 2
    assert rows["CCC"]["stage"] == "traded"
    assert rows["CCC"]["reason"] == "bought @ 20.05"
    assert (rows["DDD"]["stage"], rows["DDD"]["reason"]) == (
        "no signal", "no detector fired")
    assert rows["EEE"]["stage"] == "in position"
    assert rows["FFF"]["stage"] == "no signal"
    assert (rows["GGG"]["stage"], rows["GGG"]["reason"]) == ("gatekeeper",
                                                             "gk_conviction c64")


def test_stage_vocabulary_is_the_funnel_studys():
    import study_trade_funnel as funnel
    for key, stage in (("adx_low", "rules filter"),
                       ("volume_low", "rules filter"),
                       ("outside_hours", "entry window"),
                       ("size_zero", "size"),
                       ("spy_bearish", "regime (SPY chop)")):
        assert funnel.REASON_STAGE[key] == stage


@pytest.mark.parametrize("status,stage,reason", [
    ("Waiting (No Signal)", "no signal", "no detector fired"),
    ("Pass (trend_continuation: adx_low)", "rules filter", "adx_low"),
    ("Pass (momentum_continuation: change_too_small)", "rules filter",
     "change_too_small"),
    ("Pass (outside hours)", "entry window", "outside_hours"),
    ("Pass (SPY bearish)", "regime (SPY chop)", "spy_bearish"),
    ("Pass (size_zero: stop $26.00 vs budget $20.00)", "size", "size_zero"),
    ("Pass (probation_position_open)", "probation", "probation_position_open"),
    ("Gatekeeper blocked (RSI falling)", "gatekeeper",
     "Gatekeeper blocked (RSI falling)"),
    ("In Pos [bot] (-2.52%)", "in position", "In Pos [bot] (-2.52%)"),
    ("No Data", "no data", "no bars (last cycle)"),
])
def test_cycle_status_mapping(status, stage, reason):
    assert mm.stage_from_status(status) == (stage, reason)


def test_cycle_status_is_used_when_the_journal_is_silent(tmp_path,
                                                         temp_journal):
    _universe(tmp_path, {"AAA": 3.0, "BBB": 2.0, "CCC": 1.0, "DDD": 0.5})
    _status(tmp_path, {"AAA": "Gatekeeper blocked (cached this bar)",
                       "BBB": "In Pos [bot] (+1.10%)",
                       "CCC": "Pass (trend_continuation: adx_low)"})
    rows = _by_ticker(_report(tmp_path))
    assert rows["AAA"]["stage"] == "gatekeeper"
    assert rows["BBB"]["stage"] == "in position"
    assert rows["CCC"]["stage"] == "rules filter"
    assert rows["DDD"]["stage"] == "not monitored"   # absent from the cycle


def test_last_cycle_ignores_another_days_line(tmp_path):
    _status(tmp_path, {"AAA": "Waiting (No Signal)"}, day="2026-10-15")
    assert mm.last_cycle(str(tmp_path / "bot_status.log"), DAY) == ({}, None)


# ============================================================ outputs

def test_phone_table_fits_and_lists_every_row(desk):
    report = _report(desk)
    lines = mm.table_lines(report)
    assert all(len(line) <= trade_table.WIDTH for line in lines)
    assert any(line.startswith(" 1 AAA") and "rules filter" in line
               and "adx_low" in line for line in lines)
    assert any(" CCC " in line and "traded" in line for line in lines)
    msgs = mm.messages(report)
    assert msgs[0].startswith("**Missed movers — Fri 2026-10-16**\n```")
    assert all(len(m) <= trade_table.DISCORD_LIMIT for m in msgs)


def test_markdown_file(desk):
    report = _report(desk)
    path = mm.write_report(report, str(desk / "out"))
    assert path.endswith(f"missed_movers_{DAY}.md")
    text = open(path, encoding="utf-8").read()
    assert text.startswith(f"# Missed movers — {DAY}")
    assert "universe_today.json @ 15:01 ET" in text
    assert "| 1 | AAA | +9.5% | rules filter | adx_low | trend_continuation " \
           "| ADX 19.2 < 30 | Waiting (No Signal) | 1 |" in text
    assert "| 3 | CCC | +7.2% | traded |" in text
    assert "| 4 | DDD | +6.0% | no signal | no detector fired |" in text
    assert text.count("| traded |") == 1


def _no_network(monkeypatch):
    calls = []

    def refuse(*a, **k):
        calls.append(a)
        raise AssertionError("missed_movers made a network call")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    return calls


def test_post_goes_through_alerts_post_async(desk, monkeypatch,
                                             discord_posts):
    calls = _no_network(monkeypatch)
    n = mm.post(_report(desk))
    assert n == len(discord_posts) == 1 and calls == []
    payload = discord_posts[0]["payload"]
    assert isinstance(payload, str) and "Missed movers" in payload
    assert "AAA" in payload and "gk_rsi c25" in payload


def test_main_end_to_end_posts_and_drains(desk, monkeypatch, discord_posts):
    import journal
    calls = _no_network(monkeypatch)
    drained = []
    monkeypatch.setattr(__import__("alerts"), "drain",
                        lambda *a, **k: drained.append(1) or 0)
    # The journal fixture lives outside --root; link it in.
    os.symlink(journal.DB_FILE, desk / "journal.db")
    rc = mm.main(["--date", DAY, "--root", str(desk),
                  "--out", str(desk / "out")])
    assert rc == 0 and calls == [] and drained == [1]
    assert len(discord_posts) == 1
    assert "CCC" in discord_posts[0]["payload"]


def test_no_post_and_config_switch(desk, monkeypatch, discord_posts):
    import journal
    _no_network(monkeypatch)
    os.symlink(journal.DB_FILE, desk / "journal.db")
    args = ["--date", DAY, "--root", str(desk), "--out", str(desk / "out")]
    assert mm.main(args + ["--no-post"]) == 0
    assert discord_posts == []
    (desk / "bot_config.json").write_text(json.dumps(
        {"discord": {"missed_movers": False}}))
    assert mm.main(args) == 0
    assert discord_posts == []


def test_a_day_without_worker_activity_posts_nothing(tmp_path, monkeypatch,
                                                     temp_journal,
                                                     discord_posts):
    _no_network(monkeypatch)
    _universe(tmp_path, {"AAA": 3.0})
    os.symlink(temp_journal.DB_FILE, tmp_path / "journal.db")
    assert mm.main(["--date", DAY, "--root", str(tmp_path)]) == 0
    assert discord_posts == []
    assert os.path.exists(tmp_path / "reports" / f"missed_movers_{DAY}.md")


def test_the_module_imports_no_broker_or_model_client():
    src = open(mm.__file__, encoding="utf-8").read()
    for name in ("broker", "anthropic", "alpaca", "requests", "analyst",
                 "local_analyst", "claude_integration"):
        assert f"import {name}" not in src
        assert f"from {name}" not in src


def test_config_switch_defaults_on():
    import trade_alerts
    assert trade_alerts.enabled("missed_movers", {}) is True
    cfg = json.load(open("bot_config.json", encoding="utf-8"))
    assert cfg["discord"]["missed_movers"] is True


# ============================================================ schedule

def test_plist_runs_weekdays_at_1620_eastern_through_run_job():
    with open(os.path.join("jobs", "macos",
                           "com.tradingbot.missed_movers.plist"), "rb") as f:
        p = plistlib.load(f)
    assert p["Label"] == "com.tradingbot.missed_movers"
    assert p["ProgramArguments"] == ["/bin/bash",
                                     "__REPO__/jobs/macos/run_job.sh",
                                     "missed_movers.py"]
    assert p["EnvironmentVariables"]["TZ"] == "America/New_York"
    assert p["RunAtLoad"] is False
    assert sorted((s["Weekday"], s["Hour"], s["Minute"])
                  for s in p["StartCalendarInterval"]) == [
        (d, 16, 20) for d in range(1, 6)]
