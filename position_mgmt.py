"""
position_mgmt.py — ownership-aware open-position management.

Every position carries a source:
  "bot"     — opened by the live loop. The bot manages it fully (trailing
              stop ratchets, leg replacement).
  "ceo"     — opened by an orders.py CEO sheet. DISPLAY-ONLY for the bot:
              it never replaces or cancels these legs. CEO stops move only
              via TIGHTEN_STOP sheets. (Incident 2026-07-15: the bot's 5-min
              ATR trail replaced a CEO swing trade's designed stop.)
  "unknown" — adopted at reconciliation from the broker with no local
              history. Display-only, same as CEO.

The bot still OBSERVES all positions (exit-fill detection, hard_exit_date
enforcement for event_flow, manual override commands) — observation is not
management.
"""

import math
import logging

import pandas_ta as ta

from broker import BrokerError

logger = logging.getLogger(__name__)


def duplicate_entry_exists(broker, ticker: str):
    """DEFENSE IN DEPTH (2026-08-13 duplicate-worker incident): before ANY
    entry, ask the ACCOUNT whether this ticker already has a position or a
    working order. Two workers, a retry, or a stale cache can all produce a
    second bracket; the broker is the only source that sees them all.

    Returns a reason string if an entry must be refused, else None."""
    try:
        for p in broker.get_positions():
            if p.symbol == ticker and abs(float(p.qty)) > 0:
                return f"position already open ({p.qty} sh)"
    except BrokerError as e:
        # Cannot verify -> refuse. A missed trade beats a double fill.
        return f"position check failed: {e}"
    try:
        working = [o for o in broker.get_live_orders(ticker)
                   if o.symbol == ticker]
        if working:
            kinds = ", ".join(sorted({
                str(getattr(o, "order_type", None)
                    or getattr(o, "type", "")).split(".")[-1].lower()
                for o in working}))
            return f"{len(working)} working order(s) already at the broker ({kinds})"
    except BrokerError as e:
        return f"open-order check failed: {e}"
    return None


def is_bot_managed(state: dict) -> bool:
    """Trailing/leg management applies ONLY to positions the bot opened.
    Missing source defaults to 'unknown' -> not managed (safe direction)."""
    return (state or {}).get("source") == "bot"


PROFIT_LOCK = "profit_lock"
PCT_LOCK = "pct_lock"
LADDER = "ladder"
R_ATR = "r_atr"
DAILY_LOCKS = (PCT_LOCK, LADDER, R_ATR)


def trailing_type_for(state: dict, risk_profile: dict) -> str:
    """Which trailing rule applies to THIS position.

    A daily position reads `daily_trailing` from its profile; an intraday
    one reads `trailing_stop_type` as before. `daily_trailing` falls back to
    `trailing_stop_type`, so a profile that does not mention it keeps the
    behaviour it already had — the key can only ever be an explicit choice.
    """
    profile = risk_profile or {}
    if (state or {}).get("timeframe") == "daily":
        return str(profile.get("daily_trailing",
                               profile.get("trailing_stop_type", "ATR"))).lower()
    return str(profile.get("trailing_stop_type", "ATR")).lower()


def compute_trailing_stop(df, risk_profile: dict, current_price: float,
                          trailing_type: str = None):
    """New trailing-stop candidate from the profile's ATR/percent rule,
    or None if it can't be computed. Type "none" means the position does
    not trail at all: there is no candidate, ever."""
    trailing_stop_type = (trailing_type if trailing_type is not None
                          else risk_profile.get('trailing_stop_type', 'ATR'))
    trailing_stop_value = risk_profile.get('trailing_stop_value', 2.0)
    trailing_stop_type = str(trailing_stop_type).lower()
    if trailing_stop_type == 'none':
        return None
    # Case-insensitive: the profile spells it 'ATR', trailing_type_for hands
    # back a normalised 'atr'. An exact-match test here would have read the
    # normalised form as "not ATR" and silently trailed every intraday
    # position on the percent rule instead.
    if trailing_stop_type == 'atr':
        atr_series = ta.atr(df['high'], df['low'], df['close'], length=14)
        if atr_series is None or atr_series.dropna().empty:
            return None
        atr_value = float(atr_series.dropna().iloc[-1])
        if math.isnan(atr_value):
            return None
        return current_price - (atr_value * trailing_stop_value)
    return current_price * (1 - (trailing_stop_value / 100.0))


def r_multiple(state: dict, current_price: float):
    """Open profit in R, measured against the INITIAL structural stop.
    None when the entry/stop geometry is unknown."""
    entry = state.get("entry_price")
    initial = state.get("initial_stop", state.get("trailing_stop_price"))
    try:
        entry, initial = float(entry), float(initial)
    except (TypeError, ValueError):
        return None
    risk = entry - initial
    if risk <= 0:
        return None
    return (current_price - entry) / risk


