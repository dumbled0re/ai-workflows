"""Runtime configuration loaded from environment variables.

CLI flags override these values where applicable. All numeric/string validation
happens at process start (fail-fast).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(ValueError):
    pass


def _env_int(name: str, default: int, *, low: int, high: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not (low <= value <= high):
        raise ConfigError(f"{name}={value} out of range [{low}, {high}]")
    return value


def _env_str(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise ConfigError(f"{name} is required but not set")
    return value


@dataclass(frozen=True)
class Config:
    gmail_user: str
    gmail_app_password: str
    slack_webhook_url: str
    gmail_query: str
    dry_run: bool
    click_interval_min: int
    click_interval_max: int
    max_attempts: int
    max_messages: int
    state_path: str
    log_level: str
    moppy_label: str

    @classmethod
    def from_env(cls) -> Config:
        webhook = _env_str("SLACK_WEBHOOK_URL_MOPPY", required=True)
        assert webhook is not None
        if not webhook.startswith("https://hooks.slack.com/"):
            raise ConfigError("SLACK_WEBHOOK_URL_MOPPY must start with https://hooks.slack.com/")

        gmail_user = _env_str("GMAIL_USER", required=True)
        assert gmail_user is not None
        if "@" not in gmail_user:
            raise ConfigError(f"GMAIL_USER must be a full email address, got {gmail_user!r}")

        gmail_app_password = _env_str("GMAIL_APP_PASSWORD", required=True)
        assert gmail_app_password is not None
        # Google displays app passwords as "abcd efgh ijkl mnop" — strip spaces.
        cleaned_password = gmail_app_password.replace(" ", "")
        if len(cleaned_password) != 16:
            raise ConfigError(
                f"GMAIL_APP_PASSWORD must be 16 characters (after stripping spaces); "
                f"got {len(cleaned_password)}"
            )

        interval_min = _env_int("MOPPY_CLICK_INTERVAL_MIN", 5, low=1, high=600)
        interval_max = _env_int("MOPPY_CLICK_INTERVAL_MAX", 15, low=1, high=600)
        if interval_min > interval_max:
            raise ConfigError(f"MOPPY_CLICK_INTERVAL_MIN ({interval_min}) > MAX ({interval_max})")

        log_level = (_env_str("MOPPY_LOG_LEVEL", "INFO") or "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ConfigError(f"MOPPY_LOG_LEVEL invalid: {log_level}")

        return cls(
            gmail_user=gmail_user,
            gmail_app_password=cleaned_password,
            slack_webhook_url=webhook,
            gmail_query=_env_str(
                "MOPPY_GMAIL_QUERY",
                "from:moppy.jp -label:moppy-clicked -label:moppy-no-coins newer_than:3d",
            )
            or "from:moppy.jp -label:moppy-clicked -label:moppy-no-coins newer_than:3d",
            dry_run=os.environ.get("MOPPY_DRY_RUN", "0") == "1",
            click_interval_min=interval_min,
            click_interval_max=interval_max,
            max_attempts=_env_int("MOPPY_MAX_ATTEMPTS", 3, low=1, high=10),
            max_messages=_env_int("MOPPY_MAX_MESSAGES", 50, low=1, high=500),
            state_path=_env_str("MOPPY_STATE_PATH", "data/state.json") or "data/state.json",
            log_level=log_level,
            moppy_label=_env_str("MOPPY_LABEL", "moppy-clicked") or "moppy-clicked",
        )
