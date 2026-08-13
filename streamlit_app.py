import streamlit as st
import yfinance as yf
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timedelta, time as dttime
import logging
import os
import threading
import time as a_time
import pytz
from dotenv import load_dotenv
import finnhub
import json
import math
from discord_webhook import DiscordWebhook, DiscordEmbed

# --- Import from Claude integration file ---
from claude_integration import (
    get_technical_verdict,
    get_news_and_sentiment,
    get_macro_economic_analysis,
    get_social_sentiment_analysis,
    get_final_decision,
    count_ema_crossovers
)

# --- Alpaca paper broker, journal, risk rules, strategies, analyst router ---
import journal
import risk
import analyst
import universe
import position_mgmt
import daily_eval
import safe_io
from broker import Broker, BrokerError
from strategies import enabled_strategies, Signal, Rejection


# --- Page Configuration and Global Setup ---
st.set_page_config(page_title="Algorithmic Trading Suite", page_icon="🤖", layout="wide")
load_dotenv()
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

# --- API Client Initialization ---
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
try:
    finnhub_client = finnhub.Client(api_key=os.getenv("FINNHUB_API_KEY"))
except Exception as e:
    finnhub_client = None
    logger.warning(f"Could not initialize Finnhub client: {e}")

EASTERN_TZ = pytz.timezone("US/Eastern")

# --- File-based state management ---
LOCK_FILE = "bot.run"
STATUS_FILE = "bot_status.log"
TRADE_LOG_FILE = "live_trades.csv"
CONFIG_FILE = "bot_config.json"
PORTFOLIO_STATE_FILE = "portfolio_state.json"
POSITIONS_STATE_FILE = "positions.json"
CONFIGS_DIR = "configs"
os.makedirs(CONFIGS_DIR, exist_ok=True)

# -----------------------------------------------------------------------------
# --- HELPER FUNCTIONS ---
# -----------------------------------------------------------------------------
def write_status(message: str, history: int = 3):
    """Rolling status log: newest line first, last `history` cycle summaries
    kept (floor.py reads them). Repeated identical messages just refresh the
    timestamp instead of flooding the history.

    Written ATOMICALLY (tmp + os.replace): the 2026-07-29 machine crash died
    mid-write and left 1,284 NUL bytes here. That can no longer happen."""
    stamped = f"[{datetime.now(EASTERN_TZ).strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    try:
        prior = []
        try:
            prior = safe_io.read_text_tolerant(STATUS_FILE).splitlines()
        except (FileNotFoundError, OSError):
            pass
        # Drop the previous line if it's the same message re-issued.
        if prior and prior[0].split("] ", 1)[-1] == message:
            prior = prior[1:]
        lines = [stamped] + prior[: history - 1]
        safe_io.atomic_write_text(STATUS_FILE, "\n".join(lines))
    except Exception as e:
        logger.error(f"Error writing to status file: {e}")

def write_positions(positions_dict: dict):
    """Atomic — position state must survive a crash mid-write."""
    try:
        safe_io.atomic_write_text(POSITIONS_STATE_FILE,
                                  json.dumps(positions_dict, indent=4))
    except Exception as e:
        logger.error(f"Error writing to positions file: {e}")

def append_trade_log(timestamp, ticker, action, price, shares, pnl_usd, pnl_pct, reason):
    header = ["Timestamp (ET)", "Ticker", "Action", "Price", "Shares", "PnL (USD)", "PnL (%)", "Reason"]
    log_entry = {
        "Timestamp (ET)": timestamp, "Ticker": ticker, "Action": action, 
        "Price": price, "Shares": shares, "PnL (USD)": pnl_usd, 
        "PnL (%)": pnl_pct, "Reason": reason
    }
    try:
        file_exists = os.path.exists(TRADE_LOG_FILE)
        df = pd.DataFrame([log_entry])
        df.to_csv(TRADE_LOG_FILE, mode='a', header=not file_exists, index=False)
    except Exception as e:
        logger.error(f"Error appending to trade log: {e}")


def send_discord_notification(ticker, action, price, reason, pnl_pct=None):
    if not DISCORD_WEBHOOK_URL:
        return
    webhook = DiscordWebhook(url=DISCORD_WEBHOOK_URL)
    color = "00ff00" if action.upper() == "BUY" else "ff0000"
    title = f"🚀 BUY Signal: {ticker}" if action.upper() == "BUY" else f"🛑 SELL Signal: {ticker}"
    
    embed = DiscordEmbed(title=title, color=color)
    embed.set_timestamp()
    embed.add_embed_field(name="Price", value=f"${price:,.2f}")
    embed.add_embed_field(name="Reason", value=reason)
    if pnl_pct is not None:
        embed.add_embed_field(name="PnL (%)", value=f"{pnl_pct:.2f}%")
        
    webhook.add_embed(embed)
    try:
        webhook.execute()
        logger.info("Discord notification sent.")
    except Exception as e:
        logger.error(f"Failed to send Discord notification: {e}")

# -----------------------------------------------------------------------------
# --- LIVE TRADING BOT WORKER (Alpaca paper trading) ---
# -----------------------------------------------------------------------------
def fetch_news_headlines(ticker):
    if not finnhub_client:
        return []
    try:
        today_str = datetime.now(EASTERN_TZ).strftime('%Y-%m-%d')
        week_ago_str = (datetime.now(EASTERN_TZ) - timedelta(days=5)).strftime('%Y-%m-%d')
        recent_news = finnhub_client.company_news(ticker, _from=week_ago_str, to=today_str)
        return [n['headline'] for n in recent_news[:5]]
    except Exception:
        return []


def build_gatekeeper_kwargs(signal, df, risk_profile, interval_mins, news_headlines):
    """Assemble the shared gatekeeper arguments for any strategy's signal.
    Ensures df20 carries ema_fast/ema_slow/rsi_14/adx_14 for the prompt."""
    fast_ema, slow_ema = risk_profile['fast_ema'], risk_profile['slow_ema']
    df = df.copy()
    if 'ema_fast' not in df.columns:
        df['ema_fast'] = ta.ema(df['close'], length=fast_ema)
    if 'ema_slow' not in df.columns:
        df['ema_slow'] = ta.ema(df['close'], length=slow_ema)
    if 'rsi_14' not in df.columns:
        rsi_series = ta.rsi(df['close'], length=14)
        if rsi_series is not None:
            df['rsi_14'] = rsi_series
    if 'adx_14' not in df.columns:
        adx_df = ta.adx(df['high'], df['low'], df['close'], length=14)
        if adx_df is not None and not adx_df.empty:
            df['adx_14'] = adx_df['ADX_14']
    df20 = df.dropna().tail(20)
    if df20.empty:
        df20 = df.tail(20)

    last = df20.iloc[-1]
    crossover_count = count_ema_crossovers(df20)
    avg_vol_recent = df20['volume'].tail(3).mean()
    avg_vol_prior = df20['volume'].iloc[-6:-3].mean() if len(df20) >= 6 else avg_vol_recent
    volume_trend = "increasing" if avg_vol_recent > avg_vol_prior else "decreasing"
    ema_spread_pct = 0.0
    if ('ema_fast' in df20.columns and 'ema_slow' in df20.columns
            and pd.notna(last['ema_fast']) and pd.notna(last['ema_slow'])
            and last['ema_slow']):
        ema_spread_pct = ((last['ema_fast'] - last['ema_slow']) / last['ema_slow']) * 100
    swing_high = float(df20['high'].max())
    dist_to_resistance_pct = ((swing_high - signal.entry) / signal.entry) * 100
    rr = risk.reward_risk(signal.entry, signal.stop, signal.target) or 0.0

    return {
        "ticker": signal.ticker,
        "df20": df20,
        "ema_spread_pct": float(ema_spread_pct),
        "volume_trend": volume_trend,
        "crossover_count": crossover_count,
        "dist_to_resistance_pct": float(dist_to_resistance_pct),
        "entry_price": signal.entry,
        "stop_price": signal.stop,
        "target_price": signal.target,
        "rr_ratio": float(rr),
        "interval_mins": interval_mins,
        "fast_ema": fast_ema,
        "slow_ema": slow_ema,
        "news_headlines": news_headlines,
        "setup_name": signal.setup_name,
        "setup_description": signal.reasoning,
    }