def reached_one_r(state: dict, current_price: float = None) -> bool:
    """Has this position EVER traded at or above +1R?

    Persisted as `reached_1r`, because r_multiple() reads the CURRENT price:
    without a sticky flag a trade that ran to +1.5R and eased back to +0.9R
    would forget it ever got there, and the breakeven floor would switch off
    at exactly the moment it matters.

    Backfill for positions opened before the flag existed: the ratchet only
    ever ran at r >= 1.0, so a trailing stop that has moved above the initial
    structural stop is itself proof the trade reached +1R."""
    if state.get("reached_1r"):
        return True
    if current_price is not None:
        r = r_multiple(state, current_price)
        if r is not None and r >= 1.0:
            return True
    try:
        initial = float(state.get("initial_stop"))
        trail = float(state.get("trailing_stop_price"))
    except (TypeError, ValueError):
        return False
    return trail > initial


def ratchet_floor(state: dict, candidate: float, current_price: float):
    """The ratified floor (2026-09-03): once a position reaches +1R its stop
    never sits below breakeven again, so `stop = max(ATR trail, entry)`.

    Returns (new_stop, floored: bool). The floor is NOT applied above the
    market — a stop at or above current price would be rejected by the broker
    (or fill instantly), so when price is back under entry the existing stop
    stands and the trade exits on it."""
    if candidate is None:
        return None, False
    if not reached_one_r(state, current_price):
        return candidate, False
    try:
        entry = float(state.get("entry_price"))
    except (TypeError, ValueError):
        return candidate, False
    if entry <= candidate:
        return candidate, False
    if entry >= current_price:
        return candidate, False          # cannot place a stop above market
    return entry, True


def maybe_ratchet_stop(broker, positions: dict, ticker: str, state: dict,
                       df, risk_profile: dict, current_price: float,
                       persist=None) -> bool:
    """Ratchet a BOT position's broker-side stop leg up. Returns True if the
    stop was replaced. CEO/unknown positions are never touched — that is the
    ownership boundary, enforced here and nowhere else.

    THE INITIAL STOP STANDS UNTIL +1R. The structural stop comes from the
    playbook (reclaim/breakout bar low); trailing it with INTRADAY ATR from
    the moment of entry collapses a 4.9% daily-structure stop to noise
    level — NOK 2026-08-13 entered at 10.76 with a 10.21 stop and was
    trailed out at 10.74 (0.2%) within 42 minutes. ATR trailing is only
    allowed once the trade has paid for its own risk."""
    if not is_bot_managed(state):
        return False

    # Trailing type "none": the structural stop and the target stand for the
    # life of the trade. Daily positions resolve this from the profile's
    # `daily_trailing`. The study behind it: on daily structure the ATR trail
    # (with or without the breakeven floor) gave back more than it saved —
    # a daily stop is a THESIS level, and moving it on price alone converts a
    # 4R plan into a 1R scalp. Nothing downstream reads `reached_1r` except
    # ratchet_floor, which this branch skips, so returning here costs no
    # bookkeeping.
    #
    # "profit_lock" (S16) is handled by maybe_lock_profit, on 5-minute closes;
    # this ATR ratchet must never act on it. Without this guard
    # compute_trailing_stop would read the unknown type as the PERCENT rule.
    if trailing_type_for(state, risk_profile) in ("none", PROFIT_LOCK,
                                                   PCT_LOCK, LADDER, R_ATR):
        return False

    r = r_multiple(state, current_price)
    at_one_r = reached_one_r(state, current_price)
    if not at_one_r and r is not None and r < 1.0:
        return False        # structural stop stands; not yet +1R
    if at_one_r and not state.get("reached_1r"):
        # Sticky: the trade has paid for its own risk, and that fact must
        # survive a pullback below +1R.
        positions.setdefault(ticker, state)["reached_1r"] = True
        state["reached_1r"] = True

    new_stop = compute_trailing_stop(
        df, risk_profile, current_price,
        trailing_type=trailing_type_for(state, risk_profile))
    new_stop, floored = ratchet_floor(state, new_stop, current_price)
    if new_stop is None:
        return False
    if not (new_stop > state.get("trailing_stop_price", 0)
            and new_stop < current_price and state.get("stop_order_id")):
        return False
    try:
        new_order = broker.replace_stop(state["stop_order_id"], new_stop)
        positions[ticker]["stop_order_id"] = str(new_order.id)
        positions[ticker]["trailing_stop_price"] = new_stop
        # W8: persist IMMEDIATELY. The only write used to be at the end of
        # the cycle, so a BrokerError raised later in the loop discarded the
        # new leg id and left state pointing at a superseded order
        # (2026-09-08). The broker has already moved; the file must agree.
        if persist is not None:
            try:
                persist(positions)
            except Exception as e:
                logger.error(f"{ticker}: could not persist stop id: {e}")
        logger.info(f"{ticker}: trailing stop raised to ${new_stop:.2f}"
                    + (" (breakeven floor — at/after +1R)" if floored else ""))
        return True
    except BrokerError as e:
        logger.warning(f"{ticker}: could not replace stop: {e}")
        return False


