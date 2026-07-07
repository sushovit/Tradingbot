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


def _load_config() -> dict:
    try:
        with open("bot_config.json", "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def build_report() -> str:
    from broker import Broker, BrokerError
    lines = []
    now_et = datetime.now(EASTERN_TZ)
    lines.append(f"# Paper Account Report — {now_et.strftime('%Y-%m-%d %H:%M ET')}")

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

    # --- Open positions ---
    lines.append("\n## Open positions")
    try:
        positions = broker.get_positions()
        if not positions:
            lines.append("_None._")
        else:
            lines.append("| Ticker | Qty | Entry | Current | Unrealized PnL |")
            lines.append("|---|---|---|---|---|")
            for p in positions:
                upnl = float(p.unrealized_pl)
                upct = float(p.unrealized_plpc) * 100
                lines.append(f"| {p.symbol} | {p.qty} | ${float(p.avg_entry_price):,.2f} "
                             f"| ${float(p.current_price):,.2f} "
                             f"| ${upnl:,.2f} ({upct:+.2f}%) |")
    except BrokerError as e:
        lines.append(f"_Unavailable: {e}_")

    # --- Open orders (live bracket legs) ---
    lines.append("\n## Open orders")
    try:
        open_orders = broker.get_open_orders()
        if not open_orders:
            lines.append("_None._")
        else:
            lines.append("| Ticker | Side | Type | Qty | Stop | Limit |")
            lines.append("|---|---|---|---|---|---|")
            for o in open_orders:
                otype = str(getattr(o, "order_type", None) or getattr(o, "type", "")).split(".")[-1]
                stop_p = getattr(o, "stop_price", None)
                limit_p = getattr(o, "limit_price", None)
                lines.append(f"| {o.symbol} | {str(o.side).split('.')[-1]} | {otype} "
                             f"| {o.qty} "
                             f"| {'$' + format(float(stop_p), ',.2f') if stop_p else '—'} "
                             f"| {'$' + format(float(limit_p), ',.2f') if limit_p else '—'} |")
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

    # --- Journal ---
    lines.append("\n## Journal")
    try:
        lines.append(f"- Decisions journaled today: **{journal.decision_count()}**")
        lines.append(f"- Realized PnL today: **${journal.daily_realized_pnl():,.2f}**")
    except Exception as e:
        lines.append(f"_Journal unavailable: {e}_")

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
