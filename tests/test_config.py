"""clipforge.config: secrets from env, YAML validation, publish mode precedence."""

from __future__ import annotations

from datetime import date, time
from pathlib import Path
from typing import Any

import pytest
import yaml

from clipforge.config import (
    SECRET_ENV_VARS,
    AppConfig,
    ConfigError,
    Secrets,
    load_app_config,
    load_settings,
)
from clipforge.schemas import PublishMode
from tests.conftest import EXAMPLE_CONFIG


def _raw() -> dict[str, Any]:
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    assert isinstance(data, dict)
    return data


def test_example_config_is_valid(app_config: AppConfig) -> None:
    assert app_config.publish_mode is PublishMode.DRY_RUN
    assert app_config.slots.weekdays == [time(20, 10), time(20, 25), time(20, 40)]
    assert app_config.models.copy_ == "claude-sonnet-5-5"
    assert str(app_config.tz) == "America/New_York"


def test_secret_env_var_names() -> None:
    assert set(SECRET_ENV_VARS) == {
        "ANTHROPIC_API_KEY",
        "GROQ_API_KEY",
        "UPLOAD_POST_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_OWNER_CHAT_ID",
        "YOUTUBE_API_KEY",
        "TWITCH_CLIENT_ID",
        "TWITCH_CLIENT_SECRET",
    }


def test_secrets_from_env_and_hidden_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-value-123")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "4242")
    s = Secrets()
    assert s.anthropic_api_key is not None
    assert s.anthropic_api_key.get_secret_value() == "sk-ant-test-value-123"
    assert s.telegram_owner_chat_id == 4242
    assert "sk-ant-test-value-123" not in repr(s)
    assert s.secret_values() == ["sk-ant-test-value-123"]


def test_secrets_never_read_dotenv_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("GROQ_API_KEY=from-file\n")
    monkeypatch.chdir(tmp_path)
    assert Secrets().groq_api_key is None


def test_publish_mode_defaults_dry_run_and_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    assert load_settings().publish_mode is PublishMode.DRY_RUN
    monkeypatch.setenv("PUBLISH_MODE", "private")
    assert load_settings().publish_mode is PublishMode.PRIVATE


def test_settings_paths(tmp_path: Path) -> None:
    s = load_settings()
    assert s.data_dir == tmp_path / "data"
    assert s.db_path == tmp_path / "data" / "clipforge.db"
    assert s.database_url.startswith("sqlite:///")


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_app_config(tmp_path / "nope.yaml")


def test_unknown_key_rejected() -> None:
    data = _raw()
    data["surprise"] = 1
    with pytest.raises(ValueError, match="surprise"):
        AppConfig.model_validate(data)


def test_slots_must_increase() -> None:
    data = _raw()
    data["slots"]["weekdays"] = ["20:25", "20:10"]
    with pytest.raises(ValueError, match="strictly increasing"):
        AppConfig.model_validate(data)


@pytest.mark.parametrize(
    ("path", "value", "match"),
    [
        (("timezone",), "Mars/Olympus", "unknown timezone"),
        (("submission_reminder_minutes",), 30, "less than"),
        (("account_sets", "gaming-1", "style_preset"), "missing", "unknown style"),
        (("models", "rank"), "claude-unknown", "no price"),
        (("render", "ad_label_seconds"), 2.0, "greater than or equal"),
        (("pipeline", "max_clip_s"), 120, "less than or equal"),
        (("operator_window", "weekdays"), {"start": "21:00", "end": "20:00"}, "before end"),
    ],
)
def test_invalid_values(path: tuple[str, ...], value: object, match: str) -> None:
    data = _raw()
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError, match=match):
        AppConfig.model_validate(data)


def test_day_types(app_config: AppConfig) -> None:
    assert app_config.day_type(date(2026, 11, 23)) == "weekdays"  # Monday
    assert app_config.day_type(date(2026, 11, 28)) == "saturday"
    assert app_config.day_type(date(2026, 11, 29)) == "sunday"
    assert app_config.slots_for(date(2026, 11, 28)) == [time(20, 30), time(20, 45)]
    assert app_config.window_for(date(2026, 11, 23)).start == time(20, 0)


def test_style_allows_extra_keys() -> None:
    data = _raw()
    data["styles"]["bold-yellow"]["shadow"] = 2
    cfg = AppConfig.model_validate(data)
    assert cfg.styles["bold-yellow"].model_extra == {"shadow": 2}


def test_empty_env_values_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("PUBLISH_MODE", "")
    s = load_settings()
    assert s.secrets.telegram_owner_chat_id is None
    assert s.secrets.groq_api_key is None
    assert s.publish_mode is PublishMode.DRY_RUN


def test_secret_errors_never_echo_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "not-a-number-but-maybe-sensitive")
    with pytest.raises(ConfigError) as info:
        load_settings()
    assert "TELEGRAM_OWNER_CHAT_ID" in str(info.value)
    assert "maybe-sensitive" not in str(info.value)
    assert info.value.__cause__ is None


@pytest.mark.parametrize("text", ["", "   ", "\t\n"])
def test_blank_ad_label_text_rejected(tmp_path: Path, text: str) -> None:
    """COMPLIANCE #3 hardening (docs/contract-requests/render.md 2b): a blank label would render invisibly."""
    data = _raw()
    data["render"]["ad_label_text"] = text
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="ad_label_text"):
        load_app_config(p)


def test_ad_label_text_is_stripped(tmp_path: Path) -> None:
    data = _raw()
    data["render"]["ad_label_text"] = "  Ad "
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(data))
    assert load_app_config(p).render.ad_label_text == "Ad"


def test_transcribe_block_optional_with_defaults(tmp_path: Path) -> None:
    """CR-A2: the block is optional so older config files stay valid."""
    data = _raw()
    data.pop("transcribe", None)
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(data))
    cfg = load_app_config(p).transcribe
    assert (cfg.local_fallback, cfg.local_model, cfg.language) == (True, "large-v3-turbo", "en")


def test_transcribe_block_rejects_unknown_keys(tmp_path: Path) -> None:
    data = _raw()
    data["transcribe"] = {"local_fallback": False, "bogus": 1}
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="bogus"):
        load_app_config(p)