# --- Profit lock for daily positions (S16, CEO 2026-10-09) -----------------
# Reverses 10.6 (static stops for daily setups). Live evidence: 0 of 16
# exits at target; every profitable exit came from a raised stop; since
# 10.6 only full-stop losses while SPCX sat at +0.9R with its stop below
# entry. The lock raises a daily position's stop in fixed R steps, decided
# on the last COMPLETED 5-minute close: [[trigger_R, stop_R], ...] means
# "once a completed bar closes at entry + trigger_R*R, the stop goes to
# entry + stop_R*R". Steps only ever raise the stop.

def profit_lock_steps(risk_profile: dict) -> tuple:
    """`daily_profit_lock` as sorted ((trigger_R, stop_R), ...).

    Malformed entries are dropped, as is any step whose stop would sit at or
    above its own trigger (it could never be placed below the market)."""
    raw = (risk_profile or {}).get("daily_profit_lock") or []
    steps = []
    for item in raw if isinstance(raw, (list, tuple)) else []:
        try:
            trigger_r, stop_r = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isnan(trigger_r) or math.isnan(stop_r) or stop_r >= trigger_r:
            continue
        steps.append((trigger_r, stop_r))
    return tuple(sorted(steps))


def original_stop(state: dict):
    """The stop the position was opened with - the R denominator. Stored as
    `original_stop`; falls back to `initial_stop`, which never moves."""
    for key in ("original_stop", "initial_stop"):
        try:
            value = float(state.get(key))
        except (TypeError, ValueError):
            continue
        if not math.isnan(value):
            return value
    return None


def last_completed_close(df, now_utc, interval_minutes: int = 5):
    """Close of the newest bar that has FINISHED by now_utc, or None.

    Alpaca stamps a bar with its START time, so the newest row is usually
    still forming; deciding on it would lock on a price the bar may not
    keep."""
    if df is None or getattr(df, "empty", True) or "close" not in df:
        return None
    import pandas as pd
    now = pd.Timestamp(now_utc)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    span = pd.Timedelta(minutes=interval_minutes)
    for ts, close in zip(reversed(df.index), reversed(df["close"].tolist())):
        start = pd.Timestamp(ts)
        if start.tzinfo is None:
            start = start.tz_localize("UTC")
        if start + span <= now:
            try:
                value = float(close)
            except (TypeError, ValueError):
                return None
            return None if math.isnan(value) else value
    return None


def profit_lock_target(state: dict, close: float, steps):
    """(stop_price, step_number) for the highest step `close` has reached,
    or (None, 0). Step numbers count from 1."""
    try:
        entry = float(state.get("entry_price"))
    except (TypeError, ValueError):
        return None, 0
    base = original_stop(state)
    if base is None or close is None or entry <= base:
        return None, 0
    risk = entry - base
    reached = [(n, entry + stop_r * risk)
               for n, (trigger_r, stop_r) in enumerate(steps, start=1)
               if close >= entry + trigger_r * risk]
    if not reached:
        return None, 0
    step, price = reached[-1]
    # Round UP to the cent: a lock must never sit below its step level.
    # round() would put a breakeven stop on a 202.815 entry at 202.81, half
    # a cent under entry (float 202.815 is 202.81499...).
    return math.ceil(price * 100 - 1e-6) / 100, step


