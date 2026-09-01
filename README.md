# TradingBot — Alpaca Paper Trading Suite

> ## ⚠️ PAPER TRADING ONLY
> This bot trades exclusively against **Alpaca's paper (simulated) endpoint**.
> `broker.py` hardcodes `paper=True` and **refuses to start** if the environment
> is configured with a live endpoint. Do not modify it to trade live — real-money
> trading needs far more safeguards than this project provides.

A Streamlit trading bot with:

- **Alpaca paper execution** — entries are bracket orders, so the stop-loss and
  take-profit live **at the broker**, not in a polling loop.
- **Pluggable strategy playbook** (`strategies/`) — trend continuation,
  momentum continuation, mean-reversion reclaim; per-ticker enablement.
- **Claude AI gatekeeper** — every signal is vetted by a senior-trader prompt
  before any order is placed.
- **Local analyst (shadow mode)** — a free local Ollama model scores every
  signal alongside Claude for later comparison (see below).
- **SQLite journal** (`journal.py`) — every decision (taken *and* passed),
  every fill, and every outcome is recorded.
- **Risk engine** (`risk.py`) — R:R ≥ 1.5, per-position notional capped at
  `max_position_pct` of equity (config-driven; 0.30 fallback for old
  configs), risk-based position sizing, max positions, and a daily-loss
  circuit breaker.
- **Hard capital cap** — `capital_cap_usd` in `bot_config.json` caps the
  equity every sizing/validation path sees, regardless of the broker
  balance: a $97k paper account trades like a small account. Margin is never
  used — total deployed notional can't exceed effective capital — and sizing
  is whole-share (tickers too expensive for the account are journaled as
  `price_too_high_for_account` passes).
- **Daily universe scanner** (`universe.py`) — most-actives + top movers from
  Alpaca's screener, filtered to $5–$250 price, ≥ $20M average daily dollar
  volume, tradable non-OTC common stocks (ETFs skipped by default), ranked by
  dollar volume × |% move|, top 15 → `universe_today.json`.

## Setup

```powershell
# 1. Create and activate a venv (the repo assumes .\tradingbot)
python -m venv tradingbot
.\tradingbot\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure secrets
copy .env.example .env
# then edit .env and fill in at minimum:
#   ALPACA_API_KEY / ALPACA_SECRET_KEY  (PAPER keys from app.alpaca.markets)
#   ANTHROPIC_API_KEY                   (Claude gatekeeper)
#   FINNHUB_API_KEY                     (news context, optional but recommended)
```

## Running the bot

```powershell
streamlit run streamlit_app.py
```

Open the **Live Trading Bot** tab, assign tickers to risk profiles, and press
**Start Bot**. Each 30-second cycle the bot:

1. Pulls intraday + daily bars from Alpaca (free IEX feed).
2. Runs each ticker's enabled strategies. Deterministic filter rejections are
   journaled as passes (`source="rules"`).
3. Sends surviving signals through the AI gatekeeper, then the risk engine.
4. Submits a **bracket order** (market entry + stop + target at the broker).
5. Manages open positions: polls exit-leg fills, ratchets trailing stops by
   replacing the broker-side stop order, and enforces `hard_exit_date` on
   event-flow positions.

State survives restarts: on startup, `positions.json` is reconciled against
`broker.get_positions()` — **the broker is the source of truth**.

**Position ownership**: every position carries a `source` — `bot` (opened by
the loop; fully managed, trailing stop ratchets), `ceo` (opened by an
order sheet; **display-only** — the bot never replaces or cancels its legs,
stops move only via `TIGHTEN_STOP` sheets), or `unknown`
(reconciliation-adopted; display-only). The bot still observes all positions
(exit-fill detection, `hard_exit_date` enforcement, manual overrides) —
observation is not management.

The daily-loss circuit breaker halts new entries when today's realized PnL
(from the journal) drops below `daily_loss_limit_pct` of equity
(default 3%, `bot_config.json`).

## Daily universe — universe.py

```powershell
python universe.py     # print the ranked candidate table (CEO scan session)
```

The bot refreshes the universe automatically once per session start and then
scans those tickers (plus any open positions, which are always managed even
after dropping out of the universe) across all `default_strategies`.
Per-ticker strategy overrides in `"strategies"` still apply; tickers without
a risk profile default to **Moderate**. If the screener is unavailable or the
file is stale, the bot falls back to the configured `ticker_profiles`.

Candidates come from **two sources**, tagged in the output:
- `movers` — Alpaca screener most-actives + top gainers/losers (yesterday's
  action);
- `core_watch` — a static ~48-name cross-sector watchlist in
  `bot_config.json`, flagged only when a playbook setup is forming:
  `pre_breakout` (within 3% of the 20-day high) or `washout_reclaim`
  (≥ 10% off highs). Flagged-but-quiet names get a 1% move floor in the
  ranking so a coiling base isn't drowned out by yesterday's movers.

Combined output is capped at 20. Config knobs (`bot_config.json →
"universe"`): `min_price`, `max_price`, `min_dollar_volume`,
`max_candidates`, `skip_etfs`, `core_watchlist`, `pre_breakout_pct`,
`washout_pct`.

## CEO order sheets — orders.py

