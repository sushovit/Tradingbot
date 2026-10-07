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


@pytest.fixture
def temp_journal(tmp_path, monkeypatch):
    """Point the journal at a throwaway SQLite file."""
    monkeypatch.setattr(journal, "DB_FILE", str(tmp_path / "test_journal.db"))
    journal.init_db()
    return journal
