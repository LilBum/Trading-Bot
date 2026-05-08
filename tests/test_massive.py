from __future__ import annotations

import pandas as pd

import pytest

from src.data.massive import MassiveBarsClient, _map_interval


class _DummyResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("http error")

    def json(self) -> dict:
        return self._payload


def test_map_interval():
    assert _map_interval("5m") == (5, "minute")
    assert _map_interval("1h") == (1, "hour")
    assert _map_interval("2d") == (2, "day")


def test_massive_bars_client_parses_payload(monkeypatch):
    payload = {
        "status": "OK",
        "results": [
            {"t": 1700000000000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100},
            {"t": 1700000060000, "o": 1.6, "h": 2.1, "l": 1.4, "c": 2.0, "v": 120},
        ],
    }

    def _fake_get(*_args, **_kwargs):
        return _DummyResponse(payload)

    monkeypatch.setattr("requests.get", _fake_get)
    client = MassiveBarsClient(api_key="key", base_url="https://example.com")
    df = client.get_intraday_bars("GLD", "1d", "1m")
    assert not df.empty
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert isinstance(df.index, pd.DatetimeIndex)


def test_massive_allows_delayed_status(monkeypatch):
    payload = {
        "status": "DELAYED",
        "results": [
            {"t": 1700000000000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100},
        ],
    }

    def _fake_get(*_args, **_kwargs):
        return _DummyResponse(payload)

    monkeypatch.setattr("requests.get", _fake_get)
    client = MassiveBarsClient(api_key="key", base_url="https://example.com")
    df = client.get_intraday_bars("GLD", "1d", "1m")
    assert not df.empty


def test_massive_requires_key():
    with pytest.raises(RuntimeError):
        MassiveBarsClient(api_key="", base_url="https://example.com")


def test_massive_requires_base_url():
    with pytest.raises(RuntimeError):
        MassiveBarsClient(api_key="key", base_url="")
