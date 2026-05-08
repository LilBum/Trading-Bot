from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from ..models import InstrumentSelection, OptionContract, OptionGreeks


def _safe_float(value: object) -> float:
    if value is None or pd.isna(value):
        return 0.0
    return float(value)


def _safe_int(value: object) -> int:
    if value is None or pd.isna(value):
        return 0
    return int(value)


def _norm_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _norm_pdf(value: float) -> float:
    return math.exp(-0.5 * value * value) / math.sqrt(2.0 * math.pi)


class OptionInstrumentService:
    def __init__(self, options_cfg: dict) -> None:
        self.options_cfg = options_cfg
        self.delta_target = options_cfg.get("delta_target", 0.5)
        self.delta_tolerance = options_cfg.get("delta_tolerance", 0.2)
        self.oi_score_scale = options_cfg.get("oi_score_scale", 3.0)
        self.volume_score_scale = options_cfg.get("volume_score_scale", 3.0)
        self.risk_free_rate = options_cfg.get("risk_free_rate", 0.02)
        self.gamma_warning_days = options_cfg.get("gamma_warning_days", 2)
        self.atm_threshold_pct = options_cfg.get("atm_threshold_pct", 0.5)
        self.max_candidates = options_cfg.get("max_candidates", 20)
        self.top_candidate_count = options_cfg.get("top_candidate_count", 3)
        self.scoring = options_cfg.get("scoring", {})
        self.penalties = options_cfg.get("penalties", {})

    def select_contract(
        self,
        symbol: str,
        chain_data: tuple[str, pd.DataFrame] | None,
        direction: str,
        underlying_price: float,
        decision_time_utc: str,
    ) -> InstrumentSelection:
        if chain_data is None:
            return InstrumentSelection(None, reject_reasons=["Options chain data unavailable"])

        _, chain = chain_data
        subset = chain[chain["option_type"] == direction].copy()
        if subset.empty:
            return InstrumentSelection(None, reject_reasons=["No matching option contracts"])
        preference = self.options_cfg.get("moneyness_preference", "ATM_OR_1ITM")
        if preference == "ITM_ONLY" and "strike" in subset.columns:
            if direction == "CALL":
                subset = subset[subset["strike"] <= underlying_price]
            else:
                subset = subset[subset["strike"] >= underlying_price]
            if subset.empty:
                return InstrumentSelection(None, reject_reasons=["No ITM contracts available"])
        if "strike" in subset.columns:
            subset["strike_diff"] = (subset["strike"] - underlying_price).abs()
            if self.max_candidates > 0:
                subset = subset.sort_values("strike_diff").head(self.max_candidates)

        candidates: list[dict] = []
        valid_candidates: list[dict] = []
        iv_median = self._median_iv(subset, underlying_price)

        for _, row in subset.iterrows():
            option_contract = self._option_from_row(symbol, row, underlying_price, decision_time_utc)
            reject_reasons = self._liquidity_rejections(option_contract, row, self.options_cfg, iv_median)
            candidate = self._candidate_payload(option_contract, reject_reasons)
            candidates.append(candidate)
            if reject_reasons:
                continue
            candidate["score"] = self._score_option(option_contract, iv_median)
            valid_candidates.append(candidate)

        if not valid_candidates:
            return InstrumentSelection(
                None,
                reject_reasons=["No contracts passed liquidity filters"],
                top_candidates=self._top_candidates(candidates),
            )

        valid_candidates.sort(key=lambda item: item["score"], reverse=True)
        best = valid_candidates[0]
        option_contract = best["contract"]
        warnings = self._risk_warnings(option_contract, underlying_price)
        top_candidates = self._top_candidates(valid_candidates)

        return InstrumentSelection(
            option_contract,
            reject_reasons=[],
            warnings=warnings,
            top_candidates=top_candidates,
        )

    def _candidate_payload(self, option: OptionContract, reject_reasons: list[str]) -> dict:
        delta = option.greeks.delta
        age_seconds = self._quote_age_seconds(option.quote_time_utc)
        age_minutes = None if age_seconds is None else age_seconds / 60.0
        expected_move = self._expected_move(option)
        return {
            "contract": option,
            "symbol": option.symbol,
            "expiration": option.expiration,
            "strike": option.strike,
            "option_type": option.option_type,
            "mid": option.mid,
            "implied_volatility": option.implied_volatility,
            "spread_pct": option.spread_pct,
            "open_interest": option.open_interest,
            "volume": option.volume,
            "delta": delta,
            "quote_age_minutes": age_minutes,
            "quote_age_seconds": age_seconds,
            "expected_move": expected_move,
            "reject_reasons": reject_reasons,
            "score": 0.0,
        }

    def _top_candidates(self, candidates: list[dict]) -> list[dict]:
        top = sorted(candidates, key=lambda item: item.get("score", 0.0), reverse=True)
        trimmed: list[dict] = []
        for candidate in top[: self.top_candidate_count]:
            trimmed.append(
                {
                    "symbol": candidate["symbol"],
                    "expiration": candidate["expiration"],
                    "strike": candidate["strike"],
                    "option_type": candidate["option_type"],
                    "mid": candidate["mid"],
                    "implied_volatility": candidate.get("implied_volatility"),
                    "spread_pct": candidate["spread_pct"],
                    "open_interest": candidate["open_interest"],
                    "volume": candidate["volume"],
                    "delta": candidate["delta"],
                    "quote_age_minutes": candidate.get("quote_age_minutes"),
                    "score": candidate.get("score", 0.0),
                }
            )
        return trimmed

    def _option_from_row(
        self,
        symbol: str,
        row: pd.Series,
        underlying_price: float,
        decision_time_utc: str,
    ) -> OptionContract:
        bid = _safe_float(row.get("bid", 0.0))
        ask = _safe_float(row.get("ask", 0.0))
        last_price = _safe_float(row.get("last_price", 0.0))
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
        else:
            mid = last_price
        spread = ask - bid if bid > 0 and ask > 0 else 0.0
        spread_pct = (spread / mid) if mid > 0 else 0.0

        expiration = str(row.get("expiration", ""))
        time_to_expiry_days = self._time_to_expiry_days(expiration, decision_time_utc)
        iv = _safe_float(row.get("impliedVolatility", 0.0))
        quote_time_utc = self._quote_time_from_row(row, decision_time_utc)
        greeks = self._compute_greeks(
            underlying_price,
            _safe_float(row.get("strike", 0.0)),
            time_to_expiry_days,
            iv,
            self.risk_free_rate,
            str(row.get("option_type", "")),
        )

        return OptionContract(
            symbol=symbol,
            expiration=expiration,
            strike=_safe_float(row.get("strike", 0.0)),
            option_type=str(row.get("option_type", "")),
            bid=bid,
            ask=ask,
            mid=_safe_float(mid),
            implied_volatility=iv,
            spread=spread,
            spread_pct=spread_pct,
            nbbo_bid=bid,
            nbbo_ask=ask,
            open_interest=_safe_int(row.get("open_interest", 0)),
            volume=_safe_int(row.get("volume", 0)),
            last_price=last_price,
            underlying_price=underlying_price,
            time_to_expiry_days=time_to_expiry_days,
            quote_time_utc=quote_time_utc,
            greeks=greeks,
            option_symbol=row.get("option_symbol"),
        )

    def _liquidity_rejections(
        self,
        option: OptionContract,
        row: pd.Series,
        options_cfg: dict,
        iv_median: Optional[float],
    ) -> list[str]:
        reasons: list[str] = []
        require_nbbo = options_cfg.get("require_nbbo", False)
        allow_last_price = options_cfg.get("allow_last_price_without_nbbo", True)
        if option.bid <= 0 or option.ask <= 0:
            if require_nbbo or not allow_last_price:
                reasons.append("Missing bid/ask data")
                if require_nbbo:
                    return reasons
            elif option.mid <= 0:
                reasons.append("Missing bid/ask data")
                return reasons

        if option.mid <= 0:
            reasons.append("Option mid not available")
            return reasons

        if option.bid > 0 and option.ask > 0:
            if not (option.bid <= option.mid <= option.ask):
                reasons.append("Mid outside bid/ask")

        require_quote_time = options_cfg.get("require_quote_time", False)
        if require_quote_time:
            if self._row_quote_time_raw(row) is None:
                reasons.append("Quote timestamp missing")

        require_quote_size = options_cfg.get("require_quote_size", False)
        if require_quote_size:
            bid_size = _safe_int(row.get("bidSize", 0))
            ask_size = _safe_int(row.get("askSize", 0))
            if bid_size <= 0 or ask_size <= 0:
                reasons.append("Quote size missing")

        require_iv = options_cfg.get("require_iv_for_short_dte", False)
        short_dte_days = options_cfg.get("short_dte_threshold_days", 2)
        if require_iv and option.time_to_expiry_days <= short_dte_days and option.implied_volatility <= 0:
            reasons.append("Missing IV for short-dated option")

        iv_deviation_pct = options_cfg.get("iv_deviation_pct_max")
        iv_hard_reject = options_cfg.get("iv_deviation_hard_reject", False)
        if iv_deviation_pct is not None and iv_median is not None and option.implied_volatility > 0:
            deviation = abs(option.implied_volatility - iv_median) / iv_median * 100.0
            if deviation > iv_deviation_pct:
                if iv_hard_reject:
                    reasons.append("IV deviates from median")

        max_quote_age_seconds = options_cfg.get("max_quote_age_seconds")
        if max_quote_age_seconds is not None:
            age_seconds = self._quote_age_seconds(option.quote_time_utc)
            if age_seconds is None:
                reasons.append("Quote timestamp unavailable")
            elif age_seconds > max_quote_age_seconds:
                reasons.append("Quote too stale")
        else:
            max_quote_age = options_cfg.get("max_quote_age_minutes")
            if max_quote_age is not None:
                age_minutes = self._quote_age_minutes(option.quote_time_utc)
                if age_minutes is None:
                    reasons.append("Quote timestamp unavailable")
                elif age_minutes > max_quote_age:
                    reasons.append("Quote too stale")

        min_quote_size = options_cfg.get("min_quote_size", 0)
        if min_quote_size > 0:
            bid_size = _safe_int(row.get("bidSize", 0))
            ask_size = _safe_int(row.get("askSize", 0))
            if bid_size < min_quote_size or ask_size < min_quote_size:
                reasons.append("Quote size below minimum")

        max_spread = self._max_spread_for_option(option)
        if option.spread_pct > max_spread:
            reasons.append("Bid-ask spread too wide")
        if option.open_interest < options_cfg["min_open_interest"]:
            reasons.append("Open interest below minimum")
        if option.volume < options_cfg["min_volume"]:
            reasons.append("Volume below minimum")
        if option.mid < options_cfg["min_option_price"]:
            reasons.append("Option price too cheap")
        if option.mid > options_cfg["max_option_price"]:
            reasons.append("Option price too expensive")
        return reasons

    def _max_spread_for_option(self, option: OptionContract) -> float:
        max_spread = float(self.options_cfg.get("max_spread_pct", 0.2) or 0.0)
        tiers = self.options_cfg.get("max_spread_pct_tiers") or []
        try:
            sorted_tiers = sorted(
                (tier for tier in tiers if isinstance(tier, dict)),
                key=lambda item: float(item.get("max_price", 0.0) or 0.0),
            )
        except (TypeError, ValueError):
            sorted_tiers = []
        for tier in sorted_tiers:
            try:
                max_price = float(tier.get("max_price"))
                tier_spread = float(tier.get("max_spread_pct"))
            except (TypeError, ValueError):
                continue
            if option.mid <= max_price:
                max_spread = tier_spread
                break
        return max_spread

    def _risk_warnings(self, option: OptionContract, underlying_price: float) -> list[str]:
        warnings: list[str] = []
        if option.time_to_expiry_days <= self.gamma_warning_days and underlying_price > 0:
            strike_diff_pct = abs(option.strike - underlying_price) / underlying_price * 100.0
            if strike_diff_pct <= self.atm_threshold_pct:
                warnings.append("High gamma/theta risk (short-dated, near ATM)")
        if option.greeks.delta is None:
            warnings.append("Delta not available (missing IV)")
        return warnings

    def _score_option(self, option: OptionContract, iv_median: Optional[float]) -> float:
        max_spread = self._max_spread_for_option(option)
        spread_score = max(0.0, 1.0 - (option.spread_pct / max_spread)) if max_spread > 0 else 0.0

        min_oi = max(self.options_cfg["min_open_interest"], 1)
        oi_score = min(option.open_interest / (min_oi * self.oi_score_scale), 1.0)

        min_vol = max(self.options_cfg["min_volume"], 1)
        volume_score = min(option.volume / (min_vol * self.volume_score_scale), 1.0)

        delta_score = 0.0
        if option.greeks.delta is not None:
            delta = abs(option.greeks.delta)
            delta_diff = abs(delta - self.delta_target)
            delta_score = max(0.0, 1.0 - (delta_diff / self.delta_tolerance))

        price_score = 0.0
        price_range = self.options_cfg["max_option_price"] - self.options_cfg["min_option_price"]
        if price_range > 0:
            mid_target = self.options_cfg["min_option_price"] + price_range * 0.5
            price_score = max(0.0, 1.0 - abs(option.mid - mid_target) / price_range)

        expected_move_score = self._expected_move_score(option)
        quote_freshness_score = self._quote_freshness_score(option)
        iv_deviation_score = self._iv_deviation_score(option, iv_median)

        weights = {
            "spread": self.scoring.get("weight_spread", 0.35),
            "oi": self.scoring.get("weight_oi", 0.2),
            "volume": self.scoring.get("weight_volume", 0.2),
            "delta": self.scoring.get("weight_delta", 0.2),
            "price": self.scoring.get("weight_price", 0.05),
            "expected_move": self.scoring.get("weight_expected_move", 0.05),
            "quote_freshness": self.scoring.get("weight_quote_freshness", 0.03),
            "iv_deviation": self.scoring.get("weight_iv_deviation", 0.02),
        }
        total_weight = sum(weights.values()) or 1.0
        base_score = (
            spread_score * weights["spread"]
            + oi_score * weights["oi"]
            + volume_score * weights["volume"]
            + delta_score * weights["delta"]
            + price_score * weights["price"]
            + expected_move_score * weights["expected_move"]
            + quote_freshness_score * weights["quote_freshness"]
            + iv_deviation_score * weights["iv_deviation"]
        ) / total_weight
        penalty = self._penalty_factor(option)
        score = max(0.0, base_score * (1.0 - penalty))
        return score

    def _compute_greeks(
        self,
        underlying_price: float,
        strike: float,
        time_to_expiry_days: float,
        implied_vol: float,
        risk_free_rate: float,
        option_type: str,
    ) -> OptionGreeks:
        if underlying_price <= 0 or strike <= 0:
            return OptionGreeks()
        if implied_vol <= 0 or time_to_expiry_days <= 0:
            return OptionGreeks()

        t_years = time_to_expiry_days / 365.0
        sigma = implied_vol
        d1 = (math.log(underlying_price / strike) + (risk_free_rate + 0.5 * sigma**2) * t_years) / (
            sigma * math.sqrt(t_years)
        )
        d2 = d1 - sigma * math.sqrt(t_years)

        if option_type == "CALL":
            delta = _norm_cdf(d1)
            theta = (
                -underlying_price * _norm_pdf(d1) * sigma / (2 * math.sqrt(t_years))
                - risk_free_rate * strike * math.exp(-risk_free_rate * t_years) * _norm_cdf(d2)
            )
        else:
            delta = _norm_cdf(d1) - 1.0
            theta = (
                -underlying_price * _norm_pdf(d1) * sigma / (2 * math.sqrt(t_years))
                + risk_free_rate * strike * math.exp(-risk_free_rate * t_years) * _norm_cdf(-d2)
            )

        gamma = _norm_pdf(d1) / (underlying_price * sigma * math.sqrt(t_years))
        vega = underlying_price * _norm_pdf(d1) * math.sqrt(t_years)

        return OptionGreeks(
            delta=delta,
            gamma=gamma,
            theta=theta,
            vega=vega,
        )

    def _median_iv(self, subset: pd.DataFrame, underlying_price: float) -> Optional[float]:
        if "impliedVolatility" not in subset.columns:
            return None
        series = subset["impliedVolatility"].dropna()
        series = series[series > 0]
        if series.empty:
            return None

        band_pct = self.options_cfg.get("iv_deviation_band_pct")
        if band_pct is not None and underlying_price > 0 and "strike" in subset.columns:
            strike_diff_pct = (subset["strike"] - underlying_price).abs() / underlying_price * 100.0
            band_mask = strike_diff_pct <= float(band_pct)
            band_series = subset.loc[band_mask, "impliedVolatility"].dropna()
            band_series = band_series[band_series > 0]
            if not band_series.empty:
                return float(band_series.median())

        return float(series.median())

    def _quote_time_from_row(self, row: pd.Series, decision_time_utc: str) -> str:
        raw = self._row_quote_time_raw(row)
        if raw is None or pd.isna(raw):
            return decision_time_utc
        if isinstance(raw, datetime):
            dt = raw
        else:
            try:
                if isinstance(raw, (int, float)):
                    dt = pd.to_datetime(raw, unit="ms", utc=True)
                else:
                    dt = pd.to_datetime(raw, utc=True)
            except (ValueError, TypeError):
                return decision_time_utc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat()

    def _row_quote_time_raw(self, row: pd.Series) -> object:
        for key in (
            "lastTradeDate",
            "quoteTime",
            "quoteTimeInLong",
            "lastTimestamp",
            "bidTimestamp",
            "askTimestamp",
            "timestamp",
            "quote_time",
        ):
            value = row.get(key)
            if value is not None and not pd.isna(value):
                return value
        return None

    def _quote_age_seconds(self, quote_time_utc: str) -> Optional[float]:
        try:
            quote_dt = datetime.fromisoformat(quote_time_utc).astimezone(timezone.utc)
        except ValueError:
            return None
        return (datetime.now(timezone.utc) - quote_dt).total_seconds()

    def _quote_age_minutes(self, quote_time_utc: str) -> Optional[float]:
        age_seconds = self._quote_age_seconds(quote_time_utc)
        if age_seconds is None:
            return None
        return age_seconds / 60.0

    def _penalty_factor(self, option: OptionContract) -> float:
        penalty = 0.0
        missing_iv_penalty = self.penalties.get("missing_iv", 0.1)
        if option.greeks.delta is None:
            penalty += missing_iv_penalty

        gamma_threshold = self.penalties.get("gamma_threshold")
        gamma_penalty = self.penalties.get("gamma_penalty", 0.1)
        if gamma_threshold is not None and option.greeks.gamma is not None:
            if option.greeks.gamma > gamma_threshold:
                penalty += gamma_penalty

        theta_threshold = self.penalties.get("theta_threshold")
        theta_penalty = self.penalties.get("theta_penalty", 0.1)
        if theta_threshold is not None and option.greeks.theta is not None:
            if abs(option.greeks.theta) > theta_threshold:
                penalty += theta_penalty

        max_quote_age_seconds = self.options_cfg.get("max_quote_age_seconds")
        quote_penalty = self.penalties.get("quote_staleness_penalty", 0.1)
        if max_quote_age_seconds is not None:
            age_seconds = self._quote_age_seconds(option.quote_time_utc)
            if age_seconds is not None and age_seconds > max_quote_age_seconds:
                penalty += quote_penalty
        else:
            max_quote_age = self.options_cfg.get("max_quote_age_minutes")
            if max_quote_age is not None:
                age_minutes = self._quote_age_minutes(option.quote_time_utc)
                if age_minutes is not None and age_minutes > max_quote_age:
                    penalty += quote_penalty

        return min(penalty, 0.9)

    def _expected_move(self, option: OptionContract) -> Optional[float]:
        if option.underlying_price <= 0 or option.implied_volatility <= 0 or option.time_to_expiry_days <= 0:
            return None
        t_years = option.time_to_expiry_days / 365.0
        return option.underlying_price * option.implied_volatility * math.sqrt(t_years)

    def _expected_move_score(self, option: OptionContract) -> float:
        expected_move = self._expected_move(option)
        if expected_move is None or expected_move <= 0:
            return 0.0
        strike_diff = abs(option.strike - option.underlying_price)
        return max(0.0, 1.0 - (strike_diff / expected_move))

    def _quote_freshness_score(self, option: OptionContract) -> float:
        max_quote_age_seconds = self.options_cfg.get("max_quote_age_seconds")
        if max_quote_age_seconds is not None:
            age_seconds = self._quote_age_seconds(option.quote_time_utc)
            if age_seconds is None:
                return 0.0
            return max(0.0, 1.0 - (age_seconds / max_quote_age_seconds))
        max_quote_age = self.options_cfg.get("max_quote_age_minutes")
        if max_quote_age is None:
            return 0.0
        age_minutes = self._quote_age_minutes(option.quote_time_utc)
        if age_minutes is None:
            return 0.0
        return max(0.0, 1.0 - (age_minutes / max_quote_age))

    def _iv_deviation_score(self, option: OptionContract, iv_median: Optional[float]) -> float:
        if iv_median is None or iv_median <= 0 or option.implied_volatility <= 0:
            return 0.0
        deviation = abs(option.implied_volatility - iv_median) / iv_median
        max_dev = self.options_cfg.get("iv_deviation_pct_max")
        if max_dev is None or max_dev <= 0:
            return 0.0
        return max(0.0, 1.0 - (deviation * 100.0 / max_dev))

    def _time_to_expiry_days(self, expiration: str, decision_time_utc: str) -> float:
        try:
            exp_date = datetime.strptime(expiration, "%Y-%m-%d").date()
        except ValueError:
            return 0.0
        try:
            decision_dt = datetime.fromisoformat(decision_time_utc)
        except ValueError:
            decision_dt = datetime.now(timezone.utc)
        exp_dt = datetime.combine(exp_date, datetime.min.time(), tzinfo=timezone.utc)
        delta = exp_dt - decision_dt.astimezone(timezone.utc)
        return max(delta.total_seconds() / 86400.0, 0.0)
