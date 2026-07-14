"""
Goal 10 — journal integrity. All mocked, no network.

  10.1 single-authority exit journaling: idempotence keys on the broker's
       order id across ALL paths (bot loop, sync). Bot-first then sync = 1
       row; sync-first then bot = 1 row.
  Migrations: phantom-exit deletion and pass-flood purge, both idempotent.
"""

import sqlite3
from datetime import datetime, timezone

import pytest

import journal as journal_mod
import orders


def _rows(j, query):
    conn = sqlite3.connect(j.DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(query).fetchall()]
    conn.close()
    return rows


def _reset_migration_flags(j):
    conn = sqlite3.connect(j.DB_FILE)
    conn.execute("DELETE FROM meta WHERE key LIKE 'mig_%'")
    conn.commit()
    conn.close()


class FakeClosedSell:
    def __init__(self, id, symbol, qty=2, price=59.75, order_type="stop"):
        self.id = id
        self.symbol = symbol
        self.side = "sell"
        self.filled_qty = qty
        self.filled_avg_price = price
        self.filled_at = datetime.now(timezone.utc)
        self.order_type = order_type


class FakeBroker:
    def __init__(self, closed):
        self._closed = closed

    def get_closed_orders_since(self, since_utc):
        return self._closed


@pytest.fixture
def positions_file(tmp_path, monkeypatch):
    f = tmp_path / "positions.json"
    f.write_text("{}")
    monkeypatch.setattr(orders, "POSITIONS_STATE_FILE", str(f))
    return f


# =============================================================================
# 10.1 — single-authority exits
# =============================================================================

def test_bot_journals_first_then_sync_one_row(temp_journal, positions_file):
    temp_journal.log_trade("FCX", "BUY", 2, 60.65)
    # Bot loop sees the stop fill first (order id known via detect_filled_exit):
    trade_id, pnl, _ = temp_journal.record_exit(
        "FCX", 2, 59.75, "Stop Loss", broker_order_id="ord-stop-1",
        entry_price=60.65)
    assert trade_id is not None
    assert pnl == pytest.approx(-1.80)

    # Later, sync sees the same closed order.
    orders.sync(broker=FakeBroker([FakeClosedSell("ord-stop-1", "FCX")]))

    sells = _rows(temp_journal, "SELECT * FROM trades WHERE action='SELL'")
    assert len(sells) == 1
    assert sells[0]["broker_order_id"] == "ord-stop-1"


def test_sync_first_then_bot_one_row(temp_journal, positions_file):
    temp_journal.log_trade("FCX", "BUY", 2, 60.65)
    # Sync journals the fill first:
    orders.sync(broker=FakeBroker([FakeClosedSell("ord-stop-2", "FCX")]))
    sells = _rows(temp_journal, "SELECT * FROM trades WHERE action='SELL'")
    assert len(sells) == 1

    # The bot loop then notices the position is gone and resolves the SAME
    # order id — record_exit must skip silently.
    trade_id, pnl, pct = temp_journal.record_exit(
        "FCX", 2, 59.75, "Stop Loss (resolved)", broker_order_id="ord-stop-2",
        entry_price=60.65)
    assert trade_id is None and pnl is None and pct is None

    sells = _rows(temp_journal, "SELECT * FROM trades WHERE action='SELL'")
    assert len(sells) == 1


# =============================================================================
# Migration: phantom exit deletion
# =============================================================================

def test_phantom_exit_migration(temp_journal):
    d = temp_journal.log_decision("FCX", "mean_reversion_reclaim", {},
                                  {"approved": True, "conviction_score": 80})
    temp_journal.log_trade("FCX", "BUY", 2, 60.65, decision_id=d)

    # Phantom: bot fallback journaled at last price, no order id (launch-day bug).
    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.execute(
        "INSERT INTO trades (timestamp, ticker, action, qty, price, pnl_usd, "
        "pnl_pct, reason, decision_id) VALUES "
        "('2026-07-13 14:29:37','FCX','SELL',2,59.87,-1.56,-1.29,"
        "'Exit (fill details unavailable)',?)", (d,))
    conn.commit()
    conn.close()
    # Real fill, journaled by sync with the order id:
    real_id, pnl, pct = temp_journal.record_exit(
        "FCX", 2, 59.75, "Stop Loss (synced)", decision_id=d,
        broker_order_id="real-1", entry_price=60.65)

    _reset_migration_flags(temp_journal)
    temp_journal.run_data_migrations()

    sells = _rows(temp_journal, "SELECT * FROM trades WHERE action='SELL'")
    assert len(sells) == 1
    assert sells[0]["broker_order_id"] == "real-1"
    # Decision outcome still points at the surviving real row.
    dec = _rows(temp_journal, f"SELECT * FROM decisions WHERE id={d}")[0]
    assert dec["outcome_trade_id"] == real_id
    assert dec["outcome_pnl_usd"] == pytest.approx(-1.80)

    # Idempotent: second run changes nothing.
    temp_journal.run_data_migrations()
    assert len(_rows(temp_journal, "SELECT * FROM trades WHERE action='SELL'")) == 1


