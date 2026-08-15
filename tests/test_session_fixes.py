"""
Live-session fixes 2026-07-29. No network.

  1. singleton guard: worker refuses to start beside a live heartbeat;
     watchdog kills-then-launches, never launches beside survivors
  2. Sonnet 5 JSON: fences stripped, prose around the object tolerated
  3. daily re-ask: cache key uses the completed signal DATE, stable all
     session even as today's partial bar appears
  4. ADX rubric present in the SHARED judgment prompt (shadow + gatekeeper)
"""

import os
import time

import pandas as pd
import pytest

import claude_integration
import daily_eval
import prompts
import run_worker
import watchdog


# ------------------------------------------------------------------ 1

def test_worker_refuses_beside_live_heartbeat(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot_status.log").write_text("[now] Cycle #1")   # fresh
    assert run_worker.another_worker_is_alive() is True
    assert run_worker.main() == 1                                # refused
    assert not (tmp_path / "bot.run").exists()                   # no lock taken


def test_worker_starts_when_heartbeat_is_stale(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    status = tmp_path / "bot_status.log"
    status.write_text("[old] Cycle #1")
    old = time.time() - 3600
    os.utime(status, (old, old))
    assert run_worker.another_worker_is_alive() is False


def test_worker_starts_with_no_status_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run_worker.another_worker_is_alive() is False


def test_watchdog_aborts_if_kill_fails(tmp_path, monkeypatch):
    """Never launch beside survivors: if processes outlive the kill, the
    watchdog must abort rather than create a second instance."""
    import session_clock
    monkeypatch.setattr(session_clock, "in_session_window",
                        lambda cfg, now=None: True)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot.run").write_text("")
    status = tmp_path / "bot_status.log"
    status.write_text("[old] stale")
    old = time.time() - 9999
    os.utime(status, (old, old))

    launched, alerts = [], []
    monkeypatch.setattr(watchdog, "kill_stale_workers", lambda: 1)
    monkeypatch.setattr(watchdog, "find_worker_pids", lambda: [4242])  # survives
    monkeypatch.setattr(watchdog, "relaunch_worker",
                        lambda: launched.append(True) or True)
    monkeypatch.setattr(watchdog, "post_alert", lambda m: alerts.append(m))
    monkeypatch.setattr(watchdog.time, "sleep", lambda s: None)

    assert watchdog.main() == 1
    assert launched == []                       # no second instance
    assert alerts and "could not kill" in alerts[0]
    assert (tmp_path / "bot.run").exists()      # lock left for the operator


def test_watchdog_launches_when_kill_confirmed(tmp_path, monkeypatch):
    import session_clock
    monkeypatch.setattr(session_clock, "in_session_window",
                        lambda cfg, now=None: True)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot.run").write_text("")
    status = tmp_path / "bot_status.log"
    status.write_text("[old] stale")
    old = time.time() - 9999
    os.utime(status, (old, old))

    launched = []
    monkeypatch.setattr(watchdog, "kill_stale_workers", lambda: 2)
    monkeypatch.setattr(watchdog, "find_worker_pids", lambda: [])   # all dead
    monkeypatch.setattr(watchdog, "relaunch_worker",
                        lambda: launched.append(True) or True)
    monkeypatch.setattr(watchdog, "post_alert", lambda m: None)
    monkeypatch.setattr(watchdog.time, "sleep", lambda s: None)

    assert watchdog.main() == 0
    assert launched == [True]


# ------------------------------------------------------------------ 2

@pytest.mark.parametrize("raw,expected_key", [
    ('{"approved": true}', "approved"),
    ('```json\n{"approved": true}\n```', "approved"),
    ('```\n{"approved": true}\n```', "approved"),
    ('Here is my verdict:\n{"approved": true}\nHope that helps.', "approved"),
])
def test_strip_fences_handles_sonnet5_shapes(raw, expected_key):
    import json
    parsed = json.loads(claude_integration.strip_fences(raw))
    assert expected_key in parsed


def test_gatekeeper_token_budget_is_generous():
    import inspect
    src = inspect.getsource(claude_integration.get_gatekeeper_decision)
    assert "max_tokens=512" not in src
    assert "max_tokens=4000" in src        # room for thinking + verdict


# ------------------------------------------------------------------ 3

def _daily(days, include_today, today="2026-07-29"):
    n = days + (1 if include_today else 0)
    end = pd.Timestamp(today) if include_today else \
        pd.Timestamp(today) - pd.tseries.offsets.BDay(1)
    idx = pd.bdate_range(end=end, periods=n)
    rows = [(100.0, 102.0, 98.0, 101.0, 1e6)] * n
    return pd.DataFrame(rows, index=idx,
                        columns=["open", "high", "low", "close", "volume"])


def test_daily_cache_key_is_stable_when_todays_bar_appears():
    """The BA bug: before today's bar exists index[-2] is one day; after it
    appears index[-2] shifts — re-arming the cache mid-session. The DATE of
    the completed signal bar does not shift."""
    before = _daily(5, include_today=False)     # pre-open: no bar for today
    after = _daily(5, include_today=True)       # intraday: partial bar exists

    naive_before = str(before.index[-2])[:10]
    naive_after = str(after.index[-2])[:10]
    assert naive_before != naive_after          # the old key SHIFTED

    key_before = daily_eval.completed_bar_date(before, "2026-07-29")
    key_after = daily_eval.completed_bar_date(after, "2026-07-29")
    assert key_before == key_after == "2026-07-28"   # stable all session


# ------------------------------------------------------------------ 4

def test_adx_rubric_in_shared_judgment_prompt():
    p = prompts.build_gatekeeper_user_prompt(
        ticker="BA", candle_data_str="c", adx_val=43.5, rsi_val=55.0,
        ema_spread_pct=0.4, volume_trend="increasing", crossover_count=1,
        dist_to_resistance_pct=2.0, entry_price=100.0, stop_price=95.0,
        target_price=115.0, rr_ratio=3.0, interval_mins=5, fast_ema=9,
        slow_ema=21, news_str="none")
    assert "ADX below 20 = no trend" in p
    assert "25-40 = trending" in p
    assert "NEVER describe a value above 25 as low" in p
    assert "that is a contradiction" in p       # the no-ranging-above-25 rule
    # The shadow analyst uses this same builder, so the rubric reaches it.
    import local_analyst
    assert local_analyst.build_gatekeeper_user_prompt is \
        prompts.build_gatekeeper_user_prompt
