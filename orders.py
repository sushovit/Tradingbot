"""
orders.py — CEO order-sheet ingestion.

    python orders.py ingest order_sheet.json [--dry-run]

Validates a JSON order sheet, enforces the risk rules from risk.py /
bot_config.json, executes valid orders through the Alpaca PAPER broker as
bracket orders, and journals everything (source="ceo").

Order sheet schema:
{
  "session": "2026-07-03",
  "regime": "risk-on",
  "orders": [
    {"action": "BUY|SELL|HOLD|TIGHTEN_STOP|TAKE_PARTIAL|CLOSE",
     "ticker": "NVDA", "notional_usd": 250, "entry": 172.5, "stop": 168.0,
     "target": 181.0, "setup": "momentum_continuation", "reason": "...",
     "valid_until": "2026-07-03T15:30:00",
     "hard_exit_date": "2026-07-10"}   // REQUIRED when setup == "event_flow"
  ],
  "watchlist": ["AMD", "TSLA"],
  "no_new_trades_if": {"daily_loss_pct_exceeds": 3.0}
}

Event/flow setups (index inclusions, scheduled catalysts) cannot be
auto-detected — they enter ONLY through this script, and the bot force-closes
them at hard_exit_date's close regardless of PnL.
"""

import sys
import json
import math
import argparse
import logging
from datetime import datetime

import pytz
from dotenv import load_dotenv

import risk
import journal

load_dotenv()
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

EASTERN_TZ = pytz.timezone("US/Eastern")
CONFIG_FILE = "bot_config.json"
POSITIONS_STATE_FILE = "positions.json"

VALID_ACTIONS = {"BUY", "SELL", "HOLD", "TIGHTEN_STOP", "TAKE_PARTIAL", "CLOSE"}
VALID_SETUPS = {"trend_continuation", "momentum_continuation",
                "mean_reversion_reclaim", "event_flow", "discretionary"}


# =============================================================================
# VALIDATION (pure functions — unit tested with a mocked broker)
# =============================================================================

def validate_sheet(sheet: dict) -> list:
    """Top-level schema check. Returns a list of error strings (empty = ok)."""
    errors = []
    if not isinstance(sheet, dict):
        return ["Order sheet must be a JSON object."]
    if "orders" not in sheet or not isinstance(sheet["orders"], list):
        errors.append("Missing required 'orders' array.")
    for key in ("session", "regime"):
        if key not in sheet:
            errors.append(f"Missing required field '{key}'.")
    if "watchlist" in sheet and not isinstance(sheet["watchlist"], list):
        errors.append("'watchlist' must be an array.")
    if "no_new_trades_if" in sheet and not isinstance(sheet["no_new_trades_if"], dict):
        errors.append("'no_new_trades_if' must be an object.")
    return errors


def validate_order(order: dict, equity: float, open_positions: int,
                   max_positions: int, now_et: datetime = None,
                   open_notional_usd: float = 0.0):
    """Validate one order against the risk rules. Returns (ok, reason).

    `equity` must be the EFFECTIVE equity (risk.effective_equity — capped by
    capital_cap_usd, never raw broker equity).

    Rules enforced for BUY:
      - stop is mandatory (any order missing a stop is rejected)
      - target/stop reward:risk must be >= 1.5
      - notional must not exceed 30% of equity
      - open + new notional must not exceed equity (cash only, no margin)
      - notional must buy at least 1 whole share (Alpaca bracket requirement)
      - must not exceed max_positions
      - setup 'event_flow' REQUIRES hard_exit_date
      - expired valid_until is rejected
    """
    now_et = now_et or datetime.now(EASTERN_TZ)

    action = str(order.get("action", "")).upper()
    if action not in VALID_ACTIONS:
        return False, f"invalid_action:{action or 'missing'}"

    ticker = order.get("ticker")
    if not ticker or not isinstance(ticker, str):
        return False, "missing_ticker"

    setup = order.get("setup", "discretionary")
    if setup not in VALID_SETUPS:
        return False, f"invalid_setup:{setup}"

    # Event/flow: scheduled catalysts MUST carry a hard exit date.
    if setup == "event_flow":
        hard_exit = order.get("hard_exit_date")
        if not hard_exit:
            return False, "event_flow_missing_hard_exit_date"
        try:
            datetime.strptime(hard_exit, "%Y-%m-%d")
        except (ValueError, TypeError):
            return False, "invalid_hard_exit_date"

    valid_until = order.get("valid_until")
    if valid_until:
        try:
            expiry = datetime.fromisoformat(valid_until)
            if expiry.tzinfo is None:
                expiry = EASTERN_TZ.localize(expiry)
            if now_et > expiry:
                return False, "order_expired"
        except ValueError:
            return False, "invalid_valid_until"

    if action in ("HOLD",):
        return True, None

    if action in ("SELL", "CLOSE", "TAKE_PARTIAL"):
        return True, None  # exits are always allowed

    if action == "TIGHTEN_STOP":
        if order.get("stop") is None:
            return False, "missing_stop"
        return True, None

    # ---- BUY ----
    stop = order.get("stop")
    if stop is None:
        return False, "missing_stop"
    entry = order.get("entry")
    target = order.get("target")
    notional = order.get("notional_usd")
    if entry is None or entry <= 0:
        return False, "missing_entry"
    if notional is None or notional <= 0:
        return False, "missing_notional_usd"

    ok, reason = risk.check_signal(
        entry=float(entry), stop=float(stop),
        target=float(target) if target is not None else None,
        equity=equity, notional_usd=float(notional),
        open_positions=open_positions, max_positions=max_positions,
        open_notional_usd=open_notional_usd)
    if not ok:
        return ok, reason

    # Whole-share reality: Alpaca brackets need qty >= 1.
    if math.floor(float(notional) / float(entry)) < 1:
        return False, "price_too_high_for_account"
    return True, None


