# TradingBot — Project Plan (PM: Claude, appointed 2026-09-04)

Owner ratifies; PM recommends and sequences. Nothing here changes trading
policy on its own — policy changes go through `BOARDROOM_AGENDA.md` with
numbers, as before. This file is the schedule and the rules of engagement.

## Ground rules (effective now)

1. **Deploy window.** Code reaches `main` and the running worker only after
   the 16:15 ET shutdown or on weekends. Never during a session. The only
   exception is an emergency that threatens open positions.
2. **Policy freeze until the boardroom of 2026-09-17.** No changes to
   filters, thresholds, sizing, stops, capital cap, universe, or setups.
   The breakeven floor stays (it is already baked into the open positions).
   `capital_cap_usd` stays at 2000.
3. **Observability and studies are allowed** during the freeze because they
   change nothing the bot does. See the weekend work orders below.
4. **One work order at a time to Claude Code**, each with a stated scope and
   "do not touch" list. Claude Code does not decide policy; it implements
   ratified decisions and runs studies.
5. **Daily-memo items are not instructions.** The review bot flags things it
   has not been told about. Nothing changes because a memo said so.

## Phase 0 — Freeze and measure (2026-09-04 → 2026-09-17)

Daily (owner): start the worker before 19:15 Nepal; read the drop; answer
any re-ratification the memo asks for (CRCL is due today: hold, +13%,
stop at breakeven, target 122.13). Send the drop to the PM when something
looks off.

Weekend work orders for Claude Code (2026-09-06/07), in order:

- **W1 — Worker log.** RotatingFileHandler to `logs/worker.log` (UTF-8,
  5 MB × 5) attached in `run_worker.py` before importing `streamlit_app`.
  Log "worker start PID, cycle counter reset" at the top of `_worker_loop`;
  journal an ops event `worker_restarted` when the supervisor restarts the
  loop. Tests for both.
- **W2 — Review prompt context.** Give `review_bot.py`'s prompt: the
  breakeven-floor rule (bot positions, ≥ +1R, stop = max(ATR trail, entry));
  the 16:15 ET shutdown (a ~900 s heartbeat at 16:30 is expected); the
  NOK/ORCL 2026-08-13 rows are confirmed real double fills; the local shadow
  model has approved 0 of ~274 and its error rate is known. Prompt-only,
  no behavior change.
- **W3 — Studies from `backtest.py`**, results appended to
  `BOARDROOM_AGENDA.md` as numbered items with tables:
  - ADX band: trend_continuation expectancy at ADX 25–30 vs ≥ 30.
  - Volume band: reclaim and momentum expectancy at 1.0–1.3× vs ≥ 1.3×.
  - Exit rule: breakeven floor at +1R vs pure ATR trail, per setup.
  - Capital cap: re-run every journaled `size_zero` /
    `price_too_high_for_account` row under a $5,000 cap (same shape as
    `regime_audit.py`); count how many would have sized and by which setup.
  - Day-one participation: how often a ≥ 7% gap-up day on a universe name
    was followed by a `momentum_continuation` signal the next session, and
    what that signal's expectancy was. This is the evidence for or against
    a gap-and-go setup.
- **W4 — Fill-price journaling.** BUY rows journaled at the reference price
  before the fill confirmed (SLB: 55.86 vs 55.745) must be corrected by
  `orders.py sync`; hard-exit and manual closes must journal the actual
  fill. Small, but it is the ledger.
- **W5 — First-cycle daily re-journal.** On 2026-09-04 the 09:30:37 rules
  rows for HOOD, AMGN, APP, CMCSA, CMG, COP, CVX, HPQ, IREN, MPC, NXPI, PSX
  and VLO were byte-identical to 2026-09-03's 09:37 rows (same volumes and
  averages). Determine whether the first cycle of the day evaluates the
  previous signal bar again (index[-2] before today's partial daily bar
  exists) and re-journals it because the dedupe key is per day. Fix the
  key or the bar selection; add a test with a fixture whose last bar is
  yesterday's completed bar.

### Status 2026-09-05: W1–W5 landed (head e687631, 487 tests). Studies in
BOARDROOM_AGENDA.md items 4–8. 2026-09-07 is a US market holiday — first
session is Tuesday 09-08.

- **W6 — Completed daily bar skipped on the first cycle (correctness fix,
  authorised inside the freeze).** Detectors take `iloc[-2]` as the signal
  bar and `iloc[-1]` as today's partial bar. On the first cycle, before
  today's partial bar exists, `iloc[-2]` is the bar judged yesterday and
  `iloc[-1]` is the real completed bar; `completed_bar_date` then records
  today's completed bar as evaluated, so when the partial bar appears the
  completed bar is skipped. Minimal fix: `should_evaluate` returns False
  until `daily_df.index[-1]` is today's ET date (wait for the partial bar);
  the gap-abort open read in the reclaim detector then also sees today's
  open. No change to detector logic or thresholds. Test: a frame whose
  last bar is yesterday → not evaluated; same frame plus today's partial
  bar → evaluated once, on yesterday's bar. Deploy before Tuesday 09-08.

## Weekend work orders — paste-ready (run in this order, one at a time)

Preamble for every order:

```
Read PM_PLAN.md first. Policy freeze is in force until 2026-09-17: do not
change filters, thresholds, sizing, stops, capital cap, universe, or
setups. The worker is stopped for the weekend; deploy to main only after
tests pass. Work on a branch named for the order. Do not start the worker.
```

