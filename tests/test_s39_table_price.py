"""Owner 2026-10-21: the Discord day table shows an open position's current
price (SELL column, marked '*') beside its P&L."""

from datetime import date
from types import SimpleNamespace as N

import trade_table as tt


class B:
    def get_positions(self):
        return [N(symbol="REGN", qty="1", avg_entry_price="802.61",
                  unrealized_pl="4.79", current_price="807.40")]


def test_live_positions_carry_the_current_price(tmp_path):
    pf = tmp_path / "p.json"
    pf.write_text("{}")
    assert tt.live_open_positions(B(), str(pf))["REGN"]["last"] == 807.40


def test_open_row_shows_price_now_with_a_star_and_a_footnote(tmp_path):
    pf = tmp_path / "p.json"
    pf.write_text('{"REGN": {"profit_target_price": 862.58, "trailing_stop_price": 756.25}}')
    lines = tt.day_table([], date(2026, 10, 20), tt.live_open_positions(B(), str(pf)))
    regn = [ln for ln in lines if ln.startswith("REGN")][0]
    assert regn == "REGN    1  802.6 807.4*  862.6  756.2  +4.79 open"
    assert "* SELL = price now; position still open" in lines
    assert all(len(ln) <= tt.WIDTH for ln in lines)


def test_closed_rows_and_unknown_prices_are_unchanged():
    assert "open" in tt.row_line("X", 1, 10.0, None, 12.0, 9.0, 0.5, "open")
    assert " 10.50 " in tt.row_line("X", 1, 10.0, 10.5, 12.0, 9.0, 0.5, "target")
    lines = tt.day_table([], date(2026, 10, 20), {"X": {"qty": 1, "entry": 10.0,
                                                        "unrealized": 0.1}})
    assert not any(ln.startswith("*") for ln in lines)
