"""S36 - morning catalyst scanner. Fakes only: no Alpaca, Finnhub, Ollama
or Discord traffic (the conftest also blocks Discord HTTP)."""

import json
import os
import plistlib
from datetime import datetime
from types import SimpleNamespace as N

import pandas as pd
import pytest

import catalysts as cat
import universe

ET = cat.ET
NOW = ET.localize(datetime(2026, 10, 21, 9, 0))
CFG = {"universe": {"core_watchlist": ["AAA"], "skip_etfs": True},
       "catalysts": {}}


def daily(price, vol, n=25):
    idx = pd.date_range("2026-09-10", periods=n, freq="B", tz="UTC")
    return pd.DataFrame({"open": price, "high": price, "low": price,
                         "close": price, "volume": vol}, index=idx)


class FakeBroker:
    """Snapshots for six names, built to exercise every filter."""
    SNAPS = {
        "AAA": (106.0, 100.0),     # +6%, liquid, news       -> kept
        "BBB": (92.0, 100.0),      # -8%, liquid, news       -> kept (gap down)
        "CCC": (101.0, 100.0),     # +1% gap                 -> no
        "DDD": (3.30, 3.00),       # +10% but price < 5      -> no
        "EEE": (110.0, 100.0),     # +10% but ADV $1M        -> no
        "FFF": (120.0, 100.0),     # +20%, liquid, OLD news  -> no
        "ETFX": (110.0, 100.0),    # +10% ETF                -> no
    }

    def __init__(self):
        self.data = self

    def get_stock_snapshot(self, req):
        return {s: N(latest_trade=N(price=p), previous_daily_bar=N(close=c))
                for s, (p, c) in self.SNAPS.items()
                if s in req.symbol_or_symbols}

    def get_market_movers(self, top=50):
        return [{"symbol": s} for s in self.SNAPS]

    def get_daily_bars(self, syms, lookback_days=40):
        return {s: daily(100.0, 10_000 if s == "EEE" else 1_000_000) for s in syms}

    def get_assets_map(self, syms):
        return {s: {"tradable": True, "exchange": "NASDAQ",
                    "name": "Some Index ETF" if s == "ETFX" else f"{s} Inc"}
                for s in syms}


class FakeFinnhub:
    def __init__(self):
        self.calls = []

    def general_news(self, cat_, min_id=0):
        return [{"related": "AAA,ZZZ"}]

    def company_news(self, sym, _from, to):
        self.calls.append(sym)
        t = NOW.timestamp()
        age = {"AAA": 2, "BBB": 10, "FFF": 30, "EEE": 1, "ETFX": 1}.get(sym)
        if age is None:
            return []
        return [{"headline": f"{sym} beats Q3 estimates", "datetime": t - age * 3600,
                 "source": "Wire", "url": "http://example.invalid"}]


def run_scan(monkeypatch, tagger=None):
    monkeypatch.setattr(universe, "scan_pool", lambda config: ["AAA", "CCC"])
    return cat.scan(broker=FakeBroker(), client=FakeFinnhub(), config=CFG,
                    now=NOW, tagger=tagger or (lambda h, t: "earnings"))


def test_filters_gap_price_adv_news_and_etfs(monkeypatch):
    p = run_scan(monkeypatch)
    assert [n["symbol"] for n in p["names"]] == ["BBB", "AAA"]      # |gap| desc
    bbb = p["names"][0]
    assert bbb["gap_pct"] == -8.0 and bbb["tag"] == "earnings"
    assert bbb["headline"] == "BBB beats Q3 estimates" and p["date"] == "2026-10-21"


def test_news_window_is_18_hours():
    now = NOW.astimezone(cat.timezone.utc)
    items = [{"headline": "a", "datetime": now.timestamp() - 17.9 * 3600},
             {"headline": "b", "datetime": now.timestamp() - 18.1 * 3600},
             {"headline": "", "datetime": now.timestamp()}]
    assert [n["headline"] for n in cat.recent_news(items, now, 18)] == ["a"]


def test_at_most_15_names_largest_gap_first():
    rows = [{"symbol": f"S{i:02d}", "price": 50.0, "gap_pct": 3.0 + i,
             "adv_usd": 5e7, "news": [{"headline": "x"}]} for i in range(20)]
    out = cat.select(rows, cat.settings({}))
    assert len(out) == 15 and out[0]["symbol"] == "S19" and out[-1]["symbol"] == "S05"


def test_adv_uses_completed_sessions_only():
    df = daily(10.0, 3_000_000, n=21)
    assert cat.avg_dollar_volume(df) == pytest.approx(30_000_000)


@pytest.mark.parametrize("raw,tag", [
    ("earnings", "earnings"), ("Upgrade-Downgrade", "upgrade-downgrade"),
    ("downgrade", "upgrade-downgrade"), ("FDA", "FDA-clinical"),
    ("m&a", "M&A"), ("merger", "M&A"), ("guidance", "guidance"),
    ("other", "other"), ("weather", "untagged"), (None, "untagged")])
def test_tags_normalise(raw, tag):
    assert cat.normalise_tag(raw) == tag


def test_tagger_calls_the_local_model_with_a_30s_timeout():
    seen = {}

    def post(url, json=None, timeout=None):
        seen.update(url=url, timeout=timeout, model=json["model"], fmt=json["format"])
        return N(raise_for_status=lambda: None,
                 json=lambda: {"message": {"content": '{"tag": "M&A"}'}})
    tag = cat.tag_headline("X agrees to buy Y", "X",
                           {"models": {"shadow": "qwen3.5:9b"}}, post=post)
    assert tag == "M&A" and seen["timeout"] == 30 and seen["model"] == "qwen3.5:9b"
    assert seen["url"].endswith("/api/chat") and seen["fmt"] == "json"


