"""
Goals 17-18 — daily review bot + model config. API fully mocked.
"""

import json
import sqlite3

import pytest

import claude_integration
import review_bot


# ---------------------------------------------------------------- Goal 18

def test_models_read_from_config(tmp_path, monkeypatch):
    cfg = tmp_path / "bot_config.json"
    cfg.write_text(json.dumps({"models": {"gatekeeper": "claude-sonnet-5",
                                          "review": "claude-sonnet-5",
                                          "fallback": "claude-sonnet-4-6"}}))
    monkeypatch.setattr(claude_integration, "CONFIG_FILE", str(cfg))
    assert claude_integration.get_model("gatekeeper") == "claude-sonnet-5"
    assert claude_integration.get_model("review") == "claude-sonnet-5"
    assert claude_integration.get_fallback_model() == "claude-sonnet-4-6"


def test_model_config_missing_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_integration, "CONFIG_FILE",
                        str(tmp_path / "nope.json"))
    assert claude_integration.get_model("gatekeeper") == "claude-sonnet-4-6"


def test_no_hardcoded_model_strings_in_call_paths():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for fname in ("claude_integration.py", "review_bot.py"):
        with open(os.path.join(root, fname), encoding="utf-8") as f:
            src = f.read()
        # The only allowed literal is the documented fallback constant.
        assert src.count('"claude-sonnet-5"') == 0, f"{fname} hardcodes a model"
        assert src.count('"claude-sonnet-4-6"') <= 1, \
            f"{fname} has stray model literals"


# ---------------------------------------------------------------- bundle

def test_bundle_assembly(tmp_path, monkeypatch, temp_journal):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "drop").mkdir()
    (tmp_path / "drop" / "latest.md").write_text("SESSION BUNDLE BODY",
                                                 encoding="utf-8")
    (tmp_path / "reports").mkdir()
    import clockline
    today = clockline.now_et().strftime("%Y-%m-%d")
    (tmp_path / "reports" / f"intern_{today}.md").write_text("INTERN BODY",
                                                             encoding="utf-8")
    monkeypatch.setattr(review_bot, "journal", temp_journal)

    class NoBroker:
        def __init__(self, *a, **k):
            raise RuntimeError("broker offline")
    monkeypatch.setitem(__import__("sys").modules, "broker",
                        type("m", (), {"Broker": NoBroker}))

    bundle = review_bot.collect_bundle()
    assert bundle["drop"] == "SESSION BUNDLE BODY"
    assert bundle["intern"] == "INTERN BODY"
    assert bundle["positions"] == []
    assert "broker_error" in bundle
    prompt = review_bot.build_user_prompt(bundle)
    assert "SESSION REVIEW" in prompt and "read-only" in prompt


def test_review_prompt_is_read_only():
    p = review_bot.REVIEW_SYSTEM_PROMPT
    assert "READ-ONLY" in p
    assert "cannot place, modify, or cancel" in p
    assert "Rule 3" in p and "Rule 1" in p          # cites rule numbers


def test_review_bot_imports_no_trading_path():
    import ast
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "review_bot.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "").split(".")[0]]
        assert "orders" not in names, "review bot imports the order path"
        assert "intern_trader" not in names


# ---------------------------------------------------------------- API paths

class FakeResp:
    def __init__(self, text):
        self.content = [type("c", (), {"text": text})]


def test_happy_path_journals_and_posts(temp_journal, monkeypatch):
    monkeypatch.setattr(review_bot, "journal", temp_journal)
    monkeypatch.setattr(review_bot, "collect_bundle",
                        lambda: {"date": "2026-07-26", "clock": "c",
                                 "positions": [], "trades": [], "equity": 2000.0,
                                 "realized_pnl": 0.0, "decision_count": 3,
                                 "drop": "d", "intern": "i"})
    monkeypatch.setattr(review_bot, "request_review",
                        lambda b: {"text": "The book was flat today.",
                                   "model": "claude-sonnet-5"})
    posted = []
    monkeypatch.setattr(review_bot, "post_discord",
                        lambda c, **kw: posted.append(c))

    assert review_bot.main() == 0
    assert posted and "flat today" in posted[0]
    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM decisions WHERE source='review_bot'").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["approved"] == 1


def test_api_error_posts_unavailable_and_exits_zero(temp_journal, monkeypatch):
    monkeypatch.setattr(review_bot, "journal", temp_journal)
    monkeypatch.setattr(review_bot, "collect_bundle",
                        lambda: {"date": "2026-07-26", "clock": "c",
                                 "positions": [], "trades": []})
    monkeypatch.setattr(review_bot, "request_review",
                        lambda b: {"error": "overloaded_error"})
    posted = []
    monkeypatch.setattr(review_bot, "post_discord", lambda c: posted.append(c))

    assert review_bot.main() == 0                    # never crashes
    assert "review unavailable" in posted[0].lower()
    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM decisions WHERE source='review_bot'").fetchall()
    conn.close()
    assert len(rows) == 1
    assert "review_unavailable" in rows[0]["verdict"]


def test_retries_capped_at_two(monkeypatch):
    calls = []

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                calls.append(kw)
                raise RuntimeError("overloaded_error")
    monkeypatch.setattr(claude_integration, "_get_client", lambda: Client())
    monkeypatch.setattr(review_bot.time, "sleep", lambda s: None)
    out = review_bot.request_review({"date": "d", "clock": "c",
                                     "positions": [], "trades": []})
    assert "error" in out
    assert len(calls) == review_bot.MAX_RETRIES + 1      # no retry flood


def test_prompt_caching_declared(monkeypatch):
    captured = {}

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                captured.update(kw)
                return FakeResp("ok")
    monkeypatch.setattr(claude_integration, "_get_client", lambda: Client())
    review_bot.request_review({"date": "d", "clock": "c", "positions": [],
                               "trades": []})
    assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}
