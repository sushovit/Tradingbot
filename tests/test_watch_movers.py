"""CEO 2026-10-23 - the Qwen watchlist (view only). Fakes only: no Alpaca,
Finnhub, Ollama or Discord traffic."""

import json
import os
import plistlib
import sqlite3
import time
from datetime import datetime
from types import SimpleNamespace as N

import pandas as pd
import pytest

import watch_movers as wm

ET = wm.ET
NOW = ET.localize(datetime(2026, 10, 23, 11, 0))


def daily(price, vol, n=25):
    idx = pd.date_range("2026-09-20", periods=n, freq="B", tz="UTC")
    return pd.DataFrame({"open": price, "high": price, "low": price, "close": price,
                         "volume": vol}, index=idx)


class FakeBroker:
    SNAPS = {"UPP": (106.0, 100.0), "DWN": (92.0, 100.0), "SML": (101.0, 100.0),
             "PNY": (3.3, 3.0), "THN": (110.0, 100.0), "OLD": (120.0, 100.0)}

    def __init__(self):
        self.data = self

    def get_market_movers(self, top=50):
        return [{"symbol": s} for s in ("UPP", "DWN", "SML", "PNY")]

    def get_most_actives(self, top=50):
        return [{"symbol": s} for s in ("THN", "OLD", "UPP")]

    def get_stock_snapshot(self, req):
        return {s: N(latest_trade=N(price=p), previous_daily_bar=N(close=c))
                for s, (p, c) in self.SNAPS.items() if s in req.symbol_or_symbols}

    def get_daily_bars(self, syms, lookback_days=40):
        return {s: daily(100.0, 10_000 if s == "THN" else 1_000_000) for s in syms}

    def get_assets_map(self, syms):
        return {s: {"tradable": True, "exchange": "NASDAQ", "name": f"{s} Inc"} for s in syms}


def test_filters_and_skips_names_already_posted_today():
    rows = wm.candidates(FakeBroker(), {}, already={"OLD"})
    assert [r["symbol"] for r in rows] == ["DWN", "UPP"]       # |chg| desc; SML/PNY/THN out
    assert rows[0]["change_pct"] == -8.0


def test_at_most_ten():
    class Many(FakeBroker):
        SNAPS = {f"S{i:02d}": (100.0 + 4 + i, 100.0) for i in range(15)}

        def get_market_movers(self, top=50):
            return [{"symbol": s} for s in self.SNAPS]

        def get_most_actives(self, top=50):
            return []
    assert len(wm.candidates(Many(), {})) == 10


def test_note_is_clipped_to_12_words_and_uses_the_shadow_context():
    seen = {}

    def post(url, json=None, timeout=None):
        seen.update(timeout=timeout, num_ctx=json["options"]["num_ctx"], model=json["model"])
        return N(raise_for_status=lambda: None, json=lambda: {"message": {
            "content": "Shares rise after the company raised full year guidance on strong cloud demand growth"}})
    note = wm.write_note("X", ["X raises guidance"], "Software",
                         {"models": {"shadow": "qwen3.5:9b"}}, post=post, busy=lambda: False)
    assert len(note.split()) == 12 and note.startswith("Shares rise")
    import local_analyst
    assert seen == {"timeout": 30, "num_ctx": local_analyst.NUM_CTX, "model": "qwen3.5:9b"}


def test_note_falls_back_on_timeout_and_without_headlines():
    import requests

    def boom(*a, **k):
        raise requests.Timeout("read timed out")
    assert wm.write_note("X", ["h"], "s", {}, post=boom, busy=lambda: False) == wm.NO_NOTE
    assert wm.write_note("X", [], "s", {}, post=boom, busy=lambda: False) == wm.NO_NOTE


def test_the_watchlist_yields_to_the_shadow_queue():
    calls, slept = [], []
    note = wm.write_note("X", ["h"], "s", {}, post=lambda *a, **k: calls.append(1),
                         busy=lambda: True, sleep=lambda s: slept.append(s))
    assert note == wm.NO_NOTE and calls == [] and sum(slept) >= 60


