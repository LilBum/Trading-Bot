import pandas as pd

from src.data.webull import _bars_to_dataframe, _extract_bars


def test_extract_bars_from_payload():
    payload = {"data": {"bars": [{"t": 1700000000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100}]}}
    bars = _extract_bars(payload)
    assert isinstance(bars, list)
    assert bars


def test_bars_to_dataframe_parses_dict_rows():
    bars = [{"t": 1700000000, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 100}]
    df = _bars_to_dataframe(bars)
    assert not df.empty
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert isinstance(df.index[0], pd.Timestamp)