def maybe_lock_profit(broker, positions: dict, ticker: str, state: dict,
                      df_5min, risk_profile: dict, current_price: float,
                      now_utc, interval_minutes: int = 5,
                      persist=None, journal_raise=None) -> bool:
    """Raise a BOT daily position's stop to its profit-lock step. Returns
    True if the broker stop was replaced.

    Never lowers a stop, never places one at or above the market, and is
    idempotent across restarts: the raised level is persisted, so the same
    step computes the same price and is a no-op the second time."""
    if not is_bot_managed(state):
        return False
    if trailing_type_for(state, risk_profile) != PROFIT_LOCK:
        return False
    if state.get("original_stop") is None and original_stop(state) is not None:
        positions.setdefault(ticker, state)["original_stop"] = original_stop(state)
        state["original_stop"] = original_stop(state)
        if persist is not None:
            try:
                persist(positions)
            except Exception as e:
                logger.error(f"{ticker}: could not persist original_stop: {e}")
    steps = profit_lock_steps(risk_profile)
    if not steps:
        return False
    close = last_completed_close(df_5min, now_utc, interval_minutes)
    new_stop, step = profit_lock_target(state, close, steps)
    if new_stop is None:
        return False
    try:
        old_stop = float(state.get("trailing_stop_price") or 0)
    except (TypeError, ValueError):
        old_stop = 0.0
    if new_stop <= old_stop:
        return False                      # never lower; already at this step
    if current_price is None or new_stop >= current_price:
        return False                      # cannot place a stop above market
    if not state.get("stop_order_id"):
        return False
    try:
        new_order = broker.replace_stop(state["stop_order_id"], new_stop)
    except BrokerError as e:
        logger.warning(f"{ticker}: profit lock could not replace stop: {e}")
        return False
    positions[ticker]["stop_order_id"] = str(new_order.id)
    positions[ticker]["trailing_stop_price"] = new_stop
    positions[ticker]["profit_lock_step"] = step
    trigger_r, stop_r = steps[step - 1]
    detail = (f"profit_lock step {step} (close {close:.2f} >= +{trigger_r:g}R"
              f" -> stop +{stop_r:g}R): stop {old_stop:g} -> {new_stop:g}")
    logger.info(f"{ticker}: {detail}")
    if journal_raise is not None:
        try:
            journal_raise(ticker, state.get("shares_held") or 0, new_stop,
                          detail)
        except Exception as e:
            logger.error(f"{ticker}: could not journal profit lock: {e}")
    # W8: the broker has already moved, so the file must agree NOW, not at
    # the end of the cycle.
    if persist is not None:
        try:
            persist(positions)
        except Exception as e:
            logger.error(f"{ticker}: could not persist profit-lock stop: {e}")
    return True


# --- Percent lock for daily positions (S24, CEO + owner 2026-10-14) ---------
# Replaces the S16 R-step lock. Decided on the last COMPLETED 5-minute close,
# in percent of the BROKER's average entry (never a journaled reference):
#   step1  close >= entry*(1+step1_trigger_pct)  -> stop = original_stop
#          + step1_risk_fraction*(entry - original_stop)     (halve the risk)
#   step2  close >= entry*(1+step2_trigger_pct)  -> stop = entry*(1+step2_stop_pct)
#   trail  highest close since entry >= entry*(1+trail_trigger_pct)
#          -> stop = highest_close*(1-trail_pct)
# The highest candidate wins; a stop never moves down and is kept at least
# market_gap_pct below the last price.

PCT_LOCK_DEFAULTS = {"step1_trigger_pct": 0.75, "step1_risk_fraction": 0.5,
                     "step2_trigger_pct": 1.25, "step2_stop_pct": 0.1,
                     "trail_trigger_pct": 2.0, "trail_pct": 1.0,
                     "market_gap_pct": 0.1}


def pct_lock_params(risk_profile: dict) -> dict:
    """`daily_pct_lock` over the defaults; unparseable values fall back."""
    out = dict(PCT_LOCK_DEFAULTS)
    for key, value in ((risk_profile or {}).get("daily_pct_lock") or {}).items():
        if key in out:
            try:
                out[key] = float(value)
            except (TypeError, ValueError):
                pass
    return out


def _cent_up(x: float) -> float:
    """Round UP to the cent: a lock never sits below its level."""
    return math.ceil(x * 100 - 1e-6) / 100


def pct_lock_target(entry, original_stop, close, highest_close, last_price,
                    params):
    """(stop, step) the rule asks for now, or (None, None). Pure arithmetic."""
    if entry is None or close is None:
        return None, None
    c = []
    if close >= entry * (1 + params["step1_trigger_pct"] / 100) \
            and original_stop is not None and original_stop < entry:
        c.append((original_stop + params["step1_risk_fraction"]
                  * (entry - original_stop), "step1"))
    if close >= entry * (1 + params["step2_trigger_pct"] / 100):
        c.append((entry * (1 + params["step2_stop_pct"] / 100), "step2"))
    hc = max(highest_close or close, close)
    if hc >= entry * (1 + params["trail_trigger_pct"] / 100):
        c.append((hc * (1 - params["trail_pct"] / 100), "trail"))
    if not c:
        return None, None
    price, step = max(c)
    price = _cent_up(price)
    if last_price is not None:
        ceiling = math.floor(last_price * (1 - params["market_gap_pct"] / 100)
                             * 100 + 1e-6) / 100
        if price > ceiling:
            price, step = ceiling, f"{step} (capped {params['market_gap_pct']:g}% below market)"
    return price, step


