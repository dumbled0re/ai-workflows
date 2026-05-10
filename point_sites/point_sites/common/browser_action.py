"""Daily browser-driven side-effect actions per adapter.

For SPA sites whose login bonus / daily gacha credit only fires from
client JS — visiting the page in a real browser is the minimum,
sometimes a CSS selector also has to be clicked. ``BrowserAction``
declares one such interaction in adapter config; ``run_browser_actions``
drives them all in a single Chromium session.

Pattern parallels ``ClickCandidate`` / ``ClickResult`` for the email
click loop: the adapter declares values, the orchestrator runs them.
The site-specific knowledge stays in the adapter's
``browser_actions`` tuple — selectors, URLs, expected post-action
markers — and the executor stays generic.

The result type pins ``ok`` to None when the action couldn't even
attempt navigation, True when navigation+optional click succeeded
and any ``success_marker`` was found, False when something failed.
The summary view shown in Slack should treat False as a soft warning,
not a hard failure: a flapping selector shouldn't kill the whole run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .browser import BrowserClicker


@dataclass(frozen=True)
class BrowserAction:
    """One daily browser-driven action.

    ``url`` is what gets navigated to. ``click_selector`` is an
    optional CSS selector that gets clicked once the navigation
    settles — for sites where simply loading the page is not enough
    (e.g. a "回す" gacha button). ``success_marker`` is a substring
    looked up in the post-action DOM as a soft positive signal; missing
    isn't fatal because some sites credit silently.

    ``wait_after_click_ms`` keeps the session open long enough for the
    site's XHR result to settle before close — gachas with animations
    typically need 2-4s for the credit XHR to fire.
    """

    name: str
    url: str
    click_selector: str | None = None
    wait_after_click_ms: int = 2000
    success_marker: str | None = None


@dataclass(frozen=True)
class BrowserActionResult:
    name: str
    ok: bool
    message: str


def run_browser_actions(
    bc: BrowserClicker,
    actions: tuple[BrowserAction, ...],
) -> list[BrowserActionResult]:
    """Execute each action against the open BrowserClicker session.

    Caller controls the BrowserClicker lifecycle so multiple call sites
    (cmd_run, future ad-hoc tests) can reuse one Chromium boot. The
    function never raises — each action is wrapped so a brittle
    selector for one action doesn't skip the others.
    """
    out: list[BrowserActionResult] = []
    for action in actions:
        try:
            page = bc.goto(action.url)
        except Exception as exc:
            out.append(BrowserActionResult(action.name, False, f"navigation failed: {exc}"))
            continue
        try:
            if action.click_selector is not None:
                try:
                    page.click(action.click_selector)
                except Exception as exc:
                    out.append(
                        BrowserActionResult(
                            action.name,
                            False,
                            f"selector {action.click_selector!r} not actionable: {exc}",
                        ),
                    )
                    page.close()
                    continue
                page.wait_for_timeout(action.wait_after_click_ms)
            if action.success_marker is not None:
                content = page.content()
                if action.success_marker not in content:
                    out.append(
                        BrowserActionResult(
                            action.name,
                            False,
                            f"success_marker {action.success_marker!r} not found in DOM",
                        ),
                    )
                    page.close()
                    continue
            out.append(BrowserActionResult(action.name, True, "ok"))
        finally:
            with_close_safe(page)
    return out


def with_close_safe(page: object) -> None:
    """Best-effort page.close() that swallows already-closed errors.

    Same pattern as BrowserClicker.__exit__ — a torn-down page must
    not mask the original exception path.
    """
    try:
        page.close()  # type: ignore[attr-defined]
    except Exception:
        return
