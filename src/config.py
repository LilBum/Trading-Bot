import json
import os
from pathlib import Path

# Paths in config.json that must NOT contain plaintext credentials.
# All credentials and account identifiers live in environment variables
# (see .env.example).
_REQUIRED_EMPTY_PATHS: tuple[tuple[str, ...], ...] = (
    ("webull", "app_key"),
    ("webull", "app_secret"),
    ("webull", "test_app_key"),
    ("webull", "test_app_secret"),
    ("public", "secret_token"),
    ("public", "account_id"),
    ("tradier", "access_token"),
    ("tradier", "sandbox_token"),
    ("finnhub", "api_key"),
    ("massive", "api_key"),
    ("execution", "tradier", "access_token"),
    ("execution", "tradier", "sandbox_token"),
    ("execution", "tradier", "account_id"),
    ("execution", "webull", "account_id"),
)


def load_config(path: str | Path) -> dict:
    config_path = Path(path)
    load_dotenv(config_path.parent / ".env")
    with config_path.open("r", encoding="utf-8-sig") as handle:
        raw = handle.read()
    payload = _strip_json_comments(raw)
    config = json.loads(payload)
    leaked = find_plaintext_secrets(config)
    if leaked:
        raise RuntimeError(
            "Plaintext credentials detected in config.json at: "
            + ", ".join(leaked)
            + ". Move them to environment variables (see .env.example), "
            "then blank the value in config.json."
        )
    return config


def find_plaintext_secrets(config: dict) -> list[str]:
    """Return dotted paths where a non-empty plaintext credential is present."""
    found: list[str] = []
    for path in _REQUIRED_EMPTY_PATHS:
        node: object = config
        ok = True
        for key in path[:-1]:
            if not isinstance(node, dict):
                ok = False
                break
            node = node.get(key)
            if node is None:
                ok = False
                break
        if not ok or not isinstance(node, dict):
            continue
        value = node.get(path[-1])
        if isinstance(value, str) and value.strip():
            found.append(".".join(path))
    return found


def load_dotenv(path: Path) -> None:
    """Populate os.environ from a .env file. Existing env vars take precedence."""
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _strip_json_comments(text: str) -> str:
    output: list[str] = []
    in_string = False
    escape = False
    idx = 0
    length = len(text)
    while idx < length:
        ch = text[idx]
        if in_string:
            output.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            idx += 1
            continue
        if ch == '"':
            in_string = True
            output.append(ch)
            idx += 1
            continue
        if ch == "/" and idx + 1 < length:
            nxt = text[idx + 1]
            if nxt == "/":
                idx += 2
                while idx < length and text[idx] not in "\r\n":
                    idx += 1
                continue
            if nxt == "*":
                idx += 2
                while idx + 1 < length and not (text[idx] == "*" and text[idx + 1] == "/"):
                    idx += 1
                idx = idx + 2 if idx + 1 < length else length
                output.append(" ")
                continue
        if ch == "#":
            idx += 1
            while idx < length and text[idx] not in "\r\n":
                idx += 1
            continue
        output.append(ch)
        idx += 1
    return "".join(output)
