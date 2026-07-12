"""
Goal 9 pre-flight tests — all mocked, no network.

  9.1  BUY journals the ACTUAL fill; sync corrects reference-priced BUYs and
       recomputes exit PnL; week-1 migration is idempotent
  9.2  non-numeric order fields -> clean rejection, never a traceback
  9.4  gap-abort: reclaim detector (bot side) and abort_if_open_below (sheets)
  9.5  junior-analyst role framing appended for the local model only
  9.6  universe context attached to journaled CEO decisions
"""

import json
import sqlite3
from datetime import datetime, timezone

import pandas as pd
import pytest

import journal as journal_mod
import orders
import prompts
import universe
from orders import validate_order
from strategies.mean_reversion_reclaim import MeanReversionReclaim
from strategies.base import Signal, Rejection


def _rows(j, query):
    conn = sqlite3.connect(j.DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(query).fetchall()]
    conn.close()
    return rows


# =============================================================================
# 9.1 — actual fill prices
# =============================================================================

class FakeLeg:
    def __init__(self, id, order_type):
        self.id, self.order_type = id, order_type


class FakeBracket:
    def __init__(self, id="br-1", fill=None):
        self.id = id
        self.status = "filled" if fill else "new"
        self.filled_avg_price = fill
        self.legs = [FakeLeg(f"{id}-stop", "stop"), FakeLeg(f"{id}-tp", "limit")]


class FakeExecBroker:
    """Enough broker for execute_order/ingest: submits fill at a price that
    DIFFERS from the sheet's reference entry."""
    def __init__(self, fill_price=164.21, latest_price=200.0, equity=1000.0):
        self.fill_price = fill_price
        self.latest_price = latest_price
        self.equity = equity
        self.submitted = []

    def get_equity(self):
        return self.equity

    def get_latest_price(self, ticker):
        return self.latest_price

    def submit_bracket(self, ticker, qty, stop, target):
        self.submitted.append((ticker, qty, stop, target))
        return FakeBracket(id=f"br-{ticker}")

    def get_order(self, order_id):
        return FakeBracket(id=order_id, fill=self.fill_price)


def test_execute_buy_journals_actual_fill(temp_journal, tmp_path, monkeypatch):
    monkeypatch.setattr(orders, "POSITIONS_STATE_FILE", str(tmp_path / "pos.json"))
    broker = FakeExecBroker(fill_price=164.21)
    order = {"action": "BUY", "ticker": "SPCX", "notional_usd": 250,
             "entry": 165.40, "stop": 158.0, "target": 178.0,
             "setup": "event_flow", "hard_exit_date": "2026-08-01"}
    positions = {}
    result = orders.execute_order(order, broker, decision_id=None, positions=positions)
    assert "BUY SPCX" in result

    buys = _rows(temp_journal, "SELECT * FROM trades WHERE action='BUY'")
    assert len(buys) == 1
    assert buys[0]["price"] == pytest.approx(164.21)     # fill, not 165.40
    assert buys[0]["broker_order_id"] == "br-SPCX"
    assert positions["SPCX"]["entry_price"] == pytest.approx(164.21)


class FakeClosedBuy:
    def __init__(self, id, symbol, qty, price):
        self.id = id
        self.symbol = symbol
        self.side = "buy"
        self.filled_qty = qty
        self.filled_avg_price = price
        self.filled_at = datetime.now(timezone.utc)
        self.order_type = "market"


class FakeSyncBroker:
    def __init__(self, closed):
        self._closed = closed

    def get_closed_orders_since(self, since_utc):
        return self._closed


