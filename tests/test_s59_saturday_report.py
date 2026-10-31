"""CEO 2026-10-31 operating cadence: Saturday report sections, credit reminder,
rule freeze in the agenda header. No network."""

import json
import sqlite3
from datetime import date, datetime

import pytz

import credit
import saturday_report as sr

LOG = ["[2026-10-26 09:25:21] INFO: worker start PID 1 — cycle counter reset",
       "[2026-10-26 14:17:13] INFO: worker start PID 2 — cycle counter reset",
       "[2026-10-27 09:41:02] INFO: worker start PID 3 — cycle counter reset",
       "[2026-10-26 09:25:22] INFO: protection: 🛡️ IBM stop 250.42 ✅ target 271.28 ✅",
       "[2026-10-27 09:41:03] INFO: protection: ⚠️ TXN stop 294.92 🔧 target 322.68 ✅ - x"]


def test_start_times_take_the_first_start_each_day():
    assert sr.start_times(LOG) == {"2026-10-26": (9, 25), "2026-10-27": (9, 41)}


def test_golive_checklist_flags_each_test():
    closed = [{"buy": 100.0, "qty": 10, "orig_stop": 98.0, "pnl": 30.0}] * 30
    prot = sr.protection_lines(LOG, "2026-10-01")
    lines = sr.golive_lines(closed, [0, 5000, 5100, 4900, 5200], ["2026-10-26", "2026-10-27"],
                            sr.start_times(LOG), prot)
    text = "\n".join(lines)
    assert "✅ closed trades 30 / >= 30" in text and "✅ average +1.50R over 30 trades" in text
    assert "✅ max drawdown 3.9% / < 5%" in text
    assert "❌ on-time starts (by 09:30 ET) 1/2 sessions" in text and "late/missing: 10-27" in text
    assert "❌ protection checks: 1/2 green - red: 10-27" in text


def test_max_drawdown_skips_the_unfunded_zeros():
    assert sr.max_dd_pct([0.0, 0.0, 5000, 4750, 5100]) == 5.0


def test_orders_vs_main_marks_not_done():
    orders = [{"date": "2026-10-28", "order": "cadence", "status": "NOT DONE"},
              {"date": "2026-10-30", "order": "audit", "status": "038bea4"},
              {"date": "2026-10-10", "order": "old", "status": "abc1234"}]
    lines = sr.orders_lines(orders, date(2026, 10, 31))
    assert lines[1] == "❌ 10-28 cadence → NOT DONE" and lines[2] == "✅ 10-30 audit → 038bea4"
    assert len(lines) == 3                                    # older than a week left out


def test_the_ledger_ships_and_parses():
    data = json.load(open("ceo_orders.json", encoding="utf-8"))
    assert all({"date", "order", "status"} <= set(o) for o in data["orders"])


def test_graded_conviction_60_69_and_sector_cap(tmp_path):
    c = sqlite3.connect(str(tmp_path / "j.db"))
    c.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY, conviction_score REAL)")
    c.execute("CREATE TABLE rejection_outcomes (decision_id INTEGER, reason_key TEXT, outcome TEXT, "
              "r_achieved REAL, resolved INTEGER)")
    c.executemany("INSERT INTO decisions VALUES (?,?)", [(1, 65), (2, 62), (3, 55), (4, None)])
    c.executemany("INSERT INTO rejection_outcomes VALUES (?,?,?,?,?)", [
        (1, "gatekeeper_low_conviction", "target", 2.0, 1),
        (2, "gatekeeper_low_conviction", "stop", -1.0, 1),
        (3, "gatekeeper_low_conviction", "target", 3.0, 1),      # conviction 55: excluded
        (4, "sector_cap", "stop", -1.0, 1)])
    g = sr.graded(c, "gatekeeper_low_conviction", 60, 69)
    assert (g["n"], g["resolved"], g["win"], g["avg_r"]) == (2, 2, 50.0, 0.5)
    assert sr.graded_line("x", sr.graded(c, "sector_cap")) == "x: n 1 (1 resolved) · win 0% · avg -1.00R"


def test_credit_reminds_after_30_days_without_a_top_up(tmp_path):
    ET = pytz.timezone("US/Eastern")
    cfg = {"anthropic_prices": {}, "anthropic_credit": {"balance_usd": 10, "as_of": "2026-10-26"}}
    s = credit.status(cfg, db=str(tmp_path / "j.db"), now=ET.localize(datetime(2026, 11, 24, 16, 20)))
    assert s["topup_age_days"] == 29 and not any("top-up" in a for a in credit.alerts(s))
    s = credit.status(cfg, db=str(tmp_path / "j.db"), now=ET.localize(datetime(2026, 11, 25, 16, 20)))
    assert any(a.startswith("⚠️ No Anthropic top-up recorded in 30 days (balance set 2026-10-26)")
               for a in credit.alerts(s))


def test_rule_freeze_is_in_the_agenda_header():
    head = open("BOARDROOM_AGENDA.md", encoding="utf-8").read().split("\n---\n", 1)[0]
    assert "Rule freeze: no strategy or config changes" in head
    assert "Monday-Friday except bugs and safety" in head and "2027-01-15" in head
