"""
Goal 13 — price-freshness guard + universe price correctness. No network.

  - sheet entry >2% from the LIVE quote -> clean journaled rejection
  - stop at/above live -> rejection
  - fresh price passes through to execution
  - live-quote failure -> protective rejection (never a blind submit)
  - prior_completed_close excludes today's partial bar
  - universe candidates carry the LIVE price, not a stale bar close
"""

import json
import sqlite3

import pandas as pd
import pytest

import orders
import universe


def _sheet(tmp_path, entry=100.0, stop=95.0, target=110.0):
    sheet = {"session": "t", "regime": "t",
             "orders": [{"action": "BUY", "ticker": "MRVL", "notional_usd": 300,
                         "entry": entry, "stop": stop, "target": target,
                         "setup": "mean_reversion_reclaim"}]}
    p = tmp_path / "sheet.json"
    p.write_text(json.dumps(sheet))
    return str(p)


class FakeLeg:
    def __init__(self, id, order_type):
        self.id, self.order_type = id, order_type


class FakeBracket:
    id = "br-1"
    status = "filled"
    filled_avg_price = 100.0
    legs = [FakeLeg("s1", "stop"), FakeLeg("t1", "limit")]


class FreshnessBroker:
    def __init__(self, live_price=100.0, fail_quote=False):
        self._live = live_price
        self._fail = fail_quote
        self.submitted = []

    def get_equity(self):
        return 2000.0

    def get_latest_price(self, ticker):
        if self._fail:
            raise RuntimeError("quote endpoint down")
        return self._live

    def submit_bracket(self, ticker, qty, stop, target):
        self.submitted.append(ticker)
        return FakeBracket()

    def get_order(self, oid):
        return FakeBracket()


def _rules_reasons(j):
    conn = sqlite3.connect(j.DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT verdict FROM decisions WHERE source='rules'").fetchall()
    conn.close()
    return [json.loads(r["verdict"])["rejection_reason"] for r in rows]


@pytest.fixture
def pos_file(tmp_path, monkeypatch):
    monkeypatch.setattr(orders, "POSITIONS_STATE_FILE", str(tmp_path / "p.json"))
    monkeypatch.setattr(universe, "UNIVERSE_FILE", str(tmp_path / "u.json"))


def test_live_5pct_below_sheet_entry_rejected(temp_journal, tmp_path, pos_file):
    # Sheet says $100; live is $95 — the MRVL incident shape.
    broker = FreshnessBroker(live_price=95.0)
    orders.ingest(_sheet(tmp_path, entry=100.0), broker=broker)
    assert broker.submitted == []                      # never reached the broker
    assert "stale_reference_price" in _rules_reasons(temp_journal)


def test_live_above_entry_also_rejected(temp_journal, tmp_path, pos_file):
    broker = FreshnessBroker(live_price=103.0)         # +3% > 2% tolerance
    orders.ingest(_sheet(tmp_path, entry=100.0), broker=broker)
    assert broker.submitted == []
    assert "stale_reference_price" in _rules_reasons(temp_journal)


def test_stop_at_or_above_live_rejected(temp_journal, tmp_path, pos_file):
    # Entry fresh (within 2%) but live already through the stop.
    broker = FreshnessBroker(live_price=99.0)
    orders.ingest(_sheet(tmp_path, entry=100.0, stop=99.5), broker=broker)
    assert broker.submitted == []
    assert "stop_at_or_above_live_price" in _rules_reasons(temp_journal)


def test_fresh_price_passes(temp_journal, tmp_path, pos_file):
    broker = FreshnessBroker(live_price=101.0)         # 1% drift — fine
    orders.ingest(_sheet(tmp_path, entry=100.0), broker=broker)
    assert broker.submitted == ["MRVL"]


def test_quote_failure_is_protective(temp_journal, tmp_path, pos_file):
    broker = FreshnessBroker(fail_quote=True)
    orders.ingest(_sheet(tmp_path, entry=100.0), broker=broker)
    assert broker.submitted == []
    assert "live_price_unavailable" in _rules_reasons(temp_journal)


# =============================================================================
# universe price correctness
# =============================================================================

def _daily_df(closes, last_day_today=False):
    n = len(closes)
    end = pd.Timestamp("2026-07-23") if last_day_today else pd.Timestamp("2026-07-22")
    idx = pd.date_range(end=end, periods=n, freq="D")
    return pd.DataFrame({"open": closes, "high": [c + 1 for c in closes],
                         "low": [c - 1 for c in closes], "close": closes,
                         "volume": [1_000_000] * n}, index=idx)


def test_prior_completed_close_skips_todays_partial_bar():
    df = _daily_df([100.0, 105.0, 212.0], last_day_today=True)
    assert universe.prior_completed_close(df, today_str="2026-07-23") == 105.0
    df2 = _daily_df([100.0, 105.0, 110.0], last_day_today=False)
    assert universe.prior_completed_close(df2, today_str="2026-07-23") == 110.0


def test_core_candidates_use_live_price_not_stale_close():
    class FakeUniverseBroker:
        def get_assets_map(self, syms):
            return {s: {"tradable": True, "exchange": "NASDAQ", "name": s}
                    for s in syms}

        def get_daily_bars(self, syms, lookback_days=45):
            # Bars end at a STALE 228.30 close (the incident number)...
            closes = [230.0] * 10 + [260.0] * 5 + [228.30] * 15
            return {s: _daily_df(closes) for s in syms}

        def get_latest_prices(self, syms):
            return {s: 212.55 for s in syms}           # ...but live is 212.55

    config = {"universe": {"core_watchlist": ["MRVL"], "min_price": 5,
                           "max_price": 480, "min_dollar_volume": 1,
                           "max_candidates": 20, "skip_etfs": True}}
    cands = universe.fetch_core_candidates(FakeUniverseBroker(), config)
    assert len(cands) == 1
    assert cands[0]["price"] == pytest.approx(212.55)  # LIVE, not 228.30
