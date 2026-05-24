"""Multi-step browser wizards triggered once per daily cron.

Used for site features whose credit is gated behind a small JS-driven
click sequence — e.g. hapitas's 宝くじ交換券 → バラ抽選券 wizard or
pointtown's daily login bonus modal. Each ``DailyWizard`` declares the
URL to navigate to and the ordered ``(selector, repeat_count)`` clicks
that advance the UI.

Wizards are deliberately fail-soft: a missing or already-claimed
button times out the dispatch_event call, the wizard logs a warning,
and the run continues. That matches the daily reality where some
wizards (login bonus already claimed today, or 0 tickets to exchange)
legitimately have nothing to do.

Click semantics:
- ``use_navigation_click=False`` (default): dispatch JS click event via
  ``page.dispatch_event(selector, "click")``. Works for jQuery handlers
  and modal-open buttons. Does NOT follow ``<a href>`` navigation —
  Playwright dispatch_event only fires the JS event without the
  browser's default action.
- ``use_navigation_click=True``: ``page.click(selector)`` with proper
  actionability check + native browser click that DOES follow href
  navigation. Use when the click must navigate to a different page
  (e.g. amefri /special/freepoint → /game/gacha to satisfy referer
  requirements).

Per-wizard timing:
- ``initial_wait_ms`` (default 2000): wait after goto before first
  click. Lets JS finish wiring up handlers (jQuery bindings can run
  a beat after DOM-ready). Bump to 5000-8000 for SPA hubs whose
  navigation links / start buttons are hydrated after a second XHR
  (amefri stamp sub-games, getmoney NUMBERS DX rule page).
- ``inter_click_ms`` (default 300): wait between same-selector repeats
- ``inter_step_ms`` (default 800): wait between different selectors
  (lets modal panel animations settle before next selector becomes
  actionable)
- ``final_wait_ms`` (default 2500): wait after last click, before
  closing page. Bump to 30000+ for sites that play a video ad as
  reward credit prerequisite (pointtown 宝箱、fruitmail apricot CM).

Page setup:
- ``wait_until`` (default "domcontentloaded"): goto wait condition.
  Networkidle works for non-ad-heavy pages but most ad-fraud wizard
  targets have long-poll iframes — domcontentloaded is the safe
  default.
- ``referer`` (default None): goto referer header. Required when the
  target page server-side gates access by referer (e.g. amefri
  /game/gacha 302s to / unless referer is /special/freepoint).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DailyWizard:
    """One Playwright-driven click sequence run once per cron.

    ``name`` is just a label that shows up in logs. ``url`` is the
    page Playwright navigates to. ``clicks`` is an ordered list of
    ``(selector, repeat_count)`` — the orchestrator clicks each
    selector ``repeat_count`` times via dispatch_event (or page.click
    if ``use_navigation_click=True``) with a short inter-click wait,
    then a longer wait between selectors so the next panel can slide
    in.
    """

    name: str
    url: str
    clicks: tuple[tuple[str, int], ...]

    # Page setup
    wait_until: str = "domcontentloaded"
    referer: str | None = None

    # Timing (ms)
    initial_wait_ms: int = 2000
    inter_click_ms: int = 300
    inter_step_ms: int = 800
    final_wait_ms: int = 2500

    # Click semantics
    use_navigation_click: bool = False
    # When True, Playwright page.click() skips actionability check
    # (visibility / stability / receives_events). Right for sites whose
    # click targets are present in static HTML but covered by ad-network
    # iframes or sidebar collapses that hide them. Only effective when
    # ``use_navigation_click=True`` (dispatch_event has no force option).
    click_force: bool = False
