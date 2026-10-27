"""S18 Discord sell: owner-only commands, YES within 60 s, the dashboard's
sell path. A fake Discord client and a fake desk - no network, no orders."""

import asyncio
import os
from types import SimpleNamespace

import discord_bot as db

OWNER, CHANNEL, OTHER = 111, 222, 999


def msg(text, author=OWNER, channel=CHANNEL):
    return SimpleNamespace(content=text, author=SimpleNamespace(id=author),
                           channel=SimpleNamespace(id=channel))


class FakeDesk:
    def __init__(self, open_=True, alive=True, tracked=True, held=("IBM",)):
        self.open_, self.alive, self.tracked_, self.held = open_, alive, tracked, list(held)
        self.queued = []

    def market_open(self):
        return self.open_

    def worker_alive(self):
        return self.alive

    def tracked(self, t):
        return self.tracked_

    def positions(self):
        return [{"ticker": t, "qty": 4.0, "entry": 250.42, "last": 252.95, "pnl": 10.12,
                 "stop": 250.42, "target": 271.28} for t in self.held]

    def queue_sell(self, t):
        self.queued.append(t)
        self.held.remove(t)
        return f"sell_{t}.command"

    def sell_result(self, t):
        return (t not in self.held), 252.90, 9.92


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def run(cmds, *texts, **kw):
    out = []

    async def reply(text, buttons=None):
        out.append(text)

    async def go():
        for t in texts:
            await cmds.on_message(msg(t, **kw), reply)
        if cmds.tasks:                       # confirm/expiry tasks; a 60 s window is not waited out
            _done, rest = await asyncio.wait(cmds.tasks, timeout=1)
            for task in rest:
                task.cancel()
    asyncio.run(go())
    return out


def make(desk=None, **kw):
    kw.setdefault("confirm_s", 0.05)
    kw.setdefault("fill_wait_s", 0.05)
    kw.setdefault("fill_poll_s", 0.01)
    return db.OwnerCommands(desk or FakeDesk(), OWNER, CHANNEL, **kw)


def test_everyone_else_and_other_channels_are_ignored_silently():
    desk = FakeDesk()
    cmds = make(desk)
    assert run(cmds, "!positions", "!sell IBM", "YES", author=OTHER) == []
    assert run(cmds, "!sell IBM", "YES", channel=OTHER) == []
    assert desk.queued == []


def test_positions_table_has_qty_entry_now_pnl_stop_target():
    (out,) = run(make(), "!positions")
    assert out == ("📊 Open positions\n"
                   "**IBM** 4 sh @ $250.42 → $252.95 · P&L **+$10.12**\n"
                   "stop $250.42 · target $271.28")
    assert max(len(line) for line in out.splitlines()) <= 60       # readable on a phone
    assert db.positions_text([]) == "📊 No open positions."


def test_sell_prompts_then_yes_queues_the_dashboard_path_and_confirms():
    desk = FakeDesk()
    clock = Clock()
    out = run(make(desk, clock=clock, confirm_s=60), "!sell ibm", "YES")
    assert out[0].startswith("⚠️ Sell ALL 4 IBM at market? Now $252.95") and "Press **Yes, sell** within 60 s (or type YES)" in out[0]
    assert "P&L +10.12" in out[0]
    assert desk.queued == ["IBM"]
    assert out[1].startswith("✅ Sell of 4 IBM sent to the worker")
    assert out[2] == "💰 SOLD IBM at $252.90 · P&L +9.92 · journaled as Owner close (Discord)."


def test_no_yes_within_the_window_cancels():
    desk = FakeDesk()
    out = run(make(desk, confirm_s=0.02), "!sell IBM")
    assert out[-1] == "⌛ No YES within 0.02 s - sell of IBM cancelled." and desk.queued == []


def test_late_yes_is_refused():
    desk, clock = FakeDesk(), Clock()
    cmds = make(desk, clock=clock, confirm_s=60)
    out = []

    async def reply(t, buttons=None):
        out.append(t)

    async def go():
        await cmds.on_message(msg("!sell IBM"), reply)
        clock.t = 61
        await cmds.on_message(msg("YES"), reply)
        for task in cmds.tasks:
            task.cancel()
    asyncio.run(go())
    assert out[-1].startswith("⌛ Too late") and desk.queued == []


def test_anything_but_yes_cancels_and_lowercase_yes_does_not_sell():
    desk = FakeDesk()
    out = run(make(desk, confirm_s=60), "!sell IBM", "yes")
    assert out[1] == "Sell of IBM cancelled." and desk.queued == []


def test_market_closed_unknown_ticker_and_unmanaged_are_refused():
    assert run(make(FakeDesk(open_=False)), "!sell IBM")[0].startswith("⛔ The market is closed")
    assert run(make(), "!sell AAPL")[0] == "⛔ No open position in AAPL."
    assert "dashboard Sell button" in run(make(FakeDesk(alive=False)), "!sell IBM")[0]
    assert "dashboard Sell button" in run(make(FakeDesk(tracked=False)), "!sell IBM")[0]


def test_yes_rechecks_the_market_before_selling():
    desk = FakeDesk()
    cmds = make(desk, confirm_s=60)
    out = []

    async def reply(t, buttons=None):
        out.append(t)

    async def go():
        await cmds.on_message(msg("!sell IBM"), reply)
        desk.open_ = False
        await cmds.on_message(msg("YES"), reply)
        for task in cmds.tasks:
            task.cancel()
    asyncio.run(go())
    assert out[-1].startswith("⛔ Not sold: The market is closed") and desk.queued == []