def completed_closes_since(df, since, now_utc, interval_minutes=5):
    """Closes of the 5-minute bars that STARTED at/after `since` and have
    finished by now_utc."""
    import pandas as pd
    if df is None or getattr(df, "empty", True):
        return []
    now = pd.Timestamp(now_utc)
    now = now.tz_localize("UTC") if now.tzinfo is None else now
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx
    done = idx + pd.Timedelta(minutes=interval_minutes) <= now
    if since is not None:
        s = pd.Timestamp(since)
        s = s.tz_localize("America/New_York") if s.tzinfo is None else s
        done &= idx >= s.floor(f"{interval_minutes}min")
    return [float(x) for x in df["close"][done] if not math.isnan(float(x))]


# --- Owner ladder for daily positions (S24b, owner 2026-10-14) --------------
# Replaces the S24 step1/step2/trail rule. Once a completed 5-minute close
# reaches entry x (1 + start_pct%), the stop goes to entry (breakeven); from
# then on stop = max(stop, highest close since entry x (1 - cushion_pct%)) -
# a continuous trail ~0.5% under the high (owner's choice over 0.25% steps).
# Same guards: never lowered, at least market_gap_pct below the last price,
# rounded up to the cent. Original stop and the 3R target are untouched.

LADDER_DEFAULTS = {"start_pct": 0.50, "cushion_pct": 0.50,
                   "market_gap_pct": 0.1}


def ladder_params(risk_profile: dict) -> dict:
    """`daily_lock` over the defaults; unparseable values fall back."""
    out = dict(LADDER_DEFAULTS)
    for key, value in ((risk_profile or {}).get("daily_lock") or {}).items():
        if key in out:
            try:
                out[key] = float(value)
            except (TypeError, ValueError):
                pass
    return out


def ladder_target(entry, close, highest_close, last_price, params):
    """(stop, step) the ladder asks for now, or (None, None). Pure."""
    if entry is None or close is None:
        return None, None
    hc = max(highest_close or close, close)
    if hc < entry * (1 + params["start_pct"] / 100):
        return None, None
    trail = hc * (1 - params["cushion_pct"] / 100)
    price, step = (trail, "trail") if trail > entry else (entry, "breakeven")
    price = _cent_up(price)
    if last_price is not None:
        ceiling = math.floor(last_price * (1 - params["market_gap_pct"] / 100)
                             * 100 + 1e-6) / 100
        if price > ceiling:
            price, step = ceiling, f"{step} (capped {params['market_gap_pct']:g}% below market)"
    return price, step


