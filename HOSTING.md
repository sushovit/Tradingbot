# HOSTING.md — moving the desk off the laptop

Commissioned by agenda 10.15 (S10, 2026-09-21). **Documentation only. Nothing
in this file has been executed and no code changes accompany it.**

## Why this exists

The desk runs on a Windows laptop. Over nine sessions to 2026-09-18 the
laptop's Wi-Fi produced timeouts in six of them, and on 2026-08-31 the
machine slept mid-session at 12:36 ET (Kernel-Power 42) and resumed at
21:06 ET — the worker was frozen, the watchdog was frozen with it, so
nothing restarted, and the auto-shutdown fired five hours "late" purely
because wall-clock time had moved on without it. A live desk cannot run on
a machine that sleeps, roams between access points, and is also somebody's
laptop.

This file is the audit: what is Windows-only, what replaces it on Linux,
what a small VPS actually needs, and the order the cutover happens in.

---

## 1. What in the repo is Windows-only

Six things. Everything else is portable Python.

### 1.1 `session_clock.keep_awake()` — sleep suppression

```
ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
```

Written as the fix for the 08-31 sleep. It already fails soft: the call is
wrapped in `try/except` and returns `False` on a non-Windows host, so it is
a no-op on Linux rather than a crash.

**On a VPS this problem does not exist.** A server does not suspend. The
function can stay exactly as it is, returning `False` forever, and the one
test that exercises it is already guarded with
`@pytest.mark.skipif(sys.platform != "win32")`.

### 1.2 `run_worker.pid_alive()` — `tasklist`

```
subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"])
```

This backs the S1 single-instance guard, which is load-bearing: on
2026-09-17 a second worker started by hand re-asked a signal the first had
already rejected at conviction 68, got 78, and bought it. `tasklist` does
not exist on Linux.

**Replacement:** `os.kill(pid, 0)` — raises `ProcessLookupError` if the PID
is gone, `PermissionError` if it exists but belongs to another user (which
still means alive). It is a signal-free existence check, and it is both
faster and more reliable than parsing `tasklist` output.

### 1.3 `run_worker.kill_pid()` and `watchdog.kill_stale_workers()` — `taskkill`

```
subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)])
```

`/T` kills the process tree, which matters because the worker spawns a
shadow-analyst thread and, in `local` mode, subprocesses.

**Replacement:** `os.killpg(os.getpgid(pid), signal.SIGTERM)`, then
`SIGKILL` after the existing `KILL_WAIT_SECS` grace period. This needs the
worker to be its own process-group leader (`start_new_session=True` when
it is launched), or `getpgid` returns the shell's group and the kill takes
the scheduler with it. **That is the single most dangerous line in this
migration** — get the process group wrong and a watchdog sweep kills its
own parent.

Under systemd this whole function largely goes away: `systemctl restart`
does the tree-kill correctly, and `Restart=on-failure` makes most watchdog
relaunches unnecessary.

### 1.4 `watchdog.find_worker_pids()` — `wmic`

```
wmic process where name='python.exe' get processid,commandline /format:csv
```

The fallback path used when the lock file records no PID. Note that `wmic`
is deprecated on Windows too — it is removed in recent Windows 11 builds —
so this is worth replacing regardless of the host.

**Replacement:** `pgrep -f run_worker.py`, or read `/proc/*/cmdline`
directly with no subprocess at all. The `/proc` walk is preferable: no
shell, no text parsing, no dependency.

### 1.5 `jobs/*.bat` — seven batch files

`floor.bat`, `intern.bat`, `outcomes.bat`, `review.bat`, `snapshot.bat`,
`start_worker.bat`, `watchdog.bat`.

Each does the same four things: `cd /d D:\TradingBot`, append a dated
banner to a log, `set PYTHONUTF8=1`, run one Python entry point with output
appended to `logs/<name>.log`.

Every one hardcodes `D:\TradingBot`. On Linux they become shell scripts or,
better, disappear into the systemd unit's `WorkingDirectory` and
`Environment=`.

`PYTHONUTF8=1` **must survive the port.** It is not Windows hygiene — it is
there because a cp1252 console encode crash took down a job. On Linux the
default is already UTF-8, but setting it explicitly costs nothing and
removes a class of locale surprise on a minimal VPS image where `LANG` is
often unset.

### 1.6 `run_hidden.vbs` — console-window suppression

