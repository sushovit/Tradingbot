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
- **Risk engine** (`risk.py`) — R:R ≥ 1.5, notional ≤ 30% of equity, risk-based
  position sizing, max positions, and a daily-loss circuit breaker.

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

The daily-loss circuit breaker halts new entries when today's realized PnL
(from the journal) drops below `daily_loss_limit_pct` of equity
(default 3%, `bot_config.json`).

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

Validation rejects: missing stop, reward:risk < 1.5, notional > 30% of equity,
exceeding max positions, expired `valid_until`, and `event_flow` orders without
`hard_exit_date` (the bot force-closes those at that date's close, win or lose).

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
