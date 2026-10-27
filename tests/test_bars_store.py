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
    df = bars_store.read("KMI", store)
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
    assert disk_cleanup.is_kept(str(root / "data" / "bars_5m" / "m5_SPY.parquet"), str(root))
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


def test_store_is_zstd_parquet_and_legacy_csv_converts_without_loss(tmp_path):
    import pyarrow.parquet as pq
    store = str(tmp_path)
    df = _bars("2026-10-22 13:30", 300)
    df.to_csv(os.path.join(store, "m5_OLD.csv"))
    assert bars_store.read("OLD", store) is not None          # legacy csv still readable
    r = bars_store.convert_csv(store, log=lambda *_: None)
    assert r["converted"] == 1 and not os.path.exists(os.path.join(store, "m5_OLD.csv"))
    back = bars_store.read("OLD", store)
    assert len(back) == 300 and back.index[-1] == df.index[-1]
    assert bars_store.path_for("OLD", store).endswith(".parquet")
    meta = pq.ParquetFile(bars_store.path_for("OLD", store)).metadata
    assert meta.row_group(0).column(0).compression == "ZSTD"
    # appending keeps the old rows and adds only the new ones
    assert bars_store.merge_write("OLD", _bars("2026-10-24 13:30", 10), store) == 10
    assert len(bars_store.read("OLD", store)) == 310


def test_rotate_keeps_the_open_file_and_gzips_its_content(tmp_path):
    import gzip as _gz
    p = tmp_path / "floor.log"
    p.write_text("x" * 100)
    ino = os.stat(p).st_ino
    dst = disk_cleanup.rotate(str(p), stamp="20261025")
    assert os.stat(p).st_ino == ino and p.read_text() == ""
    assert _gz.open(dst).read() == b"x" * 100


def test_ollama_cleanup_only_removes_unreferenced_blobs(tmp_path):
    import json as _json
    (tmp_path / "blobs").mkdir()
    man = tmp_path / "manifests" / "registry.ollama.ai" / "library" / "qwen3.5"
    man.mkdir(parents=True)
    (man / "9b").write_text(_json.dumps({"config": {"digest": "sha256:aa"},
                                         "layers": [{"digest": "sha256:bb"}]}))
    for b in ("sha256-aa", "sha256-bb", "sha256-cc", "sha256-dd-partial"):
        (tmp_path / "blobs" / b).write_text("1")
    assert [os.path.basename(p) for p in disk_cleanup.ollama_orphans(str(tmp_path))] == ["sha256-cc"]
    (man / "9b").write_text("{broken")
    assert disk_cleanup.ollama_orphans(str(tmp_path)) == []      # unreadable: delete nothing


def test_shadow_unload_sends_keep_alive_zero_and_checks_ps():
    import local_analyst
    sent = {}

    class R:
        def __init__(self, d): self.d = d
        def json(self): return self.d
    ok = local_analyst.unload({"models": {"shadow": "qwen3.5:9b"}},
                              post=lambda url, json, timeout: sent.update(json) or R({}),
                              get=lambda url, timeout: R({"models": []}))
    assert sent == {"model": "qwen3.5:9b", "keep_alive": 0} and ok["ok"]
    still = local_analyst.unload({"models": {"shadow": "qwen3.5:9b"}},
                                 post=lambda url, json, timeout: R({}),
                                 get=lambda url, timeout: R({"models": [{"name": "qwen3.5:9b"}]}))
    assert not still["ok"]


def test_s56_low_vol_flag_uses_only_the_prior_close():
    import pandas_ta as ta
    import study_exit_grid as g
    idx = pd.date_range("2025-01-01", periods=320, freq="B", tz="America/New_York")
    close = [100 + (i % 7) * 0.3 for i in range(320)]
    rng = [0.5 + (i % 11) * 0.1 for i in range(320)]
    df = pd.DataFrame({"open": close, "high": [c + r for c, r in zip(close, rng)],
                       "low": [c - r for c, r in zip(close, rng)], "close": close}, index=idx)
    flags = g.s56_low_vol_days(df, n=20, window=252)
    pct = ta.atr(df["high"], df["low"], df["close"], length=20) / df["close"]
    med = pct.rolling(252, min_periods=252).median()
    days = [pd.Timestamp(i).date() for i in idx]
    assert all(flags[days[k]] is None for k in range(0, 271))      # no 1-year history yet
    checked = 0
    for k in range(272, 320):
        expect = bool(pct.iloc[k - 1] < med.iloc[k - 1])           # the PRIOR close only
        assert flags[days[k]] is expect
        checked += 1
    assert checked == 48 and {flags[d] for d in days[272:]} == {True, False}


def test_rule_verdict_edge_and_risk_paths():
    import study_exit_grid as g
    base = {"trades": 1452, "exp_r": 0.093, "maxdd_usd": 1617.0}
    edge = {"trades": 1452, "exp_r": 0.114, "maxdd_usd": 1700.0}
    risk = {"trades": 1452, "exp_r": 0.103, "maxdd_usd": 1083.0}      # S56 1.5 x ATR
    assert g.rule_verdict(edge, base) == "ADOPT (edge)"
    assert g.rule_verdict(risk, base) == "tie"                         # halves not given
    assert g.rule_verdict(risk, base, [(0.12, 0.10), (0.09, 0.08)]) == "ADOPT (risk)"
    assert g.rule_verdict(risk, base, [(0.12, 0.10), (0.07, 0.08)]) == "tie"   # loses a half
    assert g.rule_verdict(dict(risk, maxdd_usd=1300.0), base, [(0.12, 0.1), (0.09, 0.08)]) == "tie"
    assert g.rule_verdict(dict(risk, exp_r=0.090), base, [(0.12, 0.1), (0.09, 0.08)]) == "tie"
    assert g.rule_verdict(dict(risk, trades=150), base, [(0.12, 0.1), (0.09, 0.08)]) == "tie"


def test_split_halves_at_the_calendar_midpoint():
    import study_exit_grid as g
    ts = [{"when": pd.Timestamp(d, tz="America/New_York")} for d in
          ("2025-10-14", "2026-04-18", "2026-04-19", "2026-10-23")]
    a, b, mid = g.split_halves(ts, "2025-10-14", "2026-10-23")
    assert str(mid) == "2026-04-19" and len(a) == 2 and len(b) == 2
