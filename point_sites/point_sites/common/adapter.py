"""Adapter contract for per-site automation.

The ``Adapter`` dataclass packs everything site-specific that the shared
pipeline needs: URLs, Gmail labels, regexes, balance patterns, discover
seeds, and the email-body → click-candidate parser. Each adapter under
``point_sites.adapters.<name>`` instantiates this dataclass with the
right values; the pipeline reads the adapter generically and never
imports adapter modules directly except via the registry.

The frozen-dataclass shape means an adapter is a *value*, not a class —
all sites use the same code paths and only differ in the values they
inject. New site = one file under ``adapters/`` + one registry entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from re import Pattern
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .sources import ClickUrlSource


@dataclass(frozen=True)
class Adapter:
    """Per-site config injected into the shared pipeline.

    The env-var names (``MOPPY_COOKIES``, ``SLACK_CHANNEL_MOPPY`` etc)
    are derived from ``name.upper()`` so adding a new adapter does not
    require any plumbing in ``Config.from_env`` — just a new entry in
    the registry plus matching GitHub Secrets.
    """

    # Identity
    name: str
    site_label: str

    # Login + balance
    mypage_url: str
    allowed_hosts: frozenset[str]
    login_keyword: str = "ログアウト"

    # Gmail-source defaults — read by ``GmailSource`` via ``Config``,
    # ignored by other source kinds. Left here (not on the source) so
    # ``<PREFIX>_GMAIL_QUERY`` / ``_LABEL`` / ``_NO_COINS_LABEL`` env
    # overrides keep working uniformly through ``Config.from_env``.
    gmail_query: str = ""
    clicked_label: str = ""
    no_coins_label: str = ""

    # Click-URL source — Gmail / on-site inbox / endpoint poll. Optional
    # because pre-source-refactor adapters (none today) and bare Adapter()
    # instances used in tests don't need one. ``cmd_run`` fails fast if
    # the chosen adapter has no source.
    source: ClickUrlSource | None = None

    # Balance scraping (compiled regexes, ordered most-specific → most-permissive)
    balance_patterns: tuple[Pattern[str], ...] = field(default_factory=tuple)

    # Read-only discover crawl seeds
    discover_seeds: tuple[str, ...] = field(default_factory=tuple)

    @property
    def env_prefix(self) -> str:
        """For env-var naming: prefix uppercase of ``name``."""
        return self.name.upper()

    @property
    def cookies_env(self) -> str:
        return f"{self.env_prefix}_COOKIES"

    @property
    def slack_channel_env(self) -> str:
        return f"SLACK_CHANNEL_{self.env_prefix}"

    def state_path(self, data_root: str) -> str:
        return f"{data_root}/{self.name}/state.json"

    def cookie_store_path(self, data_root: str) -> str:
        return f"{data_root}/{self.name}/cookies.json"

    def outcome_path(self, data_root: str) -> str:
        return f"{data_root}/{self.name}/outcomes.jsonl"