def test_sync_corrects_reference_priced_buy_and_recomputes_pnl(
        temp_journal, tmp_path, monkeypatch):
    monkeypatch.setattr(orders, "POSITIONS_STATE_FILE", str(tmp_path / "pos.json"))
    # BUY journaled at the sheet reference 165.40 (no order id), exit at 157.82:
    decision_id = temp_journal.log_decision("SPCX", "event_flow", {},
                                            {"approved": True, "conviction_score": 90})
    buy_id = temp_journal.log_trade("SPCX", "BUY", 1, 165.40, decision_id=decision_id)
    sell_id = temp_journal.log_trade("SPCX", "SELL", 1, 157.82,
                                     pnl_usd=-7.58, pnl_pct=-4.58,
                                     reason="Stop Loss", decision_id=decision_id,
                                     broker_order_id="sell-1")
    temp_journal.link_outcome(decision_id, sell_id, -7.58, -4.58)

    # Broker says the BUY actually filled at 164.21.
    broker = FakeSyncBroker([FakeClosedBuy("buy-1", "SPCX", 1, 164.21)])
    orders.sync(broker=broker)

    buy = _rows(temp_journal, f"SELECT * FROM trades WHERE id={buy_id}")[0]
    sell = _rows(temp_journal, f"SELECT * FROM trades WHERE id={sell_id}")[0]
    dec = _rows(temp_journal, f"SELECT * FROM decisions WHERE id={decision_id}")[0]
    assert buy["price"] == pytest.approx(164.21)
    assert buy["broker_order_id"] == "buy-1"
    assert sell["pnl_usd"] == pytest.approx(-6.39, abs=0.005)   # (157.82-164.21)*1
    assert dec["outcome_pnl_usd"] == pytest.approx(-6.39, abs=0.005)


def test_week1_migration_idempotent(temp_journal):
    d = temp_journal.log_decision("MRVL", "mean_reversion_reclaim", {},
                                  {"approved": True, "conviction_score": 80})
    temp_journal.log_trade("MRVL", "BUY", 1, 243.27, decision_id=d)
    s = temp_journal.log_trade("MRVL", "SELL", 1, 235.91, pnl_usd=-7.36,
                               pnl_pct=-3.03, decision_id=d, broker_order_id="x1")
    temp_journal.link_outcome(d, s, -7.36, -3.03)

    corrections = {"MRVL": (243.27, 239.19)}
    assert temp_journal.apply_fill_corrections(corrections) == 1
    assert temp_journal.apply_fill_corrections(corrections) == 0   # idempotent

    sell = _rows(temp_journal, f"SELECT * FROM trades WHERE id={s}")[0]
    assert sell["pnl_usd"] == pytest.approx(-3.28, abs=0.005)      # (235.91-239.19)
    dec = _rows(temp_journal, f"SELECT * FROM decisions WHERE id={d}")[0]
    assert dec["outcome_pnl_usd"] == pytest.approx(-3.28, abs=0.005)


# =============================================================================
# 9.2 — type guards: clean rejections, never tracebacks
# =============================================================================

def buy_order(**overrides):
    order = {"action": "BUY", "ticker": "NVDA", "notional_usd": 250,
             "entry": 100.0, "stop": 95.0, "target": 110.0,
             "setup": "trend_continuation"}
    order.update(overrides)
    return order


@pytest.mark.parametrize("field,value,reason", [
    ("entry", "abc", "invalid_entry_price"),
    ("entry", True, "invalid_entry_price"),
    ("stop", "not-a-price", "invalid_stop_price"),
    ("target", [181], "invalid_target_price"),
    ("notional_usd", "300", "invalid_notional"),
    ("abort_if_open_below", "x", "invalid_abort_level"),
])
def test_non_numeric_fields_rejected_cleanly(field, value, reason):
    ok, r = validate_order(buy_order(**{field: value}), 1000.0, 0, 3)
    assert not ok
    assert r == reason


def test_tighten_stop_non_numeric_rejected():
    order = {"action": "TIGHTEN_STOP", "ticker": "NVDA", "stop": "oops"}
    ok, r = validate_order(order, 1000.0, 0, 3)
    assert not ok
    assert r == "invalid_stop_price"


# =============================================================================
# 9.4 — gap-abort (Rule #3)
# =============================================================================

def reclaim_df_with_open(entry_open):
    rows = [(100, 100.5, 99, 100, 100_000)] * 10
    for i in range(13):
        c = 98 - i
        rows.append((c + 0.5, c + 1, c - 1, c, 100_000))
    rows.append((85, 86, 84.5, 85.5, 100_000))
    rows.append((85.5, 88.0, 85, 87, 100_000))            # prior bar
    rows.append((87, 90.5, 87.5, 90, 150_000))            # reclaim bar: mid = 89.0
    rows.append((entry_open, entry_open + 1, entry_open - 0.5,
                 entry_open + 0.5, 110_000))              # entry bar
    idx = pd.date_range("2026-06-01", periods=len(rows), freq="D")
    return pd.DataFrame(rows, index=idx,
                        columns=["open", "high", "low", "close", "volume"])


