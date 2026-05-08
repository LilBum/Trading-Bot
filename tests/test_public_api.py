from datetime import datetime, timezone

from src.data.public_api import choose_expiration, parse_osi_symbol


def test_parse_osi_symbol():
    parsed = parse_osi_symbol("AAPL  240119C00150000")
    assert parsed["root"] == "AAPL"
    assert parsed["expiration"] == "2024-01-19"
    assert parsed["option_type"] == "CALL"
    assert parsed["strike"] == 150.0


def test_choose_expiration_prefers_target():
    expirations = ["2024-01-05", "2024-01-19", "2024-02-02"]
    now = datetime(2024, 1, 4, tzinfo=timezone.utc)
    chosen = choose_expiration(expirations, target_dte=7, now=now)
    assert chosen == "2024-01-19"
