"""
discord_bot.py — S18 Discord sell (owner request, CEO 2026-10-27).

A separate process (launchd com.tradingbot.discord_bot, restarted on a crash)
that answers TWO commands, and only from OWNER_DISCORD_ID in the trading
channel - every other author or channel is ignored without a reply:

  !positions      open positions: qty, entry, now, P&L, stop, target
  !sell TICKER    confirm prompt (qty, price, P&L); "YES" within 60 s sells

The bot holds NO order code. On YES it writes the SAME command file the
dashboard's Sell button writes when the worker is running
(sell_<T>.command, source 'discord'); the worker then runs its owner close:
cancel the stop + target legs, market sell, journal "Owner close (Discord)",
Discord alert. Market hours only, and only for a position the running worker
manages - otherwise the bot says so and the dashboard Sell button remains the
way out.

.env: DISCORD_BOT_TOKEN, OWNER_DISCORD_ID and (optional)
DISCORD_TRADING_CHANNEL_ID - without it the channel is the one the existing
DISCORD_WEBHOOK_URL posts to. Missing token/owner -> exits 0 (not restarted).
"""

import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime

TOKEN_ENV, OWNER_ENV, CHANNEL_ENV = "DISCORD_BOT_TOKEN", "OWNER_DISCORD_ID", "DISCORD_TRADING_CHANNEL_ID"
CONFIRM_S = 60
FILL_WAIT_S, FILL_POLL_S = 150, 5
HEARTBEAT_FRESH_S = 300
SELL_COMMAND = "sell_{}.command"             # same file the dashboard writes
STATUS_FILE, LOCK_FILE, POSITIONS_FILE = "bot_status.log", "bot.run", "positions.json"
OWNER_CLOSE_DISCORD = "Owner close (Discord)"

logger = logging.getLogger("discord_bot")


def _money(x):
    return "-" if x is None else f"${x:,.2f}"


# ===================================================================== the desk (side effects)

