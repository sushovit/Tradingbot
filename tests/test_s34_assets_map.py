"""get_assets_map survives the SDK failing to parse the asset list
("5 validation errors for list[Asset]", 2026-10-20) by reading raw JSON."""

import pytest
from pydantic import TypeAdapter, ValidationError

import broker as B


def sdk_validation_error():
    from typing import List
    from alpaca.trading.models import Asset
    try:
        TypeAdapter(List[Asset]).validate_python([{"symbol": "X"}])
    except ValidationError as e:
        return e
    raise AssertionError("expected a ValidationError")


RAW = [{"symbol": "AAPL", "tradable": True, "exchange": "NASDAQ", "name": "Apple"},
       {"symbol": "OTCX", "tradable": True, "exchange": "OTC", "name": "Pink"},
       {"symbol": "ZZZ", "tradable": False, "exchange": "NYSE", "name": "Other"}]


class FakeTrading:
    def __init__(self, fail_with=None, models=None):
        self.fail_with, self.models, self.raw_calls = fail_with, models, []

    def get_all_assets(self, req):
        if self.fail_with:
            raise self.fail_with
        return self.models

    def get(self, path, data=None):
        self.raw_calls.append((path, data))
        return RAW


def make(trading):
    b = B.Broker.__new__(B.Broker)
    b.trading = trading
    return b


def test_sdk_validation_error_falls_back_to_raw_json(monkeypatch):
    monkeypatch.setattr(B.time, "sleep", lambda s: None)
    t = FakeTrading(fail_with=sdk_validation_error())
    out = make(t).get_assets_map(["AAPL", "OTCX"])
    assert out == {"AAPL": {"tradable": True, "exchange": "NASDAQ", "name": "Apple"},
                   "OTCX": {"tradable": True, "exchange": "OTC", "name": "Pink"}}
    path, params = t.raw_calls[0]
    assert path == "/assets" and params["status"] == "active" \
        and params["asset_class"] == "us_equity"


def test_parsed_models_still_work_and_exchange_is_plain_text():
    from alpaca.trading.enums import AssetExchange
    from types import SimpleNamespace as N
    t = FakeTrading(models=[N(symbol="AAPL", tradable=True,
                              exchange=AssetExchange.NASDAQ, name="Apple")])
    out = make(t).get_assets_map(["AAPL"])
    assert out["AAPL"]["exchange"] == "NASDAQ" and t.raw_calls == []


def test_a_network_error_is_retried_then_raised(monkeypatch):
    monkeypatch.setattr(B.time, "sleep", lambda s: None)
    t = FakeTrading(fail_with=ConnectionError("down"))
    with pytest.raises(B.BrokerError):
        make(t).get_assets_map(["AAPL"])
    assert t.raw_calls == []


def test_otc_is_still_recognised_by_the_universe_filter():
    assert B._enum_text("OTC").upper().endswith("OTC")
