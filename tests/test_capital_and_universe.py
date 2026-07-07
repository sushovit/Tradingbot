"""
Goal 6 tests — capital realism + universe scanner. All mocked, no network.

Covers:
  - effective_equity capping (broker $97k, cap $1,000 -> $1,000)
  - total-notional-vs-cash rejection (no margin, ever)
  - whole-share rejection journaled (e.g. $400 stock on a $1,000 account)
  - universe filter logic with fixture data (price/volume bounds, ranking,
    tradability, ETF skip, candidate cap)
"""

import json
import sqlite3

import pytest

import risk
import universe
from orders import validate_order


# =============================================================================
# 6.1 — hard capital cap + no margin
# =============================================================================

def test_effective_equity_caps_broker_balance():
    assert risk.effective_equity(97_584.76, {"capital_cap_usd": 1000}) == 1000.0
    assert risk.effective_equity(500.0, {"capital_cap_usd": 1000}) == 500.0
    # No cap configured -> broker equity unchanged
    assert risk.effective_equity(97_584.76, {}) == 97_584.76


def test_total_notional_cannot_exceed_cash():
    # $1,000 effective equity, $800 already deployed: a $250 order would need
    # margin -> rejected.
    ok, reason = risk.check_signal(entry=100.0, stop=95.0, target=110.0,
                                   equity=1000.0, notional_usd=250.0,
                                   open_notional_usd=800.0)
    assert not ok
    assert reason == "insufficient_cash_no_margin"

    # Same order with only $700 deployed fits in cash.
    ok, reason = risk.check_signal(entry=100.0, stop=95.0, target=110.0,
                                   equity=1000.0, notional_usd=250.0,
                                   open_notional_usd=700.0)
    assert ok, reason


def test_position_size_respects_remaining_cash():
    # 30% cap would allow ~$300 notional, but only $50 cash remains.
    qty = risk.position_size(equity=1000.0, risk_per_trade_pct=5.0,
                             entry=40.0, stop=38.0, open_notional_usd=950.0)
    assert qty * 40.0 <= 50.0
    assert qty == 1


def test_orders_validate_rejects_margin_use():
    order = {"action": "BUY", "ticker": "NVDA", "notional_usd": 250,
             "entry": 100.0, "stop": 95.0, "target": 110.0,
             "setup": "trend_continuation"}
    ok, reason = validate_order(order, equity=1000.0, open_positions=1,
                                max_positions=3, open_notional_usd=900.0)
    assert not ok
    assert reason == "insufficient_cash_no_margin"


# =============================================================================
# 6.2 — whole-share reality
# =============================================================================

def test_expensive_stock_rejected_price_too_high():
    # $400 stock, $1,000 account -> 30% cap is $300 -> 0 whole shares.
    assert risk.zero_size_reason(entry=400.0, equity=1000.0) == "price_too_high_for_account"
    qty = risk.position_size(equity=1000.0, risk_per_trade_pct=1.0,
                             entry=400.0, stop=390.0)
    assert qty == 0


def test_whole_share_rejection_is_journaled(temp_journal):
    # Exactly what live_bot_worker does when sizing yields < 1 share:
    reason = risk.zero_size_reason(entry=400.0, equity=1000.0)
    temp_journal.log_rules_pass("BRKA", "momentum_continuation", reason,
                                "entry=400.00 equity=1000.00")
    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM decisions WHERE source='rules'").fetchall()
    conn.close()
    assert len(rows) == 1
    assert "price_too_high_for_account" in rows[0]["verdict"]


def test_orders_whole_share_validation():
    # notional $300 cannot buy one $400 share -> rejected.
    order = {"action": "BUY", "ticker": "COST", "notional_usd": 300,
             "entry": 400.0, "stop": 380.0, "target": 440.0,
             "setup": "trend_continuation"}
    ok, reason = validate_order(order, equity=1000.0, open_positions=0,
                                max_positions=3)
    assert not ok
    assert reason == "price_too_high_for_account"


# =============================================================================
# 6.3 — universe filter/rank logic
# =============================================================================

def make_candidate(**overrides):
    c = {"symbol": "AAA", "price": 50.0, "avg_dollar_volume": 100e6,
         "change_pct": 4.0, "tradable": True, "exchange": "NASDAQ",
         "name": "Sample Common Stock Inc"}
    c.update(overrides)
    return c


CONFIG = {"universe": {"min_price": 5, "max_price": 250,
                       "min_dollar_volume": 20_000_000,
                       "max_candidates": 15, "skip_etfs": True}}


def test_universe_price_bounds():
    candidates = [
        make_candidate(symbol="PENNY", price=3.5),      # below $5 — manipulation zone
        make_candidate(symbol="PRICY", price=900.0),    # one share > notional cap
        make_candidate(symbol="GOOD", price=50.0),
    ]
    kept = [c["symbol"] for c in universe.filter_and_rank(candidates, CONFIG)]
    assert kept == ["GOOD"]


def test_universe_dollar_volume_floor():
    candidates = [
        make_candidate(symbol="THIN", avg_dollar_volume=5e6),   # can't exit at size
        make_candidate(symbol="LIQUID", avg_dollar_volume=50e6),
    ]
    kept = [c["symbol"] for c in universe.filter_and_rank(candidates, CONFIG)]
    assert kept == ["LIQUID"]


