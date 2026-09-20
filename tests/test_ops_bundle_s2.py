"""
S2 (PM_PLAN.md / agenda 10.14): the ops bundle. No network.

Four unrelated ops gaps, each with its own incident behind it:

 (a) 2026-09-08, credits ran out at 09:36 ET. Four signals reached the
     gatekeeper and got errors. The desk failed closed, correctly — but
     silently, and the operator learned it from the next day's memo.
 (b) the review prompt carried a dated shadow snapshot that drifts, and a
     restart's artifacts were being graded as decisions.
 (c) "start it Monday" would have started the desk into Labor Day.
 (d) W8: a lost write left positions.json pointing at a superseded stop
     leg after the 2026-09-08 outage.
"""

import json

import pytest

import claude_integration as ci
import review_bot
import streamlit_app as app


# ============================================ (a) credit pre-flight

class FakeMessages:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return object()


class FakeClient:
    def __init__(self, error=None):
        self.messages = FakeMessages(error)


def test_preflight_reports_ok_on_a_working_key():
    client = FakeClient()
    result = ci.credit_preflight(client)
    assert result["ok"] is True and result["reason"] == "ok"
    assert client.messages.calls == 1          # ONE cheap call, not a loop


def test_preflight_names_a_billing_failure():
    """The 09-08 shape: the message says credits, and that does not clear on
    its own — it is worth an alert."""
    result = ci.credit_preflight(
        FakeClient(RuntimeError("Your credit balance is too low to access")))
    assert result["ok"] is False
    assert result["reason"] == "billing"
    assert "credit balance" in result["detail"]


def test_preflight_names_an_auth_failure():
    result = ci.credit_preflight(
        FakeClient(RuntimeError("authentication_error: invalid x-api-key")))
    assert result["reason"] == "auth"


def test_preflight_distinguishes_a_transient_outage():
    """A 500 is not a billing problem and must not read as one."""
    result = ci.credit_preflight(FakeClient(RuntimeError("503 overloaded")))
    assert result["reason"] == "unavailable"


def test_preflight_never_raises(monkeypatch):
    class Exploding:
        @property
        def messages(self):
            raise RuntimeError("client is broken")

    result = ci.credit_preflight(Exploding())
    assert result["ok"] is False                # returned, did not propagate


def test_preflight_handles_a_missing_key(monkeypatch):
    monkeypatch.setattr(ci, "_get_client", lambda: None)
    result = ci.credit_preflight()
    assert result["reason"] == "no_key"


def test_the_worker_journals_and_alerts_once_on_failure():
    """Source assertion: the startup path must journal the ops event and
    alert, then CONTINUE — the fail-closed gate handles the rest."""
    with open("streamlit_app.py", encoding="utf-8") as f:
        body = f.read()
    idx = body.index("credit_preflight()")
    block = body[idx:idx + 1400]
    assert 'journal.log_integrity_event(' in block
    assert '"anthropic_unavailable"' in block
    assert "send_discord_notification(" in block
    assert 'preflight["reason"] in ("billing", "auth", "no_key")' in block


# ============================================ (b) review prompt facts

def test_the_shadow_fact_is_computed_not_hardcoded(temp_journal, monkeypatch):
    """A dated snapshot drifts and then gets argued about: on 2026-09-20 the
    desk believed the baseline was ~38% while the journal held 10.4%."""
    monkeypatch.setattr(review_bot, "journal", temp_journal)
    for i in range(8):
        temp_journal.log_decision(f"T{i}", "reclaim", {},
                                  {"approved": False, "conviction_score": 10,
                                   "reasoning": "r"}, source="local_shadow")
    for i in range(2):
        temp_journal.log_decision(f"E{i}", "reclaim", {},
                                  {"error": "Ollama unreachable"},
                                  source="local_shadow")

    fact = review_bot.shadow_fact()
    assert "approved 0 of 10" in fact
    assert "20.0% error rate" in fact           # the REAL rate, whatever it is


def test_the_shadow_fact_survives_an_empty_journal(temp_journal, monkeypatch):
    monkeypatch.setattr(review_bot, "journal", temp_journal)
    assert "no shadow decisions" in review_bot.shadow_fact()


def test_the_rendered_prompt_carries_the_live_figure():
    prompt = review_bot.review_system_prompt()
    assert "{shadow_fact}" not in prompt         # the placeholder is filled
    assert "LOCAL SHADOW ANALYST" in prompt
    assert "ADVISORY and non-blocking" in prompt


def test_the_prompt_explains_restart_artifacts():
    prompt = review_bot.review_system_prompt()
    assert "A WORKER RESTART is an ops artifact, not a decision" in prompt
    assert "resets" in prompt and "cycle counter" in prompt
    assert "do NOT grade a re-ask" in prompt


def test_the_breakeven_reporting_fact_is_still_there():
    """S2 must not drop what W2 established."""
    prompt = review_bot.review_system_prompt()
    assert "max(ATR trail, entry price)" in prompt
    assert "measure it from ENTRY" in prompt


# ============================================ (c) scheduled start

def test_the_start_script_checks_the_trading_calendar_first():
    with open("jobs/start_worker.bat", encoding="utf-8") as f:
        body = f.read()
    assert "is_open_today()" in body
    assert "PYTHONUTF8=1" in body
    guard = body.index("is_open_today()")
    launch = body.index("run_worker.py")
    assert guard < launch, "the calendar check must precede the launch"
    assert "not a trading day" in body


