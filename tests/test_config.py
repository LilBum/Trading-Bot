import os

import pytest

from src.config import find_plaintext_secrets, load_config


def test_load_config_supports_comments(tmp_path):
    payload = """
    {
      // line comment
      "mode": "paper",
      "url": "https://example.com/path",
      /* block comment */
      "value": 2,
      # hash comment
      "list": [1, 2]
    }
    """
    path = tmp_path / "config.json"
    path.write_text(payload, encoding="utf-8")
    config = load_config(path)
    assert config["mode"] == "paper"
    assert config["url"].startswith("https://")
    assert config["value"] == 2
    assert config["list"] == [1, 2]


def test_load_config_loads_dotenv_if_present(tmp_path, monkeypatch):
    monkeypatch.delenv("CFG_TEST_DOTENV_VALUE", raising=False)
    (tmp_path / ".env").write_text("CFG_TEST_DOTENV_VALUE=hello\n", encoding="utf-8")
    (tmp_path / "config.json").write_text('{"mode": "paper"}', encoding="utf-8")
    load_config(tmp_path / "config.json")
    assert os.environ.get("CFG_TEST_DOTENV_VALUE") == "hello"


def test_load_dotenv_does_not_override_existing_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CFG_TEST_PREEXISTING", "existing")
    (tmp_path / ".env").write_text(
        "CFG_TEST_PREEXISTING=overwritten\n", encoding="utf-8"
    )
    (tmp_path / "config.json").write_text('{"mode": "paper"}', encoding="utf-8")
    load_config(tmp_path / "config.json")
    assert os.environ["CFG_TEST_PREEXISTING"] == "existing"


def test_load_dotenv_strips_quotes_and_export_prefix(tmp_path, monkeypatch):
    monkeypatch.delenv("CFG_TEST_QUOTED", raising=False)
    monkeypatch.delenv("CFG_TEST_EXPORTED", raising=False)
    (tmp_path / ".env").write_text(
        'CFG_TEST_QUOTED="value with spaces"\nexport CFG_TEST_EXPORTED=v2\n# comment line\n\n',
        encoding="utf-8",
    )
    (tmp_path / "config.json").write_text('{"mode": "paper"}', encoding="utf-8")
    load_config(tmp_path / "config.json")
    assert os.environ["CFG_TEST_QUOTED"] == "value with spaces"
    assert os.environ["CFG_TEST_EXPORTED"] == "v2"


def test_load_config_rejects_plaintext_credentials(tmp_path):
    payload = '{"webull": {"app_key": "leaked-key"}}'
    (tmp_path / "config.json").write_text(payload, encoding="utf-8")
    with pytest.raises(RuntimeError, match="Plaintext credentials"):
        load_config(tmp_path / "config.json")


def test_load_config_accepts_blank_credentials(tmp_path):
    payload = '{"webull": {"app_key": "", "app_secret": null}}'
    (tmp_path / "config.json").write_text(payload, encoding="utf-8")
    config = load_config(tmp_path / "config.json")
    assert config["webull"]["app_key"] == ""


def test_find_plaintext_secrets_lists_offenders():
    config = {
        "webull": {"app_key": "AAA", "app_secret": ""},
        "tradier": {"sandbox_token": "   "},
        "finnhub": {"api_key": "KKK"},
        "execution": {"webull": {"account_id": "ACC42"}},
    }
    result = find_plaintext_secrets(config)
    assert "webull.app_key" in result
    assert "finnhub.api_key" in result
    assert "execution.webull.account_id" in result
    assert "webull.app_secret" not in result
    assert "tradier.sandbox_token" not in result


def test_find_plaintext_secrets_handles_missing_paths():
    assert find_plaintext_secrets({}) == []
    assert find_plaintext_secrets({"unrelated": "stuff"}) == []
    assert find_plaintext_secrets({"webull": "not-a-dict"}) == []


def test_find_plaintext_secrets_clean_for_default_template(tmp_path):
    config = {
        "webull": {"app_key": "", "app_secret": "", "test_app_key": "", "test_app_secret": ""},
        "public": {"secret_token": "", "account_id": None},
        "tradier": {"access_token": "", "sandbox_token": ""},
        "finnhub": {"api_key": ""},
        "massive": {"api_key": ""},
        "execution": {
            "tradier": {"access_token": "", "sandbox_token": "", "account_id": ""},
            "webull": {"account_id": ""},
        },
    }
    assert find_plaintext_secrets(config) == []
