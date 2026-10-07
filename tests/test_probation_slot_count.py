"""
Reclaim probation slot count (fix-probation-count, 2026-10-07).

THE BUG. The concurrency gate counted every open position with
setup == mean_reversion_reclaim. Probation counts from prompt_version v5
(setup_probation.count_from_prompt_version), but the two open v4 reclaims,
SPCX and SWKS, filled the single slot, so every v5 approval was passed as
probation_position_open: DDOG 71 / LRCX 76 / NET 78 on 09-23, PGR 74 on
09-29. The floor read "(2/1 slot in use)".

THE FIX. risk.probation_open_count counts only positions inside the
probation's prompt-version window, by the record's own prompt_version or,
for older records, its decision_id resolved through the journal. A record
with neither is pre-versioning and does not hold the slot - the same rule
journal.live_entry_count applies. The worker gate, the floor and the report
all call it.
"""

import json
import shutil

import pytest

import floor
import journal as journal_mod
import report
import risk
import streamlit_app as app

CONFIG = json.load(open("bot_config.json", encoding="utf-8"))
RECLAIM = "mean_reversion_reclaim"


def reclaim(prompt_version=None, decision_id=None, in_position=True):
    record = {"in_position": in_position, "setup": RECLAIM,
              "decision_id": decision_id}
    if prompt_version is not None:
        record["prompt_version"] = prompt_version
    return record


def v4_book():
    """The live book on the old account: two v4 reclaims."""
    return {"SPCX": reclaim(prompt_version=4), "SWKS": reclaim(prompt_version=4)}


def gate(positions, live_n=0, resolver=None):
    open_n = risk.probation_open_count(RECLAIM, positions, CONFIG, resolver)
    return risk.check_setup_probation(RECLAIM, open_n, live_n, CONFIG)


def no_journal(decision_id):
    raise AssertionError("a record carrying prompt_version must not hit the journal")


# ================================================ the gate

def test_the_config_under_test_still_scopes_reclaim_from_v5():
    """Everything below assumes the ratified scope; fail loudly if it moves."""
    assert risk.probation_min_prompt_version(RECLAIM, CONFIG) == 5
    assert risk.probation_max_concurrent(CONFIG) == 1


def test_two_open_v4_reclaims_do_not_block_a_v5_signal():
    """The live case. SPCX and SWKS are v4; the slot is free."""
    assert risk.probation_open_count(RECLAIM, v4_book(), CONFIG,
                                     resolver=no_journal) == 0
    assert gate(v4_book(), resolver=no_journal) == (True, None)


def test_one_open_v5_reclaim_blocks_the_next():
    """The probation still bounds exposure: one v5 reclaim at a time."""
    book = dict(v4_book(), DDOG=reclaim(prompt_version=5))
    assert risk.probation_open_tickers(RECLAIM, book, CONFIG,
                                       resolver=no_journal) == ["DDOG"]
    assert gate(book, resolver=no_journal) == (False, "probation_position_open")


def test_a_later_version_still_holds_the_slot():
    assert gate({"NET": reclaim(prompt_version=6)}, resolver=no_journal) \
        == (False, "probation_position_open")


def test_a_closed_v5_position_does_not_hold_the_slot():
    book = {"LRCX": reclaim(prompt_version=5, in_position=False)}
    assert gate(book, resolver=no_journal) == (True, None)


def test_other_setups_never_hold_the_reclaim_slot():
    book = {"ARM": {"in_position": True, "setup": "pullback_in_uptrend",
                    "prompt_version": 5}}
    assert risk.probation_open_count(RECLAIM, book, CONFIG,
                                     resolver=no_journal) == 0


# ================================================ legacy records

def test_a_legacy_record_with_a_v5_decision_id_is_counted(temp_journal):
    """Positions opened before the record stored prompt_version resolve it
    through decision_id -> decisions.context.prompt_version."""
    decision_id = temp_journal.log_decision(
        "PGR", RECLAIM, {"prompt_version": 5},
        {"approved": True, "conviction_score": 74})
    book = {"PGR": reclaim(decision_id=decision_id)}
    assert risk.probation_open_count(RECLAIM, book, CONFIG) == 1
    assert gate(book) == (False, "probation_position_open")


def test_a_legacy_record_with_a_v4_decision_id_is_not_counted(temp_journal):
    decision_id = temp_journal.log_decision(
        "SWKS", RECLAIM, {"prompt_version": 4}, {"approved": True})
    assert risk.probation_open_count(
        RECLAIM, {"SWKS": reclaim(decision_id=decision_id)}, CONFIG) == 0


def test_a_decision_from_before_versioning_is_not_counted(temp_journal):
    """SPCX/SWKS predate the stamp: their decision context has no version."""
    decision_id = temp_journal.log_decision(
        "SPCX", RECLAIM, {"setup": RECLAIM}, {"approved": True})
    assert temp_journal.decision_prompt_version(decision_id) is None
    assert risk.probation_open_count(
        RECLAIM, {"SPCX": reclaim(decision_id=decision_id)}, CONFIG) == 0


