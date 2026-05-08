from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests


PUBLIC_BASE_URL = "https://api.public.com"


@dataclass
class PublicAccessToken:
    token: str
    expires_at: datetime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_osi_symbol(osi_symbol: str) -> dict:
    raw = (osi_symbol or "").strip().upper()
    if len(raw) < 15:
        return {"root": raw, "expiration": "", "option_type": "", "strike": 0.0}
    root = raw[:6].rstrip()
    exp = raw[6:12]
    option_type = raw[12:13]
    strike_raw = raw[13:]
    try:
        strike = int(strike_raw) / 1000.0
    except ValueError:
        strike = 0.0
    try:
        expiration = datetime.strptime(exp, "%y%m%d").date().isoformat()
    except ValueError:
        expiration = ""
    return {
        "root": root,
        "expiration": expiration,
        "option_type": "CALL" if option_type == "C" else "PUT",
        "strike": strike,
    }


class PublicApiClient:
    def __init__(self, config: dict) -> None:
        public_cfg = config.get("public", {})
        self.base_url = public_cfg.get("base_url", PUBLIC_BASE_URL).rstrip("/")
        self.secret_token = public_cfg.get("secret_token") or os.getenv("PUBLIC_SECRET_TOKEN")
        self.validity_minutes = int(public_cfg.get("validity_minutes", 15))
        self.account_id = public_cfg.get("account_id") or os.getenv("PUBLIC_ACCOUNT_ID")
        self._access_token: Optional[PublicAccessToken] = None

    def _ensure_access_token(self) -> str:
        if self._access_token and self._access_token.expires_at > _utc_now() + timedelta(seconds=30):
            return self._access_token.token
        if not self.secret_token:
            raise RuntimeError("Missing Public secret token. Set public.secret_token or PUBLIC_SECRET_TOKEN.")
        payload = {"validityInMinutes": self.validity_minutes, "secret": self.secret_token}
        response = requests.post(
            f"{self.base_url}/userapiauthservice/personal/access-tokens",
            json=payload,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        token = data.get("accessToken")
        if not token:
            raise RuntimeError("Public access token missing in response.")
        expires_at = _utc_now() + timedelta(minutes=self.validity_minutes)
        self._access_token = PublicAccessToken(token=token, expires_at=expires_at)
        return token

    def _headers(self) -> dict:
        token = self._ensure_access_token()
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def get_accounts(self) -> dict:
        response = requests.get(
            f"{self.base_url}/userapigateway/trading/account",
            headers=self._headers(),
            timeout=15,
        )
        response.raise_for_status()
        return response.json()

    def ensure_account_id(self) -> str:
        if self.account_id:
            return self.account_id
        data = self.get_accounts()
        accounts = data.get("accounts") or []
        if not accounts:
            raise RuntimeError("No Public accounts returned.")
        self.account_id = accounts[0].get("accountId")
        if not self.account_id:
            raise RuntimeError("AccountId missing in Public account response.")
        return self.account_id

    def get_quotes(self, instruments: list[dict]) -> dict:
        account_id = self.ensure_account_id()
        payload = {"instruments": instruments}
        response = requests.post(
            f"{self.base_url}/userapigateway/marketdata/{account_id}/quotes",
            headers=self._headers(),
            json=payload,
            timeout=15,
        )
        response.raise_for_status()
        return response.json()

    def get_option_expirations(self, instrument: dict) -> dict:
        account_id = self.ensure_account_id()
        payload = {"instrument": instrument}
        response = requests.post(
            f"{self.base_url}/userapigateway/marketdata/{account_id}/option-expirations",
            headers=self._headers(),
            json=payload,
            timeout=15,
        )
        response.raise_for_status()
        return response.json()

    def get_option_chain(self, instrument: dict, expiration_date: str) -> dict:
        account_id = self.ensure_account_id()
        payload = {"instrument": instrument, "expirationDate": expiration_date}
        response = requests.post(
            f"{self.base_url}/userapigateway/marketdata/{account_id}/option-chain",
            headers=self._headers(),
            json=payload,
            timeout=15,
        )
        response.raise_for_status()
        return response.json()

    def get_option_greeks(self, osi_symbols: list[str]) -> dict:
        account_id = self.ensure_account_id()
        params = [("osiSymbols", symbol) for symbol in osi_symbols]
        response = requests.get(
            f"{self.base_url}/userapigateway/option-details/{account_id}/greeks",
            headers=self._headers(),
            params=params,
            timeout=15,
        )
        response.raise_for_status()
        return response.json()


def choose_expiration(expirations: list[str], target_dte: int, now: datetime) -> Optional[str]:
    if not expirations:
        return None
    exp_dates = []
    for exp in expirations:
        try:
            exp_dates.append(datetime.strptime(exp, "%Y-%m-%d").date())
        except ValueError:
            continue
    if not exp_dates:
        return None
    target_date = now.date() + timedelta(days=target_dte)
    future_dates = [item for item in exp_dates if item >= target_date]
    if future_dates:
        chosen = min(future_dates)
    else:
        chosen = min(exp_dates, key=lambda item: abs((item - target_date).days))
    return chosen.isoformat()


def quote_to_row(symbol: str, quote: dict, option_type: str | None = None) -> dict:
    instrument = quote.get("instrument") or {}
    osi_symbol = instrument.get("symbol", symbol)
    osi_parts = parse_osi_symbol(osi_symbol)
    expiration = osi_parts["expiration"]
    strike = osi_parts["strike"]
    derived_type = osi_parts["option_type"]
    if option_type:
        derived_type = option_type
    last = _safe_float(quote.get("last"))
    bid = _safe_float(quote.get("bid"))
    ask = _safe_float(quote.get("ask"))
    return {
        "symbol": osi_symbol,
        "expiration": expiration,
        "strike": strike,
        "option_type": derived_type,
        "bid": bid,
        "ask": ask,
        "last_price": last,
        "open_interest": _safe_int(quote.get("openInterest")),
        "volume": _safe_int(quote.get("volume")),
        "bidSize": _safe_int(quote.get("bidSize")),
        "askSize": _safe_int(quote.get("askSize")),
        "lastTradeDate": quote.get("lastTimestamp") or quote.get("bidTimestamp") or quote.get("askTimestamp"),
    }
