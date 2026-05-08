from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from ..indicators import compute_atr, compute_ema, compute_vwap, resample_bars, resample_5m, vwap_slope
from ..models import SignalDecision
from ..regime import count_vwap_crosses, time_filter_allows
from ..sentiment import load_sentiment_snapshot


class VwapPullbackSignalEngine:
    def __init__(self, strategy_cfg: dict) -> None:
        self.strategy_cfg = strategy_cfg

    def evaluate(self, symbol: str, df_1m: pd.DataFrame, config: dict) -> SignalDecision:
        timestamp = df_1m.index[-1].to_pydatetime()
        decision_time_utc = datetime.now(timezone.utc).isoformat()

        data_quality = config.get("data_quality", {})
        max_bar_age = data_quality.get("max_bar_age_minutes")
        allow_stale_when_closed = data_quality.get("allow_stale_when_closed", False)
        if max_bar_age is not None:
            if timestamp.tzinfo is None:
                bar_time_utc = timestamp.replace(tzinfo=timezone.utc)
            else:
                bar_time_utc = timestamp.astimezone(timezone.utc)
            age_minutes = (datetime.now(timezone.utc) - bar_time_utc).total_seconds() / 60.0
            if age_minutes > max_bar_age:
                if allow_stale_when_closed and self._market_closed():
                    reject_reasons = []
                else:
                    reject_reasons = [f"Stale market data ({age_minutes:.1f}m old)"]
            else:
                reject_reasons = []
        else:
            reject_reasons = []

        vwap_series = compute_vwap(df_1m)
        close_series = df_1m["Close"]
        close_last = close_series.iloc[-1]

        warnings: list[str] = []
        if max_bar_age is not None and allow_stale_when_closed and self._market_closed():
            warnings.append("Market closed: using latest available bars")
        atr_value = None
        atr_pct = None
        atr_cfg = self.strategy_cfg.get("atr", {})
        if atr_cfg.get("enabled", False):
            atr_interval = atr_cfg.get("interval", "5min")
            atr_period = atr_cfg.get("period", 14)
            atr_df = resample_bars(df_1m, atr_interval) if atr_interval else df_1m
            if len(atr_df) >= atr_period + 1:
                atr_series = compute_atr(atr_df, atr_period)
                if not atr_series.empty and pd.notna(atr_series.iloc[-1]):
                    atr_value = float(atr_series.iloc[-1])
                if atr_value is not None and close_last > 0:
                    atr_pct = (atr_value / close_last) * 100.0
            else:
                warnings = ["Not enough data for ATR calculation"]

        vwap_slope_value = vwap_slope(vwap_series, self.strategy_cfg["vwap_slope_lookback"])
        resampled_5m = resample_5m(df_1m)

        min_ema_bars = max(self.strategy_cfg["ema_fast"], self.strategy_cfg["ema_slow"])
        has_ema = len(resampled_5m) >= min_ema_bars
        if not has_ema:
            reject_reasons.append("Not enough 5m data for EMA trend")

        ema_fast = compute_ema(resampled_5m["Close"], self.strategy_cfg["ema_fast"]) if has_ema else pd.Series([0.0])
        ema_slow = compute_ema(resampled_5m["Close"], self.strategy_cfg["ema_slow"]) if has_ema else pd.Series([0.0])

        vwap_last = vwap_series.iloc[-1]
        ema_fast_last = ema_fast.iloc[-1]
        ema_slow_last = ema_slow.iloc[-1]

        crosses = count_vwap_crosses(close_series, vwap_series, self.strategy_cfg["chop_lookback_minutes"])
        max_crosses = self.strategy_cfg.get("max_vwap_crosses")
        open_override = self.strategy_cfg.get("max_vwap_crosses_open")
        open_window = self.strategy_cfg.get("max_vwap_crosses_open_minutes")
        minutes_since_open = self._minutes_since_open(timestamp)
        if (
            open_override is not None
            and open_window is not None
            and minutes_since_open is not None
            and minutes_since_open <= float(open_window)
        ):
            max_crosses = int(open_override)
        if max_crosses is not None and crosses >= max_crosses:
            reject_reasons.append("Chop regime detected")

        time_ok, time_reason = time_filter_allows(
            timestamp,
            self.strategy_cfg["time_filters"]["avoid_open_minutes"],
            self.strategy_cfg["time_filters"]["avoid_close_minutes"],
            self.strategy_cfg["time_filters"]["scalp_mode"],
        )
        if not time_ok and time_reason:
            reject_reasons.append(time_reason)

        had_pullback = False
        momentum_up = 0.0
        momentum_down = 0.0
        min_pullback_bars = self.strategy_cfg["pullback_lookback"] + 1
        if len(df_1m) < min_pullback_bars:
            reject_reasons.append("Not enough 1m data for pullback check")
        else:
            tolerance = close_last * (self.strategy_cfg["pullback_vwap_tolerance_pct"] / 100.0)
            pullback_slice = df_1m.tail(min_pullback_bars)
            vwap_pullback = vwap_series.tail(min_pullback_bars)
            near_vwap = (pullback_slice["Close"] - vwap_pullback).abs() <= tolerance
            had_pullback = bool(near_vwap.any())
            prev_close = pullback_slice["Close"].iloc[-2]
            momentum_up = ((close_last - prev_close) / prev_close) * 100.0
            momentum_down = ((prev_close - close_last) / prev_close) * 100.0

        is_uptrend = has_ema and close_last > vwap_last and vwap_slope_value > 0 and ema_fast_last > ema_slow_last
        is_downtrend = has_ema and close_last < vwap_last and vwap_slope_value < 0 and ema_fast_last < ema_slow_last

        call_setup = (
            is_uptrend
            and had_pullback
            and close_last > vwap_last
            and momentum_up >= self.strategy_cfg["momentum_min_pct"]
        )
        put_setup = (
            is_downtrend
            and had_pullback
            and close_last < vwap_last
            and momentum_down >= self.strategy_cfg["momentum_min_pct"]
        )

        direction = "NONE"
        if call_setup:
            direction = "CALL"
        elif put_setup:
            direction = "PUT"
        else:
            reject_reasons.append("No valid setup conditions")

        higher_timeframe_trend = None
        htf_cfg = self.strategy_cfg.get("higher_timeframe", {})
        if htf_cfg.get("enabled", False):
            htf_interval = htf_cfg.get("interval", "15min")
            htf_fast = htf_cfg.get("ema_fast", 9)
            htf_slow = htf_cfg.get("ema_slow", 21)
            htf_df = resample_bars(df_1m, htf_interval)
            min_htf_bars = max(htf_fast, htf_slow)
            if len(htf_df) < min_htf_bars:
                if direction != "NONE" and htf_cfg.get("require_alignment", False):
                    reject_reasons.append("Not enough higher-timeframe data")
                else:
                    warnings.append("Not enough higher-timeframe data")
            else:
                htf_fast_series = compute_ema(htf_df["Close"], htf_fast)
                htf_slow_series = compute_ema(htf_df["Close"], htf_slow)
                if htf_fast_series.iloc[-1] > htf_slow_series.iloc[-1]:
                    higher_timeframe_trend = "UP"
                elif htf_fast_series.iloc[-1] < htf_slow_series.iloc[-1]:
                    higher_timeframe_trend = "DOWN"
                else:
                    higher_timeframe_trend = "FLAT"
            if htf_cfg.get("require_alignment", False) and direction != "NONE":
                if higher_timeframe_trend is None:
                    reject_reasons.append("Higher-timeframe trend unavailable")
                elif direction == "CALL" and higher_timeframe_trend != "UP":
                    reject_reasons.append("Higher-timeframe trend misaligned")
                elif direction == "PUT" and higher_timeframe_trend != "DOWN":
                    reject_reasons.append("Higher-timeframe trend misaligned")

        sentiment_snapshot, sentiment_warnings = load_sentiment_snapshot(config)
        warnings.extend(sentiment_warnings)
        sentiment_value = sentiment_snapshot.value if sentiment_snapshot else None
        sentiment_label = sentiment_snapshot.label if sentiment_snapshot else None
        sentiment_source = sentiment_snapshot.source if sentiment_snapshot else None
        sentiment_cfg = config.get("sentiment", {})
        if sentiment_cfg.get("enabled", False) and direction != "NONE":
            sentiment_mode = sentiment_cfg.get("mode", "advisory")
            if sentiment_snapshot is None:
                if sentiment_cfg.get("fail_closed", False) and sentiment_mode == "hard_block":
                    reject_reasons.append("Sentiment data unavailable")
                else:
                    warnings.append("Sentiment data unavailable")
            else:
                filters = sentiment_cfg.get("filters", {})
                block_calls_above = filters.get("block_calls_above")
                block_puts_below = filters.get("block_puts_below")
                if direction == "CALL" and block_calls_above is not None and sentiment_value >= block_calls_above:
                    if sentiment_mode == "hard_block":
                        reject_reasons.append("Sentiment filter: extreme greed")
                    else:
                        warnings.append("Sentiment advisory: extreme greed")
                if direction == "PUT" and block_puts_below is not None and sentiment_value <= block_puts_below:
                    if sentiment_mode == "hard_block":
                        reject_reasons.append("Sentiment filter: extreme fear")
                    else:
                        warnings.append("Sentiment advisory: extreme fear")

        entry_trigger = "Reclaim VWAP with momentum in trend" if direction != "NONE" else "No setup"
        invalidation = "1m close back across VWAP or break pullback structure"
        sizing_cfg = config.get("position_sizing", {})
        stop_mode = sizing_cfg.get("stop_mode", "premium_pct")
        premium_stop_pct = sizing_cfg.get("premium_stop_pct", 0.25)
        if stop_mode == "delta_atr" and atr_value is not None:
            atr_multiplier = sizing_cfg.get("atr_stop_multiplier", 1.0)
            premium_stop = f"{atr_multiplier:.2f}x ATR ({atr_value:.2f} underlying)"
        else:
            premium_stop = f"-{int(premium_stop_pct * 100)}% option premium"
        targets = "+30% partial, +70% runner"

        regime_info_parts = [
            f"VWAP slope {vwap_slope_value:.4f}",
            f"EMA5m {ema_fast_last:.2f}/{ema_slow_last:.2f}",
            f"VWAP crosses {crosses}/{self.strategy_cfg['chop_lookback_minutes']}",
        ]
        if higher_timeframe_trend:
            regime_info_parts.append(f"HTF {higher_timeframe_trend}")
        if atr_pct is not None:
            regime_info_parts.append(f"ATR {atr_pct:.2f}%")
        if sentiment_snapshot:
            regime_info_parts.append(f"Sentiment {sentiment_value:.0f} {sentiment_label}")
        regime_info = ", ".join(regime_info_parts)

        return SignalDecision(
            symbol=symbol,
            direction=direction,
            setup="VWAP Trend Pullback",
            entry_trigger=entry_trigger,
            invalidation=invalidation,
            premium_stop=premium_stop,
            targets=targets,
            decision_time_utc=decision_time_utc,
            bar_timestamp=timestamp,
            regime_info=regime_info,
            atr_value=atr_value,
            atr_pct=atr_pct,
            higher_timeframe_trend=higher_timeframe_trend,
            sentiment_value=sentiment_value,
            sentiment_label=sentiment_label,
            sentiment_source=sentiment_source,
            reject_reasons=reject_reasons,
            warnings=warnings,
        )

    def _market_closed(self) -> bool:
        eastern = ZoneInfo("America/New_York")
        now = datetime.now(eastern)
        if now.weekday() >= 5:
            return True
        market_open = time(9, 30)
        market_close = time(16, 0)
        return not (market_open <= now.time() <= market_close)

    def _minutes_since_open(self, bar_ts: datetime) -> float | None:
        eastern = ZoneInfo("America/New_York")
        if bar_ts.tzinfo is None:
            bar_ts = bar_ts.replace(tzinfo=eastern)
        else:
            bar_ts = bar_ts.astimezone(eastern)
        if bar_ts.weekday() >= 5:
            return None
        market_open = bar_ts.replace(hour=9, minute=30, second=0, microsecond=0)
        if bar_ts < market_open:
            return None
        return (bar_ts - market_open).total_seconds() / 60.0
