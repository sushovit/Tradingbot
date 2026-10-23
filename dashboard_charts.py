"""
dashboard_charts.py — the Live tab's three charts. Display only: reads bars,
the journal and the broker's portfolio history, never writes anything.

  1. Market trend      SPY daily close vs its 20-day EMA - the live regime
                       rule (daily_eval.spy_regime) drawn. Chop sessions are
                       shaded.
  2. Our progress      the $5,000 account's equity and SPY, both indexed to
                       100 at the account takeover (2026-10-09), on ONE axis.
  3. Selected stocks   one small chart per open position and per signal the
                       gatekeeper looked at in the session: price, with the
                       entry, stop and target the desk set.

Colors are the dataviz reference palette: categorical slot 1 (blue) is
always SPY / the price, slot 2 (orange) is always the account; stop and
target use the reserved status colors and always carry a text label, so
no meaning is carried by color alone.
"""

from datetime import datetime

import altair as alt
import pandas as pd

import trade_table

SERIES_1 = "#2a78d6"      # SPY / price
SERIES_2 = "#eb6834"      # the account
MUTED = "#898781"         # EMA, entry, axis labels
CHOP_FILL = "#898781"
GOOD = "#0ca30c"          # target (status: good)
CRITICAL = "#d03b3b"      # stop (status: critical)
ACCOUNT_START_DATE = trade_table.ACCOUNT_START[:10]


# ============================================================ frames

def spy_trend_frame(spy: pd.DataFrame, sessions: int = 60) -> pd.DataFrame:
    """[date, close, ema20, chop] for the last `sessions` daily bars. EMA is
    computed over the whole series first so the tail is warmed up - the same
    span-20 EMA daily_eval uses."""
    close = spy["close"].astype(float)
    ema = close.ewm(span=20, adjust=False).mean()
    df = pd.DataFrame({
        "date": pd.to_datetime([str(t)[:10] for t in spy.index]),
        "close": close.values, "ema20": ema.values})
    df["chop"] = df["close"] <= df["ema20"]
    return df.tail(sessions).reset_index(drop=True)


def progress_frame(equity_points, spy: pd.DataFrame,
                   start: str = ACCOUNT_START_DATE) -> pd.DataFrame:
    """Long frame [date, series, index, value]: the account's equity and SPY's
    close, each = 100 on the first session at or after `start`.
    `equity_points` is [(date 'YYYY-MM-DD', equity)]; zero-equity days (the
    account before it was funded) are dropped."""
    eq = (pd.DataFrame(equity_points, columns=["date", "value"])
          if equity_points else pd.DataFrame(columns=["date", "value"]))
    eq = eq[(eq["value"] > 0) & (eq["date"] >= start)].drop_duplicates(
        "date", keep="last").sort_values("date")
    # One point per trading session: the broker's history carries weekend
    # rows, which would put the account on days SPY has no bar.
    eq = eq[pd.to_datetime(eq["date"]).dt.weekday < 5]
    sp = pd.DataFrame({"date": [str(t)[:10] for t in spy.index],
                       "value": spy["close"].astype(float).values})
    sp = sp[sp["date"] >= start].sort_values("date")
    out = []
    for name, part in (("$5,000 account", eq), ("SPY", sp)):
        if part.empty:
            continue
        base = float(part["value"].iloc[0])
        out.append(pd.DataFrame({
            "date": pd.to_datetime(part["date"]),
            "day": pd.to_datetime(part["date"]).dt.strftime("%b %d"),
            "series": name,
            "index": part["value"].astype(float) / base * 100.0,
            "value": part["value"].astype(float)}))
    if not out:
        return pd.DataFrame(columns=["date", "day", "series", "index",
                                     "value"])
    return pd.concat(out, ignore_index=True)


def selected_items(open_positions: dict, candidates, bars: dict,
                   session_date: str, limit: int = 6):
    """[{ticker, status, entry, stop, target, bars(df: ts, price)}].

    Open positions first (status 'open'), then the session's gatekeeper
    candidates, newest first, one per ticker. `candidates` rows:
    {ticker, setup, approved(bool), conviction, entry, stop, target, ts}."""
    items, seen = [], set()
    for tk, p in sorted((open_positions or {}).items()):
        items.append({"ticker": tk, "status": "open",
                      "setup": p.get("setup") or "",
                      "entry": p.get("entry"), "stop": p.get("stop"),
                      "target": p.get("target"), "ts": None})
        seen.add(tk)
    for c in sorted(candidates or [], key=lambda c: c["ts"], reverse=True):
        if c["ticker"] in seen:
            continue
        seen.add(c["ticker"])
        items.append({"ticker": c["ticker"], "setup": c.get("setup") or "",
                      "status": ("approved" if c.get("approved")
                                 else "rejected"),
                      "entry": c.get("entry"), "stop": c.get("stop"),
                      "target": c.get("target"), "ts": c.get("ts")})
    out = []
    for it in items[:limit]:
        df = bars.get(it["ticker"])
        frame = session_frame(df, session_date) if df is not None else None
        if frame is None or frame.empty:
            continue
        last = float(frame["price"].iloc[-1])
        it["last"] = last
        it["change_pct"] = ((last / float(it["entry"]) - 1) * 100
                            if it.get("entry") else None)
        it["bars"] = frame
        out.append(it)
    return out


