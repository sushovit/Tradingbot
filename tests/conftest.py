import hashlib
import os
import sys
import tempfile

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import journal  # noqa: E402

# ------------------------------------------------------------------ isolation
# Tests must never create or write the repo-root journal.db or data/. Those
# are the live ledger and the live bar cache on the desk's host.
#
# The journal is redirected HERE, at conftest import, not only in a fixture:
# review_bot renders its prompt from the journal at module import, which
# happens during collection - before any fixture runs. That import is what
# used to create an empty ./journal.db on a fresh clone.
_SESSION_DIR = tempfile.mkdtemp(prefix="tradingbot-tests-")
journal.DB_FILE = os.path.join(_SESSION_DIR, "journal.db")
journal.init_db()


def _root_state():
    """Fingerprint of the repo-root files a test run must not touch."""
    state = {}
    db = os.path.join(REPO_ROOT, "journal.db")
    if os.path.exists(db):
        with open(db, "rb") as f:
            state["journal.db"] = (os.path.getmtime(db),
                                   hashlib.sha256(f.read()).hexdigest())
    else:
        state["journal.db"] = None
    data = os.path.join(REPO_ROOT, "data")
    state["data"] = (sorted((n, os.path.getmtime(os.path.join(data, n)))
                            for n in os.listdir(data))
                     if os.path.isdir(data) else None)
    return state


_ROOT_STATE_AT_START = _root_state()


def pytest_sessionfinish(session, exitstatus):
    """Fail the run if any test touched the repo-root journal.db or data/."""
    after = _root_state()
    if after != _ROOT_STATE_AT_START:
        changed = [k for k in after if after[k] != _ROOT_STATE_AT_START[k]]
        session.config.pluginmanager.get_plugin("terminalreporter").write_line(
            f"ISOLATION FAILURE: the suite modified repo-root {changed}",
            red=True)
        session.exitstatus = 1


@pytest.fixture(scope="session")
def _session_data_dir(tmp_path_factory):
    """One bar cache per run, so a test that fetches bars fetches once."""
    return str(tmp_path_factory.mktemp("data"))


@pytest.fixture(autouse=True)
def _isolate_journal_and_data(tmp_path_factory, monkeypatch,
                              _session_data_dir):
    """Every test gets its own journal and a cache dir outside the repo.
    Its own directory, not the test's tmp_path, which some tests list."""
    db_dir = tmp_path_factory.mktemp("journal")
    monkeypatch.setattr(journal, "DB_FILE", str(db_dir / "journal.db"))
    journal.init_db()
    try:
        import backtest
    except Exception:
        backtest = None
    if backtest is not None:
        monkeypatch.setattr(backtest, "DATA_DIR", _session_data_dir)


class _NoDiscordResponse:
    status_code = 204


@pytest.fixture(autouse=True)
def _no_discord(monkeypatch):
    """No test may post to the real Discord channel. .env supplies a live
    DISCORD_WEBHOOK_URL, and S19 alerts fire from the order-sheet and sync
    paths the tests exercise - so blank the URL and stub every webhook send.
    Tests that check posting patch their own sender on top of this."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    try:
        import discord_webhook
        monkeypatch.setattr(discord_webhook.DiscordWebhook, "execute",
                            lambda self, *a, **k: _NoDiscordResponse())
    except Exception:
        pass


@pytest.fixture
def temp_journal(tmp_path, monkeypatch):
    """Point the journal at a throwaway SQLite file."""
    monkeypatch.setattr(journal, "DB_FILE", str(tmp_path / "test_journal.db"))
    journal.init_db()
    return journal


# ------------------------------------------------------------ Discord guard
# INCIDENT 2026-10-17 02:36 ET: test_price_freshness drove orders.ingest to a
# BUY, whose trade_alerts.alert_buy posted "BUY MRVL 3 @ $100" to the REAL
# channel - orders.py loads .env, so the live webhook was in the
# environment. Every test now runs with the webhooks blanked, the async
# sender replaced by a recorder, and any HTTP request to Discord raising.

def _webhook_env_names():
    names = {k for k in os.environ if k.startswith("DISCORD_WEBHOOK")}
    env_file = os.path.join(REPO_ROOT, ".env")
    try:
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                key = line.split("=", 1)[0].strip().removeprefix("export ").strip()
                if key.startswith("DISCORD_WEBHOOK"):
                    names.add(key)
    except OSError:
        pass
    return names | {"DISCORD_WEBHOOK_URL"}


_WEBHOOK_ENV = _webhook_env_names()
# Blank at import too: modules call load_dotenv() during collection, and
# load_dotenv never overrides a variable that is already set.
for _k in _WEBHOOK_ENV:
    os.environ[_k] = ""


class DiscordLeak(RuntimeError):
    pass


def _is_discord(url) -> bool:
    u = str(url or "").lower()
    return "discord.com" in u or "discordapp.com" in u


class _Done:
    """Stands in for the alert thread post_async returns."""
    def join(self, timeout=None):
        return None

    def is_alive(self):
        return False


@pytest.fixture
def discord_posts():
    """The payloads tests sent through alerts.post_async (never delivered)."""
    return _DISCORD_POSTS


_DISCORD_POSTS = []


@pytest.fixture(autouse=True)
def _no_real_discord(request, monkeypatch):
    for k in _WEBHOOK_ENV:
        monkeypatch.setenv(k, "")
    import requests.adapters
    real_send = requests.adapters.HTTPAdapter.send

    def guarded_send(self, req, **kw):
        # At the adapter: tests that capture requests stub this on top.
        if _is_discord(getattr(req, "url", "")):
            raise DiscordLeak(f"test tried to post to Discord: {request.node.nodeid}")
        return real_send(self, req, **kw)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", guarded_send)

    import alerts
    _DISCORD_POSTS.clear()
    if request.node.get_closest_marker("real_post_async") is None:
        def recorder(payload, url=None, sender=None):
            _DISCORD_POSTS.append({"payload": payload, "url": url,
                                   "sender": sender})
            return _Done()
        monkeypatch.setattr(alerts, "post_async", recorder)
    yield
    # A real alert thread must finish while the guards are still in place:
    # the 10-17 leak was a thread that outlived its test's monkeypatch.
    alerts.drain(timeout=5)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_post_async: run alerts.post_async for real (its "
        "own tests); the webhooks stay blank and Discord HTTP still raises")
