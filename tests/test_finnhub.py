from src.data.finnhub import _period_seconds, _resolution_from_interval


def test_resolution_from_interval():
    assert _resolution_from_interval("1m") == "1"
    assert _resolution_from_interval("5m") == "5"
    assert _resolution_from_interval("15m") == "15"
    assert _resolution_from_interval("30m") == "30"
    assert _resolution_from_interval("60m") == "60"
    assert _resolution_from_interval("1h") == "60"
    assert _resolution_from_interval("1d") == "D"


def test_period_seconds():
    assert _period_seconds("1d") == 86400
    assert _period_seconds("5d") == 5 * 86400
    assert _period_seconds("2mo") == 2 * 30 * 86400
    assert _period_seconds("1y") == 365 * 86400
