"""CEO 2026-10-23: the dashboard SELL needs a PIN (hash-only, fail closed, lockout)."""

import sell_pin

FAKE = {"SELL_PIN_HASH": sell_pin.make_hash("1234", "fakesalt")}


def test_no_hash_fails_closed(tmp_path):
    ok, msg = sell_pin.check("1234", env={}, path=str(tmp_path / "s.json"))
    assert not ok and "disabled" in msg
    assert not sell_pin.configured({})


def test_right_pin_passes_and_resets(tmp_path):
    p = str(tmp_path / "s.json")
    assert not sell_pin.check("0000", env=FAKE, path=p)[0]
    assert sell_pin.check("1234", env=FAKE, path=p) == (True, "")
    assert sell_pin._state(p)["fails"] == 0


def test_lockout_after_five_and_survives_reload(tmp_path):
    p = str(tmp_path / "s.json")
    for _ in range(sell_pin.MAX_TRIES):
        ok, msg = sell_pin.check("9999", env=FAKE, path=p, now=1000)
    assert "locked" in msg
    # even the right PIN is refused while locked (state is on disk, not in the page)
    ok, msg = sell_pin.check("1234", env=FAKE, path=p, now=1000 + 60)
    assert not ok and "locked" in msg
    assert sell_pin.locked_for(p, now=1000 + 60) > 0
    # after 15 minutes it works again
    assert sell_pin.check("1234", env=FAKE, path=p, now=1000 + sell_pin.LOCK_S + 1)[0]


def test_hash_has_no_plain_pin():
    h = sell_pin.make_hash("482913")
    assert "482913" not in h and "$" in h


def test_dashboard_checks_pin_before_selling():
    src = open("streamlit_app.py", encoding="utf-8").read()
    i = src.index("def _render_owner_sell(")
    body = src[i:src.index("\ndef ", i + 10)]
    assert 'type="password"' in body and "sell_pin.configured()" in body
    assert body.index("sell_pin.check(pin)") < body.index("_execute_owner_sell(")


def test_pin_state_and_env_are_gitignored():
    gi = open(".gitignore", encoding="utf-8").read().split()
    assert "sell_pin_state.json" in gi and ".env" in gi