Event/flow trades (index inclusions, scheduled catalysts) can't be
auto-detected. They enter through JSON order sheets:

```powershell
python orders.py ingest order_sheet.json          # validate + execute
python orders.py ingest order_sheet.json --dry-run  # validate only
```

Example sheet:

```json
{
  "session": "2026-07-03",
  "regime": "risk-on",
  "orders": [
    {"action": "BUY", "ticker": "NVDA", "notional_usd": 250,
     "entry": 172.5, "stop": 168.0, "target": 181.0,
     "setup": "momentum_continuation", "reason": "breakout continuation",
     "valid_until": "2026-07-03T15:30:00"},
    {"action": "BUY", "ticker": "TSLA", "notional_usd": 200,
     "entry": 315.0, "stop": 305.0, "target": 340.0,
     "setup": "event_flow", "hard_exit_date": "2026-07-10",
     "reason": "index inclusion flow"}
  ],
  "watchlist": ["AMD"],
  "no_new_trades_if": {"daily_loss_pct_exceeds": 3.0}
}
```

Validation rejects: missing stop, reward:risk < 1.5, notional above
`max_position_pct` of equity, exceeding max positions, expired
`valid_until`, non-numeric price fields, and
`event_flow` orders without `hard_exit_date` (the bot force-closes those at
that date's close, win or lose).

Optional per-order field `abort_if_open_below` (playbook Rule #3): if price
is already below that level at ingest, the entry is rejected and journaled as
a rules pass — protects reclaim entries from gap-downs that invalidate the
setup overnight. Journaled BUYs record the **actual average fill**, not the
sheet's reference price; `sync` corrects any that were journaled before the
fill confirmed.

### Exit reconciliation

```powershell
python orders.py sync
```

Fetches SELL fills from Alpaca (bracket legs that triggered while the bot was
off, manual closes in the dashboard, etc.) and journals any that are missing —
actual fill price, PnL against the journaled BUY, outcome linked back to the
originating decision. Idempotent: every synced trade stores its Alpaca order
id, so re-running never double-journals. `report.py` runs a sync
automatically at startup and lists live stop/target legs under "Open orders".

## Account report — report.py

```powershell
python report.py
```

Prints a compact markdown report (equity, open positions with unrealized PnL,
today's fills, journal counts, analyst shadow scorecard) ready to paste into a
chat session.

## Local analyst (Ollama, shadow mode)

A local open-source model scores every signal **in parallel with Claude** so
you can measure whether it's good enough before trusting it.

1. Install [Ollama for Windows](https://ollama.com) and pull the model:
   ```powershell
   ollama pull qwen3:4b
   ```
   *Model choice on this machine: RTX 3060 Laptop (4 GB VRAM) — from the
   fallback chain qwen3:14b → 8b → 4b, only **qwen3:4b** fits on GPU and
   returns a full gatekeeper JSON in well under 20 s. Override with
   `LOCAL_ANALYST_MODEL` in `.env`.*
2. Set `"analyst_mode"` in `bot_config.json`:
   - `"shadow"` (default) — **Claude decides**; the local verdict is journaled
     alongside (`source="local_shadow"`) with an agreement flag. Runs in a
     background thread with a 30 s timeout — it can never delay a trade. If
     Ollama isn't running, shadow errors are journaled and the loop continues.
   - `"claude"` — Claude only.
   - `"local"` — the local model decides (`source="local"`). This is a
     **manual, deliberate switch** — nothing in code ever promotes the local
     model to trade authority.
3. Read the scorecard: `python report.py` → *Analyst shadow performance*
   (agreement %, disagreement counts, average conviction gap). Consider
   switching to `"local"` only after a few hundred shadow decisions with high
   agreement.

## Tests

```powershell
python -m pytest tests -q
```

Covers order-sheet risk validation, strategy detectors (textbook fire /
counterexample / stop-invalidation placement / downstream R:R gate / journal
pass log), and the local analyst + shadow mode (all HTTP mocked — no network).

## Project layout

| File | Purpose |
|---|---|
| `streamlit_app.py` | UI + live bot worker loop |
| `broker.py` | Alpaca paper wrapper (bracket orders, bars, retries) |
| `strategies/` | Playbook detectors (see `STRATEGY_AUDIT.md`) |
| `risk.py` | The one set of risk rules every entry goes through |
| `analyst.py` | Gatekeeper routing: claude / local / shadow |
| `claude_integration.py` | Claude API calls (prompts from `prompts.py`) |
| `local_analyst.py` | Ollama mirror of the gatekeeper |
| `prompts.py` | Single source of truth for gatekeeper prompts |
| `journal.py` | SQLite journal: decisions, trades, outcomes, shadow scorecard |
| `orders.py` | CEO order-sheet CLI |
| `report.py` | Markdown account report |
| `sectors.py` | Ticker -> sector tag (incl. the crypto/DAT class) |
| `earnings.py` | Finnhub earnings-calendar gate (fails closed) |
| `universe.py` | Daily scan: 180-name liquid pool + screener feeds |
| `backtest.py` | Playbook expectancy + the research-only lanes |
| `finetune_plan.py` | QLoRA greenlight: dataset stats, split, baseline |
