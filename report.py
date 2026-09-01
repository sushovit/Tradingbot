"""
report.py — compact markdown account report for pasting into a chat session.

    python report.py

Prints: equity, open positions with unrealized PnL, today's fills, journal
decision count, daily realized PnL, and the analyst shadow scorecard.
PAPER account only (broker.py enforces this).
"""

import sys
import logging
from datetime import datetime

import pytz
from dotenv import load_dotenv

import json

import journal
import risk

load_dotenv()
logging.basicConfig(level=logging.WARNING)

EASTERN_TZ = pytz.timezone("US/Eastern")


def sessions_held(ticker: str):
    """Trading sessions since the journaled BUY (Goal 21: >= 10 = review)."""
    try:
        import pandas as pd
        buy = journal.last_buy_for_ticker(ticker)
        if not buy:
            return None
        opened = pd.Timestamp(buy["timestamp"][:10])
        return max(0, len(pd.bdate_range(opened,
                                         datetime.now(EASTERN_TZ).date())) - 1)
    except Exception:
        return None


def _load_config() -> dict:
    try:
        with open("bot_config.json", "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def build_report() -> str:
    from broker import Broker, BrokerError
    import clockline
    lines = []
    lines.append("# Paper Account Report")
    lines.append(clockline.two_zone_line())

    try:
        broker = Broker()
    except Exception as e:
        return f"**Cannot connect to Alpaca paper account:** {e}"

    journal.init_db()

    # --- Reconcile any exits that filled while we weren't watching ---
    try:
        import orders
        orders.sync(broker=broker)
    except Exception as e:
        lines.append(f"\n_Exit sync failed (report may miss recent exits): {e}_")

    # --- Account ---
    try:
        acct = broker.get_account()
        config = _load_config()
        broker_equity = float(acct.equity)
        eff = risk.effective_equity(broker_equity, config)
        cap_note = " (cap)" if eff < broker_equity else ""
        lines.append("")
        lines.append(f"**Effective capital: ${eff:,.2f}{cap_note} | "
                     f"Broker equity: ${broker_equity:,.2f}**")
        lines.append(f"Cash: ${float(acct.cash):,.2f} — margin buying power is "
                     f"never used; deployable cash is capped at effective capital.")
    except BrokerError as e:
        lines.append(f"\n**Account unavailable:** {e}")
        return "\n".join(lines)

    # --- Open positions (with tier + sessions held; 10+ = REVIEW DUE) ---
    lines.append("\n## Open positions")
    try:
        positions = broker.get_positions()
        if not positions:
            lines.append("_None._")
        else:
            b_tickers = set(journal.open_b_tickers())
            # Live stops decide the risk-free exemption below.
            live_stops = {}
            try:
                for o in broker.get_live_orders():
                    otype = str(getattr(o, "order_type", None)
                                or getattr(o, "type", "")).lower()
                    if "stop" in otype and getattr(o, "stop_price", None):
                        live_stops[o.symbol] = float(o.stop_price)
            except Exception:
                pass
            lines.append("| Ticker | Tier | Qty | Entry | Current | Unrealized PnL | Held |")
            lines.append("|---|---|---|---|---|---|---|")
            for p in positions:
                upnl = float(p.unrealized_pl)
                upct = float(p.unrealized_plpc) * 100
                tier = "B" if p.symbol in b_tickers else "A"
                held = sessions_held(p.symbol)
                # Risk-free (stop at or above entry) positions are exempt
                # from the 10-session re-ratification nag — there is nothing
                # left to re-underwrite.
                entry_px = float(p.avg_entry_price)
                risk_free = live_stops.get(p.symbol) is not None and \
                    live_stops[p.symbol] >= entry_px
                held_txt = "—" if held is None else (
                    f"{held}s"
                    + (" _(risk-free)_" if risk_free and held >= 10 else "")
                    + (" **REVIEW DUE**" if held >= 10 and not risk_free else ""))
                lines.append(f"| {p.symbol} | {tier} | {p.qty} "
                             f"| ${float(p.avg_entry_price):,.2f} "
                             f"| ${float(p.current_price):,.2f} "
                             f"| ${upnl:,.2f} ({upct:+.2f}%) | {held_txt} |")
    except BrokerError as e:
        lines.append(f"_Unavailable: {e}_")

    # --- Open orders (live bracket legs, INCLUDING held ones) ---
    lines.append("\n## Open orders")
    try:
        live_orders = broker.get_live_orders()
        if not live_orders:
            lines.append("_None._")
        else:
            lines.append("| Ticker | Side | Type | Qty | Stop | Limit | Status |")
            lines.append("|---|---|---|---|---|---|---|")
            for o in live_orders:
                otype = str(getattr(o, "order_type", None) or getattr(o, "type", "")).split(".")[-1]
                status = str(getattr(o, "status", "")).split(".")[-1].lower()
                stop_p = getattr(o, "stop_price", None)
                limit_p = getattr(o, "limit_price", None)
                lines.append(f"| {o.symbol} | {str(o.side).split('.')[-1]} | {otype} "
                             f"| {o.qty} "
                             f"| {'$' + format(float(stop_p), ',.2f') if stop_p else '—'} "
                             f"| {'$' + format(float(limit_p), ',.2f') if limit_p else '—'} "
                             f"| {status} |")
    except BrokerError as e:
        lines.append(f"_Unavailable: {e}_")

    # --- Today's fills ---
    lines.append("\n## Today's fills")
    try:
        fills = broker.get_orders_filled_today()
        if not fills:
            lines.append("_None._")
        else:
            lines.append("| Time (UTC) | Ticker | Side | Qty | Avg price |")
            lines.append("|---|---|---|---|---|")
            for o in fills:
                filled_at = o.filled_at.strftime("%H:%M:%S") if o.filled_at else "?"
                price = f"${float(o.filled_avg_price):,.2f}" if o.filled_avg_price else "?"
                lines.append(f"| {filled_at} | {o.symbol} | {str(o.side).split('.')[-1]} "
                             f"| {o.filled_qty} | {price} |")
    except BrokerError as e:
        lines.append(f"_Unavailable: {e}_")

    # --- Recent closed trades (fill-price corrected) ---
    lines.append("\n## Recent closed trades")
    try:
        sells = journal.recent_sells(limit=10)
        if not sells:
            lines.append("_None._")
        else:
            lines.append("| Date | Ticker | Qty | Exit | PnL | Reason |")
            lines.append("|---|---|---|---|---|---|")
            for s in sells:
                lines.append(f"| {s['timestamp'][:10]} | {s['ticker']} | {s['qty']:g} "
                             f"| ${s['price']:,.2f} | ${s['pnl_usd']:+,.2f} "
                             f"({s['pnl_pct']:+.2f}%) | {s['reason']} |")
    except Exception as e:
        lines.append(f"_Unavailable: {e}_")

    # --- Journal ---
    lines.append("\n## Journal")
    try:
        lines.append(f"- Decisions journaled today: **{journal.decision_count()}**")
        lines.append(f"- Realized PnL today: **${journal.daily_realized_pnl():,.2f}**")
        lines.append(f"- Cumulative realized — A-book: "
                     f"**${journal.tier_realized_pnl('A'):+,.2f}** | "
                     f"B-book (experimental): "
                     f"${journal.tier_realized_pnl('B'):+,.2f}")
    except Exception as e:
        lines.append(f"_Journal unavailable: {e}_")

    # --- Intern account (his public scoreboard) ---
    lines.append("\n## Intern account")
    try:
        from broker import Broker as _B
        intern_broker = _B(account="intern")
        try:
            import orders
            orders.sync(broker=intern_broker, desk="intern")
        except Exception as e:
            lines.append(f"_Intern sync failed: {e}_")
        iacct = intern_broker.get_account()
        iequity = float(iacct.equity)
        start_capital = 2000.0
        delta = iequity - start_capital
        lines.append(f"**Equity:** ${iequity:,.2f} "
                     f"({delta:+,.2f} vs ${start_capital:,.0f} start) | "
                     f"**Realized PnL (cumulative):** "
                     f"${journal.desk_realized_pnl('INTERN'):+,.2f}")
        ipos = intern_broker.get_positions()
        if ipos:
            lines.append("| Ticker | Qty | Entry | Current | Unrealized PnL |")
            lines.append("|---|---|---|---|---|")
            for p in ipos:
                lines.append(f"| {p.symbol} | {p.qty} | ${float(p.avg_entry_price):,.2f} "
                             f"| ${float(p.current_price):,.2f} "
                             f"| ${float(p.unrealized_pl):,.2f} "
                             f"({float(p.unrealized_plpc) * 100:+.2f}%) |")
        else:
            lines.append("_No open positions._")
        ifills = [t for t in journal.todays_trades()
                  if str(t.get("reason", "")).startswith("INTERN")]
        if ifills:
            lines.append("Today's intern fills: " + "; ".join(
                f"{t['action']} {t['qty']:g} {t['ticker']} @ ${t['price']:,.2f}"
                for t in ifills))
    except Exception as e:
        lines.append(f"_Intern account unavailable: {e}_")

    # --- Sector expectancy (Boardroom #2 item 7) ---
    # The crypto/DAT ruling was "no exclusion, but measure the class".
    # This table IS that measurement — it must stay visible even at n=1,
    # because the point is to accumulate evidence, not to wait for it.
    lines.append(chr(10) + "## Sector expectancy")
    try:
        rows = journal.sector_expectancy()
        if not rows:
            lines.append("_No closed trades yet._")
        else:
            lines.append("| Sector | Trades | W-L | Win % | Realized | "
                         "Expectancy/trade |")
            lines.append("|---|---|---|---|---|---|")
            for r in rows:
                flag = " ⚠" if r["sector"] == "unclassified" else ""
                lines.append(
                    f"| {r['sector']}{flag} | {r['trades']} | "
                    f"{r['wins']}-{r['losses']} | {r['win_rate']}% | "
                    f"${r['realized_usd']:+,.2f} | "
                    f"${r['expectancy_usd']:+,.2f} |")
            crypto = next((r for r in rows if r["sector"] == "crypto_dat"), None)
            if crypto is None:
                lines.append(chr(10) + "_crypto_dat: no closed trades yet — "
                             "the class is tradeable at standard risk and is "
                             "being measured from here._")
    except Exception as e:
        lines.append(f"_Unavailable: {e}_")

    # --- Shadow dissent ledger (advisory-vs-outcome scoreboard) ---
    lines.append(chr(10) + "## Shadow dissents")
    try:
        d = journal.shadow_dissent_report()
        if d["dissents"] == 0:
            lines.append("_None recorded._")
        else:
            lines.append(f"**Shadow dissents: {d['dissents']}, resolved "
                         f"{d['shadow_right']}-{d['shadow_wrong']}** "
                         f"(shadow right-wrong on closed trades; "
                         f"{d['unresolved']} still open)")
            lines.append(f"- Realized on dissented trades: "
                         f"${d['realized_on_dissents']:+,.2f}")
            if d["avg_conviction_gap"] is not None:
                lines.append(f"- Avg conviction gap (Claude - shadow): "
                             f"{d['avg_conviction_gap']}")
    except Exception as e:
        lines.append(f"_Unavailable: {e}_")

    # --- Analyst shadow performance ---
    lines.append("\n## Analyst shadow performance")
    try:
        rep = journal.agreement_report()
        if rep["total_shadow_decisions"] == 0:
            lines.append("_No shadow decisions recorded yet._")
        else:
            lines.append(f"- Shadow decisions: **{rep['total_shadow_decisions']}** "
                         f"(errors: {rep['errors']})")
            if rep["agreement_pct"] is not None:
                lines.append(f"- Agreement with Claude: **{rep['agreement_pct']}%**")
            lines.append(f"- Local approved / Claude rejected: "
                         f"**{rep['local_approved_claude_rejected']}**")
            lines.append(f"- Claude approved / local rejected: "
                         f"**{rep['claude_approved_local_rejected']}**")
            if rep["avg_conviction_gap"] is not None:
                lines.append(f"- Avg conviction gap: **{rep['avg_conviction_gap']}**")
    except Exception as e:
        lines.append(f"_Unavailable: {e}_")

    return "\n".join(lines)


if __name__ == "__main__":
    report = build_report()
    print(report)
    sys.exit(0 if "Cannot connect" not in report else 1)