RISK_PROFILE = {"fast_ema": 9, "slow_ema": 21, "adx_threshold": 10,
                "risk_per_trade_pct": 1.0, "rr_ratio": 2.0, "atr_multiplier": 2.0,
                "trailing_stop_type": "ATR", "trailing_stop_value": 2.0,
                "use_volume_filter": False}


def test_reclaim_aborts_on_gap_below_midpoint():
    # Reclaim bar high 90.5 / low 87.5 -> midpoint 89.0; open 88.0 gaps below.
    df = reclaim_df_with_open(88.0)
    result = MeanReversionReclaim().detect(
        df, {"ticker": "TEST", "risk_profile": RISK_PROFILE, "config": {}})
    assert isinstance(result, Rejection)
    assert result.filter_name == "gap_below_reclaim_mid"


def test_reclaim_fires_when_open_holds_midpoint():
    df = reclaim_df_with_open(90.0)                       # open above 89.0
    result = MeanReversionReclaim().detect(
        df, {"ticker": "TEST", "risk_profile": RISK_PROFILE, "config": {}})
    assert isinstance(result, Signal)


def test_ingest_gap_abort_rejects_and_journals_pass(temp_journal, tmp_path, monkeypatch):
    monkeypatch.setattr(orders, "POSITIONS_STATE_FILE", str(tmp_path / "pos.json"))
    monkeypatch.setattr(universe, "UNIVERSE_FILE", str(tmp_path / "universe.json"))
    sheet = {"session": "test", "regime": "test",
             "orders": [buy_order(ticker="FCX", entry=60.0, stop=57.0,
                                  target=66.0, notional_usd=120,
                                  abort_if_open_below=59.0)]}
    sheet_path = tmp_path / "sheet.json"
    sheet_path.write_text(json.dumps(sheet))

    broker = FakeExecBroker(latest_price=58.0)            # gapped below 59.0
    orders.ingest(str(sheet_path), broker=broker)

    assert broker.submitted == []                         # never reached the broker
    passes = _rows(temp_journal,
                   "SELECT * FROM decisions WHERE source='rules'")
    assert len(passes) == 1
    assert "gap_below_abort_level" in passes[0]["verdict"]


# =============================================================================
# 9.5 — junior-analyst role framing
# =============================================================================

def test_junior_analyst_prompt_has_observed_framing():
    junior = prompts.get_system_prompt("junior_analyst")
    assert junior.startswith(prompts.GATEKEEPER_SYSTEM_PROMPT)
    assert "junior analyst" in junior.lower()
    assert "not penalized for disagreeing" in junior.lower()
    assert "lower" in junior.lower() and "conviction_score" in junior


def test_gatekeeper_prompt_unchanged():
    assert prompts.get_system_prompt("gatekeeper") == prompts.GATEKEEPER_SYSTEM_PROMPT
    assert "junior" not in prompts.GATEKEEPER_SYSTEM_PROMPT.lower()


def test_local_analyst_uses_junior_prompt():
    import local_analyst
    assert local_analyst.SYSTEM_PROMPT == prompts.get_system_prompt("junior_analyst")


# =============================================================================
# 9.6 — universe context in journaled CEO decisions
# =============================================================================

def test_ingest_attaches_universe_context(temp_journal, tmp_path, monkeypatch):
    monkeypatch.setattr(orders, "POSITIONS_STATE_FILE", str(tmp_path / "pos.json"))
    ufile = tmp_path / "universe.json"
    ufile.write_text(json.dumps({
        "date": "2026-07-12",
        "candidates": [{"symbol": "FCX", "price": 58.75,
                        "avg_dollar_volume": 61e6, "change_pct": -3.7,
                        "source": "core_watch", "setup_flag": "washout_reclaim"}],
    }))
    monkeypatch.setattr(universe, "UNIVERSE_FILE", str(ufile))

    sheet = {"session": "test", "regime": "test",
             "orders": [buy_order(ticker="FCX", entry=60.0, stop=57.0,
                                  target=66.0, notional_usd=120)]}
    sheet_path = tmp_path / "sheet.json"
    sheet_path.write_text(json.dumps(sheet))

    orders.ingest(str(sheet_path), broker=FakeExecBroker(fill_price=60.05))

    ceo = _rows(temp_journal, "SELECT * FROM decisions WHERE source='ceo'")
    assert len(ceo) == 1
    context = json.loads(ceo[0]["context"])
    assert context["universe_context"]["setup_flag"] == "washout_reclaim"
    assert context["universe_context"]["source"] == "core_watch"
