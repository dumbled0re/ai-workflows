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

from .browser_action import BrowserAction
from .wizard import DailyWizard

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

    # Use ``BrowserClicker`` (Playwright Chromium) for balance scraping
    # instead of the default ``balance.fetch_balance`` HTTP path. Set
    # for sites whose mypage gates non-JS HTTP clients with an anti-bot
    # interstitial (e.g. pointincome's "コンテンツブロッカー" page).
    # Click-coin URL clicking continues to use ``Clicker`` regardless;
    # only the balance verification step is upgraded to a real browser.
    balance_uses_browser: bool = False

    # Daily browser-driven side-effect actions (login bonus visits,
    # gacha spin, banner clicks). Run as a single Chromium session
    # between the click loop and balance_after so any credit they
    # trigger lands in the post-click balance delta. Adapters not
    # using browser actions leave this empty and never pay the
    # browser-launch cost. See ``common.browser_action`` for the
    # action shape.
    browser_actions: tuple[BrowserAction, ...] = field(default_factory=tuple)

    # Daily-rotating banner URLs to discover via Playwright then click
    # via Clicker. Used for sites where the click targets change daily
    # and aren't surfaced over plain HTTP (e.g. hapitas's 8 top-page
    # 宝くじ交換券 banners that only render after JS hydration).
    # ``daily_banner_url`` is the page to discover from; the matching
    # selector is used as ``page.query_selector_all`` and every href
    # gathered is sent through ``Clicker.click`` so the existing
    # tracking + outcome pipeline records each.
    daily_banner_url: str | None = None
    daily_banner_selector: str | None = None

    # Multi-step browser-driven daily wizards (hapitas takarakuji
    # exchange, pointtown login bonus modal, etc). Each DailyWizard
    # declares its own URL + click sequence; cmd_run runs all of them
    # in independent Chromium sessions between the click loop and
    # balance_after. Fail-soft: a missing button times out and the
    # wizard logs a warning without aborting the run.
    daily_wizards: tuple[DailyWizard, ...] = field(default_factory=tuple)

    # Recent-run window for balance-stagnation detection. Default
    # ``None`` disables it, which is the right call for high-yield
    # sites where credit-ratio degradation already covers them.
    # Set to e.g. 30 for low-yield sites whose per-day point gain is
    # too small to anchor credit-ratio (amefri's 1pt/day login bonus
    # has expected_pt below ``MIN_EXPECTED_FOR_RATIO`` so the strong
    # detector skips it). 30 runs is one full milestone cycle for
    # amefri — if the 30-day jump didn't land either, the pipeline
    # is genuinely silent and worth alerting on.
    stagnation_window: int | None = None

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
