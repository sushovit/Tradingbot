"""
Boardroom #2 item 7 (2026-09-01): crypto / digital-asset-treasury names are
NOT excluded and trade at standard risk — the condition is that the class be
measurable. These tests hold that condition: every fill carries a sector, the
exit inherits the entry's, and per-class expectancy is a query. No network.
"""

import risk
import sectors


# ------------------------------------------------------------ classification

def test_dat_and_miners_and_crypto_brokers_are_one_class():
    for sym in ("BMNR", "MSTR", "MARA", "RIOT", "COIN"):
        assert sectors.sector_for(sym) == "crypto_dat", sym
        assert sectors.is_crypto_dat(sym)


def test_ordinary_names_are_not_swept_into_the_crypto_class():
    assert sectors.sector_for("NVDA") == "technology"
    assert sectors.sector_for("XOM") == "energy"
    assert not sectors.is_crypto_dat("JPM")


def test_name_hint_catches_an_unlisted_treasury_microcap():
    """New DAT shells appear faster than we can maintain a list; an unlisted
    one must land in the class, not in 'unclassified'."""
    assert sectors.sector_for("ZZZZ", "Acme Bitcoin Treasury Corp") == "crypto_dat"
    assert sectors.sector_for("YYYY", "Acme Widgets Inc") == "unclassified"
    assert sectors.sector_for("") == "unclassified"


def test_every_scanned_liquid_name_is_classified():
    import universe
    unclassified = [t for t in universe.LIQUID_POOL
                    if sectors.sector_for(t) == "unclassified"]
    assert unclassified == []


# ------------------------------------------------------------ no exclusion

def test_crypto_dat_trades_at_standard_risk_not_half():
    """The ruling was 'no exclusion at standard risk'. A sector tag must not
    quietly become a risk haircut."""
    assert risk.tier_risk_pct("A", 1.0) == 1.0
    ok, reason = risk.check_signal(entry=100.0, stop=95.0, target=115.0,
                                   equity=2000.0, notional_usd=400.0)
    assert ok and reason is None       # nothing in risk.py knows about sectors


# ------------------------------------------------------------ measurement

def test_buy_is_tagged_automatically(temp_journal):
    tid = temp_journal.log_trade("BMNR", "BUY", 10, 20.0)
    assert temp_journal.entry_sector("BMNR") == "crypto_dat"
    assert tid > 0


def test_exit_inherits_the_entrys_sector(temp_journal):
    temp_journal.log_trade("ZZZZ", "BUY", 10, 20.0, sector="crypto_dat")
    temp_journal.record_exit("ZZZZ", 10, 18.0, "stop",
                             entry_price=20.0, broker_order_id="o1")
    rows = temp_journal.sector_expectancy()
    assert [r["sector"] for r in rows] == ["crypto_dat"]
    assert rows[0]["realized_usd"] == -20.0


def test_expectancy_splits_the_class_out_and_ranks_it(temp_journal):
    # crypto/DAT: one winner, one loser -> net negative
    for sym, entry, exit_, oid in (("MSTR", 100.0, 90.0, "a"),
                                   ("COIN", 100.0, 104.0, "b"),
                                   ("NVDA", 100.0, 110.0, "c")):
        temp_journal.log_trade(sym, "BUY", 1, entry)
        temp_journal.record_exit(sym, 1, exit_, "exit",
                                 entry_price=entry, broker_order_id=oid)
    rows = {r["sector"]: r for r in temp_journal.sector_expectancy()}
    assert rows["crypto_dat"]["trades"] == 2
    assert rows["crypto_dat"]["wins"] == 1 and rows["crypto_dat"]["losses"] == 1
    assert rows["crypto_dat"]["expectancy_usd"] == -3.0
    assert rows["technology"]["expectancy_usd"] == 10.0
    # Best expectancy first, so a losing class cannot hide at the top.
    order = [r["sector"] for r in temp_journal.sector_expectancy()]
    assert order[0] == "technology"


def test_expectancy_can_be_scoped_to_one_tier(temp_journal):
    temp_journal.log_trade("MSTR", "BUY", 1, 100.0, tier="B")
    temp_journal.record_exit("MSTR", 1, 90.0, "stop", entry_price=100.0,
                             broker_order_id="b1")
    assert temp_journal.sector_expectancy(tier="A") == []
    assert temp_journal.sector_expectancy(tier="B")[0]["sector"] == "crypto_dat"


def test_untagged_legacy_rows_are_reported_not_hidden(temp_journal):
    """A null sector must show up as 'unclassified' rather than vanishing
    from the totals."""
    import sqlite3
    temp_journal.log_trade("NVDA", "BUY", 1, 100.0)
    temp_journal.record_exit("NVDA", 1, 90.0, "stop", entry_price=100.0,
                             broker_order_id="x")
    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.execute("UPDATE trades SET sector=NULL")
    conn.commit()
    conn.close()
    rows = temp_journal.sector_expectancy()
    assert rows[0]["sector"] == "unclassified" and rows[0]["trades"] == 1