def test_tagger_falls_back_to_untagged_on_timeout():
    import requests

    def post(*a, **k):
        raise requests.Timeout("read timed out")
    assert cat.tag_headline("anything", "X", {}, post=post) == "untagged"


def test_universe_refresh_merges_todays_catalysts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    today = datetime.now(ET).strftime("%Y-%m-%d")
    cat.write({"date": today, "names": [
        {"symbol": "NEWC", "gap_pct": 7.5, "price": 40.0, "adv_usd": 3e7,
         "tag": "FDA-clinical", "headline": "NEWC trial met endpoint"},
        {"symbol": "AAPL", "gap_pct": 3.2, "price": 250.0, "adv_usd": 1e10,
         "tag": "earnings", "headline": "Apple beats"}]})
    ranked = [{"symbol": "AAPL", "price": 249.0, "source": "movers", "score": 9.0}]
    out = universe.merge_catalysts(ranked, universe.load_catalysts_today())
    assert [c["symbol"] for c in out] == ["AAPL", "NEWC"]
    assert out[0]["source"] == "movers" and out[0]["catalyst_tag"] == "earnings"
    assert out[1]["source"] == "catalyst" and out[1]["catalyst_tag"] == "FDA-clinical"


def test_a_stale_catalysts_file_is_ignored(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cat.write({"date": "2026-01-02", "names": [{"symbol": "OLD"}]})
    assert universe.load_catalysts_today() == []


def test_added_to_todays_universe_file_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    today = datetime.now(ET).strftime("%Y-%m-%d")
    payload = {"names": [{"symbol": "NEWC", "gap_pct": 5.0}]}
    (tmp_path / "universe_today.json").write_text(json.dumps(
        {"date": "2026-01-02", "candidates": [{"symbol": "AAPL"}]}))
    assert universe.add_catalysts_to_today(payload) == 0        # stale file
    (tmp_path / "universe_today.json").write_text(json.dumps(
        {"date": today, "candidates": [{"symbol": "AAPL"}]}))
    assert universe.add_catalysts_to_today(payload) == 1
    assert universe.load_universe_tickers() == ["AAPL", "NEWC"]


def test_each_name_is_journaled_as_source_catalyst(temp_journal):
    ids = cat.journal_names({"names": [
        {"symbol": "NEWC", "gap_pct": 5.0, "tag": "M&A", "headline": "deal"}]})
    import sqlite3
    conn = sqlite3.connect(temp_journal.DB_FILE)
    row = conn.execute("SELECT ticker, setup_name, source, approved, context "
                       "FROM decisions WHERE id=?", (ids[0],)).fetchone()
    assert row[:3] == ("NEWC", "catalyst", "catalyst")     # every analytic filters on source
    assert json.loads(row[4])["tag"] == "M&A"


def test_discord_table_posts_through_post_async(discord_posts, monkeypatch):
    payload = {"date": "2026-10-21", "names": [
        {"symbol": "NEWC", "gap_pct": 7.5, "tag": "FDA-clinical",
         "headline": "NEWC phase 3 trial met its primary endpoint in a "
                     "large study of patients"}]}
    assert cat.post(payload) == 1
    msg = discord_posts[0]["payload"]
    text = msg if isinstance(msg, str) else json.dumps(msg)
    assert "📰 Morning catalysts" in text and "NEWC" in text and "+7.5" in text
    assert all(len(line) <= 50 for line in cat.table_lines(payload))


def test_switch_off_posts_nothing(discord_posts, monkeypatch):
    import trade_alerts
    monkeypatch.setattr(trade_alerts, "enabled", lambda key="x", config=None: key != "catalysts")
    assert cat.post({"names": []}) == 0 and discord_posts == []


def test_no_trades_from_news_alone():
    src = open("catalysts.py", encoding="utf-8").read()
    for forbidden in ("import orders", "submit_order", "place_bracket",
                      "import analyst", "risk.position_size"):
        assert forbidden not in src


def test_shipped_config_and_schedules():
    cfg = json.load(open("bot_config.json", encoding="utf-8"))
    c = cfg["catalysts"]
    assert (c["min_gap_pct"], c["news_hours"], c["min_price"], c["max_price"],
            c["min_dollar_volume"], c["max_names"], c["tag_timeout_s"]) == (
        3.0, 18, 5, 1200, 20000000, 15, 30)
    for label, hm, args in (("catalysts", (9, 0), ["catalysts.py"]),
                            ("catalysts_post", (9, 20), ["catalysts.py", "--post"])):
        with open(os.path.join("jobs", "macos", f"com.tradingbot.{label}.plist"), "rb") as f:
            p = plistlib.load(f)
        assert p["ProgramArguments"][-len(args):] == args
        assert sorted((s["Weekday"], s["Hour"], s["Minute"])
                      for s in p["StartCalendarInterval"]) == [
            (d, *hm) for d in range(1, 6)]


def test_prefers_a_headline_that_names_the_company():
    news = [{"headline": "Why Should You Think About Vertex Differently Now?"},
            {"headline": "Regeneron's Dupixent wins EU nod"},
            {"headline": "REGN shares jump"}]
    assert cat.best_headline(news, "REGN", "Regeneron Pharmaceuticals")["headline"] \
        == "Regeneron's Dupixent wins EU nod"
    assert cat.best_headline(news[:1], "REGN", "")["headline"].startswith("Why")