def maybe_pct_lock(broker, positions: dict, ticker: str, state: dict, df,
                   risk_profile: dict, last_price: float, now_utc,
                   broker_entry: float = None, entry_time=None,
                   interval_minutes: int = 5, persist=None,
                   journal_raise=None, alert=None, daily_df=None) -> bool:
    """Apply the S24 percent lock to one BOT daily position. Returns True if
    the broker stop was replaced. Never lowers a stop, keeps it below the
    market, and is a no-op on restart (the raised level is persisted)."""
    if not is_bot_managed(state):
        return False
    ttype = trailing_type_for(state, risk_profile)
    if ttype not in DAILY_LOCKS:
        return False
    rec = positions.setdefault(ticker, state)
    changed = False
    # Entry = the broker's average fill, never a journaled reference.
    if broker_entry is not None and state.get("entry_price") != broker_entry:
        logger.info(f"{ticker}: entry_price {state.get('entry_price')} -> "
                    f"{broker_entry} (broker average fill)")
        rec["entry_price"] = state["entry_price"] = broker_entry
        changed = True
    if state.get("original_stop") is None and original_stop(state) is not None:
        rec["original_stop"] = state["original_stop"] = original_stop(state)
        changed = True
    entry = state.get("entry_price")
    if ttype == R_ATR:
        # Only closes of bars starting at/after 10:00 ET count, and the
        # stop only ratchets after 10:00 ET.
        rp = r_atr_params(risk_profile)
        closes = qualifying_closes(df, entry_time, now_utc,
                                   rp["not_before_et"], interval_minutes)
    else:
        closes = completed_closes_since(df, entry_time, now_utc, interval_minutes)
    if closes:
        hc = max([state.get("highest_close") or 0.0] + closes)
        if hc != state.get("highest_close"):
            rec["highest_close"] = state["highest_close"] = hc
            changed = True
    # MFE for the time stop: the highest 5-minute HIGH since entry.
    highs = _completed_highs_since(df, entry_time, now_utc, interval_minutes)
    if highs:
        mh = max([state.get("max_high") or 0.0] + highs)
        if mh != state.get("max_high"):
            rec["max_high"] = state["max_high"] = mh
            changed = True
    close = last_completed_close(df, now_utc, interval_minutes)
    if ttype == R_ATR:
        import pandas as pd
        now_et = pd.Timestamp(now_utc).tz_convert("America/New_York")
        hh, mm = (int(x) for x in rp["not_before_et"].split(":"))
        atr = daily_atr(daily_df, int(rp["atr_length"]), now_et.date())
        hdc = _highest_daily_close_since(daily_df, entry_time, now_et.date())
        if (now_et.hour, now_et.minute) < (hh, mm):
            new_stop, step = None, None
        else:
            new_stop, step = r_atr_target(entry, original_stop(state),
                                          state.get("highest_close"), hdc, atr,
                                          last_price, rp)
    elif ttype == LADDER:
        new_stop, step = ladder_target(entry, close, state.get("highest_close"),
                                       last_price, ladder_params(risk_profile))
    else:
        new_stop, step = pct_lock_target(entry, original_stop(state), close,
                                         state.get("highest_close"), last_price,
                                         pct_lock_params(risk_profile))
    try:
        old_stop = float(state.get("trailing_stop_price") or 0)
    except (TypeError, ValueError):
        old_stop = 0.0
    if new_stop is None or new_stop <= old_stop or not state.get("stop_order_id"):
        if changed and persist is not None:
            persist(positions)
        return False
    try:
        new_order = broker.replace_stop(state["stop_order_id"], new_stop)
    except BrokerError as e:
        logger.warning(f"{ticker}: pct lock could not replace stop: {e}")
        if changed and persist is not None:
            persist(positions)
        return False
    rec["stop_order_id"] = str(new_order.id)
    rec["trailing_stop_price"] = new_stop
    rec["lock_step"] = step
    detail = (f"{ttype} {step} (close {close:g}, entry {entry:g}): "
              f"stop {old_stop:g} -> {new_stop:g}")
    logger.info(f"{ticker}: {detail}")
    if journal_raise is not None:
        try:
            journal_raise(ticker, state.get("shares_held") or 0, new_stop, detail)
        except Exception as e:
            logger.error(f"{ticker}: could not journal pct lock: {e}")
    if persist is not None:
        try:
            persist(positions)
        except Exception as e:
            logger.error(f"{ticker}: could not persist pct-lock stop: {e}")
    if alert is not None:
        try:
            alert(ticker, old_stop, new_stop, step, entry, last_price)
        except Exception as e:
            logger.warning(f"{ticker}: pct-lock alert skipped: {e}")
    return True


# --- R/ATR ladder for daily positions (S25, CEO 2026-10-14) ----------------
# After the stop-loss research. R = entry - original stop.
#   +1.0R -> stop = entry - 0.25R;  +1.5R -> entry;  +2R -> entry + 1R;
#   beyond +2R: stop = max(stop, highest completed DAILY close - 2.5 x ATR14).
# Steps are decided on completed 5-minute closes of bars starting at/after
# 10:00 ET (the opening range is ignored) and only ratchet after 10:00 ET.
# Never lowered; at least market_gap_pct below the last price; rounded up to
# the cent. New entries get an ATR-aware initial stop (initial_stop below);
# existing positions keep theirs.

R_ATR_DEFAULTS = {"steps": [[1.0, -0.25], [1.5, 0.0], [2.0, 1.0]],
                  "trail_atr": 2.5, "atr_length": 14, "not_before_et": "10:00",
                  "market_gap_pct": 0.1, "initial_stop_atr_buffer": 0.25,
                  "initial_stop_min_atr": 1.5, "initial_stop_max_atr": 2.5}


def r_atr_params(risk_profile: dict) -> dict:
    out = dict(R_ATR_DEFAULTS)
    for key, value in ((risk_profile or {}).get("daily_r_atr") or {}).items():
        if key not in out:
            continue
        if key == "steps":
            try:
                out[key] = sorted([float(a), float(b)] for a, b in value)
            except (TypeError, ValueError):
                pass
        elif key == "not_before_et":
            out[key] = str(value)
        else:
            try:
                out[key] = float(value)
            except (TypeError, ValueError):
                pass
    return out


def daily_atr(daily_df, length: int = 14, today=None):
    """ATR(length) as of the last COMPLETED daily bar (today's partial bar,
    if present, is excluded). None if it cannot be computed."""
    if daily_df is None or getattr(daily_df, "empty", True):
        return None
    df = daily_df
    if today is not None:
        days = [str(i)[:10] for i in df.index]
        df = df[[d < str(today) for d in days]]
    if len(df) < length + 1:
        return None
    series = ta.atr(df["high"], df["low"], df["close"], length=length)
    if series is None or series.dropna().empty:
        return None
    value = float(series.dropna().iloc[-1])
    return None if math.isnan(value) else value