def session_frame(df: pd.DataFrame, session_date: str) -> pd.DataFrame:
    """Regular-hours 5-minute closes for one ET session: [ts, price]."""
    idx = df.index.tz_convert("America/New_York")
    mins = idx.hour * 60 + idx.minute
    mask = ((idx.strftime("%Y-%m-%d") == session_date)
            & (mins >= 570) & (mins < 960))
    part = df[mask]
    return pd.DataFrame({
        "ts": idx[mask].tz_localize(None),
        "price": part["close"].astype(float).values})


# ============================================================ charts

def market_chart(df: pd.DataFrame) -> alt.Chart:
    long = df.melt(id_vars=["date", "chop"], value_vars=["close", "ema20"],
                   var_name="series", value_name="price")
    long["series"] = long["series"].map({"close": "SPY close",
                                         "ema20": "20-day EMA"})
    base = alt.Chart(long)
    color = alt.Color("series:N", title=None,
                      scale=alt.Scale(domain=["SPY close", "20-day EMA"],
                                      range=[SERIES_1, MUTED]),
                      legend=alt.Legend(orient="top"))
    chop = alt.Chart(df[df["chop"]]).mark_rect(
        color=CHOP_FILL, opacity=0.12).encode(
        x=alt.X("date:T"), x2=alt.X2("next:T"),
        tooltip=[alt.Tooltip("date:T", title="Chop session")]
    ).transform_calculate(next="datum.date + 24*3600*1000")
    lines = base.mark_line(strokeWidth=2).encode(
        x=alt.X("date:T", title=None),
        y=alt.Y("price:Q", title="SPY ($)", scale=alt.Scale(zero=False)),
        color=color,
        strokeDash=alt.StrokeDash("series:N", legend=None,
                                  scale=alt.Scale(domain=["SPY close", "20-day EMA"],
                                                  range=[[1, 0], [5, 3]])))
    nearest = alt.selection_point(nearest=True, on="pointerover",
                                  fields=["date"], empty=False)
    wide = df.assign(regime=df["chop"].map({True: "chop", False: "trending"}))
    hover = alt.Chart(wide).mark_point(size=60, filled=True,
                                       color=SERIES_1).encode(
        x="date:T", y="close:Q",
        opacity=alt.condition(nearest, alt.value(1), alt.value(0)),
        tooltip=[alt.Tooltip("date:T", title="Date"),
                 alt.Tooltip("close:Q", title="SPY close", format="$,.2f"),
                 alt.Tooltip("ema20:Q", title="20-day EMA", format="$,.2f"),
                 alt.Tooltip("regime:N", title="Regime")]).add_params(nearest)
    rule = alt.Chart(wide).mark_rule(color=MUTED).encode(
        x="date:T").transform_filter(nearest)
    return (chop + lines + rule + hover).properties(height=260)


def progress_chart(df: pd.DataFrame) -> alt.Chart:
    domain = ["$5,000 account", "SPY"]
    base = alt.Chart(df)
    color = alt.Color("series:N", title=None,
                      scale=alt.Scale(domain=domain, range=[SERIES_2, SERIES_1]),
                      legend=alt.Legend(orient="top"))
    # Never zoom past +/-1%: a 0.06% wiggle drawn full-height reads as a crash.
    lo = min(99.0, float(df["index"].min()) - 0.25)
    hi = max(101.0, float(df["index"].max()) + 0.25)
    days = [d.strftime("%b %d") for d in sorted(df["date"].unique())
            for d in [pd.Timestamp(d)]]
    x = alt.X("day:O", title=None, sort=days, axis=alt.Axis(labelAngle=0))
    # every layer gets the SAME y scale, or the reference line's layer
    # drags the shared domain down to zero
    y = alt.Y("index:Q", title="Index (100 = 2026-10-09)",
              scale=alt.Scale(domain=[lo, hi], zero=False))
    lines = base.mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=40)).encode(
        x=x, y=y,
        color=color,
        tooltip=[alt.Tooltip("date:T", title="Date"),
                 alt.Tooltip("series:N", title="Series"),
                 alt.Tooltip("index:Q", title="Index", format=".2f"),
                 alt.Tooltip("value:Q", title="Value", format="$,.2f")])
    baseline = alt.Chart(pd.DataFrame({"index": [100.0]})).mark_rule(
        color=MUTED, strokeDash=[2, 2]).encode(y=y)
    last = df.sort_values("date").groupby("series").tail(1)
    labels = alt.Chart(last).mark_text(align="left", dx=6, dy=-9, fontSize=12,
                                       color="#52514e").encode(
        x=x, y=y,
        text=alt.Text("label:N")).transform_calculate(
        label="datum.series + '  ' + format(datum.index - 100, '+.2f') + '%'")
    return (baseline + lines + labels).properties(height=260)