def test_universe_skips_nontradable_otc_and_etfs():
    candidates = [
        make_candidate(symbol="HALTED", tradable=False),
        make_candidate(symbol="SKETCH", exchange="OTC"),
        make_candidate(symbol="SPYX", name="Super Duper S&P 500 ETF"),
        make_candidate(symbol="OKAY"),
    ]
    kept = [c["symbol"] for c in universe.filter_and_rank(candidates, CONFIG)]
    assert kept == ["OKAY"]


def test_universe_etfs_allowed_when_configured():
    config = json.loads(json.dumps(CONFIG))
    config["universe"]["skip_etfs"] = False
    candidates = [make_candidate(symbol="SPYX", name="Super Duper S&P 500 ETF")]
    kept = [c["symbol"] for c in universe.filter_and_rank(candidates, config)]
    assert kept == ["SPYX"]


def test_universe_ranking_and_cap():
    # score = dollar volume x |% move|; list capped at max_candidates.
    candidates = [
        make_candidate(symbol=f"T{i:02d}", avg_dollar_volume=(i + 1) * 25e6,
                       change_pct=2.0)
        for i in range(20)
    ]
    candidates.append(make_candidate(symbol="HOT", avg_dollar_volume=100e6,
                                     change_pct=-15.0))   # big DOWN move ranks too
    ranked = universe.filter_and_rank(candidates, CONFIG)
    assert len(ranked) == 15
    assert ranked[0]["symbol"] == "HOT"
    scores = [c["score"] for c in ranked]
    assert scores == sorted(scores, reverse=True)


# =============================================================================
# Goal 8 — core watchlist setup flags + merge
# =============================================================================

def daily_df(closes, highs=None):
    import pandas as pd
    n = len(closes)
    highs = highs or [c * 1.005 for c in closes]
    idx = pd.date_range("2026-06-01", periods=n, freq="D")
    return pd.DataFrame({"open": closes, "high": highs,
                         "low": [c * 0.99 for c in closes],
                         "close": closes, "volume": [1_000_000] * n}, index=idx)


def test_classify_pre_breakout_within_3pct_of_high():
    # 20-day high 100, last close 98 -> within 3%
    closes = [90.0] * 19 + [98.0]
    df = daily_df(closes, highs=[100.0] * 20)
    assert universe.classify_core_setup(df, CONFIG) == "pre_breakout"


def test_classify_washout_10pct_off_high():
    # 20-day high 100, last close 85 -> 15% off highs
    closes = [95.0] * 19 + [85.0]
    df = daily_df(closes, highs=[100.0] * 20)
    assert universe.classify_core_setup(df, CONFIG) == "washout_reclaim"


def test_classify_midrange_returns_none():
    # last close 95: only 5% off the high — neither coiled nor washed out
    closes = [92.0] * 19 + [95.0]
    df = daily_df(closes, highs=[100.0] * 20)
    assert universe.classify_core_setup(df, CONFIG) is None


def test_merge_dedupes_and_tags_sources():
    movers = [make_candidate(symbol="NVDA", change_pct=5.0)]
    core = [dict(make_candidate(symbol="NVDA", change_pct=0.5),
                 source="core_watch", setup_flag="pre_breakout"),
            dict(make_candidate(symbol="XOM", change_pct=0.2),
                 source="core_watch", setup_flag="washout_reclaim")]
    merged = universe.merge_candidates(movers, core)
    by_sym = {c["symbol"]: c for c in merged}
    assert len(merged) == 2
    # overlap: movers entry wins but inherits the setup flag
    assert by_sym["NVDA"]["source"] == "movers"
    assert by_sym["NVDA"]["change_pct"] == 5.0
    assert by_sym["NVDA"]["setup_flag"] == "pre_breakout"
    assert by_sym["XOM"]["source"] == "core_watch"


def test_flagged_core_names_get_move_floor_and_survive_ranking():
    # A dead-quiet coiling core name should not score ~0.
    core = [dict(make_candidate(symbol="COIL", change_pct=0.05,
                                avg_dollar_volume=50e6),
                 source="core_watch", setup_flag="pre_breakout")]
    ranked = universe.filter_and_rank(core, CONFIG)
    assert ranked[0]["score"] == pytest.approx(50e6 * 1.0)
    assert ranked[0]["source"] == "core_watch"
    assert ranked[0]["setup_flag"] == "pre_breakout"


def test_combined_output_capped_at_20():
    config = json.loads(json.dumps(CONFIG))
    config["universe"]["max_candidates"] = 20
    movers = [make_candidate(symbol=f"M{i:02d}", change_pct=3.0) for i in range(15)]
    core = [dict(make_candidate(symbol=f"C{i:02d}", change_pct=1.5),
                 source="core_watch", setup_flag="pre_breakout") for i in range(15)]
    ranked = universe.filter_and_rank(universe.merge_candidates(movers, core), config)
    assert len(ranked) == 20


def test_load_universe_tickers_stale_file_returns_empty(tmp_path, monkeypatch):
    stale = {"date": "2020-01-01", "candidates": [{"symbol": "OLD"}]}
    f = tmp_path / "universe_today.json"
    f.write_text(json.dumps(stale))
    monkeypatch.setattr(universe, "UNIVERSE_FILE", str(f))
    assert universe.load_universe_tickers() == []