# ----------------------------------------------------------------- same path as the dashboard

def test_command_file_is_the_dashboards_and_journals_owner_close_discord(tmp_path):
    import streamlit_app as app
    desk = db.SellDesk(broker=SimpleNamespace(), root=str(tmp_path))
    path = desk.queue_sell("IBM")
    assert os.path.basename(path) == app.SELL_COMMAND.format("IBM")
    text = open(path, encoding="utf-8").read()
    assert app.owner_sell_reason(text) == app.OWNER_CLOSE_DISCORD == db.OWNER_CLOSE_DISCORD
    assert app.owner_sell_reason("2026-10-27T10:00:00-04:00") == app.OWNER_CLOSE   # dashboard file
    src = open("streamlit_app.py", encoding="utf-8").read()
    assert "reason=owner_sell_reason(_cmd)" in src


def test_the_bot_has_no_order_code():
    src = open("discord_bot.py", encoding="utf-8").read()
    for bad in ("submit", "close_position", "cancel_order", "replace_", "OrderRequest",
                "owner_close(", "import streamlit_app"):
        assert bad not in src, bad


def test_config_requires_token_owner_and_a_channel():
    assert "not configured" in db.config({})
    assert "not configured" in db.config({db.TOKEN_ENV: "t", db.OWNER_ENV: "abc"})
    env = {db.TOKEN_ENV: "t", db.OWNER_ENV: "111", db.CHANNEL_ENV: "222"}
    assert db.config(env) == ("t", 111, 222)
    hook = {db.TOKEN_ENV: "t", db.OWNER_ENV: "111", "DISCORD_WEBHOOK_URL": "https://x/fake"}
    fake_get = lambda url, timeout: SimpleNamespace(json=lambda: {"channel_id": "333"})
    assert db.config(hook, get=fake_get) == ("t", 111, 333)
    bad_get = lambda url, timeout: (_ for _ in ()).throw(OSError("down"))
    assert "no trading channel" in db.config(hook, get=bad_get)


# ----------------------------------------------------------------- [Yes, sell] [Cancel] buttons

def _prompted(desk, confirm_s=60):
    cmds = make(desk, confirm_s=confirm_s)
    out, btns = [], {}

    async def reply(text, buttons=None):
        out.append(text)
        if buttons:
            btns.update(buttons)
    return cmds, out, btns, reply


def test_prompt_carries_yes_and_cancel_buttons_and_owner_yes_sells():
    desk = FakeDesk()
    cmds, out, btns, reply = _prompted(desk)

    async def go():
        await cmds.on_message(msg("!sell IBM"), reply)
        assert set(btns) == {"yes", "no"} and "Press **Yes, sell**" in out[0]
        assert await btns["yes"](OWNER) == "selling"
        _done, rest = await asyncio.wait(cmds.tasks, timeout=1)
        for t in rest:
            t.cancel()
    asyncio.run(go())
    assert desk.queued == ["IBM"] and out[-1].startswith("💰 SOLD IBM")


def test_someone_elses_click_does_nothing_and_cancel_cancels():
    desk = FakeDesk()
    cmds, out, btns, reply = _prompted(desk)

    async def go():
        await cmds.on_message(msg("!sell IBM"), reply)
        assert await btns["yes"](OTHER) == "not_owner"
        assert await btns["no"](OWNER) == "cancelled"
        assert await btns["yes"](OWNER) == "expired"          # an old prompt
        for t in cmds.tasks:
            t.cancel()
    asyncio.run(go())
    assert desk.queued == [] and out[-1] == "Sell of IBM cancelled."


def test_button_after_the_window_is_expired():
    desk, clock = FakeDesk(), Clock()
    cmds = make(desk, confirm_s=60, clock=clock)
    btns = {}

    async def reply(text, buttons=None):
        if buttons:
            btns.update(buttons)

    async def go():
        await cmds.on_message(msg("!sell IBM"), reply)
        clock.t = 61
        assert await btns["yes"](OWNER) == "expired"
        for t in cmds.tasks:
            t.cancel()
    asyncio.run(go())
    assert desk.queued == []


class FakeResponse:
    def __init__(self):
        self.calls = []

    async def send_message(self, text, ephemeral=False):
        self.calls.append(("send", text, ephemeral))

    async def edit_message(self, **kw):
        self.calls.append(("edit", kw))


def test_discord_view_answers_at_once_and_blocks_other_users():
    View = db.confirm_view_class()
    pressed = []

    async def yes(uid):
        pressed.append(uid)
        return "selling"

    async def go():
        view = View({"yes": yes, "no": yes}, OWNER)
        assert [c.label for c in view.children] == ["Yes, sell", "Cancel"]
        edits = []

        async def edit(**kw):
            edits.append(kw)
        stranger = SimpleNamespace(user=SimpleNamespace(id=OTHER), response=FakeResponse(),
                                   message=SimpleNamespace(content="prompt", edit=edit))
        await view._click(stranger, "yes")
        assert stranger.response.calls == [("send", "Only the owner can do this.", True)]
        assert pressed == [] and not any(c.disabled for c in view.children)
        owner = SimpleNamespace(user=SimpleNamespace(id=OWNER), response=FakeResponse(),
                                message=SimpleNamespace(content="prompt", edit=edit))
        await view._click(owner, "yes")
        assert owner.response.calls[0][0] == "edit"          # answered before the sell
        assert pressed == [OWNER] and all(c.disabled for c in view.children)
        assert edits[-1]["content"] == "prompt\n**✅ Sell sent**"
    asyncio.run(go())