def journal_rules_pass(ticker, setup_name, filter_name, details=""):
    """Complete pass log: a detector fired but a deterministic filter killed it."""
    try:
        journal.log_rules_pass(ticker, setup_name, filter_name, details)
    except Exception as e:
        logger.error(f"Failed to journal rules pass for {ticker}: {e}")


def reconcile_positions(broker, positions):
    """On startup: broker is the source of truth for what we actually hold."""
    try:
        broker_positions = {p.symbol: p for p in broker.get_positions()}
    except BrokerError as e:
        logger.error(f"Reconcile skipped — cannot reach broker: {e}")
        return positions

    for ticker, state in list(positions.items()):
        if state.get("in_position") and ticker not in broker_positions:
            logger.warning(f"Reconcile: {ticker} marked open locally but flat at broker — clearing.")
            positions[ticker] = {"in_position": False}

    for symbol, pos in broker_positions.items():
        state = positions.get(symbol, {})
        if not state.get("in_position"):
            logger.warning(f"Reconcile: adopting broker position {symbol} not tracked locally.")
            stop_id, target_id = None, None
            try:
                for o in broker.get_open_orders(symbol):
                    otype = str(getattr(o, "order_type", None) or getattr(o, "type", "")).lower()
                    if "stop" in otype:
                        stop_id = str(o.id)
                    elif "limit" in otype:
                        target_id = str(o.id)
            except BrokerError:
                pass
            positions[symbol] = {
                "in_position": True,
                # Adopted with no local history: display-only, never managed.
                "source": "unknown",
                "entry_price": float(pos.avg_entry_price),
                "shares_held": abs(float(pos.qty)),
                "stop_order_id": stop_id,
                "target_order_id": target_id,
                "trailing_stop_price": 0.0,
                "profit_target_price": None,
                "decision_id": None,
                "entry_trade_id": None,
                "setup": "reconciled",
            }
    return positions


def detect_filled_exit(broker, ticker, state):
    """If one of the bracket exit legs filled, return (fill_price, reason, order_id)."""
    for key, reason in (("stop_order_id", "Stop Loss"), ("target_order_id", "Profit Target")):
        order_id = state.get(key)
        if not order_id:
            continue
        try:
            order = broker.get_order(order_id)
        except BrokerError:
            continue
        status = str(getattr(order, "status", "")).lower()
        if "filled" in status and getattr(order, "filled_avg_price", None):
            return float(order.filled_avg_price), reason, str(order_id)
    return None, None, None


def handle_position_exit(broker, positions, ticker, state, fill_price, reason,
                         now_et, broker_order_id=None):
    """Journal a SELL from actual fill prices and clear local state.

    Single-authority journaling: journal.record_exit keys idempotence on the
    broker's order id, shared with orders.py sync — whichever path sees the
    fill first journals it; this one skips silently if sync got there first."""
    entry_price = state.get("entry_price", fill_price)
    qty = state.get("shares_held", 0)

    trade_id, pnl_usd, pnl_pct = journal.record_exit(
        ticker, qty, fill_price, reason,
        decision_id=state.get("decision_id"),
        broker_order_id=broker_order_id,
        entry_price=entry_price)
    positions[ticker] = {"in_position": False}
    if trade_id is None:
        logger.info(f"{ticker}: exit already journaled (order {broker_order_id}) — "
                    "state cleared, no duplicate row.")
        return

    append_trade_log(now_et.strftime('%Y-%m-%d %H:%M:%S'), ticker, "SELL",
                     f"${fill_price:.2f}", qty, pnl_usd, f"{pnl_pct:.2f}%", reason)
    send_discord_notification(ticker, "SELL", fill_price, reason, pnl_pct)
    logger.info(f"EXIT {ticker} @ ${fill_price:.2f} ({reason}). PnL: ${pnl_usd:.2f}")


def resolve_exit_fill(broker, ticker):
    """The position is gone but no tracked leg reports filled (leg ids lost,
    replaced order, manual dashboard close...). Find the REAL exit fill from
    the broker's closed orders. Returns (fill_price, reason, order_id) or
    (None, None, None) — callers must NOT journal a phantom at last price."""
    from datetime import timezone as _tz
    try:
        since = datetime.now(_tz.utc) - timedelta(days=3)
        candidates = [o for o in broker.get_closed_orders_since(since)
                      if o.symbol == ticker
                      and "sell" in str(getattr(o, "side", "")).lower()
                      and getattr(o, "filled_avg_price", None)]
        if not candidates:
            return None, None, None
        latest = max(candidates, key=lambda o: o.filled_at)
        otype = str(getattr(latest, "order_type", None)
                    or getattr(latest, "type", "")).lower()
        reason = ("Stop Loss" if "stop" in otype
                  else "Profit Target" if "limit" in otype else "Exit")
        return float(latest.filled_avg_price), f"{reason} (resolved)", str(latest.id)
    except BrokerError as e:
        logger.warning(f"{ticker}: could not resolve exit fill: {e}")
        return None, None, None


def live_bot_worker():
    """SUPERVISOR — the cycle loop must never die silently.

    Three unattended hangs (Jul 24, Jul 28, Aug 7) showed the same shape:
    the process alive, the heartbeat frozen. An unhandled exception escaping
    the cycle body kills the loop while the process lingers. This wrapper
    catches ANY escape, logs the traceback, writes a visible status, and
    restarts the loop instead of leaving a zombie."""
    crashes = 0
    while os.path.exists(LOCK_FILE):
        try:
            _worker_loop()
            return                      # clean exit: the lock was removed
        except Exception as e:
            crashes += 1
            logger.exception(f"WORKER LOOP CRASHED (#{crashes}) — restarting in 30s")
            try:
                write_status(f"Loop crashed ({type(e).__name__}: {e}) — "
                             f"auto-restarting (crash #{crashes})")
                send_discord_notification(
                    "WORKER", "SELL", 0.0,
                    f"⚠️ Loop crashed ({type(e).__name__}: {str(e)[:120]}) — "
                    f"auto-restarting")
            except Exception:
                pass
            a_time.sleep(30)
    logger.info("Bot worker supervisor exiting (lock removed).")