def selected_chart(item: dict) -> alt.Chart:
    """One small multiple: the session's price with entry / stop / target.
    The y-domain holds every level, so a far target never clips off."""
    frame = item["bars"]
    levels = [(name, float(val), col, dash) for name, val, col, dash in (
        ("target", item.get("target"), GOOD, [6, 3]),
        ("entry", item.get("entry"), MUTED, [2, 2]),
        ("stop", item.get("stop"), CRITICAL, [6, 3])) if val is not None]
    ys = list(frame["price"]) + [v for _, v, _, _ in levels]
    pad = (max(ys) - min(ys)) * 0.08 or 1.0
    y = alt.Y("price:Q", title=None,
              scale=alt.Scale(domain=[min(ys) - pad, max(ys) + pad]))
    base = alt.Chart(frame)
    nearest = alt.selection_point(nearest=True, on="pointerover",
                                  fields=["ts"], empty=False)
    layers = [
        base.mark_line(strokeWidth=2, color=SERIES_1).encode(
            x=alt.X("ts:T", title=None, axis=alt.Axis(format="%H:%M")), y=y),
        base.mark_rule(color=MUTED).encode(x="ts:T").transform_filter(nearest),
        base.mark_point(size=60, filled=True, color=SERIES_1).encode(
            x="ts:T", y="price:Q",
            opacity=alt.condition(nearest, alt.value(1), alt.value(0)),
            tooltip=[alt.Tooltip("ts:T", title="Time (ET)", format="%H:%M"),
                     alt.Tooltip("price:Q", title="Price", format="$,.2f")]
        ).add_params(nearest),
    ]
    last_ts = frame["ts"].max()
    for name, val, col, dash in levels:
        one = pd.DataFrame({"price": [val], "ts": [last_ts],
                            "label": [f"{name} ${val:,.2f}"]})
        layers.append(alt.Chart(one).mark_rule(
            color=col, strokeWidth=1.5, strokeDash=dash).encode(y="price:Q"))
        # stop's label hangs BELOW its line, the others sit above, so a
        # tight stop never prints on top of the entry label
        below = name == "stop"
        layers.append(alt.Chart(one).mark_text(
            align="right", baseline="top" if below else "bottom",
            dy=2 if below else -2, fontSize=11,
            color="#52514e").encode(x="ts:T", y="price:Q", text="label:N"))
    if item.get("ts"):
        sig = pd.Timestamp(item["ts"]).floor("5min")
        if frame["ts"].min() <= sig <= frame["ts"].max():
            mark = pd.DataFrame({"ts": [sig],
                                 "label": [f"signal {sig:%H:%M}"]})
            layers.append(alt.Chart(mark).mark_rule(
                color=MUTED, strokeWidth=1).encode(x="ts:T"))
            layers.append(alt.Chart(mark).mark_text(
                align="left", baseline="top", dx=3, y=2, fontSize=11,
                color="#52514e").encode(x="ts:T", text="label:N"))
    return alt.layer(*layers).properties(height=170, title=title_for(item))


CANDLE_UP = "#2a9d8f"      # candle closed up (polarity, not a status colour)
CANDLE_DOWN = "#e76f51"    # candle closed down


