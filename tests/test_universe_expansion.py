"""
Boardroom #2 items 5 & 6 (2026-09-01): wide scan / narrow display, and a
second concurrent B-book slot. No network.

Measured on the live paper account 2026-09-01: 330 names fetched -> 142
qualified under the UNCHANGED $20M floor; universe refresh 9.6s (once per
session), per-cycle fetch + 423 detector evaluations 13.0s against a
300s cycle.
"""

import json

import pytest

import risk
import universe


def _cfg(**over):
    cfg = {"universe": {"min_price": 5, "max_price": 480,
                        "min_dollar_volume": 20_000_000,
                        "max_candidates": 20, "max_evaluated": 200,
                        "skip_etfs": True, "core_watchlist": []}}
    cfg["universe"].update(over)
    return cfg


def _cand(sym, adv=50e6, price=100.0, change=1.0):
    return {"symbol": sym, "price": price, "avg_dollar_volume": adv,
            "change_pct": change, "tradable": True, "exchange": "NASDAQ",
            "name": f"{sym} Inc"}


# ------------------------------------------------------------ 5. wide scan

def test_pool_is_the_requested_size_and_unique():
    """~150-200 liquid US names, no duplicates."""
    assert 150 <= len(universe.LIQUID_POOL) <= 200
    assert len(set(universe.LIQUID_POOL)) == len(universe.LIQUID_POOL)


def test_scan_pool_unions_core_watchlist_without_duplicates():
    cfg = _cfg(core_watchlist=["NVDA", "ZZZZ"])   # NVDA already in the pool
    pool = universe.scan_pool(cfg)
    assert "ZZZZ" in pool
    assert pool.count("NVDA") == 1
    assert len(pool) == len(set(pool))


def test_detectors_evaluate_far_more_than_the_display_cap():
    ranked = universe.filter_and_rank(
        [_cand(f"T{i:03d}", adv=50e6 + i) for i in range(150)], _cfg())
    assert len(ranked) == 150                       # full qualifying set
    assert len(universe.display_slice(ranked, _cfg())) == 20


def test_evaluated_set_is_capped_at_max_evaluated():
    ranked = universe.filter_and_rank(
        [_cand(f"T{i:03d}", adv=50e6 + i) for i in range(260)], _cfg())
    assert len(ranked) == 200


def test_dollar_volume_floor_is_unchanged_by_the_expansion():
    """The pool widens what we LOOK at. It must not widen what QUALIFIES."""
    ranked = universe.filter_and_rank(
        [_cand("RICH", adv=50e6), _cand("THIN", adv=19_999_999)], _cfg())
    assert [c["symbol"] for c in ranked] == ["RICH"]


def test_config_floor_still_governs():
    cfg = _cfg(min_dollar_volume=100e6)
    ranked = universe.filter_and_rank([_cand("MID", adv=50e6)], cfg)
    assert ranked == []


def test_fetch_seeds_from_the_standing_pool(monkeypatch):
    """Screener endpoints only surface what MOVED; the pool is the floor of
    coverage, so a quiet liquid name must still be fetched."""
    seen = {}

    class FakeBroker:
        def get_most_actives(self, top=50):
            return [{"symbol": "MOVR"}]
        def get_market_movers(self, top=50):
            return []
        def get_assets_map(self, syms):
            return {}
        def get_daily_bars(self, syms, lookback_days=30):
            seen["asked"] = list(syms)
            return {}
        def get_latest_prices(self, syms):
            return {}

    universe.fetch_candidates(FakeBroker(), _cfg())
    assert "AAPL" in seen["asked"] and "MOVR" in seen["asked"]
    assert len(seen["asked"]) > 150


def test_refresh_records_runtime_and_counts(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(universe, "fetch_candidates",
                        lambda b, c: [_cand(f"T{i:03d}") for i in range(40)])
    monkeypatch.setattr(universe, "fetch_core_candidates", lambda b, c: [])
    ranked = universe.refresh(None, _cfg())

    payload = json.loads((tmp_path / universe.UNIVERSE_FILE).read_text())
    assert payload["scanned"] == 40
    assert payload["evaluated"] == len(ranked) == 40
    assert isinstance(payload["scan_seconds"], float)
    assert len(payload["display"]) == 20            # table stays readable
    assert len(universe.load_universe_tickers()) == 40   # detectors see all
    assert len(universe.load_display_candidates()) == 20
    assert universe.last_scan_stats()["evaluated"] == 40


def test_display_loader_tolerates_pre_expansion_files(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / universe.UNIVERSE_FILE).write_text(json.dumps({
        "date": "2026-09-01",
        "candidates": [_cand(f"T{i:02d}") for i in range(30)]}))
    assert len(universe.load_display_candidates()) == 20   # no "display" key
    assert universe.last_scan_stats() == {}


# ------------------------------------------------------------ 6. B-book x2

def test_two_concurrent_b_slots():
    assert risk.TIER_B_MAX_OPEN == 2
    assert risk.check_tier_b(0, 0)[0] is True
    assert risk.check_tier_b(1, 1)[0] is True          # second slot is open
    assert risk.check_tier_b(2, 1) == (False, "b_book_position_open")


def test_weekly_cadence_is_per_slot_not_doubled_risk():
    assert risk.TIER_B_ENTRIES_PER_WEEK == 2
    assert risk.check_tier_b(0, 2) == (False, "b_book_weekly_limit")
    # Half risk per slot is UNCHANGED — two slots, not two-sized bets.
    assert risk.TIER_B_RISK_PCT == 0.5
    assert risk.tier_risk_pct("B", 1.0) == 0.5
    assert risk.tier_risk_pct("A", 1.0) == 1.0


# ------------------------------------------- 5b. downstream of the expansion

def test_intern_scan_list_keeps_room_for_the_watchlist(monkeypatch, tmp_path):
    """Regression: the expansion made "candidates" ~150 names, so an intern
    scan list built from its head filled all 40 slots with universe names and
    silently dropped the entire core watchlist."""
    import intern_desk
    monkeypatch.chdir(tmp_path)
    (tmp_path / "universe_today.json").write_text(json.dumps({
        "date": "2026-09-01",
        "candidates": [_cand(f"U{i:03d}") for i in range(150)],
        "display": [_cand(f"U{i:03d}") for i in range(20)]}))
    watchlist = [f"W{i:02d}" for i in range(48)]
    (tmp_path / "bot_config.json").write_text(json.dumps(
        {"universe": {"core_watchlist": watchlist}}))

    scan = intern_desk.build_scan_list()
    assert len(scan) == intern_desk.MAX_SCAN_TICKERS
    covered = sum(1 for t in watchlist if t in scan)
    assert covered == 20, f"watchlist crowded out: only {covered} names"
    assert scan[0] == "U000"                 # best-ranked mover still first


def test_intern_scan_list_tolerates_a_pre_expansion_universe(monkeypatch,
                                                             tmp_path):
    import intern_desk
    monkeypatch.chdir(tmp_path)
    (tmp_path / "universe_today.json").write_text(json.dumps({
        "date": "2026-09-01",
        "candidates": [_cand(f"U{i:03d}") for i in range(150)]}))   # no display
    (tmp_path / "bot_config.json").write_text(json.dumps(
        {"universe": {"core_watchlist": [f"W{i:02d}" for i in range(48)]}}))
    scan = intern_desk.build_scan_list()
    assert sum(1 for t in scan if t.startswith("W")) == 20