def _worker_loop():
    try:
        broker = Broker()
    except Exception as e:
        logger.error(f"Cannot start bot — broker init failed: {e}")
        write_status(f"BOT STOPPED: broker init failed — {e}")
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
        return

    journal.init_db()

    # Position state must survive restarts: broker is the source of truth.
    try:
        with open(POSITIONS_STATE_FILE, 'r') as f:
            positions = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        positions = {}
    positions = reconcile_positions(broker, positions)
    write_positions(positions)

    # Refresh the candidate universe once per session start (graceful on failure).
    try:
        with open(CONFIG_FILE, "r") as f:
            startup_config = json.load(f)
        candidates = universe.refresh(broker, startup_config)
        logger.info(f"Universe refreshed: {len(candidates)} candidates.")
    except Exception as e:
        logger.warning(f"Universe refresh failed — falling back to configured tickers: {e}")

    logger.info("Live bot worker started against Alpaca PAPER account.")

    cycle_count = 0                # liveness counter (status line, NOT the journal)
    journaled_passes = set()       # (ticker, setup, filter, bar/day) already journaled
    daily_evaluated = {}           # (ticker, strat) -> last completed bar evaluated
    gatekeeper_errors = {}         # (ticker, setup, bar) -> [count, next_retry_ts]
    gatekeeper_alerted = set()     # keys we've already Discord-alerted about
    gatekeeper_rejected = set()    # (ticker, setup, bar) the gatekeeper already declined —
                                   # never re-ask about the same signal bar (launch sessions
                                   # burned 60+ Claude calls re-asking about one XOM bar)

    while os.path.exists(LOCK_FILE):
        cycle_count += 1
        try:
            with open(CONFIG_FILE, "r") as f: config = json.load(f)

            ticker_profiles = config["ticker_profiles"]
            # Daily universe candidates replace the fixed ticker list; open
            # positions are ALWAYS scanned so they stay managed even after
            # dropping out of the universe. Falls back to configured tickers.
            universe_tickers = universe.load_universe_tickers()
            base_list = universe_tickers or list(ticker_profiles.keys())
            held = [t for t, s in positions.items() if s.get("in_position")]
            ticker_list = list(dict.fromkeys(base_list + held))
            interval_mins = config["interval"]
            max_positions = config["max_positions"]
            use_spy_filter = config.get("use_spy_filter", True)
            use_time_filter = config.get("use_time_filter", True)
            use_claude_filter = config.get("use_claude_filter", True)
            claude_conviction_threshold = config.get("claude_conviction_threshold", 70)
            analyst_mode = config.get("analyst_mode", "shadow")
            daily_loss_limit_pct = config.get("daily_loss_limit_pct", 3.0)
            position_cap_pct = risk.max_position_pct(config)
        except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
            logger.error(f"Could not load/parse config, skipping iteration: {e}")
            a_time.sleep(30)
            continue

        now_et = datetime.now(EASTERN_TZ)
        if not (now_et.weekday() < 5 and dttime(9, 30) <= now_et.time() < dttime(16, 0)):
            write_status("Market is CLOSED. Waiting...")
            a_time.sleep(30)
            continue

        # --- Capital comes from the broker, hard-capped by capital_cap_usd.
        # portfolio_state.json is display cache only ---
        try:
            broker_equity = broker.get_equity()
            equity = risk.effective_equity(broker_equity, config)
            with open(PORTFOLIO_STATE_FILE, 'w') as f:
                json.dump({"capital": equity, "broker_equity": broker_equity,
                           "source": "alpaca_paper",
                           "as_of": now_et.isoformat()}, f)
        except BrokerError as e:
            logger.error(f"Cannot fetch account equity, skipping cycle: {e}")
            write_status("Broker unreachable. Retrying...")
            a_time.sleep(60)
            continue

        # --- Daily-loss circuit breaker, based on journaled realized PnL ---
        daily_pnl = 0.0
        try:
            daily_pnl = journal.daily_realized_pnl()
        except Exception as e:
            logger.error(f"Could not compute daily realized PnL: {e}")
        loss_limit_usd = risk.daily_loss_limit_usd(equity, daily_loss_limit_pct)
        breaker_tripped = daily_pnl <= -loss_limit_usd
        if breaker_tripped:
            logger.warning(f"CIRCUIT BREAKER: daily PnL ${daily_pnl:.2f} <= -${loss_limit_usd:.2f}. No new trades.")

        # --- SPY market filter (Alpaca data) ---
        is_market_bullish = not use_spy_filter
        if use_spy_filter:
            try:
                spy_df = broker.get_bars(["SPY"], timeframe_minutes=interval_mins,
                                         lookback_days=2).get("SPY")
                if spy_df is not None and not spy_df.empty:
                    spy_ema20 = spy_df['close'].ewm(span=20, adjust=False).mean()
                    is_market_bullish = bool(spy_df['close'].iloc[-1] > spy_ema20.iloc[-1])
            except BrokerError as e:
                logger.warning(f"Could not fetch SPY data: {e}")

        # --- Market data from Alpaca (replaces yfinance) ---
        try:
            intraday_bars = broker.get_bars(ticker_list, timeframe_minutes=interval_mins,
                                            lookback_days=5)
        except BrokerError as e:
            logger.error(f"Market data download failed, skipping cycle: {e}")
            write_status("Failed to download market data. Retrying...")
            a_time.sleep(60)
            continue

        daily_bars = {}
        needs_daily = any(
            s.timeframe == "daily"
            for t in ticker_list for s in enabled_strategies(t, config)
        )
        if needs_daily:
            try:
                daily_bars = broker.get_daily_bars(ticker_list, lookback_days=60)
            except BrokerError as e:
                logger.warning(f"Daily bars unavailable this cycle: {e}")

        # Snapshot of broker positions once per cycle for exit detection.
        try:
            broker_symbols = {p.symbol for p in broker.get_positions()}
        except BrokerError:
            broker_symbols = None  # unknown — don't infer exits this cycle

        def journal_pass_once(t, setup_name, filter_name, details="", bar_key=None):
            """Journal a rules pass ONCE per (ticker, setup, filter, bar/day).
            The same rejection recurring every 30s cycle is noise, not signal —
            launch day wrote 2,000+ duplicate rows before this dedupe."""
            key = (t, setup_name, filter_name, bar_key or now_et.strftime("%Y-%m-%d"))
            if key in journaled_passes:
                return
            if len(journaled_passes) > 20000:
                journaled_passes.clear()
            journaled_passes.add(key)
            journal_rules_pass(t, setup_name, filter_name, details)

        status_updates = []
        for ticker in ticker_list:
            open_positions_count = sum(1 for s in positions.values() if s.get("in_position"))
            # Cash actually deployed — entries must never push this above
            # effective equity (no margin, ever).
            open_notional = sum(s.get("shares_held", 0) * s.get("entry_price", 0)
                                for s in positions.values() if s.get("in_position"))
            state = positions.get(ticker, {})

            df = intraday_bars.get(ticker, pd.DataFrame())
            if df.empty or df['close'].dropna().empty:
                status_updates.append(f"{ticker}: No Data")
                continue
            current_price = float(df['close'].dropna().iloc[-1])

            profile_name = ticker_profiles.get(ticker, 'Moderate')
            risk_profile = config['risk_profiles'][profile_name]

            # =================== MANAGE OPEN POSITION ===================
            if state.get("in_position"):
                try:
                    # 1) Did a bracket exit leg fill? (stops live AT THE BROKER)
                    fill_price, reason, exit_order_id = detect_filled_exit(broker, ticker, state)
                    if fill_price is None and broker_symbols is not None \
                            and ticker not in broker_symbols:
                        # Position gone but no tracked leg marked filled.
                        # NEVER journal a phantom at last price — resolve the
                        # real fill, or clear state and let sync() journal it.
                        fill_price, reason, exit_order_id = resolve_exit_fill(broker, ticker)
                        if fill_price is None:
                            logger.warning(f"{ticker}: position gone, fill unresolved — "
                                           "state cleared; sync() will journal the real fill.")
                            positions[ticker] = {"in_position": False}
                            status_updates.append(f"{ticker}: Exited (fill pending sync)")
                            continue
                    if fill_price is not None:
                        handle_position_exit(broker, positions, ticker, state,
                                             fill_price, reason, now_et,
                                             broker_order_id=exit_order_id)
                        status_updates.append(f"{ticker}: SOLD ({reason})")
                        continue

                    # 2) Hard exit date (event_flow positions from order sheets)
                    hard_exit = state.get("hard_exit_date")
                    if hard_exit:
                        exit_date = datetime.strptime(hard_exit, "%Y-%m-%d").date()
                        past_date = now_et.date() > exit_date
                        at_close = (now_et.date() == exit_date
                                    and now_et.time() >= dttime(15, 55))
                        if past_date or at_close:
                            close_order = broker.close_position(ticker)
                            handle_position_exit(broker, positions, ticker, state,
                                                 current_price, "Hard Exit Date", now_et,
                                                 broker_order_id=str(getattr(close_order, "id", "")) or None)
                            status_updates.append(f"{ticker}: CLOSED (hard exit date)")
                            continue

                    # 3) Manual override
                    manual_sell_command_file = f"sell_{ticker}.command"
                    if os.path.exists(manual_sell_command_file):
                        os.remove(manual_sell_command_file)
                        close_order = broker.close_position(ticker)
                        handle_position_exit(broker, positions, ticker, state,
                                             current_price, "Manual Override", now_et,
                                             broker_order_id=str(getattr(close_order, "id", "")) or None)
                        status_updates.append(f"{ticker}: SOLD (manual)")
                        continue

                    # 4) Trailing stop: BOT positions only, and only past
                    # +1R (the structural stop stands until then). Daily
                    # setups trail on DAILY bars — trailing a daily
                    # structure with 5-min ATR is what killed NOK.
                    trail_df = df
                    if state.get("timeframe") == "daily":
                        daily_df_for_trail = daily_bars.get(ticker)
                        if daily_df_for_trail is not None and not daily_df_for_trail.empty:
                            trail_df = daily_df_for_trail
                    position_mgmt.maybe_ratchet_stop(broker, positions, ticker,
                                                     state, trail_df, risk_profile,
                                                     current_price)

                    entry_price = state.get("entry_price", current_price)
                    pnl_percent = ((current_price / entry_price) - 1) * 100 if entry_price else 0.0
                    owner = state.get("source", "unknown")
                    status_updates.append(f"{ticker}: In Pos [{owner}] ({pnl_percent:+.2f}%)")
                except BrokerError as e:
                    logger.error(f"{ticker}: broker error managing position: {e}")
                    status_updates.append(f"{ticker}: In Pos (broker retry)")
                continue

            # =================== SCAN FOR NEW ENTRY ===================
            is_primary_trading_hours = not use_time_filter or (
                dttime(9, 30) <= now_et.time() < dttime(11, 30)
                or dttime(14, 0) <= now_et.time() < dttime(15, 0))

            signal_found = None
            signal_bar_key = None
            pass_notes = []
            for strat in enabled_strategies(ticker, config):
                timeframe = daily_eval.strategy_timeframe(strat.name, config,
                                                          strat.timeframe)
                is_daily = timeframe == "daily"
                strat_df = daily_bars.get(ticker, pd.DataFrame()) \
                    if is_daily else df
                if strat_df.empty:
                    continue
                # Daily strategies evaluate ONCE per completed session (Goal
                # 15) — a new daily bar triggers exactly one evaluation.
                if is_daily and not daily_eval.should_evaluate(
                        daily_evaluated, ticker, strat.name, strat_df):
                    continue
                context = {"ticker": ticker, "risk_profile": risk_profile,
                           "config": config}
                try:
                    result = strat.detect(strat_df, context)
                except Exception as e:
                    logger.error(f"{ticker}: {strat.name} detector error: {e}")
                    continue
                finally:
                    if is_daily:
                        daily_eval.mark_evaluated(daily_evaluated, ticker,
                                                  strat.name, strat_df)

                if isinstance(result, Rejection):
                    # Detector fired but a deterministic filter killed it —
                    # journal the pass once per signal bar, not per cycle.
                    bar_key = str(strat_df.index[-2]) if len(strat_df) >= 2 else None
                    journal_pass_once(ticker, result.setup_name, result.filter_name,
                                      result.details, bar_key=bar_key)
                    pass_notes.append(f"Pass ({result.setup_name}: {result.filter_name})")
                elif isinstance(result, Signal):
                    # Cache key: for DAILY strategies use the completed signal
                    # bar's DATE, which is stable all session. Using index[-2]
                    # shifted the moment today's partial bar appeared, which
                    # re-armed the gatekeeper cache mid-session and re-asked
                    # about an already-rejected setup (BA, 2026-07-29).
                    if is_daily:
                        signal_bar_key = daily_eval.completed_bar_date(strat_df) \
                            or now_et.strftime("%Y-%m-%d")
                    else:
                        signal_bar_key = (str(strat_df.index[-2])
                                          if len(strat_df) >= 2
                                          else now_et.strftime("%Y-%m-%d"))
                    # Rule #3 default for daily entries: abort if the session
                    # open is below the signal bar's midpoint (auto-set).
                    if is_daily:
                        aborted, open_p, mid = daily_eval.gap_abort(
                            strat_df, current_price)
                        if aborted:
                            bar_key = (str(strat_df.index[-2])
                                       if len(strat_df) >= 2 else None)
                            journal_pass_once(
                                ticker, result.setup_name, "gap_below_signal_mid",
                                f"open {open_p:.2f} < signal bar mid {mid:.2f}",
                                bar_key=bar_key)
                            pass_notes.append(
                                f"Pass ({result.setup_name}: gap_below_signal_mid)")
                            continue
                    signal_found = result
                    break

            if signal_found is None:
                # One status line per ticker (no Pass + Waiting double-print).
                status_updates.append(
                    f"{ticker}: " + (pass_notes[0] if pass_notes else "Waiting (No Signal)"))
                continue

            signal = signal_found

            # --- Global deterministic gates: journaled as passes too ---
            if breaker_tripped:
                journal_pass_once(ticker, signal.setup_name, "circuit_breaker",
                                  f"daily PnL ${daily_pnl:.2f} <= -${loss_limit_usd:.2f}")
                status_updates.append(f"{ticker}: Pass (circuit breaker)")
                continue
            # Rule #5 amendment (boardroom 2026-07-28): continuation setups
            # stay regime-blocked; mean_reversion_reclaim is EXEMPT because
            # chop is its best cell (backtest +0.38R vs +0.25R trending).
            # Exempt fires taken in chop are tagged for later analysis.
            if not is_market_bullish:
                if signal.setup_name in config.get("spy_filter_exempt", []):
                    tag_key = (ticker, signal.setup_name, "chop_reclaim",
                               signal_bar_key)
                    if tag_key not in journaled_passes:
                        journaled_passes.add(tag_key)
                        try:
                            journal.log_signal_tag(
                                ticker, signal.setup_name, "chop_reclaim",
                                f"Rule #5 exemption: proceeding with SPY below "
                                f"its 20-EMA (entry {signal.entry:.2f})")
                        except Exception as e:
                            logger.error(f"Failed to tag chop_reclaim: {e}")
                    status_updates.append(f"{ticker}: chop_reclaim (Rule #5)")
                else:
                    journal_pass_once(ticker, signal.setup_name, "spy_bearish",
                                      "SPY below its 20-EMA")
                    status_updates.append(f"{ticker}: Pass (SPY bearish)")
                    continue
            if not is_primary_trading_hours:
                journal_pass_once(ticker, signal.setup_name, "outside_hours", "")
                status_updates.append(f"{ticker}: Pass (outside hours)")
                continue
            if open_positions_count >= max_positions:
                journal_pass_once(ticker, signal.setup_name, "max_positions", "")
                status_updates.append(f"{ticker}: Pass (max positions)")
                continue

            ok, reject_reason = risk.check_signal(
                signal.entry, signal.stop, signal.target, equity,
                open_positions=open_positions_count, max_positions=max_positions,
                daily_pnl=daily_pnl, daily_loss_limit_usd=loss_limit_usd,
                open_notional_usd=open_notional,
                position_cap_pct=position_cap_pct)
            if not ok:
                journal_pass_once(ticker, signal.setup_name, reject_reason,
                                  f"entry={signal.entry:.2f} stop={signal.stop:.2f} "
                                  f"target={signal.target:.2f}")
                status_updates.append(f"{ticker}: Pass ({reject_reason})")
                continue

            # --- AI gatekeeper (Claude authoritative; shadow journals local model) ---
            # Never re-ask about a signal bar the gatekeeper already declined.
            gate_key = (ticker, signal.setup_name, signal_bar_key)
            if gate_key in gatekeeper_rejected:
                status_updates.append(f"{ticker}: Gatekeeper blocked (cached this bar)")
                continue
            # Error backoff (Goal 21): after 3 consecutive errors on the same
            # (ticker, setup, bar), retry only every 10 minutes.
            err_state = gatekeeper_errors.get(gate_key)
            if err_state and err_state[0] >= 3 and a_time.time() < err_state[1]:
                status_updates.append(f"{ticker}: Gatekeeper backoff "
                                      f"({err_state[0]} errors)")
                continue

            decision_id = None
            if use_claude_filter:
                try:
                    news_headlines = fetch_news_headlines(ticker)
                    gk_df = signal.extras.get("df", df)
                    gk_kwargs = build_gatekeeper_kwargs(signal, gk_df, risk_profile,
                                                        interval_mins, news_headlines)
                    decision_context = {
                        "setup": signal.setup_name, "entry": signal.entry,
                        "stop": signal.stop, "target": signal.target,
                        "reasoning": signal.reasoning, "equity": equity,
                    }
                    verdict, decision_id = analyst.get_verdict(
                        analyst_mode, ticker, signal.setup_name,
                        decision_context, gk_kwargs)

                    conviction = verdict.get('conviction_score', 0)
                    approved = verdict.get('approved', False)
                    if isinstance(approved, str):
                        approved = approved.lower() == "true"
                    if "error" in verdict:
                        count = gatekeeper_errors.get(gate_key, [0, 0])[0] + 1
                        gatekeeper_errors[gate_key] = [count, a_time.time() + 600]
                        if count >= 3 and gate_key not in gatekeeper_alerted:
                            gatekeeper_alerted.add(gate_key)
                            logger.error(f"GATEKEEPER: {count} consecutive errors on "
                                         f"{ticker}/{signal.setup_name} — backing off "
                                         f"to 10-minute retries. {verdict['error']}")
                            try:
                                send_discord_notification(
                                    ticker, "SELL", current_price,
                                    f"⚠️ Gatekeeper erroring ({count}x): "
                                    f"{str(verdict['error'])[:150]} — 10-min backoff")
                            except Exception:
                                pass
                    elif gate_key in gatekeeper_errors:
                        del gatekeeper_errors[gate_key]   # recovered

                    if "error" in verdict or not approved \
                            or conviction < claude_conviction_threshold:
                        reason_msg = (verdict.get('rejection_reason')
                                      or verdict.get('error')
                                      or f"conviction={conviction}")
                        if "error" not in verdict:
                            # Genuine rejection: cache so this signal bar is
                            # never re-sent. Errors are transient — retry OK.
                            if len(gatekeeper_rejected) > 20000:
                                gatekeeper_rejected.clear()
                            gatekeeper_rejected.add(gate_key)
                        status_updates.append(f"{ticker}: Gatekeeper blocked ({reason_msg})")
                        logger.info(f"Gatekeeper blocked {ticker}: {verdict.get('reasoning', reason_msg)}")
                        continue
                except Exception as e:
                    logger.error(f"Gatekeeper error for {ticker}: {e}")
                    status_updates.append(f"{ticker}: Waiting (gatekeeper failed)")
                    continue

            # --- Risk-based sizing (whole shares), then bracket order AT THE BROKER ---
            qty = risk.position_size(equity, risk_profile['risk_per_trade_pct'],
                                     signal.entry, signal.stop,
                                     open_notional_usd=open_notional,
                                     position_cap_pct=position_cap_pct)
            if qty < 1:
                # Whole-share reality: journal WHY (tells us which tickers this
                # account can't afford).
                zero_reason = risk.zero_size_reason(signal.entry, equity,
                                                    position_cap_pct=position_cap_pct)
                journal_pass_once(ticker, signal.setup_name, zero_reason,
                                  f"entry={signal.entry:.2f} equity={equity:.2f}")
                status_updates.append(f"{ticker}: Pass ({zero_reason})")
                continue
            ok, reject_reason = risk.check_signal(
                signal.entry, signal.stop, signal.target, equity,
                notional_usd=qty * signal.entry,
                open_positions=open_positions_count, max_positions=max_positions,
                daily_pnl=daily_pnl, daily_loss_limit_usd=loss_limit_usd,
                open_notional_usd=open_notional,
                position_cap_pct=position_cap_pct)
            if not ok:
                journal_pass_once(ticker, signal.setup_name, reject_reason, "")
                status_updates.append(f"{ticker}: Pass ({reject_reason})")
                continue

            # Order-side dedupe: ask the ACCOUNT, not our cache, whether this
            # ticker already has a position or a working bracket. This is what
            # would have stopped the 2026-08-13 double fills (NOK 2x28sh).
            dup = position_mgmt.duplicate_entry_exists(broker, ticker)
            if dup:
                journal_pass_once(ticker, signal.setup_name,
                                  "duplicate_entry_blocked", dup,
                                  bar_key=signal_bar_key)
                logger.warning(f"{ticker}: DUPLICATE ENTRY BLOCKED — {dup}")
                status_updates.append(f"{ticker}: duplicate blocked ({dup[:40]})")
                continue

            try:
                order = broker.submit_bracket(ticker, qty, signal.stop, signal.target)
            except BrokerError as e:
                logger.error(f"{ticker}: bracket order failed: {e}")
                status_updates.append(f"{ticker}: Order failed (will retry)")
                continue

            # Bracket legs come with the submit response.
            stop_order_id, target_order_id = None, None
            for leg in (getattr(order, "legs", None) or []):
                ltype = str(getattr(leg, "order_type", None)
                            or getattr(leg, "type", "")).lower()
                if "stop" in ltype:
                    stop_order_id = str(leg.id)
                elif "limit" in ltype:
                    target_order_id = str(leg.id)

            # Wait briefly for the market entry to fill so we journal real prices.
            fill_price = signal.entry
            try:
                for _ in range(10):
                    o = broker.get_order(order.id)
                    if str(getattr(o, "status", "")).lower() == "filled" \
                            and getattr(o, "filled_avg_price", None):
                        fill_price = float(o.filled_avg_price)
                        # nested get_order also carries legs — fill any gaps.
                        for leg in (getattr(o, "legs", None) or []):
                            ltype = str(getattr(leg, "order_type", None)
                                        or getattr(leg, "type", "")).lower()
                            if "stop" in ltype and not stop_order_id:
                                stop_order_id = str(leg.id)
                            elif "limit" in ltype and not target_order_id:
                                target_order_id = str(leg.id)
                        break
                    a_time.sleep(1)
            except BrokerError as e:
                logger.warning(f"{ticker}: could not confirm entry fill yet: {e}")

            trade_id = journal.log_trade(ticker, "BUY", qty, fill_price,
                                         reason=signal.setup_name,
                                         decision_id=decision_id)
            positions[ticker] = {
                "in_position": True,
                "source": "bot",   # ownership: the bot manages ONLY its own entries
                "entry_price": fill_price,
                "shares_held": qty,
                # initial_stop is the PLAYBOOK-STRUCTURAL stop (reclaim/
                # breakout bar low). It never moves, so the R distance
                # survives every ratchet and gates trailing until +1R.
                "initial_stop": signal.stop,
                "trailing_stop_price": signal.stop,
                "profit_target_price": signal.target,
                "timeframe": ("daily" if signal.setup_name in
                              ("mean_reversion_reclaim", "momentum_continuation")
                              else "intraday"),
                "stop_order_id": stop_order_id,
                "target_order_id": target_order_id,
                "entry_order_id": str(order.id),
                "decision_id": decision_id,
                "entry_trade_id": trade_id,
                "setup": signal.setup_name,
            }
            # Capital persisted on BUY (display cache; broker stays authoritative).
            try:
                post_buy_equity = broker.get_equity()
                with open(PORTFOLIO_STATE_FILE, 'w') as f:
                    json.dump({"capital": risk.effective_equity(post_buy_equity, config),
                               "broker_equity": post_buy_equity,
                               "source": "alpaca_paper",
                               "as_of": now_et.isoformat()}, f)
            except (BrokerError, OSError) as e:
                logger.warning(f"Could not refresh capital cache after BUY: {e}")

            append_trade_log(now_et.strftime('%Y-%m-%d %H:%M:%S'), ticker, "BUY",
                             f"${fill_price:.2f}", qty, 0.0, "0.00%", signal.setup_name)
            send_discord_notification(ticker, "BUY", fill_price,
                                      f"{signal.setup_name}: {signal.reasoning}")
            logger.info(f"BOUGHT {qty} {ticker} @ ${fill_price:.2f} "
                        f"(bracket: stop {signal.stop:.2f} / target {signal.target:.2f})")
            status_updates.append(f"{ticker}: BOUGHT ({signal.setup_name})")

        write_positions(positions)
        breaker_note = " | ⛔ CIRCUIT BREAKER ACTIVE" if breaker_tripped else ""
        write_status(f"Cycle #{cycle_count} | Monitoring Live (Alpaca paper): "
                     + " | ".join(status_updates) + breaker_note)
        a_time.sleep(30)

    logger.info("Bot worker thread has been stopped.")
    write_status("Bot is Idle.")