def test_the_scheduler_xml_is_weekdays_only():
    with open("jobs/StartWorker.xml", encoding="utf-16") as f:
        xml = f.read()
    for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"):
        assert f"<{day} />" in xml
    for day in ("Saturday", "Sunday"):
        assert f"<{day} />" not in xml
    assert "T19:10:00" in xml
    assert "IgnoreNew" in xml                   # never two workers
    assert "start_worker.bat" in xml


def test_the_readme_documents_the_import():
    with open("README.md", encoding="utf-8") as f:
        body = f.read()
    assert "schtasks /Create" in body
    assert "StartWorker.xml" in body


def test_is_open_today_fails_open_when_the_calendar_is_unreachable():
    """A false SKIP silently loses a session; a false START is caught by
    session_clock. So unknown must mean 'start'."""
    import broker as broker_mod

    class Broken:
        def get_calendar(self, *a, **k):
            raise RuntimeError("no network")

    obj = broker_mod.Broker.__new__(broker_mod.Broker)
    obj.trading = Broken()
    assert obj.is_open_today() is True


# ============================================ (d) W8 order-id reconcile

class FakeOrder:
    def __init__(self, oid, otype, side="sell"):
        self.id = oid
        self.order_type = otype
        self.side = side


class FakeBroker:
    def __init__(self, live=None):
        self._live = live or {}

    def get_live_orders(self, ticker=None):
        return self._live.get(ticker, [])


def test_a_superseded_stop_id_is_refreshed_from_the_broker():
    """The 2026-09-08 state: positions.json held 8bd254f2 (the PRE-replace
    id) while the live leg was 7a28ad16 at 95.80."""
    positions = {"CRCL": {"in_position": True,
                          "stop_order_id": "8bd254f2",
                          "target_order_id": "e7b048da"}}
    broker = FakeBroker({"CRCL": [FakeOrder("7a28ad16", "stop"),
                                  FakeOrder("e7b048da", "limit")]})

    fixed = app.refresh_order_ids(broker, positions)

    assert positions["CRCL"]["stop_order_id"] == "7a28ad16"
    assert positions["CRCL"]["target_order_id"] == "e7b048da"
    assert [(t, f) for t, f, _, _ in fixed] == [("CRCL", "stop_order_id")]


def test_nothing_changes_when_state_already_agrees():
    positions = {"CRCL": {"in_position": True, "stop_order_id": "a",
                          "target_order_id": "b"}}
    broker = FakeBroker({"CRCL": [FakeOrder("a", "stop"),
                                  FakeOrder("b", "limit")]})
    assert app.refresh_order_ids(broker, positions) == []


def test_a_closed_position_is_not_touched():
    positions = {"OLD": {"in_position": False, "stop_order_id": "stale"}}
    assert app.refresh_order_ids(FakeBroker(), positions) == []
    assert positions["OLD"]["stop_order_id"] == "stale"


def test_no_live_leg_leaves_the_record_alone():
    """Absence of a working order is not evidence the id is wrong — the
    position may simply have no stop yet."""
    positions = {"CRCL": {"in_position": True, "stop_order_id": "keep"}}
    assert app.refresh_order_ids(FakeBroker({"CRCL": []}), positions) == []
    assert positions["CRCL"]["stop_order_id"] == "keep"


def test_the_entry_leg_is_never_mistaken_for_an_exit():
    positions = {"CRCL": {"in_position": True, "stop_order_id": None}}
    broker = FakeBroker({"CRCL": [FakeOrder("entry1", "market", side="buy")]})
    assert app.refresh_order_ids(broker, positions) == []


def test_a_broker_error_on_one_ticker_does_not_stop_the_rest():
    class Partial(FakeBroker):
        def get_live_orders(self, ticker=None):
            if ticker == "BAD":
                raise app.BrokerError("timeout")
            return [FakeOrder("good-stop", "stop")]

    positions = {"BAD": {"in_position": True, "stop_order_id": "x"},
                 "OK": {"in_position": True, "stop_order_id": "y"}}
    fixed = Partial() and app.refresh_order_ids(Partial(), positions)
    assert [t for t, _, _, _ in fixed] == ["OK"]
    assert positions["BAD"]["stop_order_id"] == "x"      # left as it was


def test_state_is_persisted_immediately_after_a_replace():
    """The root cause was a LOST WRITE: the only persistence was at the end
    of the cycle, so an exception mid-loop discarded the new leg id."""
    import position_mgmt
    import inspect
    src = inspect.getsource(position_mgmt.maybe_ratchet_stop)
    assert "persist" in src
    idx = src.index('positions[ticker]["stop_order_id"] = str(new_order.id)')
    assert "persist(positions)" in src[idx:idx + 600]

    with open("streamlit_app.py", encoding="utf-8") as f:
        body = f.read()
    assert "persist=write_positions" in body


def test_startup_refreshes_ids_before_the_loop_manages_anything():
    with open("streamlit_app.py", encoding="utf-8") as f:
        body = f.read()
    recon = body.index("positions = reconcile_positions(broker, positions)")
    refresh = body.index("refresh_order_ids(broker, positions)", recon)
    write = body.index("write_positions(positions)", refresh)
    assert recon < refresh < write
    assert '"stale_order_id"' in body