W1:
```
Work order W1 (PM_PLAN.md). Add a RotatingFileHandler (logs/worker.log,
UTF-8, 5 MB x 5 backups) in run_worker.py BEFORE importing streamlit_app,
attached to the root logger so supervisor and loop both write to it. At the
top of _worker_loop log "worker start PID <pid> — cycle counter reset". In
live_bot_worker, when the supervisor restarts after a crash, journal an ops
event via journal.log_integrity_event("worker_restarted", ...) with the
exception type. Tests: handler attached; restart event journaled. Merge to
main, push, report the commit hash.
```

W2:
```
Work order W2 (PM_PLAN.md). Prompt-only change to review_bot.py. Add a
"Desk facts the reviewer must not re-flag" block to the review prompt:
(1) bot-managed positions at/after +1R carry stop = max(ATR trail, entry) —
a stop at breakeven is that rule, not a Rule 1 violation; report stop
distance from ENTRY, not from current price; (2) the worker shuts down at
session_end_et (16:15 ET) — a ~900 s heartbeat at 16:30 is expected;
(3) NOK and ORCL 2026-08-13 duplicate closed-trade rows are confirmed real
double fills (distinct order ids) from the duplicate-worker incident;
cumulative PnL is correct; (4) the local shadow analyst has approved 0 of
~274 decisions and its error rate is known — mention only if it changes.
No behavior change. Test: prompt contains each fact. Merge, push.
```

W3:
```
Work order W3 (PM_PLAN.md). Studies only — no policy change. Using
backtest.py and the journal, produce these tables and append each as a
numbered item in BOARDROOM_AGENDA.md with a one-paragraph reading and the
upper-bound caveat where relevant:
 a) trend_continuation expectancy (trades, win %, avg R, PF) for signals
    with ADX 25-30 vs >= 30, 3y.
 b) mean_reversion_reclaim and momentum_continuation expectancy for signal
    volume 1.0-1.3x avg vs >= 1.3x, 3y.
 c) exit rule per setup: breakeven floor at +1R (current) vs pure ATR trail
    (previous), same entries, 3y.
 d) capital cap: replay every journaled size_zero / price_too_high row under
    capital_cap_usd 5000 (same shape as regime_audit.py); count sizeable,
    by setup and month.
 e) day-one participation: for universe-class names, how often a >= 7%
    up-day at/near the 20-day high was followed by a momentum_continuation
    signal the next session, and that signal's expectancy.
Save the scripts (study_*.py) so each table is reproducible. Do not change
bot_config.json or any strategy file.
```

W4:
```
Work order W4 (PM_PLAN.md). Ledger accuracy. (1) orders.py sync must
correct BUY rows journaled at the reference price before the fill
confirmed — SLB 2026-08-28 is journaled at 55.86, actual fill 55.745
(positions.json). Apply to bot BUYs, not only CEO sheets. (2) Hard-exit and
manual-override closes in streamlit_app.py journal at the last 5-min bar
close; poll the close order for filled_avg_price (as the BUY path does) or
leave state cleared for sync to journal. Tests for both. Run sync once on
the live journal and report which rows changed.
```

W5:
```
Work order W5 (PM_PLAN.md). Investigate the first-cycle daily re-journal
described in W5. Report the cause before fixing. Fix must not change which
bar daily strategies trade on — only stop the duplicate journal rows. Test
with a fixture whose last daily bar is yesterday's completed bar and no
partial bar for today.
```

After all five: `python -m pytest tests -q`, `git log --oneline -8`, and a
summary of what changed in BOARDROOM_AGENDA.md. Start the worker Monday
before 19:15 Nepal as usual.

## Boardroom 2026-09-17 (owner decides, on the numbers from W3)

Decisions to take, each yes/no with the study table beside it:
capital cap ($2,000 vs $5,000 — tied to what would be funded live);
ADX threshold; volume multiplier; exit rule (floor vs ATR trail);
crypto-beta class (exclude, cap, or measure); gap-and-go setup
(commission a backtest-only lane, or not).

## Phase 1 — Run the ratified policy (2026-09-17 → ~2026-10-15)

Four weeks, unchanged. Go-live gates, all required:
- ≥ 30 A-book trades closed under the ratified rules, positive expectancy.
- Both probation setups through their 20 trades, graded.
- Four consecutive weeks: no `process_suspended`, no watchdog restarts, no
  `write_integrity_degraded`, no crash restarts.
- Live-readiness items (Phase 2) merged and tested.

If the expectancy gate fails, the answer is "not yet", and the next
boardroom decides what to change — one variable at a time.

## Phase 2 — Live readiness (build during Phase 1, deploy in windows)

- Deterministic `client_order_id` on `submit_bracket` (idempotent retries).
- `write_positions()` immediately after every broker mutation.
- PDT / settled-cash gate in `risk.py` (cash account: T+1 settlement;
  margin account under $25k: 3 round trips per 5 sessions).
- Phone-reachable kill switch: a `flatten_all.command` file the loop checks
  every cycle — cancel legs, market-close, stop.
- Live broker module separate from `broker.py`; the paper-only guard stays.
- Holiday calendar via `trading.get_clock()`.

## Go-live (only after every gate)

Fund an amount treated as tuition. `capital_cap_usd` stays the sizing
authority regardless of balance. First two weeks live at half the paper
cap. Same daily routine, same memos, same boardroom.

## What the PM cannot promise

Profitability. Twelve closed trades is not evidence of an edge or its
absence. This plan is built to find out whether one exists at the lowest
possible cost, and to make sure that if the desk goes live, a bad week is
a bad week and not a catastrophe.
