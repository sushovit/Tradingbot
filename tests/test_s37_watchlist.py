"""S37 - the dashboard's watchlist table: parsed from the worker's last
cycle line and joined to today's universe and catalyst files. Files only."""

import json

import dashboard_data as d

LINE = ("[2026-10-21 10:05:12] Cycle #12 | Monitoring Live (Alpaca paper): "
        "REGN: In Pos [bot] (+0.38%) | MU: Waiting (No Signal) | "
        "MS: Pass (trend_continuation: adx_low) | "
        "XOM: chop_reclaim (Rule #5) | XOM: Gatekeeper blocked (RSI not rising) | "
        "SMCI: Pass (watch_only: catalyst) | WBD: No Data")


def write(tmp_path, status=LINE, day="2026-10-21"):
    (tmp_path / "bot_status.log").write_text(
        "[2026-10-21 16:15:15] Session ended 16:15 ET\n" + status + "\n")
    (tmp_path / "universe_today.json").write_text(json.dumps({"date": day, "candidates": [
        {"symbol": "REGN", "price": 807.5, "change_pct": 0.4, "source": "movers"},
        {"symbol": "MU", "price": 200.0, "change_pct": 1.0, "source": "core"},
        {"symbol": "MS", "price": 150.0, "change_pct": -2.5, "source": "movers"},
        {"symbol": "XOM", "price": 177.7, "change_pct": 0.9, "source": "core"},
        {"symbol": "SMCI", "price": 40.0, "change_pct": 12.2, "source": "catalyst",
         "catalyst": True, "watch_only": True, "catalyst_tag": "earnings",
         "catalyst_headline": "SMCI blowout prelim revenue"}]}))
    (tmp_path / "catalysts_today.json").write_text(json.dumps({"date": day, "names": [
        {"symbol": "SMCI", "gap_pct": 12.2, "tag": "earnings",
         "headline": "SMCI blowout prelim revenue"}]}))
    return {k: str(tmp_path / f) for k, f in (("status_file", "bot_status.log"),
                                               ("universe_file", "universe_today.json"),
                                               ("catalysts_file", "catalysts_today.json"))}


def test_uses_the_last_cycle_line_not_the_session_ended_line(tmp_path):
    w = d.watchlist_rows(**write(tmp_path))
    assert w["cycle"] == 12 and w["time"] == "2026-10-21 10:05:12"
    assert [r["Ticker"] for r in w["rows"]] == ["REGN", "XOM", "SMCI", "MS", "MU", "WBD"]


def test_status_groups_and_details(tmp_path):
    rows = {r["Ticker"]: r for r in d.watchlist_rows(**write(tmp_path))["rows"]}
    assert (rows["REGN"]["Status"], rows["REGN"]["Detail"]) == ("Position", "[bot] (+0.38%)")
    assert rows["MS"]["Status"] == "Filtered" and rows["MS"]["Detail"] == "trend_continuation: adx_low"
    assert rows["MU"]["Status"] == "Waiting" and rows["MU"]["Detail"] == ""
    assert rows["WBD"]["Status"] == "No data"
    # a ticker seen twice keeps its furthest stage and both reasons
    assert rows["XOM"]["Status"] == "Gatekeeper"
    assert rows["XOM"]["Detail"] == "Rule #5; RSI not rising"


def test_catalyst_rows_are_flagged_watch_only_with_tag_and_headline(tmp_path):
    rows = {r["Ticker"]: r for r in d.watchlist_rows(**write(tmp_path))["rows"]}
    s = rows["SMCI"]
    assert (s["Status"], s["Source"], s["Watch-only"], s["Catalyst"]) == (
        "Watch-only", "catalyst", True, "earnings")
    assert s["Headline"] == "SMCI blowout prelim revenue" and s["Change %"] == 12.2
    assert rows["MU"]["Watch-only"] is False and rows["MU"]["Catalyst"] == ""


def test_another_days_files_are_not_joined(tmp_path):
    rows = {r["Ticker"]: r for r in d.watchlist_rows(**write(tmp_path, day="2026-10-20"))["rows"]}
    assert rows["SMCI"]["Price"] is None and rows["SMCI"]["Catalyst"] == ""


def test_no_cycle_yet_gives_no_rows(tmp_path):
    (tmp_path / "bot_status.log").write_text("[2026-10-21 09:25:30] Worker starting\n")
    w = d.watchlist_rows(str(tmp_path / "bot_status.log"), str(tmp_path / "u.json"),
                         str(tmp_path / "c.json"))
    assert w["rows"] == [] and w["cycle"] is None


def test_every_worker_status_string_has_a_group():
    for s in ("In Pos [bot] (+1.0%)", "In Pos (unconfirmed: x)", "BOUGHT (x)",
              "Entry pending (new)", "SOLD (Stop)", "CLOSED (x)",
              "Exited (fill pending sync)", "Order failed (will retry)",
              "Gatekeeper blocked (x)", "Gatekeeper backoff 30s",
              "Waiting (gatekeeper failed)", "Pass (watch_only: catalyst)",
              "Pass (SPY bearish)", "chop_reclaim (Rule #5)",
              "duplicate blocked (x)", "Waiting (No Signal)", "No Data"):
        assert d.status_group(s)[0] != "Other", s


def test_the_worker_never_renders_it():
    src = open("streamlit_app.py", encoding="utf-8").read()
    i = src.index("_render_watchlist()\n", src.index("Raw status lines"))
    assert "if not desk_config.in_worker():" in src[i - 120:i]