# =============================================================================
# Migration: gatekeeper replay collapse + unique-decision agreement stats
# =============================================================================

def _shadow_row(j, ticker, stop, approved=False, agreement=True,
                conviction=35, claude_conviction=22, claude_approved=None):
    if claude_approved is None:
        claude_approved = approved
    return j.log_decision(
        ticker, "mean_reversion_reclaim",
        {"setup": "mean_reversion_reclaim", "entry": stop + 2.0, "stop": stop,
         "target": stop + 6.0, "claude_approved": claude_approved,
         "claude_conviction": claude_conviction},
        {"approved": approved, "conviction_score": conviction,
         "market_regime": "Ranging", "crossover_quality": "Choppy",
         "rejection_reason": None if approved else "ADX low",
         "key_risk": "x", "reasoning": "y"},
        source="local_shadow", agreement=agreement)


def test_collapse_gatekeeper_replays_migration(temp_journal):
    # The re-ask loop: ONE XOM decision journaled 5 times (same day, same
    # bar-anchored stop), conviction jitter across replays.
    for conviction in (28, 28, 22, 22, 18):
        temp_journal.log_decision(
            "XOM", "mean_reversion_reclaim",
            {"setup": "mean_reversion_reclaim", "entry": 108.4, "stop": 106.10,
             "target": 112.0},
            {"approved": False, "conviction_score": conviction,
             "market_regime": "Ranging", "crossover_quality": "Choppy",
             "rejection_reason": "ADX low", "key_risk": "r", "reasoning": "s"},
            source="claude")
    # A genuinely distinct signal (different bar -> different stop) survives.
    distinct = temp_journal.log_decision(
        "XOM", "mean_reversion_reclaim",
        {"setup": "mean_reversion_reclaim", "entry": 109.0, "stop": 107.55,
         "target": 113.0},
        {"approved": False, "conviction_score": 30, "market_regime": "Ranging",
         "crossover_quality": "Choppy", "rejection_reason": "ADX low",
         "key_risk": "r", "reasoning": "s"},
        source="claude")
    # An approved decision referenced by a trade must NEVER be deleted.
    approved_id = temp_journal.log_decision(
        "FCX", "mean_reversion_reclaim",
        {"setup": "mean_reversion_reclaim", "entry": 60.65, "stop": 58.40,
         "target": 64.90},
        {"approved": True, "conviction_score": 80, "market_regime": "Trending",
         "crossover_quality": "Clean", "rejection_reason": None,
         "key_risk": "r", "reasoning": "s"},
        source="claude")
    temp_journal.log_trade("FCX", "BUY", 2, 60.65, decision_id=approved_id)

    _reset_migration_flags(temp_journal)
    temp_journal.run_data_migrations()

    claude_rows = _rows(temp_journal,
                        "SELECT * FROM decisions WHERE source='claude' ORDER BY id")
    assert len(claude_rows) == 3          # collapsed replay + distinct + approved
    assert claude_rows[0]["replays"] == 5  # replay count recorded on the keeper
    assert claude_rows[0]["conviction_score"] == 28  # first occurrence kept
    assert {r["id"] for r in claude_rows} >= {distinct, approved_id}


def test_agreement_report_counts_unique_decisions(temp_journal):
    # One decision replayed 3x (all agree)...
    for _ in range(3):
        _shadow_row(temp_journal, "XOM", stop=106.10, approved=False,
                    agreement=True)
    # ...plus one genuinely distinct decision where the local model DISAGREED
    # (local approved what Claude rejected).
    _shadow_row(temp_journal, "FIG", stop=22.80, approved=True,
                claude_approved=False, agreement=False, claude_conviction=40)

    rep = temp_journal.agreement_report()
    assert rep["raw_shadow_rows"] == 4
    assert rep["total_shadow_decisions"] == 2      # unique, not replayed
    assert rep["agreement_pct"] == 50.0            # 1 agree of 2 unique
    assert rep["local_approved_claude_rejected"] == 1


# =============================================================================
# Migration: pass-flood purge
# =============================================================================

def test_pass_flood_purge_keeps_first_per_bar_day(temp_journal):
    # Same rejection journaled 5x in one day (the 30s-cycle flood)...
    for _ in range(5):
        temp_journal.log_rules_pass("IREN", "mean_reversion_reclaim",
                                    "below_ema9", "flood")
    # ...plus one legitimately different rejection and one other ticker.
    temp_journal.log_rules_pass("IREN", "mean_reversion_reclaim", "volume_low", "x")
    temp_journal.log_rules_pass("NOK", "trend_continuation", "adx_low", "y")

    _reset_migration_flags(temp_journal)
    temp_journal.run_data_migrations()

    rows = _rows(temp_journal, "SELECT * FROM decisions WHERE source='rules'")
    assert len(rows) == 3   # 1 deduped IREN/below_ema9 + volume_low + NOK/adx_low
    reasons = sorted(set(
        (r["ticker"], __import__("json").loads(r["verdict"])["rejection_reason"])
        for r in rows))
    assert reasons == [("IREN", "below_ema9"), ("IREN", "volume_low"),
                       ("NOK", "adx_low")]
