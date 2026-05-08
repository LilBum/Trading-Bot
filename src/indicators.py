import numpy as np
import pandas as pd


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    typical_price = (df["High"] + df["Low"] + df["Close"]) / 3.0
    cumulative_pv = (typical_price * df["Volume"]).cumsum()
    cumulative_volume = df["Volume"].cumsum().replace(0, np.nan)
    vwap = cumulative_pv / cumulative_volume
    return vwap


def compute_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def vwap_slope(vwap: pd.Series, lookback: int) -> float:
    if len(vwap) < lookback + 1:
        return 0.0
    recent = vwap.tail(lookback + 1)
    return (recent.iloc[-1] - recent.iloc[0]) / lookback


def resample_bars(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    ohlc = {
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }
    resampled = df.resample(rule).apply(ohlc).dropna()
    return resampled


def resample_5m(df: pd.DataFrame) -> pd.DataFrame:
    return resample_bars(df, "5min")


def compute_atr(df: pd.DataFrame, period: int) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=float)
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()
