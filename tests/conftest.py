import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import journal  # noqa: E402


@pytest.fixture
def temp_journal(tmp_path, monkeypatch):
    """Point the journal at a throwaway SQLite file."""
    monkeypatch.setattr(journal, "DB_FILE", str(tmp_path / "test_journal.db"))
    journal.init_db()
    return journal
