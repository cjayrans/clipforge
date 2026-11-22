"""Settings: secrets from the environment, behaviour from config/clipforge.yaml.

- `Secrets`: API keys and tokens, read from environment variables only (Docker passes `.env` via `env_file`).
  This module never opens a `.env` file and never logs secret values; use `Secrets.secret_values()` to feed the
  log redaction filter.
- `RuntimeEnv`: non-secret environment switches (`PUBLISH_MODE`, `CLIPFORGE_CONFIG`, `CLIPFORGE_DATA_DIR`,
  `CLIPFORGE_INBOX_DIR`).
- `AppConfig`: the YAML file, validated (incl. invariant 11: every slot inside the operator window).

Phase 0 owns this file; Phase 1 branches must not edit it (see docs/TASKS.md).
"""

from __future__ import annotations

from datetime import date, time
from functools import cached_property
from pathlib import Path
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from clipforge import compliance
from clipforge.schemas import Platform, PublishMode

DEFAULT_CONFIG_PATH = Path("config/clipforge.yaml")
EXAMPLE_CONFIG_PATH = Path("config/clipforge.example.yaml")

DayType = Literal["weekdays", "saturday", "sunday"]


class ConfigError(ValueError):
    """Configuration file missing or invalid."""


# --------------------------------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------------------------------


class Secrets(BaseSettings):
    """Secrets from environment variables. All optional so tests and CI run without any; clients check at use."""

    # Empty values (KEY= in .env) count as unset.
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False, env_ignore_empty=True)

    anthropic_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    upload_post_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_owner_chat_id: int | None = None
    youtube_api_key: SecretStr | None = None

    def secret_values(self) -> list[str]:
        """Non-empty secret strings, for the log redaction filter. Never print the result."""
        values: list[str] = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr) and value.get_secret_value():
                values.append(value.get_secret_value())
        return values


SECRET_ENV_VARS: tuple[str, ...] = tuple(name.upper() for name in Secrets.model_fields)


