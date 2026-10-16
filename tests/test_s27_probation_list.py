"""
S27 item 1: the review's probation list is the floor's probation window -
prompt_version >= count_from_prompt_version, exclude_trade_ids dropped - so
its #n/20 always matches the floor's n/20.
"""

import json

import journal
import review_bot


def _buy(j, ticker, version, price=100.0, sell=None):
    did = j.log_decision(ticker, "mean_reversion_reclaim",
                         {"prompt_version": version} if version else {},
                         {"approved": True})
    tid = j.log_trade(ticker, "BUY", 1, price, reason="mean_reversion_reclaim",
                      decision_id=did)
    if sell is not None:
        j.log_trade(ticker, "SELL", 1, sell, pnl_usd=sell - price,
                    pnl_pct=(sell / price - 1) * 100, reason="Stop Loss",
                    decision_id=did)
    return tid


def test_review_list_is_exactly_the_floor_window(temp_journal, tmp_path, monkeypatch):
    for i in range(3):                                   # v4: outside the window
        _buy(temp_journal, f"OLD{i}", 4)
    _buy(temp_journal, "PRE", None)                      # pre-versioning
    t1 = _buy(temp_journal, "TXN", 5, 280.25, sell=278.5325)
    excluded = _buy(temp_journal, "PLTR", 5, 202.815)    # exclude_trade_ids
    t3 = _buy(temp_journal, "REGN", 5, 802.61)
    cfg = json.load(open("bot_config.json", encoding="utf-8"))
    cfg["setup_probation"]["exclude_trade_ids"] = [excluded]
    monkeypatch.chdir(tmp_path)
    json.dump(cfg, open(tmp_path / "bot_config.json", "w"))

    floor_n = journal.live_entry_count("mean_reversion_reclaim", 5,
                                       exclude_trade_ids=[excluded])
    rows = journal.probation_trades("mean_reversion_reclaim", 20, 5,
                                    exclude_trade_ids=[excluded])
    assert floor_n == len(rows) == 2
    assert [r["trade_id"] for r in rows] == [t1, t3]
    text = review_bot.probation_lines()
    assert "mean_reversion_reclaim: 2/20 probation trades so far" in text
    assert "#1/20" in text and "TXN" in text and "#2/20" in text and "REGN" in text
    assert "OLD" not in text and "PRE" not in text and "PLTR" not in text
    assert "closed -1.72 USD" in text and "still OPEN" in text


def test_the_exit_follows_the_trade_not_the_next_sell_of_the_ticker(temp_journal):
    a = _buy(temp_journal, "AMD", 5, 100.0)              # still open
    _buy(temp_journal, "AMD", 5, 110.0, sell=105.0)      # a later AMD trade's exit
    rows = {r["trade_id"]: r for r in journal.probation_trades(
        "mean_reversion_reclaim", 20, 5)}
    assert rows[a]["closed"] is False                    # not the later sell
