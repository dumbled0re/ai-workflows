"""Runtime configuration loaded from environment variables.

The shape changed in Phase 1C of the multi-site refactor: instead of
hardcoding ``MOPPY_*`` env-var names, we look up env-var names from the
active ``Adapter``. So the same ``Config.from_env(adapter)`` works for
moppy, pointincome, etc — it just reads ``MOPPY_COOKIES`` /
``POINTINCOME_COOKIES`` based on ``adapter.cookies_env``. CLI flags
override env defaults where applicable. All numeric/string validation
happens at process start (fail-fast).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .common.adapter import Adapter


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


def _parse_cookies(
    raw: str | None,
    *,
    env_name: str = "COOKIES",
    default_domain: str = ".moppy.jp",
) -> list[dict[str, object]] | None:
    """Parse the per-site COOKIES env var (JSON-encoded list of cookie dicts).

    Each entry must contain at least ``name`` and ``value``. ``domain``,
    ``path`` and ``secure`` are optional (defaults: ``default_domain`` /
    ``/`` / ``True``). ``secure`` defaults to ``True`` so an exported
    session cookie is never accidentally sent over plain HTTP.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{env_name} must be valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ConfigError(f"{env_name} must be a JSON array of cookie objects")
    cookies: list[dict[str, object]] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ConfigError(f"{env_name}[{i}] must be an object")
        name = item.get("name")
        value = item.get("value")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{env_name}[{i}].name must be a non-empty string")
        if not isinstance(value, str):
            raise ConfigError(f"{env_name}[{i}].value must be a string")
        secure = item.get("secure", True)
        if not isinstance(secure, bool):
            raise ConfigError(f"{env_name}[{i}].secure must be a boolean")
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": item.get("domain", default_domain),
                "path": item.get("path", "/"),
                "secure": secure,
            }
        )
    return cookies


@dataclass(frozen=True)
class Config:
    # Adapter (everything site-specific lives here)
    adapter: Adapter

    # Per-site secrets / channels (read via adapter.cookies_env / slack_channel_env)
    slack_bot_token: str
    slack_channel: str
    cookies: list[dict[str, object]] | None  # None = anonymous (no points credited)

    # Gmail (only relevant when adapter uses email-based clicks).
    # OAuth2 credentials: obtained once via ``scripts/get_refresh_token.py``
    # and stored as repo Secrets. The previous IMAP path (GMAIL_USER /
    # GMAIL_APP_PASSWORD) was retired 2026-05-17 after Google bot-suspended
    # the IMAP-using account.
    gmail_client_id: str
    gmail_client_secret: str
    gmail_refresh_token: str
    gmail_query: str
    # Labels are kept for backward compat in adapter configs but are now
    # informational only — readonly OAuth scope cannot write labels.
    clicked_label: str
    no_coins_label: str

    # Behavior knobs (per-site env-var prefix)
    dry_run: bool
    click_interval_min: int
    click_interval_max: int
    max_attempts: int
    max_messages: int
    log_level: str

    # Paths (per-site by default; ``<PREFIX>_*_PATH`` env vars override
    # for tests or one-off relocations).
    state_path: str
    outcome_path: str
    cookie_store_path: str
    # Local dedup file for Gmail message IDs already processed. Replaces
    # the prior server-side label-based dedup, which is no longer possible
    # under the readonly OAuth scope.
    processed_messages_path: str

    @classmethod
    def from_env(cls, adapter: Adapter, *, data_root: str = "data") -> Config:
        bot_token = _env_str("SLACK_BOT_TOKEN", required=True)
        assert bot_token is not None
        if not bot_token.startswith("xoxb-"):
            raise ConfigError("SLACK_BOT_TOKEN must start with xoxb-")

        slack_channel = _env_str(adapter.slack_channel_env, required=True)
        assert slack_channel is not None

        gmail_client_id = _env_str("GMAIL_CLIENT_ID", required=True)
        gmail_client_secret = _env_str("GMAIL_CLIENT_SECRET", required=True)
        gmail_refresh_token = _env_str("GMAIL_REFRESH_TOKEN", required=True)
        assert gmail_client_id is not None
        assert gmail_client_secret is not None
        assert gmail_refresh_token is not None

        prefix = adapter.env_prefix
        interval_min = _env_int(f"{prefix}_CLICK_INTERVAL_MIN", 5, low=1, high=600)
        interval_max = _env_int(f"{prefix}_CLICK_INTERVAL_MAX", 15, low=1, high=600)
        if interval_min > interval_max:
            raise ConfigError(f"{prefix}_CLICK_INTERVAL_MIN ({interval_min}) > MAX ({interval_max})")

        log_level = (_env_str(f"{prefix}_LOG_LEVEL", "INFO") or "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ConfigError(f"{prefix}_LOG_LEVEL invalid: {log_level}")

        # Default cookie domain comes from the adapter's mypage URL host
        # if not specified in the cookie record.
        from urllib.parse import urlparse

        default_cookie_domain = "." + (urlparse(adapter.mypage_url).hostname or "moppy.jp").split(".", 1)[-1]
        cookies = _parse_cookies(
            _env_str(adapter.cookies_env),
            env_name=adapter.cookies_env,
            default_domain=default_cookie_domain,
        )

        return cls(
            adapter=adapter,
            gmail_client_id=gmail_client_id,
            gmail_client_secret=gmail_client_secret,
            gmail_refresh_token=gmail_refresh_token,
            slack_bot_token=bot_token,
            slack_channel=slack_channel,
            cookies=cookies,
            # Adapter values are defaults; per-site env vars override so an
            # operator can scope a run to a custom Gmail query (e.g.
            # broader date window for backfill) or relocate state for tests
            # without editing the adapter.
            gmail_query=_env_str(f"{prefix}_GMAIL_QUERY", adapter.gmail_query) or adapter.gmail_query,
            clicked_label=_env_str(f"{prefix}_LABEL", adapter.clicked_label) or adapter.clicked_label,
            no_coins_label=(_env_str(f"{prefix}_NO_COINS_LABEL", adapter.no_coins_label) or adapter.no_coins_label),
            dry_run=os.environ.get(f"{prefix}_DRY_RUN", "0") == "1",
            click_interval_min=interval_min,
            click_interval_max=interval_max,
            max_attempts=_env_int(f"{prefix}_MAX_ATTEMPTS", 3, low=1, high=10),
            max_messages=_env_int(f"{prefix}_MAX_MESSAGES", 50, low=1, high=500),
            state_path=_env_str(f"{prefix}_STATE_PATH", adapter.state_path(data_root)) or adapter.state_path(data_root),
            outcome_path=(
                _env_str(f"{prefix}_OUTCOME_PATH", adapter.outcome_path(data_root)) or adapter.outcome_path(data_root)
            ),
            cookie_store_path=(
                _env_str(f"{prefix}_COOKIE_STORE_PATH", adapter.cookie_store_path(data_root))
                or adapter.cookie_store_path(data_root)
            ),
            processed_messages_path=(
                _env_str(f"{prefix}_PROCESSED_MESSAGES_PATH", adapter.processed_messages_path(data_root))
                or adapter.processed_messages_path(data_root)
            ),
            log_level=log_level,
        )
