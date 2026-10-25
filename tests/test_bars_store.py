"""CEO 2026-10-25: the permanent 5-minute bar store and the cleanup KEEP list.
No network: a fake data client."""

import os
from types import SimpleNamespace

import pandas as pd

import bars_store
import disk_cleanup
import study_adx_band
import study_exit_grid


def _bars(start, n):
    idx = pd.date_range(start, periods=n, freq="5min", tz="UTC")
    return pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
                         "volume": 100.0}, index=idx)


class _FakeData:
    def __init__(self, frame):
        self.frame, self.calls = frame, []

    def get_stock_bars(self, req):
        self.calls.append((req.start, req.end))
        s, e = pd.Timestamp(req.start), pd.Timestamp(req.end)
        s = s.tz_localize("UTC") if s.tzinfo is None else s
        e = e.tz_localize("UTC") if e.tzinfo is None else e
        f = self.frame[(self.frame.index >= s) & (self.frame.index <= e)]
        return SimpleNamespace(df=f)


def test_backfill_then_append_only_adds_new_bars(tmp_path):
    store = str(tmp_path / "bars_5m")
    full = _bars("2026-10-22 13:30", 200)
    fake = _FakeData(full)
    r = bars_store.update(["KMI"], start="2026-10-22 13:30", end="2026-10-22 20:00",
                          store=store, data_client=fake, log=lambda *_: None, pause_s=0)
    first = r["added"]
    assert 0 < first < 200
    r2 = bars_store.update(["KMI"], start="2026-10-22 13:30", end="2026-10-23 07:00",
                           store=store, data_client=fake, log=lambda *_: None, pause_s=0)
    df = pd.read_csv(bars_store.path_for("KMI", store), index_col=0)
    assert len(df) == first + r2["added"] == 200
    assert not df.index.duplicated().any()
    # the second request starts after the last stored bar, not from the beginning
    s1 = pd.Timestamp(fake.calls[1][0])
    assert (s1 if s1.tzinfo else s1.tz_localize("UTC")) > pd.Timestamp("2026-10-22 19:00", tz="UTC")


def test_store_file_is_what_the_study_loader_reads(tmp_path):
    store = str(tmp_path)
    bars_store.merge_write("ABC", _bars("2026-10-22 13:30", 78), store)
    m5 = study_exit_grid.load_m5_rth("ABC", cache=store)
    assert len(m5) == 78 and str(m5.index.tz) == "America/New_York"


def test_store_lives_in_the_main_checkout_not_a_worktree(tmp_path):
    main = tmp_path / "TradingBot"
    (main / ".git" / "worktrees" / "x").mkdir(parents=True)
    wt = tmp_path / "TradingBot-x"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'x'}\n")
    assert bars_store.repo_root(str(wt)) == str(main)
    assert bars_store.repo_root(str(main)) == str(main)


def test_studies_read_the_permanent_store():
    assert study_exit_grid.S34_CACHE == bars_store.BARS_DIR
    assert study_adx_band.S33_CACHE == bars_store.BARS_DIR
    assert bars_store.BARS_DIR.endswith(os.path.join("data", "bars_5m"))


def test_cleanup_never_touches_the_bar_store(tmp_path):
    root = tmp_path
    (root / "data" / "bars_5m").mkdir(parents=True)
    (root / "logs").mkdir()
    old = root / "logs" / "old.log"
    old.write_text("x")
    os.utime(old, (0, 0))
    assert disk_cleanup.is_kept(str(root / "data" / "bars_5m" / "m5_SPY.csv"), str(root))
    assert not disk_cleanup.is_kept(str(old), str(root))
    assert list(disk_cleanup.old_logs(str(root))) == [str(old)]
    assert "data/bars_5m" in disk_cleanup.KEEP


def test_s52_hold_limit_forces_the_exit_at_1555_of_the_last_session():
    import study_exit_grid as g
    idx = pd.date_range("2026-10-21 09:30", "2026-10-21 15:55", freq="5min",
                        tz="America/New_York")
    idx = idx.append(pd.date_range("2026-10-22 09:30", "2026-10-22 15:55", freq="5min",
                                   tz="America/New_York"))
    m5 = pd.DataFrame({"open": 100.5, "high": 101.0, "low": 100.0, "close": 100.5,
                       "volume": 1.0}, index=idx)
    args = (m5, {}, {}, idx[0], 100.0, 98.0, 110.0, [(1.0, 0.0)])
    r1 = g.simulate_hold(*args, max_days=1)
    assert r1[1] == "hold limit day 1" and r1[3].strftime("%m-%d %H:%M") == "10-21 15:55"
    assert g.simulate_hold(*args, max_days=None) is None       # live: still open


def test_s52_morning_dip_triggers_on_the_reclaim_of_the_open():
    import study_exit_grid as g
    idx = pd.date_range("2026-10-21 09:30", periods=12, freq="5min", tz="America/New_York")
    px = [(100, 100.2, 99.0, 99.2), (99.2, 99.3, 98.6, 98.8), (98.8, 99.4, 98.7, 99.3),
          (99.3, 100.1, 99.2, 100.05), (100.1, 100.5, 100.0, 100.4)] + [(100.4, 100.6, 100.3, 100.5)] * 7
    bars = pd.DataFrame(px, index=idx, columns=["open", "high", "low", "close"])
    ts, fill, low = g.s52_day_signal(bars, atr=2.0)          # dip 1.4 >= 0.5 x 2.0
    assert ts.strftime("%H:%M") == "09:50" and fill == 100.1 and low == 98.6
    assert g.s52_day_signal(bars, atr=4.0) is None             # dip 1.4 < 0.5 x 4.0
