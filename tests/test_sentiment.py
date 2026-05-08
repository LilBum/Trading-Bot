from datetime import datetime, timezone

from src.sentiment import _parse_cnn_payload, _read_cache, classify_sentiment, load_sentiment_snapshot


def test_classify_sentiment_buckets():
    assert classify_sentiment(80) == "Extreme Greed"
    assert classify_sentiment(60) == "Greed"
    assert classify_sentiment(50) == "Neutral"
    assert classify_sentiment(30) == "Fear"
    assert classify_sentiment(10) == "Extreme Fear"


def test_sentiment_manual_snapshot():
    config = {
        "sentiment": {
            "enabled": True,
            "source": "manual",
            "manual_value": 80,
            "manual_timestamp_utc": "2024-01-02T15:00:00+00:00",
        }
    }
    snapshot, warnings = load_sentiment_snapshot(config)
    assert snapshot is not None
    assert snapshot.value == 80
    assert snapshot.label == "Extreme Greed"
    assert warnings == []


def test_sentiment_manual_missing_value():
    config = {"sentiment": {"enabled": True, "source": "manual", "manual_value": None}}
    snapshot, warnings = load_sentiment_snapshot(config)
    assert snapshot is None
    assert warnings


def test_parse_cnn_payload():
    payload = {
        "fear_and_greed": {
            "score": 55,
            "rating": "Greed",
            "timestamp": 1700000000,
        }
    }
    snapshot = _parse_cnn_payload(payload)
    assert snapshot is not None
    assert snapshot.value == 55
    assert snapshot.label == "Greed"
    assert snapshot.source == "cnn"


def test_read_cache(tmp_path):
    now = datetime.now(timezone.utc).isoformat()
    cache_path = tmp_path / "sentiment.json"
    cache_path.write_text(
        f'{{"value": 42, "label": "Fear", "source": "cnn", "timestamp_utc": "{now}"}}',
        encoding="utf-8",
    )
    snapshot = _read_cache(cache_path, cache_minutes=60)
    assert snapshot is not None
    assert snapshot.value == 42