# -----------------------------------------------------------------------------
# --- STREAMLIT UI ---
# -----------------------------------------------------------------------------
st.title("📈 Algorithmic Trading Suite")
tab1, tab2, tab3, tab4 = st.tabs(["🤖 Live Trading Bot", "Backtesting Suite",
                                  "🔬 AI Analysis Tools", "🎓 Intern Desk"])

# --- Dashboard helpers: observe + start/stop only. bot_config.json is the
# single source of truth and is NEVER written from this UI. ---
def load_active_config():
    """Read bot_config.json (the source of truth). Returns (config, error)."""
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f), None
    except FileNotFoundError:
        return None, f"{CONFIG_FILE} not found — nothing for the bot to run."
    except json.JSONDecodeError as e:
        return None, f"{CONFIG_FILE} is not valid JSON: {e}"


def connect_broker():
    """Broker handle for live account reads. Returns (broker, error)."""
    try:
        return Broker(), None
    except Exception as e:
        return None, str(e)


with tab1:
    st.header("Live Day Trading Bot")
    st.caption("Dashboard — observe and start/stop only. Configuration lives in "
               f"`{CONFIG_FILE}` (edited via git or a Claude session), never from here.")
    is_running = os.path.exists(LOCK_FILE)

    active_config, config_error = load_active_config()
    broker, broker_err = connect_broker()

    # --- Real effective capital, straight from the broker (hard-capped) ---
    if broker_err:
        st.warning(f"Broker unreachable — {broker_err}")
    else:
        try:
            broker_equity = broker.get_equity()
            effective = risk.effective_equity(broker_equity, active_config or {})
            cap = (active_config or {}).get("capital_cap_usd")
            state = "🟢 Running" if is_running else "⚪ Idle"
            if cap is not None:
                st.metric(f"{state} · Effective Capital", f"${effective:,.2f}",
                          help=f"min(capital_cap_usd ${cap:,.0f}, broker equity ${broker_equity:,.2f})")
            else:
                st.metric(f"{state} · Effective Capital (UNCAPPED)", f"${effective:,.2f}",
                          help="capital_cap_usd missing from bot_config.json — sizing off full broker equity!")
        except BrokerError as e:
            st.warning(f"Could not read account equity: {e}")

    # --- Start / Stop (never create or modify bot_config.json) ---
    ctrl1, ctrl2 = st.columns(2)
    start_clicked = ctrl1.button("▶️ Start Bot", width='stretch',
                                 disabled=is_running or config_error is not None)
    stop_clicked = ctrl2.button("⏹️ Stop Bot", width='stretch',
                                disabled=not is_running)
    if config_error and not is_running:
        st.error(f"Cannot start — {config_error} "
                 f"Edit {CONFIG_FILE} (git / Claude session), then reload this page.")

    if start_clicked:
        # Config already exists on disk — rotate the prior session's trade log
        # so this session's view starts clean, then launch the worker thread.
        if os.path.exists(TRADE_LOG_FILE):
            os.rename(TRADE_LOG_FILE,
                      f"trades_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        n = len(active_config.get("ticker_profiles", {})) if active_config else 0
        write_status(f"Bot starting ({n} configured tickers; universe scan pending)...")
        # Lock records the owning PID so the watchdog/takeover can target it
        # precisely instead of guessing at python processes.
        safe_io.atomic_write_text(LOCK_FILE, str(os.getpid()))
        threading.Thread(target=live_bot_worker, daemon=True).start()
        st.toast("Bot started!", icon="✅"); a_time.sleep(1); st.rerun()

    if stop_clicked:
        if os.path.exists(LOCK_FILE): os.remove(LOCK_FILE)
        st.toast("Bot stopping...", icon="⚠️"); a_time.sleep(1); st.rerun()

    st.divider()

    # --- Live status line ---
    st.subheader("Live Status")
    status_message = "Bot is Idle."
    if is_running:
        if os.path.exists(STATUS_FILE):
            try:
                with open(STATUS_FILE, "r") as f: status_message = f.read()
            except Exception: pass
        else:
            status_message = "Bot is initializing..."
    st.info(status_message)

    # --- Open positions (broker is the source of truth) ---
    st.subheader("📊 Open Positions")
    if broker_err:
        st.caption("Broker unreachable — cannot list positions.")
    else:
        try:
            positions_list = broker.get_positions()
            if positions_list:
                rows = []
                for p in positions_list:
                    pl = getattr(p, "unrealized_pl", None)
                    plpc = getattr(p, "unrealized_plpc", None)
                    rows.append({
                        "Ticker": p.symbol,
                        "Qty": float(p.qty),
                        "Avg Entry": float(p.avg_entry_price),
                        "Current": float(getattr(p, "current_price", 0) or 0),
                        "Mkt Value": float(getattr(p, "market_value", 0) or 0),
                        "Unreal. P/L $": float(pl) if pl is not None else 0.0,
                        "P/L %": (float(plpc) * 100) if plpc is not None else 0.0,
                    })
                st.dataframe(
                    pd.DataFrame(rows), width='stretch', hide_index=True,
                    column_config={
                        "Avg Entry": st.column_config.NumberColumn(format="$%.2f"),
                        "Current": st.column_config.NumberColumn(format="$%.2f"),
                        "Mkt Value": st.column_config.NumberColumn(format="$%.2f"),
                        "Unreal. P/L $": st.column_config.NumberColumn(format="$%.2f"),
                        "P/L %": st.column_config.NumberColumn(format="%.2f%%"),
                    })
            else:
                st.caption("No open positions.")
        except BrokerError as e:
            st.caption(f"Could not fetch positions: {e}")

    # --- Session trade log ---
    st.subheader("📋 Current Session Trade Log")
    if os.path.exists(TRADE_LOG_FILE):
        try:
            st.dataframe(pd.read_csv(TRADE_LOG_FILE), width='stretch',
                         hide_index=True)
        except Exception: st.caption("Trade log is being updated.")
    else:
        st.caption("Trade log for this session is empty.")

    # --- Read-only view of the active config (verify what the bot loaded) ---
    st.subheader("⚙️ Active Configuration (read-only)")
    if config_error:
        st.error(config_error)
    else:
        tp = active_config.get("ticker_profiles", {})
        left = {
            "analyst_mode": active_config.get("analyst_mode", "— (defaults to shadow)"),
            "capital_cap_usd": active_config.get("capital_cap_usd", "⚠️ MISSING (uncapped!)"),
            "daily_loss_limit_pct": active_config.get("daily_loss_limit_pct", "— (defaults to 3.0)"),
            "interval (min)": active_config.get("interval", "—"),
            "max_positions": active_config.get("max_positions", "—"),
        }
        right = {
            "use_spy_filter": active_config.get("use_spy_filter", "—"),
            "use_time_filter": active_config.get("use_time_filter", "—"),
            "use_claude_filter": active_config.get("use_claude_filter", "—"),
            "claude_conviction_threshold": active_config.get("claude_conviction_threshold", "—"),
        }
        col_l, col_r = st.columns(2)
        with col_l:
            for k, v in left.items(): st.write(f"**{k}:** {v}")
        with col_r:
            for k, v in right.items(): st.write(f"**{k}:** {v}")
        st.write("**configured tickers:** "
                 + (", ".join(f"{k}:{v}" for k, v in tp.items()) or "— (universe-driven)"))
        with st.expander("Full bot_config.json"):
            st.json(active_config)

with tab2:
    st.header("Backtesting Suite")
    st.warning("Backtesting features are under development.", icon="⚠️")

with tab3:
    st.header("🔬 AI Analysis Tools")
    st.caption("Get on-demand analysis from the multi-agent AI team.")

    ai_ticker = st.text_input("Enter Ticker Symbol for AI Analysis", "NVDA", key="ai_ticker").upper()

    if st.button("🤖 Run Full AI Analysis", width='stretch'):
        if not ai_ticker:
            st.error("Please enter a ticker symbol.")
        elif not os.getenv("ANTHROPIC_API_KEY"):
            st.error("ANTHROPIC_API_KEY is not set. Please add it to your .env file.")
        else:
            st.subheader(f"Analyzing {ai_ticker}...")
            
            tech_placeholder = st.empty()
            news_placeholder = st.empty()
            macro_placeholder = st.empty()
            social_placeholder = st.empty()
            final_placeholder = st.empty()

            tech_report, news_report, macro_report, social_report = {}, {}, {}, {}

            try:
                with st.spinner("📈 Technical Trader is analyzing the charts..."):
                    tech_data = yf.download(ai_ticker, period="1mo", interval="15m", progress=False, auto_adjust=True)

                    if tech_data.empty:
                        raise ValueError(f"No data downloaded for '{ai_ticker}'. It may be an invalid symbol or have no recent data.")

                    if isinstance(tech_data.columns, pd.MultiIndex):
                        tech_data.columns = tech_data.columns.droplevel(0)
                    tech_data.columns = [col.lower() for col in tech_data.columns]

                    required_ohlc = ['open', 'high', 'low', 'close', 'volume']
                    if not all(col in tech_data.columns for col in required_ohlc):
                        raise ValueError(f"Data from yfinance is missing required columns: {required_ohlc}. Please check the ticker symbol.")

                    tech_data['ema_fast'] = ta.ema(tech_data['close'], length=10)
                    tech_data['ema_slow'] = ta.ema(tech_data['close'], length=30)
                    tech_data['rsi_14'] = ta.rsi(tech_data['close'], length=14)
                    
                    adx_df = ta.adx(tech_data['high'], tech_data['low'], tech_data['close'], length=14)
                    if adx_df is not None and not adx_df.empty:
                        adx_df.columns = [col.lower() for col in adx_df.columns]
                        if 'adx_14' in adx_df.columns:
                            tech_data['adx_14'] = adx_df['adx_14']
                        else: tech_data['adx_14'] = None
                    else: tech_data['adx_14'] = None

                    tech_data.dropna(inplace=True)

                    required_for_ai = ['open', 'high', 'low', 'close', 'volume', 'ema_fast', 'ema_slow', 'rsi_14', 'adx_14']
                    if not all(col in tech_data.columns for col in required_for_ai):
                         raise ValueError(f"Data preparation failed. Missing columns for AI analysis: {[c for c in required_for_ai if c not in tech_data.columns]}")
                    
                    tech_report = get_technical_verdict(ai_ticker, tech_data.tail(20))
            except Exception as e:
                logger.error(f"Technical analysis failed: {e}", exc_info=True)
                tech_report = {"error": str(e)}
            
            with tech_placeholder.container(border=True):
                st.markdown("#### 📈 Technical Trader's Report")
                if "error" in tech_report: st.error(tech_report["error"])
                else: st.json(tech_report)

            try:
                with st.spinner("📰 Fundamental Analyst is scanning headlines..."):
                    news_report = get_news_and_sentiment(finnhub_client, ai_ticker)
            except Exception as e:
                logger.error(f"News analysis failed: {e}", exc_info=True)
                news_report = {"error": str(e)}

            with news_placeholder.container(border=True):
                st.markdown("#### 📰 Fundamental Analyst's Report")
                if "error" in news_report: st.error(news_report["error"])
                else: st.json(news_report)

            try:
                with st.spinner("🌍 Macro-Economic Analyst is assessing the climate..."):
                    macro_report = get_macro_economic_analysis(finnhub_client)
            except Exception as e:
                logger.error(f"Macro analysis failed: {e}", exc_info=True)
                macro_report = {"error": str(e)}

            with macro_placeholder.container(border=True):
                st.markdown("#### 🌍 Macro-Economic Analyst's Report")
                if "error" in macro_report: st.error(macro_report["error"])
                else: st.json(macro_report)

            try:
                with st.spinner("💬 Social Sentiment Analyst is checking the buzz..."):
                    social_report = get_social_sentiment_analysis(finnhub_client, ai_ticker)
            except Exception as e:
                logger.error(f"Social sentiment analysis failed: {e}", exc_info=True)
                social_report = {"error": str(e)}

            with social_placeholder.container(border=True):
                st.markdown("#### 💬 Social Sentiment Analyst's Report")
                if "error" in social_report: st.error(social_report["error"])
                else: st.json(social_report)

            with st.spinner("🧑‍⚖️ Hedge Fund Manager is making the final decision..."):
                reports = [tech_report, news_report, macro_report, social_report]
                if not any(isinstance(r, dict) and "error" in r for r in reports):
                    final_report = get_final_decision(ai_ticker, tech_report, news_report, macro_report, social_report)
                    with final_placeholder.container(border=True):
                        st.markdown("#### 🧑‍⚖️ Final Coordinated Decision")
                        if "error" in final_report: st.error(final_report["error"])
                        else:
                            decision = final_report.get('final_decision', 'N/A').upper()
                            color = "green" if decision == "BUY" else ("red" if decision == "SELL" else "orange")
                            st.markdown(f"#### Final Decision: <span style='color:{color};'>{decision}</span>", unsafe_allow_html=True)
                            st.markdown(f"**Reasoning:** {final_report.get('trade_reason', 'No reason provided.')}")
                            if final_report.get('veto_applied'):
                                st.warning(f"Veto applied: {final_report.get('veto_reason', 'Unknown veto condition')}")
                else:
                    with final_placeholder.container(border=True):
                         st.error("Could not make a final decision because one or more specialist agents failed.")

with tab4:
    import glob as _glob
    import subprocess as _subprocess
    import intern_desk

    st.header("🎓 Intern Desk")
    st.caption("Read-only. Trading stays CLI/scheduler-only; grades via "
               "`python intern_desk.py grade ...`.")

    # --- 1. Live run progress ---
    status = intern_desk.read_status()
    run_active = bool(status and status.get("active"))
    if run_active:
        done, total = status.get("done_count", 0), max(status.get("total", 1), 1)
        st.progress(min(done / total, 1.0))
        current = status.get("current_ticker") or "…"
        st.markdown(f"**Interviewing {current} ({done}/{total})** — "
                    f"model `{status.get('model', '?')}`")
        lv = status.get("last_verdicts") or []
        if lv:
            st.caption(" · ".join(
                f"{t}: {s}" + (f" ({c})" if c is not None else "")
                for t, s, c in lv[-10:][::-1]))
    elif status:
        last = status.get("finished_at") or status.get("started_at") or "?"
        st.caption(f"Desk idle — last run {str(last)[:16].replace('T', ' ')}")
    else:
        st.caption("Desk idle — no runs recorded yet.")

    # The single allowed convenience: analysis-only run (never --trade).
    if st.button("🧠 Run analysis now (no trading)", disabled=run_active):
        _subprocess.Popen([r"tradingbot\Scripts\python.exe", "intern_desk.py"],
                          cwd=os.getcwd(),
                          creationflags=getattr(_subprocess, "CREATE_NO_WINDOW", 0))
        st.toast("Intern analysis started — progress appears above.", icon="🧠")
        a_time.sleep(1); st.rerun()

    st.divider()

    # --- 2. Scoreboard (intern account) ---
    st.subheader("📊 Scoreboard")
    try:
        _ib = Broker(account="intern")
        _iacct = _ib.get_account()
        _ieq = float(_iacct.equity)
        c1, c2, c3 = st.columns(3)
        c1.metric("Equity", f"${_ieq:,.2f}", delta=f"{_ieq - 2000.0:+,.2f} vs start")
        c2.metric("Realized PnL (cum.)",
                  f"${journal.desk_realized_pnl('INTERN'):+,.2f}")
        _ipos = _ib.get_positions()
        c3.metric("Open positions", len(_ipos))
        if _ipos:
            st.dataframe(pd.DataFrame([{
                "Ticker": p.symbol, "Qty": float(p.qty),
                "Entry": float(p.avg_entry_price),
                "Current": float(getattr(p, "current_price", 0) or 0),
                "Unreal. P/L $": float(getattr(p, "unrealized_pl", 0) or 0),
            } for p in _ipos]), use_container_width=True, hide_index=True)
    except Exception as e:
        st.caption(f"Intern account unavailable: {e}")

    # --- 3. Latest report ---
    st.subheader("📄 Latest report")
    _reports = sorted(_glob.glob(os.path.join("reports", "intern_*.md")))
    if _reports:
        try:
            with open(_reports[-1], "r", encoding="utf-8") as f:
                st.markdown(f.read())
        except OSError as e:
            st.caption(f"Could not read report: {e}")
    else:
        st.caption("No intern reports yet — first scheduled run is 15:35 ET.")

    # --- 4. Verdict history + report card ---
    st.subheader("🗂️ Verdict history & grades")
    try:
        _hist = journal.intern_history(limit=200)
        if _hist:
            card = intern_desk.build_report_card(_hist)
            g1, g2, g3, g4 = st.columns(4)
            g1.metric("Calls", card["total_calls"])
            g2.metric("Graded", f"{card['graded']} "
                      + (f"({card['grade_rate_pct']}%)" if card["grade_rate_pct"] is not None else ""))
            g3.metric("Good", card["good"])
            g4.metric("Good rate", f"{card['good_pct']}%" if card["good_pct"] is not None else "—")
            st.dataframe(pd.DataFrame(_hist)[
                ["date", "ticker", "stance", "conviction", "grade", "grade_note"]],
                use_container_width=True, hide_index=True)
        else:
            st.caption("No intern calls recorded yet.")
    except Exception as e:
        st.caption(f"History unavailable: {e}")

# --- FIX: Auto-refresh logic moved to the very end of the script ---
is_running = os.path.exists(LOCK_FILE)
intern_run_active = False
try:
    import intern_desk as _idesk
    _st = _idesk.read_status()
    intern_run_active = bool(_st and _st.get("active"))
except Exception:
    pass
if is_running or intern_run_active:
    a_time.sleep(10 if intern_run_active else 15)
    st.rerun()