def initial_stop(entry, structural_stop, atr, params=None):
    """The ATR-aware initial stop for a NEW daily entry: the structural low
    minus buffer x ATR, with its distance from entry clamped to
    [min_atr, max_atr] x ATR. Returns the structural stop unchanged when ATR
    is unknown."""
    p = params or R_ATR_DEFAULTS
    if atr is None or atr <= 0 or entry is None or structural_stop is None:
        return structural_stop
    dist = entry - (structural_stop - p["initial_stop_atr_buffer"] * atr)
    dist = min(max(dist, p["initial_stop_min_atr"] * atr),
               p["initial_stop_max_atr"] * atr)
    return math.floor((entry - dist) * 100 + 1e-6) / 100


def r_atr_target(entry, orig_stop, highest_close, highest_daily_close, atr,
                 last_price, params):
    """(stop, step) the R/ATR ladder asks for now, or (None, None). Pure.
    `highest_close` is the highest qualifying 5-minute close since entry."""
    if entry is None or orig_stop is None or orig_stop >= entry \
            or highest_close is None:
        return None, None
    risk = entry - orig_stop
    reached_r = (highest_close - entry) / risk
    c = []
    for trigger_r, stop_r in params["steps"]:
        if reached_r >= trigger_r:
            c.append((entry + stop_r * risk, f"+{trigger_r:g}R"))
    last_trigger = params["steps"][-1][0] if params["steps"] else None
    if last_trigger is not None and reached_r >= last_trigger \
            and highest_daily_close and atr:
        c.append((highest_daily_close - params["trail_atr"] * atr,
                  f"trail {params['trail_atr']:g}xATR"))
    if not c:
        return None, None
    price, step = max(c)
    price = _cent_up(price)
    if last_price is not None:
        ceiling = math.floor(last_price * (1 - params["market_gap_pct"] / 100)
                             * 100 + 1e-6) / 100
        if price > ceiling:
            price, step = ceiling, f"{step} (capped {params['market_gap_pct']:g}% below market)"
    return price, step


def qualifying_closes(df, since, now_utc, not_before_et="10:00",
                      interval_minutes=5):
    """completed_closes_since, keeping only bars that START at/after
    `not_before_et` (ET) on their own day - the opening range is ignored."""
    import pandas as pd
    if df is None or getattr(df, "empty", True):
        return []
    hh, mm = (int(x) for x in not_before_et.split(":"))
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx
    et = idx.tz_convert("America/New_York")
    keep = [(t.hour, t.minute) >= (hh, mm) for t in et]
    sub = df[keep]
    return completed_closes_since(sub, since, now_utc, interval_minutes)


# --- Time stop and earnings exit for daily positions (S25) ------------------

DAILY_EXIT_DEFAULTS = {"time_stop_day": 5, "time_stop_min_mfe_r": 0.5,
                       "max_hold_days": 10, "exit_at_et": "15:55",
                       "earnings_exit": True, "earnings_exit_at_et": "15:50"}


def daily_exit_params(risk_profile: dict) -> dict:
    out = dict(DAILY_EXIT_DEFAULTS)
    for key, value in ((risk_profile or {}).get("daily_exits") or {}).items():
        if key in out:
            if key in ("exit_at_et", "earnings_exit_at_et"):
                out[key] = str(value)
            elif key == "earnings_exit":
                out[key] = bool(value)
            else:
                try:
                    out[key] = float(value)
                except (TypeError, ValueError):
                    pass
    return out


def session_day(entry_date, today) -> int:
    """Trading day number of `today` with the entry session as day 1
    (weekends excluded; holidays are not, which can only make the count
    run a day early - the conservative direction for a time stop)."""
    import pandas as pd
    if entry_date is None:
        return None
    return int(len(pd.bdate_range(entry_date, today)))


def daily_exit_due(day, mfe_r, now_et, earnings_soon, params):
    """Reason to close a daily position now, or None.
      - day >= max_hold_days, at/after exit_at_et          -> time stop
      - day == time_stop_day and MFE < min_mfe_r, same time -> time stop
      - earnings_soon and at/after earnings_exit_at_et      -> earnings exit
    `earnings_soon` None (calendar unknown) never forces an exit."""
    def at_or_after(hhmm):
        hh, mm = (int(x) for x in hhmm.split(":"))
        return (now_et.hour, now_et.minute) >= (hh, mm)
    if params["earnings_exit"] and earnings_soon and \
            at_or_after(params["earnings_exit_at_et"]):
        return "Earnings exit (reports within 1 session)"
    if day is not None and at_or_after(params["exit_at_et"]):
        if day >= params["max_hold_days"]:
            return f"Time stop (day {day} close)"
        if day >= params["time_stop_day"] and (mfe_r is None
                                               or mfe_r < params["time_stop_min_mfe_r"]):
            return (f"Time stop (day {day}, MFE "
                    f"{'n/a' if mfe_r is None else f'{mfe_r:+.2f}R'} "
                    f"< +{params['time_stop_min_mfe_r']:g}R)")
    return None


