"""
Tests must never create or write the repo-root journal.db or data/ (S12.4).

Before conftest redirected them, a fresh clone grew an empty ./journal.db the
moment review_bot was imported (its prompt reads the journal at import) and a
./data/daily_NVDA.csv from the ADX study test. On the desk's host those are
the live ledger and the live bar cache.

conftest.pytest_sessionfinish fails ANY run that changes them. This test
proves it directly: it runs the modules that used to leak in a child pytest
and compares the repo root before and after.
"""

import os
import subprocess
import sys

import backtest
import conftest
import journal

REPO = conftest.REPO_ROOT
LEAKERS = ["tests/test_review_desk_facts.py",
           "tests/test_review_truncation.py",
           "tests/test_study_harness.py"]


def test_the_journal_and_bar_cache_point_outside_the_repo():
    assert not os.path.abspath(journal.DB_FILE).startswith(REPO + os.sep)
    assert not os.path.abspath(backtest.DATA_DIR).startswith(REPO + os.sep)


def test_a_suite_run_leaves_the_repo_root_journal_untouched():
    before = conftest._root_state()
    r = subprocess.run([sys.executable, "-m", "pytest", "-q",
                        "-p", "no:cacheprovider", *LEAKERS],
                       cwd=REPO, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-2000:]
    assert "ISOLATION FAILURE" not in r.stdout
    assert conftest._root_state() == before
