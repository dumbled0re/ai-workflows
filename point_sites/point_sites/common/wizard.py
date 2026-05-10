"""Multi-step browser wizards triggered once per daily cron.

Used for site features whose credit is gated behind a small JS-driven
click sequence — e.g. hapitas's 宝くじ交換券 → バラ抽選券 wizard or
pointtown's daily login bonus modal. Each ``DailyWizard`` declares the
URL to navigate to and the ordered ``(selector, repeat_count)`` clicks
that advance the UI. cmd_run dispatches each click via
``page.dispatch_event("click", ...)`` so jQuery handlers fire even when
the target panel hasn't fully slid in (CSS animation timing).

Wizards are deliberately fail-soft: a missing or already-claimed
button times out the dispatch_event call, the wizard logs a warning,
and the run continues. That matches the daily reality where some
wizards (login bonus already claimed today, or 0 tickets to exchange)
legitimately have nothing to do.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DailyWizard:
    """One Playwright-driven click sequence run once per cron.

    ``name`` is just a label that shows up in logs. ``url`` is the
    page Playwright navigates to. ``clicks`` is an ordered list of
    ``(selector, repeat_count)`` — the orchestrator clicks each
    selector ``repeat_count`` times via dispatch_event with a short
    inter-click wait, then a longer wait between selectors so the
    next panel can slide in.
    """

    name: str
    url: str
    clicks: tuple[tuple[str, int], ...]
