"""CEO 2026-10-24: Claude credit report - api_usage journal, $ cost, credit
left, Discord lines, low-credit alerts. No network: fake clients/brokers."""

import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytz

import claude_integration
import credit
import journal

ET = pytz.timezone("US/Eastern")
PRICES = {"_note": "x",
          "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.2},
          "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.3}}


def _at(s):
    return ET.localize(datetime.strptime(s, "%Y-%m-%d %H:%M"))


def _u(i=0, o=0, cw=0, cr=0):
    return {"input_tokens": i, "output_tokens": o, "cache_write_tokens": cw,
            "cache_read_tokens": cr}


def _rows():
    conn = sqlite3.connect(journal.DB_FILE)
    try:
        return conn.execute("SELECT purpose, model, input_tokens, output_tokens, "
                            "cost_usd FROM api_usage ORDER BY id").fetchall()
    finally:
        conn.close()


# ------------------------------------------------------------- pricing

def test_cost_uses_list_prices_per_million():
    assert credit.cost_usd("claude-sonnet-5", _u(1_000_000, 1_000_000), PRICES) == (12.0, True)
    assert credit.cost_usd("claude-sonnet-4-6", _u(2000, 500, 1000, 10000), PRICES) == (
        round((2000 * 3 + 500 * 15 + 1000 * 3.75 + 10000 * 0.3) / 1e6, 6), True)


def test_dated_model_id_matches_its_family_and_unknown_is_priced_dear():
    assert credit.cost_usd("claude-sonnet-5-20260801", _u(0, 1_000_000), PRICES) == (10.0, True)
    cost, priced = credit.cost_usd("claude-mystery-9", _u(0, 1_000_000), PRICES)
    assert not priced and cost == 15.0          # dearest row, never an underestimate


def test_usage_of_reads_sdk_usage_including_cache_fields():
    resp = SimpleNamespace(usage=SimpleNamespace(
        input_tokens=120, output_tokens=40, cache_creation_input_tokens=7,
        cache_read_input_tokens=900))
    assert credit.usage_of(resp) == _u(120, 40, 7, 900)
    assert credit.usage_of(SimpleNamespace()) == _u()


def test_every_configured_anthropic_model_has_a_price():
    cfg = json.load(open("bot_config.json", encoding="utf-8"))
    for role in ("gatekeeper", "review", "fallback"):
        assert credit.price_for(cfg["models"][role], cfg["anthropic_prices"]), role
    assert "platform.claude.com" in cfg["anthropic_prices"]["_note"]
    assert set(cfg["anthropic_credit"]) >= {"balance_usd", "as_of"}


# ------------------------------------------------------------- record / status

def test_record_and_status_remaining_today_avg_days():
    cfg = {"anthropic_prices": PRICES,
           "anthropic_credit": {"balance_usd": 10.0, "as_of": "2026-10-20"}}
    # 2026-10-19 is before as_of: counts in the 7-day average, not against the balance
    credit.record("gatekeeper", "claude-sonnet-5", usage=_u(0, 100_000), config=cfg,
                  now=_at("2026-10-19 10:00"))                       # $1.00
    credit.record("review", "claude-sonnet-5", usage=_u(0, 200_000), config=cfg,
                  now=_at("2026-10-22 16:40"))                       # $2.00
    credit.record("gatekeeper", "claude-sonnet-5", usage=_u(500_000, 0), config=cfg,
                  now=_at("2026-10-24 09:40"))                       # $1.00
    s = credit.status(cfg, now=_at("2026-10-24 16:20"))
    assert round(s["spent_since"], 6) == 3.0
    assert round(s["remaining"], 6) == 7.0
    assert round(s["today"], 6) == 1.0
    assert round(s["avg7"], 6) == round(4.0 / 7, 6)
    assert round(s["days_left"], 3) == round(7.0 / (4.0 / 7), 3)
    assert [r[0] for r in _rows()] == ["gatekeeper", "review", "gatekeeper"]


def test_as_of_timestamp_excludes_earlier_spend_that_day():
    cfg = {"anthropic_prices": PRICES,
           "anthropic_credit": {"balance_usd": 5.0, "as_of": "2026-10-24 12:00:00"}}
    credit.record("gatekeeper", "claude-sonnet-5", usage=_u(0, 100_000), config=cfg,
                  now=_at("2026-10-24 09:40"))
    credit.record("gatekeeper", "claude-sonnet-5", usage=_u(0, 100_000), config=cfg,
                  now=_at("2026-10-24 13:00"))
    s = credit.status(cfg, now=_at("2026-10-24 16:20"))
    assert round(s["remaining"], 6) == 4.0 and round(s["today"], 6) == 2.0


def test_no_balance_configured_shows_not_set_and_never_raises():
    s = credit.status({"anthropic_prices": PRICES}, now=_at("2026-10-24 09:30"))
    assert s["remaining"] is None and s["days_left"] is None
    assert "balance not set" in credit.credit_line(s)
    assert credit.alerts(s) == []


def test_record_never_raises_on_a_bad_db(tmp_path):
    assert credit.record("gatekeeper", "claude-sonnet-5", usage=_u(1, 1),
                         config={"anthropic_prices": PRICES},
                         db=str(tmp_path / "no" / "such" / "dir.db")) is None


# ------------------------------------------------------------- text / alerts

def test_lines_match_the_ceo_format():
    s = {"remaining": 12.345, "today": 0.4, "avg7": 1.5, "days_left": 8.2,
         "balance": 20, "as_of": "x", "spent_since": 7.655}
    assert credit.credit_line(s) == ("💳 Claude credit: ~$12.35 left · today $0.40 · "
                                     "7-day avg $1.50/day → ~8 days")
    acct = SimpleNamespace(equity="5012.5", cash="3100", buying_power="6200.75")
    b = SimpleNamespace(trading=SimpleNamespace(get_account=lambda: acct))
    assert credit.account_line(b) == ("💵 Account: equity $5,012.50 · cash $3,100.00 · "
                                      "buying power $6,200.75")
    bad = SimpleNamespace(trading=SimpleNamespace(get_account=lambda: 1 / 0))
    assert credit.account_line(bad).startswith("💵 Account: unavailable")


def test_alert_when_less_than_seven_days_left_or_preflight_fails():
    base = {"balance": 10, "as_of": "x", "spent_since": 0, "today": 0}
    low = dict(base, remaining=6.0, avg7=1.0, days_left=6.0)
    ok = dict(base, remaining=8.0, avg7=1.0, days_left=8.0)
    gone = dict(base, remaining=-0.5, avg7=0.0, days_left=None)
    assert len(credit.alerts(low)) == 1 and credit.alerts(low)[0].startswith("⚠️ Claude credit low")
    assert credit.alerts(ok) == []
    assert len(credit.alerts(gone)) == 1
    pf = {"ok": False, "reason": "billing", "detail": "credit balance is too low"}
    msgs = credit.alerts(ok, pf)
    assert len(msgs) == 1 and "pre-flight FAILED (billing)" in msgs[0]
    assert credit.alerts(ok, {"ok": True}) == []


def test_post_respects_the_discord_toggle(monkeypatch):
    import alerts
    sent = []
    monkeypatch.setattr(alerts, "post_async", lambda m, *a, **k: sent.append(m))
    assert credit.post(["a", "b"], config={"discord": {"credit_report": False}}) == 0
    assert credit.post(["a", "b"], config={"discord": {"credit_report": True}}) == 2
    assert sent == ["a", "b"]


# ------------------------------------------------------------- call sites

class _FakeMessages:
    def __init__(self, text='{"approved": false}'):
        self.text = text

    def create(self, **kw):
        return SimpleNamespace(
            model=kw["model"], content=[SimpleNamespace(type="text", text=self.text)],
            usage=SimpleNamespace(input_tokens=1500, output_tokens=300,
                                  cache_creation_input_tokens=0,
                                  cache_read_input_tokens=0))


def test_gatekeeper_call_is_journaled_with_tokens_and_cost(monkeypatch):
    monkeypatch.setattr(claude_integration, "_get_client",
                        lambda: SimpleNamespace(messages=_FakeMessages()))
    monkeypatch.setattr(claude_integration, "get_model", lambda role="gatekeeper": "claude-sonnet-5")
    out = claude_integration.call_claude_api("sys", "user", purpose="gatekeeper")
    assert out == {"approved": False}
    (purpose, model, i, o, cost), = _rows()
    assert (purpose, model, i, o) == ("gatekeeper", "claude-sonnet-5", 1500, 300)
    assert round(cost, 6) == round((1500 * 2 + 300 * 10) / 1e6, 6)


def test_preflight_is_journaled(monkeypatch):
    monkeypatch.setattr(claude_integration, "get_model", lambda role="gatekeeper": "claude-sonnet-5")
    r = claude_integration.credit_preflight(SimpleNamespace(messages=_FakeMessages("ok")))
    assert r["ok"] and [x[0] for x in _rows()] == ["preflight"]


def test_gatekeeper_review_worker_and_dashboard_are_wired():
    ci = open("claude_integration.py", encoding="utf-8").read()
    assert 'purpose="gatekeeper"' in ci[ci.index("def get_gatekeeper_decision("):]
    rb = open("review_bot.py", encoding="utf-8").read()
    assert rb.index('_record_usage("review", model, resp)') > rb.index("stream.get_final_message()")
    app = open("streamlit_app.py", encoding="utf-8").read()
    assert "credit.post(credit.report(broker, _pf))" in app
    assert "if not desk_config.in_worker():\n    for _line in _credit_lines(" in app