def test_a_record_with_neither_version_nor_decision_is_not_counted():
    """Pre-versioning by construction - same rule as live_entry_count."""
    book = {"SPCX": reclaim()}
    assert risk.probation_open_count(RECLAIM, book, CONFIG,
                                     resolver=no_journal) == 0


def test_an_unreachable_journal_fails_closed():
    """A lookup error is not evidence of v4: keep the slot occupied."""
    def broken(decision_id):
        raise OSError("database is locked")
    book = {"PGR": reclaim(decision_id=7)}
    assert risk.probation_open_count(RECLAIM, book, CONFIG, broken) == 1


def test_decision_prompt_version_handles_unknown_ids(temp_journal):
    assert temp_journal.decision_prompt_version(None) is None
    assert temp_journal.decision_prompt_version(99999) is None


# ================================================ unscoped setups unchanged

def test_a_setup_without_a_version_scope_counts_every_open_position():
    """pullback_in_uptrend has no count_from_prompt_version: as before,
    every open position for the setup holds the slot, versioned or not."""
    name = "pullback_in_uptrend"
    assert risk.probation_min_prompt_version(name, CONFIG) is None
    book = {"ARM": {"in_position": True, "setup": name},
            "AMD": {"in_position": True, "setup": name, "prompt_version": 3},
            "X": {"in_position": False, "setup": name}}
    assert risk.probation_open_count(name, book, CONFIG,
                                     resolver=no_journal) == 2
    ok, reason = risk.check_setup_probation(
        name, risk.probation_open_count(name, book, CONFIG), 0, CONFIG)
    assert (ok, reason) == (False, "probation_position_open")


def test_no_config_counts_everything():
    assert risk.probation_open_count(RECLAIM, v4_book(), {},
                                     resolver=no_journal) == 2


# ================================================ floor and report

@pytest.fixture
def desk(tmp_path, monkeypatch, temp_journal):
    """A working directory with the real config and a v4-only book."""
    shutil.copy("bot_config.json", tmp_path / "bot_config.json")
    (tmp_path / "positions.json").write_text(json.dumps(v4_book()),
                                             encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_the_floor_reads_zero_of_one_for_the_v4_only_book(desk):
    lines = floor.probation_section()
    line = next(l for l in lines if RECLAIM in l)
    assert "(0/1 slot in use)" in line


def test_the_floor_counts_a_v5_position(desk):
    (desk / "positions.json").write_text(
        json.dumps(dict(v4_book(), DDOG=reclaim(prompt_version=5))),
        encoding="utf-8")
    line = next(l for l in floor.probation_section() if RECLAIM in l)
    assert "(1/1 slot in use)" in line


class _OfflineBroker:
    """A flat $2,000 account; every other broker read fails, and each report
    section catches its own failure. No network."""
    def get_account(self):
        class Account:
            equity, cash = "2000", "2000"
        return Account()

    def get_positions(self):
        return []

    def __getattr__(self, name):
        def fail(*a, **k):
            from broker import BrokerError
            raise BrokerError("offline")
        return fail


def test_the_report_line_uses_the_same_count(desk, monkeypatch):
    import broker
    monkeypatch.setattr(broker, "Broker", _OfflineBroker)
    text = report.build_report()
    line = next(l for l in text.splitlines()
                if RECLAIM in l and "probation**" in l)
    assert "0/1 slot in use" in line


# ================================================ the worker

def _worker_src():
    with open("streamlit_app.py", encoding="utf-8") as f:
        return f.read()


def test_the_worker_gate_uses_the_shared_helper():
    src = _worker_src()
    idx = src.index("ok_prob, prob_reason = risk.check_setup_probation(")
    window = src[idx - 600:idx]
    assert "risk.probation_open_tickers(" in window
    assert 's.get("setup") == signal.setup_name' not in window


def test_the_pass_reason_names_the_counted_positions():
    src = _worker_src()
    idx = src.index("journal_pass_once(ticker, signal.setup_name, prob_reason,")
    assert "', '.join(prob_open)" in src[idx:idx + 400]


def test_the_entry_record_stores_the_prompt_version():
    src = _worker_src()
    record = src[src.index("positions[ticker] = {\n                \"in_position\": True,"):]
    record = record[:record.index("write_positions(positions)")]
    assert '"prompt_version": prompt_version,' in record
    # Set from the verdict, null on the rules-only path.
    assert "prompt_version = None    # rules-only entry" in src
    after_verdict = src[src.index("verdict, decision_id = analyst.get_verdict("):]
    assert ("prompt_version = prompts.GATEKEEPER_PROMPT_VERSION"
            in after_verdict[:500])


class _Pos:
    def __init__(self, symbol):
        self.symbol, self.avg_entry_price, self.qty = symbol, 100.0, 1


class _Broker:
    def get_positions(self):
        return [_Pos("SPCX")]

    def get_open_orders(self, symbol):
        return []


def test_a_reconciled_position_has_a_null_prompt_version():
    positions = app.reconcile_positions(_Broker(), {})
    assert "prompt_version" in positions["SPCX"]
    assert positions["SPCX"]["prompt_version"] is None
    assert risk.probation_open_count(RECLAIM, positions, CONFIG) == 0
