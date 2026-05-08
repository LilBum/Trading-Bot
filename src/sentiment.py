from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass
class SentimentSnapshot:
    value: float
    label: str
    source: str
    timestamp_utc: str


def classify_sentiment(value: float) -> str:
    if value >= 75:
        return "Extreme Greed"
    if value >= 55:
        return "Greed"
    if value >= 45:
        return "Neutral"
    if value >= 25:
        return "Fear"
    return "Extreme Fear"


def load_sentiment_snapshot(config: dict) -> tuple[Optional[SentimentSnapshot], list[str]]:
    cfg = config.get("sentiment", {})
    if not cfg.get("enabled", False):
        return None, []

    warnings: list[str] = []
    source = cfg.get("source", "manual")
    if source == "manual":
        manual_value = cfg.get("manual_value")
        if manual_value is None:
            return None, ["Manual sentiment value is missing"]
        label = classify_sentiment(float(manual_value))
        timestamp = cfg.get("manual_timestamp_utc") or datetime.now(timezone.utc).isoformat()
        return SentimentSnapshot(
            value=float(manual_value),
            label=label,
            source="manual",
            timestamp_utc=timestamp,
        ), warnings

    if source != "cnn":
        return None, [f"Unsupported sentiment source: {source}"]

    cache_path = Path(cfg.get("cache_path", "sentiment_cache.json"))
    cache_minutes = cfg.get("cache_minutes", 30)
    snapshot = _read_cache(cache_path, cache_minutes)
    if snapshot:
        return snapshot, warnings

    url = cfg.get(
        "cnn_url",
        "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
    )
    try:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, json.JSONDecodeError) as exc:
        return None, [f"Sentiment fetch failed: {exc}"]

    snapshot = _parse_cnn_payload(payload)
    if snapshot is None:
        return None, ["Sentiment payload missing expected fields"]

    _write_cache(cache_path, snapshot)
    return snapshot, warnings


def _parse_cnn_payload(payload: dict) -> Optional[SentimentSnapshot]:
    fng = payload.get("fear_and_greed") or payload.get("fear_and_greed_index")
    if not isinstance(fng, dict):
        return None
    value = fng.get("score") or fng.get("value")
    if value is None:
        return None
    label = fng.get("rating") or classify_sentiment(float(value))
    timestamp = fng.get("timestamp")
    if timestamp:
        try:
            ts = datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat()
        except ValueError:
            ts = datetime.now(timezone.utc).isoformat()
    else:
        ts = datetime.now(timezone.utc).isoformat()
    return SentimentSnapshot(
        value=float(value),
        label=str(label),
        source="cnn",
        timestamp_utc=ts,
    )


def _read_cache(path: Path, cache_minutes: int) -> Optional[SentimentSnapshot]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    timestamp = payload.get("timestamp_utc")
    if not timestamp:
        return None
    try:
        cached_time = datetime.fromisoformat(timestamp).astimezone(timezone.utc)
    except ValueError:
        return None
    if datetime.now(timezone.utc) - cached_time > timedelta(minutes=cache_minutes):
        return None
    value = payload.get("value")
    label = payload.get("label") or classify_sentiment(float(value))
    source = payload.get("source", "cnn")
    if value is None:
        return None
    return SentimentSnapshot(
        value=float(value),
        label=str(label),
        source=str(source),
        timestamp_utc=timestamp,
    )


def _write_cache(path: Path, snapshot: SentimentSnapshot) -> None:
    payload = {
        "value": snapshot.value,
        "label": snapshot.label,
        "source": snapshot.source,
        "timestamp_utc": snapshot.timestamp_utc,
    }
    path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