class RuntimeEnv(BaseSettings):
    """Non-secret environment switches."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False, env_ignore_empty=True)

    publish_mode: PublishMode | None = None
    clipforge_config: Path = DEFAULT_CONFIG_PATH
    clipforge_data_dir: Path = Path("data")
    clipforge_inbox_dir: Path = Path("inbox")


# --------------------------------------------------------------------------------------------------
# YAML config
# --------------------------------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TimeWindow(_Strict):
    start: time
    end: time

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start >= self.end:
            raise ValueError(f"window start {self.start} must be before end {self.end}")
        return self


class OperatorWindow(_Strict):
    weekdays: TimeWindow
    saturday: TimeWindow
    sunday: TimeWindow


class SlotTimes(_Strict):
    weekdays: list[time]
    saturday: list[time]
    sunday: list[time]


class AccountSet(_Strict):
    upload_post_user: str = Field(min_length=1)
    platforms: list[Platform] = Field(min_length=1)
    style_preset: str


class Style(BaseModel):
    """Caption style preset. Extra keys are allowed so render (Phase 1 C) can add style options in YAML."""

    model_config = ConfigDict(extra="allow", frozen=True)

    font: str
    size: int = Field(gt=0)
    primary: str
    highlight: str
    outline: int | None = Field(default=None, ge=0)
    box: str | None = None
    y_pct: float = Field(gt=0, lt=100)


class PipelineConfig(_Strict):
    nightly_candidate_target: int = Field(gt=0)
    max_sources_per_night: int = Field(gt=0)
    window_min_s: float = Field(ge=20)
    window_max_s: float = Field(le=75)
    windows_scored_per_source: int = Field(gt=0)
    top_n_per_source: int = Field(gt=0)
    max_clip_s: float = Field(le=compliance.MAX_CLIP_S)
    min_clip_s: float = Field(ge=compliance.MIN_CLIP_S)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.window_min_s >= self.window_max_s:
            raise ValueError("pipeline.window_min_s must be < window_max_s")
        if self.min_clip_s >= self.max_clip_s:
            raise ValueError("pipeline.min_clip_s must be < max_clip_s")
        return self


class RenderConfig(_Strict):
    width: Literal[1080]
    height: Literal[1920]
    fps: int = Field(gt=0)
    preset: str
    crf: int = Field(ge=0, le=51)
    loudnorm_i: float
    hook_seconds: float = Field(gt=0)
    ad_label_seconds: float = Field(ge=compliance.AD_LABEL_MIN_END_S)
    ad_label_text: str = Field(min_length=1)


class ModelsConfig(_Strict):
    rank: str
    copy_: str = Field(alias="copy")
    transcribe: str


class Price(_Strict):
    input_per_mtok: float | None = Field(default=None, ge=0)
    output_per_mtok: float | None = Field(default=None, ge=0)
    per_audio_hour: float | None = Field(default=None, ge=0)


class CampaignFilter(_Strict):
    min_approval_rate_pct: float = Field(ge=0, le=100)
    min_budget_to_cap_ratio: float = Field(ge=0)
    max_views_to_min_payout: int = Field(gt=0)
    min_budget_remaining_usd: float = Field(ge=0)
    max_participants: int = Field(gt=0)
    allowed_footage_modes: list[Literal["supplied", "streamer_stream", "own_gameplay", "official_broadcast"]]


class YoutubeConfig(_Strict):
    category_id: str


class RetentionConfig(_Strict):
    source_days_after_terminal: int = Field(gt=0)
    clip_days: int = Field(gt=0)
    backups_keep: int = Field(gt=0)


class AppConfig(_Strict):
    timezone: str
    operator_window: OperatorWindow
    slots: SlotTimes
    arm_message_minutes_before_first_slot: int = Field(ge=0)
    submission_window_minutes: int = Field(gt=0)
    submission_reminder_minutes: int = Field(gt=0)
    publish_mode: PublishMode = PublishMode.DRY_RUN
    account_sets: dict[str, AccountSet] = Field(min_length=1)
    styles: dict[str, Style] = Field(min_length=1)
    pipeline: PipelineConfig
    render: RenderConfig
    models: ModelsConfig
    prices: dict[str, Price]
    monthly_api_cap_usd: float = Field(ge=0)
    campaign_filter: CampaignFilter
    youtube: YoutubeConfig
    retention: RetentionConfig

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v

    @model_validator(mode="after")
    def _cross_checks(self) -> Self:
        # Invariant 11: every slot inside its day's operator window, strictly increasing.
        for day in ("weekdays", "saturday", "sunday"):
            window: TimeWindow = getattr(self.operator_window, day)
            times: list[time] = getattr(self.slots, day)
            for t in times:
                if not window.start <= t < window.end:
                    raise ValueError(
                        f"slot {t:%H:%M} on {day} is outside the operator window "
                        f"{window.start:%H:%M}-{window.end:%H:%M} (COMPLIANCE #11)"
                    )
            if any(b <= a for a, b in zip(times, times[1:], strict=False)):
                raise ValueError(f"slots on {day} must be strictly increasing")
        if self.submission_reminder_minutes >= self.submission_window_minutes:
            raise ValueError("submission_reminder_minutes must be less than submission_window_minutes")
        for name, acct in self.account_sets.items():
            if acct.style_preset not in self.styles:
                raise ValueError(f"account set {name!r} uses unknown style preset {acct.style_preset!r}")
        for model in (self.models.rank, self.models.copy_, self.models.transcribe):
            if model not in self.prices:
                raise ValueError(f"no price configured for model {model!r}")
        return self

    @cached_property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @staticmethod
    def day_type(d: date) -> DayType:
        wd = d.weekday()
        return "saturday" if wd == 5 else "sunday" if wd == 6 else "weekdays"

    def window_for(self, d: date) -> TimeWindow:
        window: TimeWindow = getattr(self.operator_window, self.day_type(d))
        return window

    def slots_for(self, d: date) -> list[time]:
        times: list[time] = getattr(self.slots, self.day_type(d))
        return list(times)


def load_app_config(path: Path | str) -> AppConfig:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p} (copy {EXAMPLE_CONFIG_PATH} to {DEFAULT_CONFIG_PATH})")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{p} must contain a mapping")
    try:
        return AppConfig.model_validate(data)
    except ValueError as exc:
        raise ConfigError(f"invalid config {p}:\n{exc}") from exc


# --------------------------------------------------------------------------------------------------
# Combined settings
# --------------------------------------------------------------------------------------------------


class Settings:
    """Everything a process needs: validated config, secrets, effective publish mode and paths."""

    def __init__(self, app: AppConfig, secrets: Secrets, env: RuntimeEnv) -> None:
        self.app = app
        self.secrets = secrets
        self.env = env

    @property
    def publish_mode(self) -> PublishMode:
        """`PUBLISH_MODE` env var overrides the YAML; default dry_run."""
        return self.env.publish_mode or self.app.publish_mode

    @property
    def data_dir(self) -> Path:
        return self.env.clipforge_data_dir

    @property
    def inbox_dir(self) -> Path:
        return self.env.clipforge_inbox_dir

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clipforge.db"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def heartbeat_dir(self) -> Path:
        return self.data_dir / "heartbeat"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"


def load_secrets() -> Secrets:
    """Load secrets; on error report only the variable names and error kinds, never the values."""
    try:
        return Secrets()
    except ValidationError as exc:
        problems = ", ".join(f"{'.'.join(str(p) for p in e['loc']).upper()} ({e['type']})" for e in exc.errors())
        raise ConfigError(f"invalid secret environment variables: {problems}") from None


def load_settings(config_path: Path | str | None = None) -> Settings:
    try:
        env = RuntimeEnv()
    except ValidationError as exc:
        raise ConfigError(f"invalid environment: {exc}") from None
    app = load_app_config(config_path or env.clipforge_config)
    return Settings(app=app, secrets=load_secrets(), env=env)
