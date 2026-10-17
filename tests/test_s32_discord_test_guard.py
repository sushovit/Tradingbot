"""INCIDENT 2026-10-17 02:36 ET: a test posted "BUY MRVL 3 @ $100" to the
real Discord channel. The conftest guard must make that impossible."""

import os

import pytest
import requests

import alerts
import trade_alerts

from tests.test_price_freshness import FreshnessBroker, _sheet  # noqa: F401
import orders


def test_every_discord_webhook_variable_is_blank():
    for k, v in os.environ.items():
        if k.startswith("DISCORD_WEBHOOK"):
            assert v == "", k


def test_post_async_is_a_recorder_and_starts_no_thread(discord_posts):
    t = trade_alerts.alert_buy(ticker="MRVL", qty=3, fill=100.0, ts=None)
    assert not t.is_alive()
    assert len(discord_posts) == 1


def test_a_real_http_post_to_discord_raises():
    with pytest.raises(Exception) as e:
        requests.post("https://discord.com/api/webhooks/1/fake-token",
                      json={"content": "x"}, timeout=1)
    assert "Discord" in str(e.value)


def test_the_mrvl_test_path_is_recorded_not_sent(temp_journal, tmp_path,
                                                monkeypatch, discord_posts):
    """test_price_freshness::test_fresh_price_passes - the 10-17 leak."""
    monkeypatch.setattr(orders, "POSITIONS_STATE_FILE", str(tmp_path / "p.json"))
    import universe
    monkeypatch.setattr(universe, "UNIVERSE_FILE", str(tmp_path / "u.json"))
    broker = FreshnessBroker(live_price=101.0)
    orders.ingest(_sheet(tmp_path, entry=100.0), broker=broker)
    assert broker.submitted == ["MRVL"]
    assert len(discord_posts) == 1 and alerts.pending() == 0


@pytest.mark.real_post_async
def test_a_real_alert_thread_finishes_before_the_guards_lift(monkeypatch):
    sent = []
    alerts.post_async({"title": "x", "color": 1, "fields": []},
                      sender=lambda p, u: sent.append(os.getenv("DISCORD_WEBHOOK_URL")))
    alerts.drain(timeout=2)
    assert sent == [""]