def check_no_new_trades(sheet: dict, equity: float) -> str:
    """Evaluate no_new_trades_if conditions. Returns a reason string or ''."""
    conditions = sheet.get("no_new_trades_if") or {}
    limit_pct = conditions.get("daily_loss_pct_exceeds")
    if limit_pct is not None:
        try:
            daily_pnl = journal.daily_realized_pnl()
            if equity > 0 and daily_pnl <= -abs(equity * float(limit_pct) / 100.0):
                return f"daily_loss_pct_exceeds {limit_pct}% (PnL ${daily_pnl:.2f})"
        except Exception as e:
            logger.warning(f"Could not evaluate daily loss condition: {e}")
    for key in conditions:
        if key not in ("daily_loss_pct_exceeds",):
            logger.warning(f"Unsupported no_new_trades_if condition ignored: {key}")
    return ""


# =============================================================================
# EXECUTION
# =============================================================================

def _load_positions() -> dict:
    try:
        with open(POSITIONS_STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_positions(positions: dict):
    with open(POSITIONS_STATE_FILE, "w") as f:
        json.dump(positions, f, indent=4)


def execute_order(order: dict, broker, decision_id: int, positions: dict) -> str:
    """Execute one validated order. Returns a human-readable result line."""
    action = order["action"].upper()
    ticker = order["ticker"].upper()

    if action == "HOLD":
        return f"HOLD {ticker}: acknowledged."

    if action in ("SELL", "CLOSE"):
        broker.close_position(ticker)
        price = broker.get_latest_price(ticker)
        state = positions.get(ticker, {})
        entry = state.get("entry_price", price)
        qty = state.get("shares_held", 0)
        pnl_usd = (price - entry) * qty
        pnl_pct = ((price / entry) - 1) * 100 if entry else 0.0
        trade_id = journal.log_trade(ticker, "SELL", qty, price, pnl_usd, pnl_pct,
                                     reason=f"CEO {action}", decision_id=decision_id)
        journal.link_outcome(state.get("decision_id"), trade_id, pnl_usd, pnl_pct)
        positions[ticker] = {"in_position": False}
        return f"{action} {ticker}: closed @ ~${price:.2f}."

    if action == "TIGHTEN_STOP":
        state = positions.get(ticker, {})
        stop_order_id = state.get("stop_order_id")
        new_stop = float(order["stop"])
        if not stop_order_id:
            return f"TIGHTEN_STOP {ticker}: SKIPPED — no tracked stop order id."
        new_order = broker.replace_stop(stop_order_id, new_stop)
        state["stop_order_id"] = str(new_order.id)
        state["trailing_stop_price"] = new_stop
        positions[ticker] = state
        journal.log_trade(ticker, "TIGHTEN_STOP", state.get("shares_held", 0),
                          new_stop, reason="CEO TIGHTEN_STOP", decision_id=decision_id)
        return f"TIGHTEN_STOP {ticker}: stop moved to ${new_stop:.2f}."

    if action == "TAKE_PARTIAL":
        state = positions.get(ticker, {})
        qty_held = state.get("shares_held", 0)
        qty_to_sell = max(1, math.floor(qty_held / 2)) if qty_held else 0
        if qty_to_sell <= 0:
            return f"TAKE_PARTIAL {ticker}: SKIPPED — no tracked position."
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        req = MarketOrderRequest(symbol=ticker, qty=qty_to_sell,
                                 side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
        broker.trading.submit_order(req)
        price = broker.get_latest_price(ticker)
        entry = state.get("entry_price", price)
        pnl_usd = (price - entry) * qty_to_sell
        pnl_pct = ((price / entry) - 1) * 100 if entry else 0.0
        journal.log_trade(ticker, "SELL", qty_to_sell, price, pnl_usd, pnl_pct,
                          reason="CEO TAKE_PARTIAL", decision_id=decision_id)
        state["shares_held"] = qty_held - qty_to_sell
        positions[ticker] = state
        return f"TAKE_PARTIAL {ticker}: sold {qty_to_sell} of {qty_held}."

    # ---- BUY (bracket order) ----
    entry = float(order["entry"])
    stop = float(order["stop"])
    target = float(order["target"])
    qty = math.floor(float(order["notional_usd"]) / entry)
    if qty <= 0:
        return f"BUY {ticker}: SKIPPED — notional too small for one share."
    bracket = broker.submit_bracket(ticker, qty, stop, target)

    stop_order_id, target_order_id = None, None
    for leg in (getattr(bracket, "legs", None) or []):
        ltype = str(getattr(leg, "order_type", None) or getattr(leg, "type", "")).lower()
        if "stop" in ltype:
            stop_order_id = str(leg.id)
        elif "limit" in ltype:
            target_order_id = str(leg.id)

    trade_id = journal.log_trade(ticker, "BUY", qty, entry,
                                 reason=f"CEO {order.get('setup', 'discretionary')}",
                                 decision_id=decision_id)
    positions[ticker] = {
        "in_position": True,
        "entry_price": entry,
        "shares_held": qty,
        "trailing_stop_price": stop,
        "profit_target_price": target,
        "stop_order_id": stop_order_id,
        "target_order_id": target_order_id,
        "entry_order_id": str(bracket.id),
        "decision_id": decision_id,
        "entry_trade_id": trade_id,
        "setup": order.get("setup", "discretionary"),
        "hard_exit_date": order.get("hard_exit_date"),
    }
    return (f"BUY {ticker}: bracket submitted — {qty} sh @ ~${entry:.2f}, "
            f"stop ${stop:.2f}, target ${target:.2f}.")


def ingest(sheet_path: str, dry_run: bool = False, equity_override: float = None) -> int:
    with open(sheet_path, "r") as f:
        sheet = json.load(f)

    errors = validate_sheet(sheet)
    if errors:
        for e in errors:
            print(f"REJECTED SHEET: {e}")
        return 1

    journal.init_db()
    try:
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        config = {}
    max_positions = config.get("max_positions", 3)

    broker = None
    if dry_run and equity_override is not None:
        equity = float(equity_override)   # offline validation, no broker needed
    else:
        from broker import Broker  # imported here so validation tests need no alpaca creds
        try:
            broker = Broker()
            # Hard capital cap: never size off raw broker equity.
            equity = risk.effective_equity(broker.get_equity(), config)
        except Exception as e:
            print(f"CANNOT REACH BROKER (paper account): {e}")
            print("No orders were executed. Check ALPACA_API_KEY / ALPACA_SECRET_KEY in .env.")
            return 1
    positions = _load_positions()
    open_positions = sum(1 for s in positions.values() if s.get("in_position"))
    open_notional = sum(s.get("shares_held", 0) * s.get("entry_price", 0)
                        for s in positions.values() if s.get("in_position"))

    block_reason = check_no_new_trades(sheet, equity)
    if block_reason:
        print(f"NO NEW TRADES: {block_reason} — BUY orders will be rejected.")

    exit_code = 0
    for order in sheet.get("orders", []):
        ticker = str(order.get("ticker", "?")).upper()
        action = str(order.get("action", "?")).upper()
        ok, reason = validate_order(order, equity, open_positions, max_positions,
                                    open_notional_usd=open_notional)
        if ok and action == "BUY" and block_reason:
            ok, reason = False, f"no_new_trades_if:{block_reason}"

        context = {"order": order, "session": sheet.get("session"),
                   "regime": sheet.get("regime"), "equity": equity}
        verdict = {"approved": bool(ok),
                   "rejection_reason": reason,
                   "source": "ceo",
                   "conviction_score": 100 if ok else 0}
        decision_id = journal.log_decision(ticker, order.get("setup", "discretionary"),
                                           context, verdict, source="ceo")

        if not ok:
            print(f"REJECTED {action} {ticker}: {reason}")
            exit_code = 1
            continue
        if dry_run:
            print(f"VALID (dry-run) {action} {ticker}")
            continue
        try:
            result = execute_order(order, broker, decision_id, positions)
            print(result)
            if action == "BUY":
                open_positions += 1
                open_notional += float(order.get("notional_usd", 0))
        except Exception as e:
            print(f"EXECUTION FAILED {action} {ticker}: {e}")
            exit_code = 1

    if not dry_run:
        _save_positions(positions)
    if sheet.get("watchlist"):
        print(f"Watchlist noted: {', '.join(sheet['watchlist'])}")
    return exit_code


def main():
    parser = argparse.ArgumentParser(description="CEO order-sheet ingestion (PAPER ONLY)")
    sub = parser.add_subparsers(dest="command", required=True)
    p_ingest = sub.add_parser("ingest", help="Validate and execute an order sheet")
    p_ingest.add_argument("sheet", help="Path to order_sheet.json")
    p_ingest.add_argument("--dry-run", action="store_true",
                          help="Validate only; do not submit orders")
    p_ingest.add_argument("--equity", type=float, default=None,
                          help="With --dry-run: validate offline against this "
                               "equity instead of contacting the broker")
    args = parser.parse_args()
    if args.command == "ingest":
        sys.exit(ingest(args.sheet, dry_run=args.dry_run,
                        equity_override=args.equity))


if __name__ == "__main__":
    main()