def _completed_highs_since(df, since, now_utc, interval_minutes=5):
    if df is None or getattr(df, "empty", True) or "high" not in df:
        return []
    sub = df.drop(columns=["close"]).rename(columns={"high": "close"})
    return completed_closes_since(sub, since, now_utc, interval_minutes)


def _highest_daily_close_since(daily_df, since, today):
    """Highest COMPLETED daily close from the entry day onward (today's
    partial bar excluded), or None."""
    if daily_df is None or getattr(daily_df, "empty", True):
        return None
    start = str(since)[:10] if since else None
    closes = [float(c) for i, c in zip(daily_df.index, daily_df["close"])
              if str(i)[:10] < str(today)
              and (start is None or str(i)[:10] >= start)]
    return max(closes) if closes else None



def stepped_ladder_target(entry, close, highest_close, params):
    """SHADOW owner ladder (S25 - computed, never ordered): once the highest
    close reaches entry x (1 + start_pct%), stop = entry; then
    stop = max(entry, floor_to_step(highest close x (1 - cushion_pct%)))
    where the steps are entry + k x step_pct% of entry. Pure."""
    if entry is None or close is None:
        return None
    hc = max(highest_close or close, close)
    if hc < entry * (1 + params["start_pct"] / 100):
        return None
    step = entry * params.get("step_pct", 0.25) / 100
    raw = hc * (1 - params["cushion_pct"] / 100)
    k = math.floor((raw - entry) / step + 1e-9)
    return _cent_up(max(entry, entry + k * step))


# ============================================================ S28 target cap
# CEO 2026-10-16: target = fill + min(3R, cap x ATR14). The initial stop sits
# 1.5-2.5 ATR under the entry, so 3R is 4.5-7.5 ATR - out of reach in a
# 10-session hold (REGN 10-15: 3R = +139 on an ATR of 20). Applied AFTER the
# fill, to the live target leg, so the entry gates (reward:risk >= 1.5) still
# judge the setup's own target and no trade is refused because of the cap.

def target_cap_atr(config: dict):
    try:
        v = float(((config or {}).get("exits") or {}).get("target_cap_atr"))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def capped_target(entry, stop, atr, cap_atr, rr: float = 3.0):
    """fill + min(rr x R, cap x ATR), to the cent; None without geometry."""
    if entry is None or stop is None or entry <= stop:
        return None
    reach = rr * (entry - stop)
    if atr and cap_atr:
        reach = min(reach, cap_atr * atr)
    return round(entry + reach, 2)


def maybe_cap_target(broker, ticker: str, state: dict, atr, cap_atr,
                     rr: float = 3.0, persist=None, journal_event=None,
                     alert=None) -> bool:
    """Bring a BOT position's live target leg down to the cap, once.
    Returns True if the broker leg was replaced. Never raises the target;
    marks the position so the check is not repeated."""
    if not is_bot_managed(state) or state.get("target_capped") is not None:
        return False
    if not cap_atr or not atr or not state.get("target_order_id"):
        return False
    entry = state.get("entry_price")
    stop = original_stop(state)
    new = capped_target(entry, stop, atr, cap_atr, rr)
    old = state.get("profit_target_price")
    if new is None:
        return False
    if old is not None and new >= float(old) - 0.005:
        state["target_capped"] = False          # cap not binding: 3R is closer
        if persist:
            persist()
        return False
    order = broker.replace_target(state["target_order_id"], new)
    state["uncapped_target"] = old
    state["profit_target_price"] = new
    state["target_order_id"] = str(getattr(order, "id", state["target_order_id"]))
    state["target_capped"] = True
    state["target_cap_atr14"] = round(float(atr), 4)
    if persist:
        persist()
    detail = (f"{ticker}: target {old} -> {new} (fill {entry} + min(3R "
              f"{rr * (entry - stop):.2f}, {cap_atr:g} x ATR14 {atr:.2f}))")
    logger.warning(f"TARGET CAPPED - {detail}")
    if journal_event:
        try:
            journal_event("target_capped", detail)
        except Exception as e:
            logger.error(f"{ticker}: could not journal target cap: {e}")
    if alert:
        try:
            alert(f"🎯 Target capped - {detail}")
        except Exception:
            pass
    return True
