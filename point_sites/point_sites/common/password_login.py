"""Password-based login fallback for sites whose cookie lifetime is short.

Cookie-only operation (Cookie-Editor export → GitHub Secret) is the
default for the framework — it sidesteps anti-bot login flows and keeps
the runner stateless. The trade-off is manual cookie refresh whenever
the session expires (fruitmail observed ~24h, pointtown ~20h, moppy
intermittent shadow-ban).

For sites where that refresh cadence is unacceptable, an adapter can
declare a ``PasswordLoginConfig`` and the orchestrator will perform a
fresh Playwright login from ``<SITE>_USER`` / ``<SITE>_PASS`` Secrets
when the persisted cookies fail verification. The rotated cookie jar
is captured back into the same store the cookie-only path uses, so the
remainder of the cron run proceeds identically.

Risk-management posture:
- TOS risk: most Japanese point sites prohibit automated login in their
  fine print. We mitigate by (a) reusing the rotated cookie jar so the
  login form is hit at most once per cookie lifetime and (b) gating the
  feature behind explicit per-site opt-in (adapter must set
  ``password_login`` non-None).
- Credential risk: User/Pass live in GitHub Secrets, accessible only at
  workflow runtime as env vars. We never log them and never persist
  them on disk; the rotated cookie jar is the only stored artifact.
- Failure mode: if password login fails (wrong creds, captcha,
  selector drift), the orchestrator falls back to the existing Slack
  alert — same as a cookie-only failure today. No silent retries.
"""

from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .browser import BrowserClicker

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PasswordLoginConfig:
    """Per-site configuration for a Playwright-driven login form fill.

    Selectors should be the simplest unique reference for the field —
    e.g. ``input[name="login_id"]`` or ``#email``. The submit selector
    is dispatched via ``page.dispatch_event("click", ...)`` to match
    the rest of the wizard framework (jQuery handlers, form-managed
    submit buttons both work).

    ``success_marker`` is the substring searched in the post-submit
    page content; reuse ``Adapter.login_keyword`` (typically
    "ログアウト") for sites where the redirect lands directly on
    a logged-in page with the standard marker visible.

    ``username_env`` / ``password_env`` default to ``<SITE>_USER`` /
    ``<SITE>_PASS`` derived from the adapter name. Adapters can
    override only when they share Secrets with another adapter (rare).
    """

    login_url: str
    username_selector: str
    password_selector: str
    submit_selector: str
    success_marker: str = "ログアウト"
    username_env: str | None = None
    password_env: str | None = None
    # Some forms reveal the password field only after the username step
    # is submitted (2-step auth pattern). If set, we click this selector
    # between filling username and filling password.
    intermediate_submit_selector: str | None = None

    def resolve_username_env(self, adapter_name: str) -> str:
        return self.username_env or f"{adapter_name.upper()}_USER"

    def resolve_password_env(self, adapter_name: str) -> str:
        return self.password_env or f"{adapter_name.upper()}_PASS"


def login_with_password(
    bc: BrowserClicker,
    config: PasswordLoginConfig,
    adapter_name: str,
) -> bool:
    """Fresh-login flow using ``BrowserClicker``'s Playwright context.

    Returns True iff post-submit page contains ``success_marker``. On
    failure the caller falls back to the same Slack alert path as a
    cookie-only verification failure — distinguishing "stale cookies"
    from "credentials wrong" in the alert text is the caller's job.

    The rotated cookie jar is left on the BrowserClicker context;
    ``bc.export_cookies()`` then captures it for the rest of the run.
    """
    username_env = config.resolve_username_env(adapter_name)
    password_env = config.resolve_password_env(adapter_name)
    username = os.environ.get(username_env)
    password = os.environ.get(password_env)
    if not username or not password:
        logger.warning(
            "password login skipped: %s or %s not set",
            username_env,
            password_env,
        )
        return False

    try:
        page = bc.goto(config.login_url, wait_until="domcontentloaded")
    except Exception as exc:
        logger.warning("password login navigation failed: %s", exc)
        return False
    try:
        # Let any login form JS finish wiring up before we fill — same
        # 2s buffer the daily-wizard runner uses for jQuery handlers.
        page.wait_for_timeout(2000)
        page.fill(config.username_selector, username)
        if config.intermediate_submit_selector:
            page.dispatch_event(config.intermediate_submit_selector, "click", timeout=5000)
            page.wait_for_timeout(1500)
        page.fill(config.password_selector, password)
        page.dispatch_event(config.submit_selector, "click", timeout=5000)
        # networkidle so the post-login redirect and any session-cookie
        # set-cookies finish landing before we check the marker. ad-heavy
        # sites can keep polling forever; fall through and check content
        # anyway — the success_marker test is the real signal here.
        with contextlib.suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=15_000)
        # SSO sites (pointtown → id.gmo.jp → pointtown.com) take an
        # additional 3-5s for the cross-domain redirect chain to land
        # on the final logged-in page. networkidle may return early on
        # the SSO host before pointtown's session redirect fires. Give
        # the chain room to finish before content() check.
        page.wait_for_timeout(5000)
        content = page.content()
        ok = config.success_marker in content
        if ok:
            logger.info("password login succeeded (success_marker found)")
            bc.authenticated = True
        else:
            logger.warning("password login submitted but success_marker not found")
        return ok
    except Exception as exc:
        logger.warning("password login form interaction failed: %s", exc)
        return False
    finally:
        with contextlib.suppress(Exception):
            page.close()