def test_shadow_queue_raises_and_clears_the_busy_marker(tmp_path, monkeypatch):
    import analyst
    monkeypatch.chdir(tmp_path)
    seen = []

    def infer(job):
        seen.append(os.path.exists(analyst.SHADOW_BUSY_FILE))
        return {"approved": False}
    q = analyst.ShadowQueue(infer=infer, record=lambda j, v: None)
    q._q.put({"ticker": "X", "enqueued": q._clock()})
    q.run_one(block=False)
    assert seen == [True] and not os.path.exists(analyst.SHADOW_BUSY_FILE)
    assert wm.shadow_busy(str(tmp_path / "nope")) is False


LINE = ("[2026-10-23 10:55:00] Cycle #9 | Monitoring Live (Alpaca paper): "
        "AAA: Waiting (No Signal) | BBB: Pass (momentum_continuation: volume_low) | "
        "CCC: Gatekeeper blocked (RSI falling) | DDD: Pass (watch_only: catalyst)")


@pytest.mark.parametrize("tk,why", [
    ("ZZZ", "not in list"), ("AAA", "no setup"),
    ("BBB", "filtered: momentum_continuation: volume_low"),
    ("CCC", "gatekeeper: RSI falling"), ("DDD", "watch-only (catalyst)"),
    ("EEE", "not scanned yet")])
def test_why_not_traded(tk, why):
    assert wm.why_not_traded(tk, ["AAA", "BBB", "CCC", "DDD", "EEE"], LINE) == why


def test_journal_rows_are_source_watch_with_price(temp_journal):
    ids = wm.journal_rows([{"symbol": "UPP", "price": 106.0, "change_pct": 6.0,
                            "note": "n", "why": "not in list"}])
    row = sqlite3.connect(temp_journal.DB_FILE).execute(
        "SELECT source, setup_name, context FROM decisions WHERE id=?", (ids[0],)).fetchone()
    assert row[:2] == ("watch", "watch") and json.loads(row[2])["price"] == 106.0


def test_discord_table_is_posted_through_post_async(discord_posts):
    rows = [{"symbol": "UPP", "change_pct": 6.0, "note": "Raised guidance on cloud demand",
             "why": "not in list"}]
    assert wm.post(rows, NOW) == 1
    text = discord_posts[0]["payload"]
    assert "👀 Watching" in text and "UPP" in text and "not in list" in text
    import trade_table
    assert all(len(l) <= trade_table.WIDTH for l in wm.table_lines(rows, NOW))


def test_outcome_tracker_grades_plus_1_3_5_sessions(temp_journal):
    import outcomes
    did = temp_journal.log_decision("UPP", "watch", {"price": 100.0}, {"watch": True},
                                    source="watch")
    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.execute("UPDATE decisions SET timestamp='2026-10-05 11:00:00' WHERE id=?", (did,))
    conn.commit()
    idx = pd.to_datetime(["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
                          "2026-10-09", "2026-10-12"])
    bars = pd.DataFrame({"close": [100, 101, 102, 103, 104, 105]}, index=idx)
    res = outcomes.grade_watch(temp_journal.DB_FILE, fetch=lambda t: {"UPP": bars})
    assert res == {"graded": 1, "complete": 1}
    r = sqlite3.connect(temp_journal.DB_FILE).execute(
        "SELECT ret_1, ret_3, ret_5 FROM watch_outcomes WHERE decision_id=?", (did,)).fetchone()
    assert r == (1.0, 3.0, 5.0)


def test_never_tradable():
    src = open("watch_movers.py", encoding="utf-8").read()
    for bad in ("import orders", "submit_", "add_catalysts_to_today", "UNIVERSE_FILE",
                "write_positions", "analyst.get_verdict"):
        assert bad not in src


def test_schedule_every_30_min_1000_to_1500_weekdays():
    with open("jobs/macos/com.tradingbot.watch_movers.plist", "rb") as f:
        p = plistlib.load(f)
    slots = sorted({(s["Hour"], s["Minute"]) for s in p["StartCalendarInterval"]})
    assert slots[0] == (10, 0) and slots[-1] == (15, 0) and len(slots) == 11
    assert {s["Weekday"] for s in p["StartCalendarInterval"]} == {1, 2, 3, 4, 5}


def test_a_long_note_wraps_instead_of_being_cut():
    rows = [{"symbol": "SMCI", "change_pct": 9.5, "why": "no setup",
             "note": "Super Micro raises full-year guidance citing strong AI server demand"}]
    lines = wm.table_lines(rows, NOW)
    body = " ".join(l.strip() for l in lines[3:5])
    assert body == "Super Micro raises full-year guidance citing strong AI server demand"
