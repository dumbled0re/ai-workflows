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
from .password_login import PasswordLoginConfig
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

    # Display unit for ``balance_patterns`` result in Slack notifications
    # (e.g. "pt" / "コイン" / "マイル"). Defaults to "pt" for compatibility
    # with the existing notifier hard-codes. Adapters override when the
    # site uses a non-point unit as primary (e.g. pointtown's coin counter).
    balance_label: str = "pt"

    # Optional secondary balance — for sites that surface TWO currencies
    # whose values interact (e.g. pointtown: 10 コイン auto-convert to 1
    # ポイント; coin balance drops on conversion which looks like a loss
    # in the Slack delta unless the converted-to point value is shown
    # alongside). When set, ``Notifier.send_summary`` renders an extra
    # ``/ <label>: before→after (Δ)`` clause next to the primary balance
    # line. The primary balance still drives credit-ratio degradation,
    # OutcomeTracker, and stagnation detection — the secondary is purely
    # for user display.
    secondary_balance_patterns: tuple[Pattern[str], ...] | None = None
    secondary_balance_label: str | None = None

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

    # Dynamic wizard discovery: scrape a list page → find prize links →
    # build wizards on-the-fly with the discovered URLs. Used for sites
    # where the daily prize roster changes (chanceit 応募が簡単 14 件
    # 毎日入れ替え 等)。
    # ``dynamic_wizard_list_url``: list page to scrape (single URL).
    #   Kept for backward compatibility (chanceit / dreammail original
    #   shape). New adapters should prefer ``dynamic_wizard_list_urls``.
    # ``dynamic_wizard_list_urls``: tuple of list pages to scrape in
    #   sequence. Prizes are merged + URL-deduped across all sources
    #   before the per-prize wizards are expanded. ``_max_count`` is
    #   applied to the *combined* unique-URL set, not per-list. Used by
    #   sites with multiple category pages sharing the same prize-detail
    #   schema (chanceit easy-entry / daily-weekly-entry / instant-win
    #   etc all surface ``/present/detail/<id>/`` with the same jump.srv
    #   apply mechanism). If both fields are set, ``_list_urls`` wins.
    # ``dynamic_wizard_link_selector``: CSS selector for individual
    #   prize page anchors on the list (e.g. 'a[href*="/present/detail/"]')
    # ``dynamic_wizard_template``: template DailyWizard whose ``url``
    #   is replaced with each discovered href. ``clicks`` /
    #   ``initial_wait_ms`` etc are reused per-prize. Wizard names get
    #   "_<index>" suffix.
    # ``dynamic_wizard_max_count``: safety cap on wizards/cron run
    #   (default 30) to avoid runaway from a buggy selector match.
    dynamic_wizard_list_url: str | None = None
    dynamic_wizard_list_urls: tuple[str, ...] = field(default_factory=tuple)
    dynamic_wizard_link_selector: str | None = None
    dynamic_wizard_template: DailyWizard | None = None
    dynamic_wizard_max_count: int = 30

    # ID/PW Playwright login fallback. When the persisted cookie jar
    # fails ``verify_login``, the orchestrator opens a Chromium session,
    # navigates to ``login_url``, fills username/password from the
    # configured Secrets, and captures the rotated cookie jar. Set
    # only for sites whose cookie lifetime is too short to keep up with
    # manual refresh (fruitmail ~24h, pointtown ~20h, moppy intermittent).
    # ``None`` (default) = cookie-only operation, manual refresh on
    # expiry (same as before this field existed).
    password_login: PasswordLoginConfig | None = None

    # Lottery-style Slack notification: send「応募した賞品一覧」instead
    # of generic point_sites summary. For chanceit / 抽選専用 adapter
    # where yield is "entries submitted" + (later) "prize won notification"
    # rather than "points credited". When True, dynamic_wizard discovery
    # extracts per-prize title from the list page (heuristic JS), and
    # the wizard loop tracks per-wizard success → final Slack message
    # lists each prize with status emoji.
    lottery_mode: bool = False

    # Override per-wizard Slack labels when ``lottery_mode=True`` but the
    # adapter isn't actually a 抽選 site. chanceit is the canonical case:
    # tasklist article-visit missions feed the same per-wizard format,
    # but the run isn't 抽選応募 (it's 記事閲覧 for stamp accrual). When
    # set, the notifier substitutes these strings into the run header
    # and the verified-section label. ``None`` falls back to the 抽選
    # defaults so existing adapters (dreammail / fruitmail_lottery) are
    # unaffected.
    lottery_run_header: str | None = None
    lottery_verified_label: str | None = None

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

    def processed_messages_path(self, data_root: str) -> str:
        return f"{data_root}/{self.name}/processed_messages.json"