def candle_chart(item: dict, candles: pd.DataFrame, timeframe: str = "1m") -> alt.Chart:
    """S20 (2026-10-23): live candles (ts, open, high, low, close) with the
    entry / stop / target the desk set. Same levels and labels as
    selected_chart; the y-domain holds every level."""
    frame = candles.copy()
    frame["up"] = frame["close"] >= frame["open"]
    levels = [(name, float(val), col, dash) for name, val, col, dash in (
        ("target", item.get("target"), GOOD, [6, 3]),
        ("entry", item.get("entry"), MUTED, [2, 2]),
        ("stop", item.get("stop"), CRITICAL, [6, 3])) if val is not None]
    ys = list(frame["low"]) + list(frame["high"]) + [v for _, v, _, _ in levels]
    pad = (max(ys) - min(ys)) * 0.08 or 1.0
    scale = alt.Scale(domain=[min(ys) - pad, max(ys) + pad])
    color = alt.condition("datum.up", alt.value(CANDLE_UP), alt.value(CANDLE_DOWN))
    x = alt.X("ts:T", title=None, axis=alt.Axis(format="%H:%M"))
    tip = [alt.Tooltip("ts:T", title="Time (ET)", format="%H:%M"),
           alt.Tooltip("open:Q", format="$,.2f"), alt.Tooltip("high:Q", format="$,.2f"),
           alt.Tooltip("low:Q", format="$,.2f"), alt.Tooltip("close:Q", format="$,.2f")]
    base = alt.Chart(frame).encode(x=x, color=color, tooltip=tip)
    layers = [
        base.mark_rule(strokeWidth=1).encode(
            y=alt.Y("low:Q", title=None, scale=scale), y2="high:Q"),
        base.mark_bar(width={"band": 0.7} if len(frame) > 1 else 4).encode(
            y=alt.Y("open:Q", scale=scale), y2="close:Q"),
    ]
    last_ts = frame["ts"].max()
    for name, val, col, dash in levels:
        one = pd.DataFrame({"price": [val], "ts": [last_ts],
                            "label": [f"{name} ${val:,.2f}"]})
        layers.append(alt.Chart(one).mark_rule(
            color=col, strokeWidth=1.5, strokeDash=dash).encode(y="price:Q"))
        below = name == "stop"
        layers.append(alt.Chart(one).mark_text(
            align="right", baseline="top" if below else "bottom",
            dy=2 if below else -2, fontSize=11,
            color="#52514e").encode(x="ts:T", y="price:Q", text="label:N"))
    last = float(frame["close"].iloc[-1])
    chg = (last / float(item["entry"]) - 1) * 100 if item.get("entry") else None
    status = {"open": "open position", "approved": "gatekeeper approved",
              "rejected": "gatekeeper rejected"}.get(item.get("status"), "")
    title = (f"{item['ticker']} · {status} · {timeframe} · ${last:,.2f}"
             + (f" ({chg:+.2f}% vs entry)" if chg is not None else ""))
    return alt.layer(*layers).properties(height=190, title=title)


def title_for(item: dict) -> str:
    chg = item.get("change_pct")
    move = f" · {chg:+.2f}% vs entry" if chg is not None else ""
    status = {"open": "open position", "approved": "gatekeeper approved",
              "rejected": "gatekeeper rejected"}[item["status"]]
    return f"{item['ticker']} · {status}{move}"


# ============================================================ live inputs

def equity_points(broker):
    """[(date, equity)] daily from the broker's portfolio history, plus
    today's live equity as the last point."""
    from alpaca.trading.requests import GetPortfolioHistoryRequest
    h = broker.trading.get_portfolio_history(
        GetPortfolioHistoryRequest(period="1M", timeframe="1D"))
    pts = [(datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"), float(e or 0))
           for t, e in zip(h.timestamp or [], h.equity or [])]
    try:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        pts.append((today, float(broker.get_equity())))
    except Exception:
        pass
    return pts


def session_candidates(conn, session_date: str):
    """The gatekeeper's calls in one session, with the geometry it saw."""
    import json
    rows = []
    for r in conn.execute(
            "SELECT timestamp, ticker, setup_name, approved, conviction_score, "
            "context FROM decisions WHERE source='claude' "
            "AND substr(timestamp,1,10)=? ORDER BY id", (session_date,)):
        try:
            ctx = json.loads(r[5] or "{}")
        except ValueError:
            ctx = {}
        approved = bool(r[3]) and (r[4] or 0) >= 70
        rows.append({"ts": r[0], "ticker": r[1], "setup": r[2],
                     "approved": approved, "conviction": r[4],
                     "entry": ctx.get("entry"), "stop": ctx.get("stop"),
                     "target": ctx.get("target")})
    return rows


def latest_session_with_calls(conn, today: str) -> str:
    row = conn.execute("SELECT max(substr(timestamp,1,10)) FROM decisions "
                       "WHERE source='claude' AND substr(timestamp,1,10)<=?",
                       (today,)).fetchone()
    return row[0] if row and row[0] else today