A `wscript` shim that runs a batch file with no visible window, because
Task Scheduler launching `cmd /c` pops a console every time and the
watchdog fires every 15 minutes. It also hardcodes `D:\TradingBot`.

**On Linux this has no equivalent and needs none** — cron and systemd have
no console to hide. The file is deleted outright.

### 1.7 Not Windows-only, but worth knowing

`safe_io.atomic_write_text()` carries retry logic for two Windows failure
modes: `WinError 32` (destination locked by a reader — the dashboard polls
status every 15s) and `WinError 2` (the temp file vanished between write
and rename, almost certainly antivirus). Neither occurs on Linux.

**Leave the code alone.** `os.replace` is atomic on both platforms, the
retry loop is harmless when nothing ever fails, and removing it would be an
untested change to the one function that protects `positions.json`. The
comments should eventually note that the retries are Windows-specific, but
that is cosmetic.

---

## 2. Cron / systemd equivalents

Current Windows schedule (Nepal local time; the desk's operator is in
Kathmandu, and 19:10 Nepal is 09:25 ET during US daylight time).

| Job | Windows | Purpose |
|---|---|---|
| `start_worker.bat` | Task Scheduler XML, weekdays 19:10, `IgnoreNew` | Starts the session, but only on a trading day |
| `watchdog.bat` | every 15 min | Kills a stale worker, relaunches it |
| `floor.bat` | daily | `floor.py --to-file --discord` |
| `review.bat` | daily | `review_bot.py` |
| `intern.bat` | daily | `intern_desk.py --trade` |
| `snapshot.bat` | daily | `snapshot.py` then `drop.py --discord` |
| `outcomes.bat` | nightly, after the 16:15 ET shutdown | `outcomes.py` |

### 2.1 The recommended shape: systemd for the worker, cron for the rest

The worker is a long-running process with a restart policy. That is a
service, not a cron job. Everything else is a scheduled one-shot, which is
what cron is for. Mixing them is the right answer, not a compromise.

**`/etc/systemd/system/tradingbot.service`**

```ini
[Unit]
Description=TradingBot worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tradingbot
WorkingDirectory=/opt/tradingbot
Environment=PYTHONUTF8=1
Environment=TZ=America/New_York
EnvironmentFile=/opt/tradingbot/.env
ExecStart=/opt/tradingbot/venv/bin/python run_worker.py
Restart=on-failure
RestartSec=30
KillMode=control-group
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
```

`KillMode=control-group` is what replaces `taskkill /T`: systemd tracks the
whole cgroup and stops the shadow thread and any child with the parent.
`Restart=on-failure` plus `RestartSec=30` replaces most of what
`watchdog.py` does on a crash — the watchdog then only has to handle the
harder case, a worker that is alive but wedged.

**Starting it only on a trading day.** The service must not be
`WantedBy=timers` on a holiday. Keep the existing guard — it already
works and was written for Labor Day 2026-09-07:

```
# /etc/systemd/system/tradingbot-start.timer  -> OnCalendar=Mon..Fri 09:25 America/New_York
# /etc/systemd/system/tradingbot-start.service (Type=oneshot):
ExecStart=/opt/tradingbot/bin/start_if_open.sh
```

where `start_if_open.sh` is the direct translation of `start_worker.bat`:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd /opt/tradingbot
export PYTHONUTF8=1
if venv/bin/python -c "from dotenv import load_dotenv; load_dotenv(); \
     from broker import Broker; import sys; \
     sys.exit(0 if Broker().is_open_today() else 1)"; then
  systemctl start tradingbot.service
else
  echo "$(date -Is) not a trading day - worker not started" >> logs/start_worker.log
fi
```

`Broker.is_open_today()` fails **open** on a calendar error (a false SKIP
silently loses a session; a false START is caught by `session_clock`).
That behaviour is deliberate and must not be "fixed" during the port.

### 2.2 Crontab for the rest

Timezone is the trap. Set `TZ` in the crontab explicitly rather than
relying on the VPS's clock, which defaults to UTC on most images.

```cron
TZ=America/New_York
PYTHONUTF8=1
BOT=/opt/tradingbot

