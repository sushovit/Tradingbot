"""
Local analyst + shadow-mode tests — Ollama HTTP endpoint is mocked, no network.

Verifies:
  (a) valid JSON from Ollama is parsed correctly
  (b) malformed JSON -> {"error": ...}, never a fake approval
  (c) a slow/hung local model in shadow mode never blocks the main decision path
  (d) shadow verdicts land in the journal with correct source and agreement flag
"""

import json
import sqlite3
import time

import pandas as pd
import pytest

import analyst
import local_analyst


VALID_VERDICT = {
    "approved": True,
    "conviction_score": 82,
    "market_regime": "Trending",
    "crossover_quality": "Clean",
    "rejection_reason": None,
    "key_risk": "resistance overhead",
    "reasoning": "Strong trend with volume confirmation.",
}


def make_df20():
    rows = 20
    return pd.DataFrame({
        "open": [100.0] * rows, "high": [101.0] * rows, "low": [99.0] * rows,
        "close": [100.5] * rows, "volume": [100_000] * rows,
        "ema_fast": [100.4] * rows, "ema_slow": [100.1] * rows,
        "rsi_14": [58.0] * rows, "adx_14": [31.0] * rows,
    })


def gk_kwargs():
    return {
        "ticker": "TEST", "df20": make_df20(), "ema_spread_pct": 0.3,
        "volume_trend": "increasing", "crossover_count": 1,
        "dist_to_resistance_pct": 2.5, "entry_price": 100.5, "stop_price": 98.0,
        "target_price": 105.5, "rr_ratio": 2.0, "interval_mins": 5,
        "fast_ema": 9, "slow_ema": 21, "news_headlines": [],
    }


class FakeResponse:
    def __init__(self, content: str, status: int = 200):
        self._content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        # Ollama native /api/chat response shape
        return {"message": {"content": self._content}}


def test_valid_json_parsed(monkeypatch):
    monkeypatch.setattr(local_analyst.requests, "post",
                        lambda *a, **k: FakeResponse(json.dumps(VALID_VERDICT)))
    result = local_analyst.get_gatekeeper_decision(**gk_kwargs())
    assert result["approved"] is True
    assert result["conviction_score"] == 82
    assert "error" not in result


def test_malformed_json_returns_error_not_approval(monkeypatch):
    monkeypatch.setattr(local_analyst.requests, "post",
                        lambda *a, **k: FakeResponse("this is not json {{{"))
    monkeypatch.setattr(local_analyst.time, "sleep", lambda s: None)  # skip backoff
    result = local_analyst.get_gatekeeper_decision(**gk_kwargs())
    assert "error" in result
    assert result.get("approved") is not True


def test_missing_keys_returns_error(monkeypatch):
    incomplete = {"approved": True}  # missing all other required keys
    monkeypatch.setattr(local_analyst.requests, "post",
                        lambda *a, **k: FakeResponse(json.dumps(incomplete)))
    result = local_analyst.get_gatekeeper_decision(**gk_kwargs())
    assert "error" in result


def test_shadow_timeout_never_blocks_main_path(temp_journal, monkeypatch):
    claude_verdict = dict(VALID_VERDICT)
    monkeypatch.setattr(analyst.claude_integration, "get_gatekeeper_decision",
                        lambda **k: claude_verdict)

    def hung_local(**kwargs):
        time.sleep(10)   # simulates a hung Ollama call
        return dict(VALID_VERDICT)
    monkeypatch.setattr(analyst.local_analyst, "get_gatekeeper_decision", hung_local)
    monkeypatch.setattr(analyst, "SHADOW_TIMEOUT_SECS", 1)

    start = time.monotonic()
    verdict, decision_id = analyst.get_verdict(
        "shadow", "TEST", "trend_continuation", {"entry": 100.5}, gk_kwargs())
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, "shadow call must not delay the trade path"
    assert verdict["approved"] is True
    assert decision_id is not None

    # The shadow thread journals a timeout error after ~1s.
    deadline = time.monotonic() + 5
    shadow_rows = []
    while time.monotonic() < deadline:
        conn = sqlite3.connect(temp_journal.DB_FILE)
        conn.row_factory = sqlite3.Row
        shadow_rows = conn.execute(
            "SELECT * FROM decisions WHERE source='local_shadow'").fetchall()
        conn.close()
        if shadow_rows:
            break
        time.sleep(0.2)
    assert len(shadow_rows) == 1
    verdict_json = json.loads(shadow_rows[0]["verdict"])
    assert verdict_json.get("error") == "timeout"


@pytest.mark.parametrize("local_approved,expected_agreement", [(True, 1), (False, 0)])
def test_shadow_verdict_journaled_with_agreement(temp_journal, monkeypatch,
                                                 local_approved, expected_agreement):
    claude_verdict = dict(VALID_VERDICT)   # Claude approves
    monkeypatch.setattr(analyst.claude_integration, "get_gatekeeper_decision",
                        lambda **k: claude_verdict)
    local_verdict = dict(VALID_VERDICT)
    local_verdict["approved"] = local_approved
    local_verdict["conviction_score"] = 60
    monkeypatch.setattr(analyst.local_analyst, "get_gatekeeper_decision",
                        lambda **k: local_verdict)

    verdict, _ = analyst.get_verdict("shadow", "TEST", "trend_continuation",
                                     {"entry": 100.5}, gk_kwargs())
    assert verdict["approved"] is True   # Claude is authoritative

    deadline = time.monotonic() + 5
    rows = []
    while time.monotonic() < deadline:
        conn = sqlite3.connect(temp_journal.DB_FILE)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM decisions WHERE source='local_shadow'").fetchall()
        conn.close()
        if rows:
            break
        time.sleep(0.2)

    assert len(rows) == 1
    row = rows[0]
    assert row["agreement"] == expected_agreement
    context = json.loads(row["context"])
    assert context["claude_approved"] is True
    assert context["claude_conviction"] == 82

    # Claude's authoritative row is journaled too, as source='claude'
    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.row_factory = sqlite3.Row
    claude_rows = conn.execute(
        "SELECT * FROM decisions WHERE source='claude'").fetchall()
    conn.close()
    assert len(claude_rows) == 1


def test_local_mode_journals_source_local(temp_journal, monkeypatch):
    local_verdict = dict(VALID_VERDICT)
    monkeypatch.setattr(analyst.local_analyst, "get_gatekeeper_decision",
                        lambda **k: local_verdict)
    claude_called = []
    monkeypatch.setattr(analyst.claude_integration, "get_gatekeeper_decision",
                        lambda **k: claude_called.append(1) or dict(VALID_VERDICT))

    verdict, decision_id = analyst.get_verdict(
        "local", "TEST", "trend_continuation", {}, gk_kwargs())
    assert verdict["approved"] is True
    assert not claude_called, "Claude must not be called in local mode"

    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM decisions").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["source"] == "local"
