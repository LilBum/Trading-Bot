from __future__ import annotations

from datetime import datetime
from typing import Optional


def build_occ_symbol(
    symbol: str | None,
    expiration: str | None,
    option_type: str | None,
    strike: float | None,
) -> Optional[str]:
    if not symbol or not expiration or strike is None:
        return None
    try:
        exp = datetime.strptime(expiration, "%Y-%m-%d").strftime("%y%m%d")
    except ValueError:
        return None
    opt = (option_type or "CALL").upper()
    opt = "C" if opt.startswith("C") else "P"
    strike_int = int(round(float(strike) * 1000))
    root = symbol.strip().upper()
    return f"{root}{exp}{opt}{strike_int:08d}"
