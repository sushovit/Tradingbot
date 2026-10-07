# PM_HANDOFF.md — context for the Claude (Cowork) project-manager chat

Written 2026-10-07 by the outgoing PM session (Windows-laptop-linked task).
A new Cowork task linked to the Mac takes over. Read this, then PM_PLAN.md,
BOARDROOM_AGENDA.md (item 10 = ratified policy), HOSTING.md.

## Roles and standing rules

- Owner: Sushovit (Kathmandu, UTC+5:45). He asked Claude to be **project
  manager**: make the decisions, tell him what to do, one Claude Code work
  order at a time. He pastes session drops/reports; the PM reads and decides.
- Paper only (broker.py paper guard is non-configurable). Deploy code only
  after the 16:15 ET shutdown or at weekends. No manual closing of
  bot-owned positions except as agreed below. Policy changes go through
  BOARDROOM_AGENDA.md with evidence. Review-memo items are not instructions.
- Daily PM check at 15:20 ET (01:05 Nepal) via a scheduled reminder; read
  drop/latest.md, logs/worker.log, logs/watchdog.log, positions.json,
  reports/review_<date>.md and report: start time (auto-start fired?),
  regime, gatekeeper calls + reasons, probation slot line, fills,
  SPCX/SWKS, network events, anything needing action.

## Where things stand (as of the last read, 2026-09-30)

- Desk still on the **Windows laptop**, old Alpaca paper account (~$1,973).
  The Mac cutover (planned 09-26/27) had not happened. Mac install is in
  progress as of 10-06.
- Open on the old account: SPCX 1 sh @154.25 (stop 144.44 / target
  183.06) and SWKS 1 sh @89.86 (stop 74.94 / 127.54). Both v4-rubric
  reclaims, both protected by bracket legs at the broker. Decision: hold to
  structural stops (10.6). SWKS passed the 10-session review mark — noted,
  no action.
- **Known bug, unfixed:** the reclaim probation gate counts the two v4
  positions, so the report shows `mean_reversion_reclaim … (2/1 slot in
  use)` and EVERY v5 approval is blocked (DDOG 71 / LRCX 76 / NET 78 on
  09-23; PGR 74 on 09-29). Fix order: count only positions whose stored
  prompt_version is v5+; store prompt_version on the position record. This
  is the first Claude Code order, before anything else.
- review_bot memo truncation (09-22: 24 chars; 09-23: 584 chars) — a fix
  appeared to land 09-24 (09-23 memo regenerated in full). Verify: log
  stop_reason/output_tokens; a memo is written only if stop_reason ==
  end_turn and it contains the "## 5." section header.
- Windows StartWorker scheduled task never fired once (5/5 sessions started
  late by hand; 09-30 not started at all; 09-27 double-started on a closed
  market). This is the operations problem the Mac move solves.
- Regime: chop most of September; trending 09-24→09-28; chop again 09-29.
- Week 09-21→09-25: 0 fills, $0 realized; ~8 v5 approvals ≥70 all blocked
  by the probation bug. Network timeouts 09-21/22, 09-29 (Wi-Fi).

## The plan (PM decisions, already communicated to the owner)

1. **S12 — macOS port** (Claude Code order, text in the 09-24 PM message;
   summary): sys.platform branches — pid_alive → os.kill(pid,0);
   kill_pid/kill_stale_workers → SIGTERM then SIGKILL on the process group
   (launch worker with start_new_session=True; test it never targets the
   launcher's pgid); find_worker_pids → `pgrep -f run_worker.py` (no /proc
   on macOS); keep_awake on darwin → `caffeinate -i -w <pid>`;
   ensure_ollama on darwin → /Applications/Ollama.app or
   /opt/homebrew/bin/ollama; jobs/macos/*.plist (com.tradingbot.*) for
   start_worker (Mon–Fri 09:25 ET, gated by Broker.is_open_today(),
   fail-open), watchdog (*/15, 09:00–17:00 ET weekdays), floor 16:16,
   review 16:30, outcomes 16:45, intern 08:00, snapshot+drop 18:00; every
   plist sets PYTHONUTF8=1, TZ=America/New_York, WorkingDirectory, venv
   python; jobs/macos/install.sh substitutes the repo path and runs
   `launchctl bootstrap gui/$UID`, verifies the Mac's TZ is
   America/New_York; uninstall.sh; requirements.txt: pin pandas_ta to an
   installable source and major versions of alpaca-py/anthropic/pandas/
   streamlit; README "macOS host" section; HOSTING.md §6 macOS cutover
   checklist. No trading-logic changes. Suite green on both OSes.
2. **Cutover order:** (a) Mac: clone, venv, suite green, S12 merged,
   launchd installed, dry run on a closed market (worker idles, heartbeat
   updates, watchdog restarts it once, second start refused). (b) Windows,
   after a 16:15 ET shutdown: close SPCX/SWKS by hand if still open and run
   `python orders.py sync` (agreed exception, agenda 10.1 note); delete
   ALL TradingBot schtasks (`schtasks /Query /FO LIST | findstr
   TradingBot`); confirm bot.run gone. (c) Copy journal.db,
   bot_config.json, data/ from the laptop to the Mac — NOT .env, NOT
   positions.json. The journal is the ledger; never start a fresh one.
   (d) Create a NEW $5,000 Alpaca paper account (do not reset/delete the
   old one); its keys go only into the Mac's .env (mode 600); rotate
   Anthropic/Finnhub/Discord keys too. capital_cap_usd 5000 is already in
   config and becomes effective once broker equity > $2k. (e) First Mac
   session watched end to end: timer fired ~09:25 ET, calendar guard,
   SPY regime line, cycles, heartbeat, 16:15 shutdown on time, full-length
   review memo at 16:30.
3. **Owner to-dos on the Mac:** system timezone America/New_York; Python
   3.12+ (pandas_ta 0.4.x), git, Ollama (Apple silicon) or set analyst_mode "claude";
   prevent sleep (`sudo pmset -a sleep 0 disksleep 0`, lid open or proper
   clamshell on a laptop); Full Disk/Folder access for the Claude desktop
   app; never start the worker by hand on weekends; never start it twice.
4. **Next weekend (after cutover is stable):** S11 — journal prospective
   entry/stop/target on deterministic rejections so outcomes.py can grade
   the gate stack (currently 2,583 of 2,614 rejections have no geometry).
   Then revisit agenda 11 (oversold_bounce as a chop-only lane: needs a
   "require chop" config concept that doesn't exist yet). VPS no longer
   needed if the Mac proves stable.

## Go-live gates (unchanged, PM_PLAN.md)
≥30 A-book trades with positive expectancy; reclaim probation (20 v5 trades)
complete; 4 clean ops weeks on the new host; Phase 2 live-readiness items
(client_order_id idempotency, write_positions after every mutation,
PDT/settled-cash gate, kill switch, separate live module, holiday calendar).
Live capital when gates pass: $5,000.