*/15 9-17 * * 1-5  cd $BOT && venv/bin/python watchdog.py        >> logs/watchdog.log 2>&1
16 16 * * 1-5      cd $BOT && venv/bin/python floor.py --to-file --discord >> logs/floor.log 2>&1
30 16 * * 1-5      cd $BOT && venv/bin/python review_bot.py      >> logs/review.log 2>&1
45 16 * * 1-5      cd $BOT && venv/bin/python outcomes.py        >> logs/outcomes.log 2>&1
00 08 * * 1-5      cd $BOT && venv/bin/python intern_desk.py --trade >> logs/intern.log 2>&1
00 18 * * *        cd $BOT && venv/bin/python snapshot.py && venv/bin/python drop.py --discord >> logs/snapshot.log 2>&1
```

Two notes on that table. The watchdog is scoped to session hours on
weekdays instead of running every 15 minutes around the clock — there is
nothing for it to do at 03:00, and each run is a Discord-capable process.
And `outcomes.py` must stay **after** the 16:15 shutdown; it is read-only
against the journal and daily bars, but running it against a live session
would measure a half-finished day.

### 2.3 Logs

The `.bat` files append to `logs/*.log` and nothing rotates them except
`run_worker.setup_logging()`, which uses a `RotatingFileHandler` for the
worker's own log only. On Linux, either keep the redirects and add a
`logrotate` stanza, or drop the redirects entirely and let systemd/journald
capture stdout for the service. **Do not do both** — duplicated logs are
how an incident timeline stops being trustworthy.

---

## 3. What a $5–10/month Linux VPS needs

This workload is tiny. It makes a handful of HTTPS calls per five-minute
cycle and writes a small SQLite file. The constraint is uptime and network
quality, not compute.

| | Requirement | Note |
|---|---|---|
| vCPU | 1 | pandas/pandas_ta on 20–60 bar frames is trivial |
| RAM | 1 GB | 2 GB is more comfortable with pandas + streamlit installed |
| Disk | 10–25 GB | the journal is kilobytes; `data/` holds ~50 daily CSVs |
| Python | **3.11+** | dev is on 3.13.8; 3.11 is the floor for `zoneinfo`/`tzdata` use in `clockline.py` and `daily_eval.py` |
| GPU | **none** | see below |
| Network | stable, low-jitter | the actual reason for the move |

Any of Hetzner CX22, DigitalOcean's $6 droplet, Vultr or Linode's $5–10
tiers clears this comfortably. **Pick a region close to Alpaca's endpoints
(US East)** — the desk polls every five minutes and places bracket orders;
30 ms beats 300 ms, and more importantly the link does not roam.

### 3.1 The shadow analyst is the one real decision

`local_analyst.py` talks to Ollama at `http://localhost:11434` and runs
`qwen3:4b`, warming the model into VRAM before the session. A $5–10 VPS has
no GPU and typically 1–2 GB of RAM. **A 4B model will not run usefully
there** — on CPU it would take tens of seconds per verdict against a 30 s
timeout.

Three options, in order of preference:

1. **Turn it off.** Set `analyst_mode: "claude"` in `bot_config.json`. The
   shadow is explicitly **advisory and non-blocking** by CEO ruling, runs in
   a daemon thread with a 30 s timeout, and can never delay or block a
   trade. Losing it costs the comparison ledger, not a single trade
   decision. Its measured error rate is ~10.4% over 316 decisions and it has
   approved **0** signals, so the ledger is currently evidence that it is not
   ready for authority — which is information the desk already has.

2. **Point it at the laptop.** `OLLAMA_URL` is already an environment
   variable (`os.getenv("OLLAMA_URL", "http://localhost:11434")`), so the
   VPS can call the laptop's Ollama over a private network — Tailscale or
   WireGuard, never a public port. This keeps the shadow ledger running for
   free. It is also the only option that reintroduces the laptop's network
   as a dependency, so it must stay strictly advisory.

3. **Pay for a GPU box.** Not justified by a model that has approved
   nothing in 316 decisions.

**Either way, `ensure_ollama()` needs attention.** It tries to launch
`%LOCALAPPDATA%\Programs\Ollama\ollama app.exe` when the service is down.
On Linux that path never exists, so the function returns
`detail="ollama app not found at ..."` and continues — it fails soft, and
with `analyst_mode: "claude"` it is never called at all. No change is
required for option 1.

### 3.2 Secrets

`.env` carries the Alpaca, Anthropic, Finnhub and Discord credentials and
is loaded with `python-dotenv`. On the VPS it becomes
`/opt/tradingbot/.env`, owned by the service user, mode `600`, referenced
by `EnvironmentFile=`. **The keys should be rotated as part of the cutover,
not copied** — a credential that has lived on a roaming laptop should not
be the credential that runs an unattended server.

### 3.3 Dependencies

`requirements.txt` is pure Python with no Windows-specific packages:
`anthropic`, `alpaca-py`, `streamlit`, `yfinance`, `pandas`, `pandas_ta`,
`pytz`, `python-dotenv`, `finnhub-python`, `discord-webhook`, `requests`,
`pytest`, `tzdata`. All install cleanly on Linux.

`streamlit` is only needed for the dashboard. If the VPS runs headless, it
still has to be installed — `streamlit_app.py` is the worker loop's own
module and imports it at module scope.

---

## 4. Cutover checklist

Ordered. Steps 1–6 are safe to do while the laptop keeps trading; **the
desk is only ever running in one place from step 8 onward.**

**Do not run two desks against one Alpaca account at any point.** Both
would reconcile the same positions, both would manage the same bracket
legs, and the 2026-09-17 double-instance incident is what that looks like
on one machine.

- [ ] **1. Provision.** 1 vCPU / 2 GB / US-East, Debian or Ubuntu LTS,
      Python 3.11+. Create a non-root `tradingbot` user. SSH keys only.
- [ ] **2. Clone and install.** `/opt/tradingbot`, a venv, `pip install -r
      requirements.txt`.
- [ ] **3. Rotate credentials** at Alpaca, Anthropic, Finnhub and Discord.
      Write the new `.env` on the VPS, mode `600`. Do not copy the old file.
- [ ] **4. Set `analyst_mode: "claude"`** in the VPS's `bot_config.json`,
      unless option 2 above is chosen and the tunnel is already up.
- [ ] **5. Run the suite.** `python -m pytest tests -q` must be green on the
      VPS before anything is scheduled. The Windows-only test is skipped by
      its existing platform guard; if anything else fails, the port is not
      done.
- [ ] **6. Port the process helpers.** `pid_alive` → `os.kill(pid, 0)`;
      `kill_pid` → `SIGTERM` then `SIGKILL`; `find_worker_pids` → `/proc`
      walk. **Verify the process group** before trusting any kill path —
      launch a dummy worker, confirm `watchdog.py` kills it and nothing
      else. Delete `run_hidden.vbs` and the `.bat` files.
- [ ] **7. Install the units.** `tradingbot.service`, the start timer,
      `start_if_open.sh`, the crontab, a `logrotate` stanza. Verify the
      timer with `systemctl list-timers` and confirm it reads
      `America/New_York`, not UTC.
- [ ] **8. Pick a flat weekend.** Cut over with **no open positions**. Two
      reasons: `positions.json` does not have to be migrated and reconciled
      across hosts, and a mistake costs nothing. (This is the same
      precondition the $5,000 paper reset is waiting on — do both at once.)
- [ ] **9. Stop the laptop desk for good.** Delete the Windows scheduled
      tasks (`schtasks /Delete /TN "TradingBot\StartWorker" /F` and the
      rest), and confirm `bot.run` is gone. A scheduled task left behind on
      a laptop that wakes up is exactly the double-instance failure.
- [ ] **10. Copy the state.** `journal.db`, `bot_config.json`, `.env`
      (already written in step 3) and `data/`. The journal is the ledger —
      its continuity is the whole point, and a fresh one would silently
      reset every probation count and every statistic.
- [ ] **11. Dry-run on a closed market.** Start the service on a Saturday.
      It should come up, read the calendar, log that the market is closed,
      and idle. Confirm the heartbeat file updates.
- [ ] **12. First live session, watched.** Be present for the whole
      session. Confirm in order: the start timer fired, the calendar guard
      passed, the SPY regime line appears in the log, a cycle completes, the
      heartbeat is fresh, and the 16:15 shutdown fires on time.
- [ ] **13. Confirm the watchdog.** Kill the worker by hand mid-session and
      confirm it comes back — once. Then confirm a second manual start is
      refused by the S1 PID guard.
- [ ] **14. Reset the paper account to $5,000** (owner's action at Alpaca,
      while flat — agenda 10.1) and let the first real session run.
- [ ] **15. Keep the laptop for a fortnight** as a cold standby with its
      tasks disabled, then decommission.

## 5. What this file does not decide

Whether to move at all, when, or which provider. 10.15 scoped this as Phase
2 and the PM's read is unchanged: the move is justified by the network
evidence alone, but it should follow the $5,000 reset rather than precede
it, and both want the same flat weekend.