class SellDesk:
    """Reads the broker and the worker's files; the ONLY write is the sell
    command file. Every broker call here is a read."""

    def __init__(self, broker=None, root="."):
        self._broker = broker
        self.root = root

    @property
    def broker(self):
        if self._broker is None:
            from dotenv import load_dotenv
            load_dotenv(os.path.join(self.root, ".env"))
            from broker import Broker
            self._broker = Broker()
        return self._broker

    def _p(self, name):
        return os.path.join(self.root, name)

    def market_open(self) -> bool:
        return bool(self.broker.trading.get_clock().is_open)

    def worker_alive(self) -> bool:
        try:
            age = time.time() - os.path.getmtime(self._p(STATUS_FILE))
        except OSError:
            return False
        return os.path.exists(self._p(LOCK_FILE)) and age < HEARTBEAT_FRESH_S

    def _state(self) -> dict:
        try:
            with open(self._p(POSITIONS_FILE), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def tracked(self, ticker) -> bool:
        return bool((self._state().get(ticker) or {}).get("in_position"))

    def _leg(self, order_id, field):
        if not order_id:
            return None
        try:
            v = getattr(self.broker.get_order(order_id), field, None)
            return float(v) if v is not None else None
        except Exception:
            return None

    def positions(self) -> list:
        state = self._state()
        out = []
        for p in self.broker.get_positions():
            s = state.get(p.symbol) or {}
            stop = self._leg(s.get("stop_order_id"), "stop_price") or s.get("trailing_stop_price")
            target = self._leg(s.get("target_order_id"), "limit_price") or s.get("profit_target_price")
            out.append({"ticker": p.symbol, "qty": abs(float(p.qty)),
                        "entry": float(p.avg_entry_price),
                        "last": float(getattr(p, "current_price", 0) or 0) or None,
                        "pnl": float(getattr(p, "unrealized_pl", 0) or 0),
                        "stop": stop, "target": target})
        return out

    def queue_sell(self, ticker) -> str:
        import safe_io
        path = self._p(SELL_COMMAND.format(ticker))
        safe_io.atomic_write_text(path, f"discord {datetime.now().astimezone().isoformat()}")
        return path

    def sell_result(self, ticker):
        """(done, fill, pnl): the worker consumed the command, the broker no
        longer holds the stock, and (if journaled) the fill and P&L."""
        if os.path.exists(self._p(SELL_COMMAND.format(ticker))):
            return False, None, None
        if any(p.symbol == ticker for p in self.broker.get_positions()):
            return False, None, None
        try:
            import journal
            conn = sqlite3.connect(journal.DB_FILE, timeout=15)
            row = conn.execute("SELECT price, pnl_usd FROM trades WHERE ticker=? AND action='SELL' "
                               "AND reason=? ORDER BY id DESC LIMIT 1",
                               (ticker, OWNER_CLOSE_DISCORD)).fetchone()
            conn.close()
        except Exception:
            row = None
        return True, (row[0] if row else None), (row[1] if row else None)


# ===================================================================== commands (pure, testable)

def positions_text(rows) -> str:
    if not rows:
        return "No open positions."
    head = f"{'Ticker':6} {'Qty':>5} {'Entry':>9} {'Now':>9} {'P&L':>9} {'Stop':>9} {'Target':>9}"
    lines = [head] + [
        f"{r['ticker']:6} {r['qty']:>5g} {r['entry']:>9.2f} "
        f"{(r['last'] or 0):>9.2f} {r['pnl']:>+9.2f} "
        f"{(r['stop'] or 0):>9.2f} {(r['target'] or 0):>9.2f}" for r in rows]
    return "📊 Open positions\n```\n" + "\n".join(lines) + "\n```"


class OwnerCommands:
    """!positions and !sell TICKER -> YES, from the owner in the trading
    channel only. `reply` is an async callable(text)."""

    def __init__(self, desk, owner_id: int, channel_id: int, confirm_s=CONFIRM_S,
                 fill_wait_s=FILL_WAIT_S, fill_poll_s=FILL_POLL_S, clock=time.monotonic):
        self.desk, self.owner_id, self.channel_id = desk, int(owner_id), int(channel_id)
        self.confirm_s, self.fill_wait_s, self.fill_poll_s = confirm_s, fill_wait_s, fill_poll_s
        self.clock = clock
        self.pending = None              # {"ticker", "deadline", "token"}
        self.tasks = []

    def accepts(self, message) -> bool:
        return (getattr(message.author, "id", None) == self.owner_id
                and getattr(message.channel, "id", None) == self.channel_id)

    async def _call(self, fn, *a):
        return await asyncio.to_thread(fn, *a)

    async def on_message(self, message, reply):
        if not self.accepts(message):
            return                                   # everyone else: silence
        text = (message.content or "").strip()
        if self.pending is not None:
            p, self.pending = self.pending, None
            if text == "YES":
                if self.clock() > p["deadline"]:
                    await reply(f"⌛ Too late - the sell of {p['ticker']} expired. Send !sell again.")
                    return
                await self._execute(p["ticker"], reply)
                return
            await reply(f"Sell of {p['ticker']} cancelled.")
        low = text.lower()
        if low == "!positions":
            await reply(positions_text(await self._call(self.desk.positions)))
        elif low.startswith("!sell"):
            parts = text.split()
            if len(parts) != 2:
                await reply("Usage: !sell TICKER")
                return
            await self._prompt(parts[1].upper(), reply)

    async def _blocker(self, ticker):
        if not await self._call(self.desk.market_open):
            return "The market is closed - Discord sells work in market hours only."
        rows = {r["ticker"]: r for r in await self._call(self.desk.positions)}
        if ticker not in rows:
            return f"No open position in {ticker}."
        if not (await self._call(self.desk.worker_alive) and await self._call(self.desk.tracked, ticker)):
            return (f"The worker is not managing {ticker} right now, so Discord cannot sell it. "
                    f"Use the dashboard Sell button.")
        return rows[ticker]

    async def _prompt(self, ticker, reply):
        r = await self._blocker(ticker)
        if isinstance(r, str):
            await reply(f"⛔ {r}")
            return
        token = object()
        self.pending = {"ticker": ticker, "deadline": self.clock() + self.confirm_s, "token": token}
        await reply(f"⚠️ Sell ALL {r['qty']:g} {ticker} at market? Now {_money(r['last'])} · "
                    f"entry {_money(r['entry'])} · P&L {r['pnl']:+,.2f}. Its stop and target are "
                    f"cancelled first. Reply YES within {self.confirm_s} s.")
        self.tasks.append(asyncio.ensure_future(self._expire(ticker, token, reply)))

    async def _expire(self, ticker, token, reply):
        await asyncio.sleep(self.confirm_s)
        if self.pending is not None and self.pending["token"] is token:
            self.pending = None
            await reply(f"⌛ No YES within {self.confirm_s} s - sell of {ticker} cancelled.")

    async def _execute(self, ticker, reply):
        r = await self._blocker(ticker)              # re-check at YES time
        if isinstance(r, str):
            await reply(f"⛔ Not sold: {r}")
            return
        await self._call(self.desk.queue_sell, ticker)
        await reply(f"✅ Sell of {r['qty']:g} {ticker} sent to the worker (same path as the dashboard "
                    f"Sell button): stop + target cancelled, then a market sell at its next cycle, "
                    f"about a minute. I'll confirm the fill here.")
        self.tasks.append(asyncio.ensure_future(self._confirm(ticker, reply)))

    async def _confirm(self, ticker, reply):
        waited = 0.0
        while waited < self.fill_wait_s:
            await asyncio.sleep(self.fill_poll_s)
            waited += self.fill_poll_s
            done, px, pnl = await self._call(self.desk.sell_result, ticker)
            if done:
                await reply(f"💰 SOLD {ticker}" + (f" at {_money(px)}" if px else "")
                            + (f" · P&L {pnl:+,.2f}" if pnl is not None else "")
                            + " · journaled as Owner close (Discord).")
                return
        await reply(f"⚠️ {ticker} sell not confirmed after {int(self.fill_wait_s)} s - check the "
                    f"dashboard (Open Positions).")


# ===================================================================== runner

def channel_from_webhook(url, get=None):
    """The channel the existing webhook posts to (Discord returns channel_id
    for GET <webhook url>)."""
    import requests
    get = get or requests.get
    try:
        return int(get(url, timeout=10).json()["channel_id"])
    except Exception:
        return None


def config(env=os.environ, get=None):
    """(token, owner_id, channel_id) or a reason string why the bot must not run."""
    token, owner = env.get(TOKEN_ENV, "").strip(), env.get(OWNER_ENV, "").strip()
    if not token or not owner.isdigit():
        return f"not configured: {TOKEN_ENV} and a numeric {OWNER_ENV} are required in .env"
    chan = env.get(CHANNEL_ENV, "").strip()
    channel = int(chan) if chan.isdigit() else channel_from_webhook(env.get("DISCORD_WEBHOOK_URL", ""), get)
    if not channel:
        return f"no trading channel: set {CHANNEL_ENV} in .env"
    return token, int(owner), channel


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(".env")
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
    cfg = config()
    if isinstance(cfg, str):
        print(f"discord_bot: {cfg} - not starting.")
        return 0                                  # launchd: success = no restart loop
    token, owner, channel = cfg
    import discord
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    cmds = OwnerCommands(SellDesk(), owner, channel)

    @client.event
    async def on_ready():
        logger.info(f"connected as {client.user}; owner {owner}, channel {channel}")

    @client.event
    async def on_message(message):
        if message.author == client.user:
            return
        await cmds.on_message(message, message.channel.send)

    client.run(token, log_handler=None)
    return 1                                      # a disconnect that ends run(): restart


if __name__ == "__main__":
    sys.exit(main())
