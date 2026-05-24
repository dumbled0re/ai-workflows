"""point_sites CLI entry point.

The same code path works for every adapter — pass ``--site <name>`` to
choose between Moppy, ポイントインカム, etc. All site-specific values
(URLs, regexes, Gmail queries, labels) come from the adapter's
``Adapter`` instance; this module just orchestrates.

Subcommands:
  run [--site X]         fetch → parse → click → notify
  click <URL> [--site X] manual single-URL click (host whitelist per adapter)
  state [--site X]       dump state for a single message_id
  balance [--site X]     fetch and print current point balance
  discover [--site X]    read-only crawl of the daily-earn section
  html <URL> [--site X]  GET a URL with auth, dump body (debug)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from .adapters import REGISTRY, get_adapter
from .common.adapter import Adapter
from .common.balance import fetch_balance
from .common.clicker import Clicker, is_manual_url_allowed
from .common.cookie_store import domain_matches_hosts
from .common.cookie_store import load as load_persisted_cookies
from .common.cookie_store import save_jar as save_cookie_jar
from .common.discover import discover, render_report
from .common.gmail_client import GmailAuthError
from .common.models import ClickCandidate, RunSummary
from .common.notifier import Notifier
from .common.outcome_tracker import OutcomeTracker, make_outcome
from .common.password_login import PasswordLoginConfig
from .common.redaction import host_only, redact_subject, redact_url
from .common.sources.base import ClickBatch
from .common.state_store import StateStore
from .config import Config, ConfigError

logger = logging.getLogger("point_sites")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _migrate_legacy_data_paths(adapter: Adapter, data_root: str = "data") -> None:
    """Move ``data/<file>`` → ``data/<site>/<file>`` if legacy layout detected.

    Pre-multi-site runs put state.json, cookies.json, outcomes.jsonl
    directly under ``data/``. Per-site reorganization moved them under
    ``data/<site>/``. The cache from a pre-refactor run restores to the
    flat layout; this one-shot migration moves the files in place so we
    don't lose the rotated cookie jar (which would force re-bootstrap
    from the original MOPPY_COOKIES Secret and likely kill the session).

    Only runs for the Moppy adapter — other sites never had the flat
    layout and shouldn't pick up stray files.
    """
    if adapter.name != "moppy":
        return
    legacy_root = Path(data_root)
    new_root = legacy_root / adapter.name
    for fname in ("state.json", "cookies.json", "outcomes.jsonl"):
        legacy = legacy_root / fname
        new = new_root / fname
        if legacy.exists() and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            legacy.rename(new)
            logger.info("migrated legacy %s → %s", legacy, new)


def _resolve_cookies(cfg: Config) -> list[dict[str, object]] | None:
    """Prefer persisted post-rotation cookies over the bootstrap Secret.

    Many sites rotate session cookies on each request; submitting the
    stale Secret value on a subsequent run gets the session killed. The
    persisted jar from the previous run carries the latest rotation.
    """
    persisted = load_persisted_cookies(cfg.cookie_store_path)
    if persisted is not None:
        logger.info("using persisted cookie jar (%d cookies)", len(persisted))
        return persisted
    if cfg.cookies is not None:
        logger.info(
            "no persisted cookie jar; bootstrapping from %s (%d cookies)",
            cfg.adapter.cookies_env,
            len(cfg.cookies),
        )
        return cfg.cookies
    return None


def _persist_cookies(clicker: Clicker, cfg: Config) -> None:
    """Save the live jar so the next process picks up rotated values.

    Called after successful authenticated work. Failures are logged and
    swallowed: the run already succeeded, and a state-write hiccup
    shouldn't break the user-visible result. Worst case the next run
    starts from the stale Secret again.

    Cookies whose domain is not covered by ``adapter.allowed_hosts`` are
    dropped at save time — Playwright wizards can pick up hundreds of
    third-party tracking cookies (analytics / ads) from pages that
    embed those scripts, and re-sending them on the next run looks
    like an anomalous session and gets the site to invalidate it
    (observed 2026-05-15 on pointtown).
    """
    try:
        n = save_cookie_jar(
            clicker.session.cookies,
            cfg.cookie_store_path,
            allowed_hosts=cfg.adapter.allowed_hosts,
        )
        logger.info("persisted %d cookies to %s", n, cfg.cookie_store_path)
    except OSError as exc:
        logger.warning("failed to persist cookie jar: %s", exc)


def _build_clicker(cfg: Config, cookies: list[dict[str, object]]) -> Clicker:
    """Construct a Clicker with site-specific defaults from the adapter."""
    # Default cookie domain is derived from the mypage host (e.g.
    # "pc.moppy.jp" → ".moppy.jp"). Adapter values override per-cookie.
    from urllib.parse import urlparse

    host = urlparse(cfg.adapter.mypage_url).hostname or ""
    default_domain = "." + (host.split(".", 1)[-1] if "." in host else host)
    return Clicker(
        interval_min=cfg.click_interval_min,
        interval_max=cfg.click_interval_max,
        cookies=cookies,
        default_cookie_domain=default_domain,
    )


def _verify_login(clicker: Clicker, cfg: Config) -> bool:
    """Verify session is logged in; fall back to password login if configured.

    1. Try the cookie-based verification (existing path).
    2. On failure, if ``adapter.password_login`` is set AND the matching
       ``<SITE>_USER`` / ``<SITE>_PASS`` env vars are set, run a
       Playwright login flow that fills the form and captures the
       rotated cookie jar back into ``clicker``.
    3. Re-verify with cookies. Failure here falls through to the
       existing Slack-alert path.

    Setting ``FORCE_PASSWORD_LOGIN_TEST=1`` skips step 1 entirely so
    the fallback can be exercised even with valid cookies — used to
    verify password_login wiring during framework development.
    """
    if not os.environ.get("FORCE_PASSWORD_LOGIN_TEST"):
        if clicker.verify_login(cfg.adapter.mypage_url, cfg.adapter.login_keyword):
            return True
    else:
        logger.info("FORCE_PASSWORD_LOGIN_TEST=1 — skipping cookie verify, exercising password_login fallback directly")
    pw_login = cfg.adapter.password_login
    if pw_login is None:
        return False
    if not os.environ.get(pw_login.resolve_username_env(cfg.adapter.name)):
        return False
    return _attempt_password_login(clicker, cfg, pw_login)


def _attempt_password_login(
    clicker: Clicker,
    cfg: Config,
    pw_login: PasswordLoginConfig,
) -> bool:
    """Open a fresh BrowserClicker session, fill the login form, merge cookies back.

    The Playwright session is started **without** any existing cookies
    so the site's login URL doesn't redirect to a logged-in page (which
    would hide the form and cause page.fill to time out on the missing
    selector). Once login succeeds, the rotated cookie jar is merged
    back into the Clicker that drives the rest of the run.
    """
    from .common.browser import BrowserClicker
    from .common.password_login import login_with_password

    logger.info("cookie verification failed — attempting password login")
    with BrowserClicker(cookies=None) as bc:
        ok = login_with_password(bc, pw_login, cfg.adapter.name)
        if not ok:
            return False
        _merge_browser_cookies(
            clicker,
            bc.export_cookies(),
            allowed_hosts=cfg.adapter.allowed_hosts,
        )
    # Re-verify via the now-rotated Clicker jar so the rest of the run
    # sees the same authenticated state the BrowserClicker did.
    return clicker.verify_login(cfg.adapter.mypage_url, cfg.adapter.login_keyword)


def _jar_to_cookies(clicker: Clicker) -> list[dict[str, object]]:
    """Project the live ``requests.Session`` jar into the persisted shape.

    Same shape as ``cookie_store.save_jar`` writes — name/value/domain/
    path/secure — so ``BrowserClicker`` can be initialized from a Clicker
    session without going through disk.
    """
    out: list[dict[str, object]] = []
    for c in clicker.session.cookies:
        out.append(
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain or "",
                "path": c.path or "/",
                "secure": bool(c.secure),
            },
        )
    return out


def _merge_browser_cookies(
    clicker: Clicker,
    browser_cookies: list[dict[str, object]],
    allowed_hosts: frozenset[str] | None = None,
) -> None:
    """Merge cookies rotated by a BrowserClicker session back into Clicker.

    Without this the click loop and the persisted jar would lose any
    Set-Cookie updates the browser saw — and BrowserClicker is the one
    most likely to trip JS-driven anti-bot rotations. Same .set() pattern
    Clicker.__init__ uses so the cookie semantics stay identical.

    When ``allowed_hosts`` is provided, only cookies whose domain
    covers one of those hosts are merged. The rest (third-party
    analytics / ad / tracker cookies the Playwright wizard picked up
    incidentally) are dropped on the floor — re-sending them to the
    site on subsequent requests bloats the cookie header and can
    trigger anti-bot session invalidation.
    """
    for c in browser_cookies:
        name = str(c.get("name", ""))
        if not name:
            continue
        domain = str(c.get("domain", ""))
        if allowed_hosts is not None and not domain_matches_hosts(domain, allowed_hosts):
            continue
        clicker.session.cookies.set(
            name,
            str(c.get("value", "")),
            domain=domain,
            path=str(c.get("path", "/")),
            secure=bool(c.get("secure", True)),
        )


def _fetch_balance(clicker: Clicker, cfg: Config) -> int | None:
    if cfg.adapter.balance_uses_browser:
        # Lazy import keeps Playwright off the import path of adapters
        # that don't opt in (no chromium download cost on those runs).
        from urllib.parse import urlparse

        from .common.browser import BrowserClicker

        host = urlparse(cfg.adapter.mypage_url).hostname or ""
        default_domain = "." + (host.split(".", 1)[-1] if "." in host else host)
        browser_cookies_in = _jar_to_cookies(clicker)
        try:
            with BrowserClicker(
                cookies=browser_cookies_in,
                default_cookie_domain=default_domain,
            ) as bc:
                balance = bc.fetch_balance(
                    cfg.adapter.mypage_url,
                    patterns=cfg.adapter.balance_patterns,
                )
                _merge_browser_cookies(clicker, bc.export_cookies(), allowed_hosts=cfg.adapter.allowed_hosts)
        except Exception as exc:
            logger.warning("browser balance fetch failed: %s", exc)
            return None
        return balance
    return fetch_balance(
        clicker.session,
        cfg.adapter.mypage_url,
        patterns=cfg.adapter.balance_patterns,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="point_sites")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # --site is added per-subcommand instead of as a global so older
    # invocations like `python -m point_sites.main run` keep working
    # with the default (moppy).
    def add_site_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--site",
            choices=sorted(REGISTRY),
            default="moppy",
            help="which point-site adapter to use (default: moppy)",
        )

    p_run = sub.add_parser("run", help="fetch and click")
    add_site_arg(p_run)
    p_run.add_argument("--dry-run", action="store_true", help="extract only, no click (redacted)")
    p_run.add_argument(
        "--extract-links",
        action="store_true",
        help=(
            "post full clickable URLs to Slack instead of clicking; "
            "no state changes, no labels — for manual user-driven clicks"
        ),
    )
    p_run.add_argument("--max-messages", type=int, default=None)
    p_run.add_argument("--no-notify", action="store_true")

    p_click = sub.add_parser("click", help="manual single-URL click (adapter host whitelist)")
    add_site_arg(p_click)
    p_click.add_argument("url")

    p_state = sub.add_parser("state", help="dump state for a message_id")
    add_site_arg(p_state)
    p_state.add_argument("--message-id", required=True)

    p_balance = sub.add_parser("balance", help="fetch and print current point balance")
    add_site_arg(p_balance)

    p_discover = sub.add_parser(
        "discover",
        help="read-only crawl of the daily-earn section; prints a structural report (no clicks)",
    )
    add_site_arg(p_discover)

    p_html = sub.add_parser(
        "html",
        help="GET a URL with auth and print its body (debug, capped at 80KB stripped)",
    )
    add_site_arg(p_html)
    p_html.add_argument("url")
    p_html.add_argument(
        "--browser",
        action="store_true",
        help="render the URL via Playwright Chromium so JS runs and SPA shells expand",
    )
    p_html.add_argument(
        "--cap",
        type=int,
        default=80_000,
        help=(
            "max stripped-body bytes to print (default: 80000). Bump for "
            "SPA pages where the relevant section is past 80KB."
        ),
    )
    p_html.add_argument(
        "--wait-selector",
        default=None,
        help=(
            "browser mode only: wait for this CSS selector to appear "
            "before reading page.content(). Required for fully-SPA "
            "sites (e.g. warau) where server-side HTML is a shell."
        ),
    )
    p_html.add_argument(
        "--wait-timeout-ms",
        type=int,
        default=15_000,
        help="bound for --wait-selector (default 15000ms)",
    )
    p_html.add_argument(
        "--anonymous",
        action="store_true",
        help=(
            "skip cookie loading and login verification — for inspecting "
            "login forms and other pages where logged-in sessions redirect away"
        ),
    )
    p_html.add_argument(
        "--capture-network",
        action="store_true",
        help=(
            "browser mode only: log all network requests the page makes "
            "(method + url + resource_type). Useful for discovering API "
            "endpoints behind fully-SPA sites (e.g. warau)."
        ),
    )
    p_html.add_argument(
        "--wait-until",
        default="networkidle",
        choices=["networkidle", "domcontentloaded", "load", "commit"],
        help=(
            "browser mode only: page.goto wait condition. Default "
            "``networkidle`` is safe for normal SPAs but ad-heavy pages "
            "(sugutama mypage, warau /game/list) hang because long-poll "
            "ad iframes never settle — use ``domcontentloaded`` paired "
            "with --wait-selector to gate on real content."
        ),
    )
    return parser


def cmd_run(
    cfg: Config,
    dry_run: bool,
    extract_links: bool,
    max_messages: int | None,
    notify: bool,
) -> int:
    if dry_run and extract_links:
        logger.error("--dry-run and --extract-links are mutually exclusive")
        return 2
    started_at = datetime.now(UTC)
    notifier = Notifier(cfg.slack_bot_token, cfg.slack_channel) if notify else None

    source = cfg.adapter.source
    if source is None:
        # Adapters without a click-URL source (e.g. balance-only test fixtures)
        # have nothing to drive the run loop with. Fail fast rather than no-op.
        logger.error("adapter %r has no source; cmd_run cannot proceed", cfg.adapter.name)
        return 2

    state = StateStore(cfg.state_path)
    state.prune_old(days=30)

    parse_failure_ids: list[str] = []
    anomaly_ids: list[str] = []
    all_results = []
    estimated_pt_total = 0
    dry_run_view: list[tuple[str, str, list[str]]] = []
    extract_view: list[tuple[str, str, list[str]]] = []
    # Batches that fed extract_view, in the same order — so after a
    # successful Slack delivery we can label them on the source side
    # and they won't be re-posted on the next cron run. Without this
    # the same click-mail would surface every day until ``newer_than``
    # in the Gmail query naturally prunes it.
    extract_batches: list[ClickBatch] = []

    # Build the Clicker when (a) we'll actually click, or (b) cookies are
    # present and the source might need an authenticated http_session for
    # enumeration (e.g. ``OnsiteInboxSource`` reading an on-site mailbox
    # via extract-links). The Gmail/endpoint-poll sources ignore
    # http_session, so the only "extra" cost in case (b) is building a
    # requests Session with the cookie jar — cheap.
    clicker: Clicker | None = None
    cookies = _resolve_cookies(cfg)
    if cookies is not None or not extract_links:
        clicker = _build_clicker(cfg, cookies or [])
        if cookies is None:
            clicker.authenticated = False

    if not dry_run and not extract_links and clicker is not None and not clicker.authenticated:
        # Anonymous clicks return HTTP 200 but the site does NOT credit points.
        # Recording these as "clicked" would also block later credited retries
        # because the email would already be labeled and skipped.
        msg = (
            f"{cfg.adapter.cookies_env} is not set; refusing to run. Anonymous clicks would "
            "be marked as completed without crediting points, blocking future "
            f"credited retries. Set {cfg.adapter.cookies_env} or use --dry-run."
        )
        logger.error(msg)
        if notifier:
            notifier.send_auth_error(msg)
        return 1
    # Cookie health check runs regardless of mode (click / extract /
    # dry-run) so silent expiry never goes unreported. Click mode
    # exits 1 because anonymous-but-marked-clicks would block retries;
    # extract / dry-run continue (no side effects from a stale-cookie
    # read), but Slack still gets the auth_error so the operator knows
    # to rotate the Secret. Adapters without cookies skip verify
    # entirely.
    if clicker is not None and clicker.authenticated and not dry_run:
        if not _verify_login(clicker, cfg):
            msg = (
                f"{cfg.adapter.site_label} login verification failed: cookies are stale or invalid. "
                f"対応: (1) ブラウザで {cfg.adapter.site_label} にログイン → "
                f"Cookie-Editor で JSON export → "
                f"GitHub Secret `{cfg.adapter.cookies_env}` を更新 → "
                f"(2) workflow を `force_fresh_cookies=true` で手動 dispatch"
            )
            logger.error(msg)
            if notifier:
                notifier.send_auth_error(msg)
            if not extract_links:
                return 1
        else:
            logger.info(
                "%s login verified — clicks will be credited to the account",
                cfg.adapter.site_label,
            )
            # Persist immediately after the verify_login GET so the rotated
            # cookies survive even if a later step crashes.
            _persist_cookies(clicker, cfg)

    # Capture the pre-click balance so the post-run summary can prove
    # whether points actually credited. Only meaningful in real click mode;
    # dry-run / extract-links don't trigger any click side-effects.
    balance_before: int | None = None
    if not dry_run and not extract_links and clicker is not None and clicker.authenticated:
        balance_before = _fetch_balance(clicker, cfg)
        if balance_before is not None:
            logger.info("balance before clicks: %d pt", balance_before)

    # Source caps max_messages internally via cfg.max_messages; allow CLI
    # ``--max-messages`` to tighten that for this run only.
    effective_cfg = cfg if max_messages is None else replace(cfg, max_messages=max_messages)
    http_session = clicker.session if clicker is not None else None

    state_keys: list[str] = []
    try:
        source.start(effective_cfg, http_session=http_session)
    except GmailAuthError as exc:
        logger.error("auth error: %s", exc)
        if notifier:
            notifier.send_auth_error(str(exc))
        return 1

    try:
        state_keys = source.list_state_keys()
        logger.info("found %d candidate batches", len(state_keys))

        for state_key in state_keys:
            # Extract mode bypasses state — its job is to surface every URL so
            # the user can click manually. Stale anonymous-success records and
            # exhausted-attempt records would otherwise hide URLs the user
            # never actually got credit for.
            if not extract_links and state.is_message_complete(state_key, cfg.max_attempts):
                continue
            batch = source.fetch_batch(state_key)
            if batch.parse_failed:
                parse_failure_ids.append(state_key)
                continue
            if batch.anomalies:
                logger.warning(
                    "anomalous parse for %s: anomalies=%s candidates=%d",
                    state_key,
                    batch.anomalies,
                    len(batch.candidates),
                )
                anomaly_ids.append(state_key)
                continue
            if not batch.candidates:
                # No click-coin URLs: legitimate non-coin batch (newsletter,
                # confirmation, etc.). Source decides how to mark it.
                # Skip in dry_run / extract_links to keep state pristine.
                if not dry_run and not extract_links:
                    source.mark_no_credit(batch)
                continue

            new_candidates: list[ClickCandidate] = [
                c for c in batch.candidates if not state.is_url_done(state_key, str(c.url), cfg.max_attempts)
            ]

            if dry_run:
                dry_run_view.append(
                    (
                        state_key,
                        redact_subject(batch.label),
                        [redact_url(str(c.url)) for c in new_candidates],
                    )
                )
                continue

            if extract_links:
                # Post full URLs so the user can click in their logged-in browser.
                # Labels are NOT redacted here (private channel, user needs
                # to triage). State is intentionally untouched so re-runs after
                # a manual click won't be blocked.
                extract_view.append(
                    (
                        state_key,
                        batch.label,
                        [str(c.url) for c in batch.candidates],
                    )
                )
                extract_batches.append(batch)
                continue

            if not new_candidates:
                continue

            assert clicker is not None  # narrowed by the extract_links branch above
            state.increment_attempt(state_key)
            new_urls = {c.url for c in new_candidates}
            for idx, candidate in enumerate(new_candidates):
                if idx > 0:
                    clicker.sleep_between()
                result = clicker.click(candidate)
                state.record_attempt(state_key, result)
                all_results.append(result)
                if result.final_status == "success" and candidate.estimated_points:
                    estimated_pt_total += candidate.estimated_points
            state.save()

            if state.is_message_complete(state_key, cfg.max_attempts) and all(
                r.final_status == "success" for r in all_results if r.candidate.url in new_urls
            ):
                source.mark_complete(batch)
    finally:
        source.close()

    # Discover daily-rotating banner URLs (e.g. hapitas top-page
    # 宝くじ交換券 click_get banners) via Playwright and feed them
    # through the existing Clicker pipeline so each click is tracked
    # alongside the email-driven clicks. Skipped on dry/extract runs.
    if (
        cfg.adapter.daily_banner_url
        and cfg.adapter.daily_banner_selector
        and not dry_run
        and not extract_links
        and clicker is not None
        and clicker.authenticated
    ):
        from urllib.parse import urljoin, urlparse

        from .common.browser import BrowserClicker

        host = urlparse(cfg.adapter.mypage_url).hostname or ""
        default_domain = "." + (host.split(".", 1)[-1] if "." in host else host)
        try:
            with BrowserClicker(
                cookies=_jar_to_cookies(clicker),
                default_cookie_domain=default_domain,
            ) as bc:
                page = bc.goto(cfg.adapter.daily_banner_url)
                try:
                    anchors = page.query_selector_all(cfg.adapter.daily_banner_selector)
                    raw_hrefs = [a.get_attribute("href") for a in anchors]
                    # The site renders relative hrefs (``/item/redirect-...``);
                    # urljoin against the inspect URL keeps host-only banners
                    # attached to the right host.
                    discovered = [urljoin(cfg.adapter.daily_banner_url, h) for h in raw_hrefs if h]
                    # De-dupe while preserving discovery order. Hapitas
                    # renders both the banner image link and a separate
                    # dialog link to the same target — clicking once is
                    # all the credit we'll get.
                    seen: set[str] = set()
                    unique_hrefs: list[str] = []
                    for href in discovered:
                        if href not in seen:
                            seen.add(href)
                            unique_hrefs.append(href)
                finally:
                    page.close()
                _merge_browser_cookies(clicker, bc.export_cookies(), allowed_hosts=cfg.adapter.allowed_hosts)
            logger.info("discovered %d unique daily banners", len(unique_hrefs))
            for href in unique_hrefs:
                candidate = ClickCandidate(
                    url=href,  # type: ignore[arg-type]
                    anchor_text="<daily_banner>",
                    extraction_reason="daily_banner_discover",
                )
                result = clicker.click(candidate)
                all_results.append(result)
                clicker.sleep_between()
        except Exception as exc:
            logger.warning("daily banner discover/click failed: %s", exc)

    # Run each configured DailyWizard (e.g. hapitas takarakuji exchange,
    # pointtown login bonus modal). Each wizard gets its own Chromium
    # session so a stuck panel in one doesn't poison another. dispatch_event
    # bypasses pointer-events interception from sibling panels during
    # the wizard's slide-in animations.
    if (
        cfg.adapter.daily_wizards
        and not dry_run
        and not extract_links
        and clicker is not None
        and clicker.authenticated
    ):
        from urllib.parse import urlparse

        from .common.browser import BrowserClicker

        host = urlparse(cfg.adapter.mypage_url).hostname or ""
        default_domain = "." + (host.split(".", 1)[-1] if "." in host else host)
        for wizard in cfg.adapter.daily_wizards:
            try:
                with BrowserClicker(
                    cookies=_jar_to_cookies(clicker),
                    default_cookie_domain=default_domain,
                ) as bc:
                    # ``wait_until`` defaults to ``domcontentloaded``:
                    # ad-heavy mypages (pointtown carries Criteo + double-
                    # click polling iframes that never settle) blow past
                    # the 30s ``networkidle`` timeout. Per-wizard
                    # override lets edge cases (e.g. ``commit`` for slow
                    # SSO chains) pick a different condition.
                    #
                    # ``referer`` is for sites that gate access by referer
                    # header (amefri /game/gacha returns 302 to / unless
                    # the request came from /special/freepoint). When
                    # set, navigate first to a blank-ish page then issue
                    # the real goto with referer (Playwright's
                    # page.goto supports referer kwarg via the
                    # underlying API but BrowserClicker.goto wraps it).
                    if wizard.referer:
                        page = bc.new_page()
                        page.goto(
                            wizard.url,
                            wait_until=wizard.wait_until,  # type: ignore[arg-type]
                            referer=wizard.referer,
                        )
                    else:
                        page = bc.goto(wizard.url, wait_until=wizard.wait_until)
                    completed = False
                    try:
                        # Let JS finish wiring up the wizard click handlers
                        # before we start firing clicks; jQuery $()
                        # bindings can run a beat after DOM-ready. SPA
                        # hubs (amefri stamp、getmoney NUMBERS DX rule)
                        # need 5-8s instead of the default 2s — bump
                        # ``initial_wait_ms`` per-wizard.
                        page.wait_for_timeout(wizard.initial_wait_ms)
                        for step_idx, (selector, repeat) in enumerate(wizard.clicks):
                            for _ in range(repeat):
                                # Click semantics:
                                # - dispatch_event: fires JS click handler
                                #   without going through the browser's
                                #   default action (so <a href> doesn't
                                #   navigate). Right for jQuery-bound
                                #   modal buttons.
                                # - page.click: native browser click with
                                #   actionability check + follows default
                                #   action including href navigation.
                                #   Right for navigation links.
                                try:
                                    if wizard.use_navigation_click:
                                        # force=click_force lets us bypass
                                        # actionability check when the
                                        # target element is present but
                                        # covered by ad-iframes. Default
                                        # False keeps the strict check.
                                        page.click(
                                            selector,
                                            timeout=5000,
                                            force=wizard.click_force,
                                        )
                                    else:
                                        page.dispatch_event(selector, "click", timeout=5000)
                                except Exception as exc:
                                    logger.warning(
                                        "%s wizard step %d (%s) failed: %s",
                                        wizard.name,
                                        step_idx,
                                        selector,
                                        exc,
                                    )
                                    raise
                                page.wait_for_timeout(wizard.inter_click_ms)
                            # Longer wait between steps to let the panel
                            # animation settle before the next selector
                            # becomes actionable.
                            page.wait_for_timeout(wizard.inter_step_ms)
                        # Final settle for the credit XHR + success pane.
                        # Bump per-wizard for video-watching wizards
                        # (動画 CM 視聴 / 動画広告 30s 視聴 等).
                        page.wait_for_timeout(wizard.final_wait_ms)
                        completed = True
                    except Exception:
                        pass
                    finally:
                        page.close()
                    _merge_browser_cookies(clicker, bc.export_cookies(), allowed_hosts=cfg.adapter.allowed_hosts)
                logger.info(
                    "%s wizard %s",
                    wizard.name,
                    "succeeded" if completed else "failed",
                )
            except Exception as exc:
                logger.warning("%s wizard session failed: %s", wizard.name, exc)

    # Run any browser-driven daily actions (login bonus visits, gacha
    # spins, banner clicks). One Chromium boot covers all of them so
    # the per-action overhead stays low. Skipped on dry/extract runs
    # for the same reason the click loop is.
    browser_action_names: list[str] = []
    browser_action_failures: list[str] = []
    if (
        cfg.adapter.browser_actions
        and not dry_run
        and not extract_links
        and clicker is not None
        and clicker.authenticated
    ):
        from urllib.parse import urlparse

        from .common.browser import BrowserClicker
        from .common.browser_action import run_browser_actions

        host = urlparse(cfg.adapter.mypage_url).hostname or ""
        default_domain = "." + (host.split(".", 1)[-1] if "." in host else host)
        try:
            with BrowserClicker(
                cookies=_jar_to_cookies(clicker),
                default_cookie_domain=default_domain,
            ) as bc:
                action_results = run_browser_actions(bc, cfg.adapter.browser_actions)
                _merge_browser_cookies(clicker, bc.export_cookies(), allowed_hosts=cfg.adapter.allowed_hosts)
            for r in action_results:
                browser_action_names.append(r.name)
                if r.ok:
                    logger.info("browser action %s: %s", r.name, r.message)
                else:
                    logger.warning("browser action %s failed: %s", r.name, r.message)
                    browser_action_failures.append(f"{r.name}: {r.message}")
        except Exception as exc:
            logger.warning("browser actions session failed: %s", exc)
            browser_action_failures.append(f"session: {exc}")

    # Snapshot the balance again *after* the click loop so we can compare
    # against ``balance_before``. Skipped on dry/extract runs (no clicks
    # were issued) and on failed-auth paths (we already returned earlier).
    balance_after: int | None = None
    if not dry_run and not extract_links and clicker is not None and clicker.authenticated:
        balance_after = _fetch_balance(clicker, cfg)
        if balance_after is not None:
            logger.info("balance after clicks: %d pt", balance_after)
        # Save the jar one more time at the very end so all rotation
        # from the click loop is captured for the next run.
        _persist_cookies(clicker, cfg)

    finished_at = datetime.now(UTC)
    summary = RunSummary(
        started_at=started_at,
        finished_at=finished_at,
        messages_processed=len(state_keys),
        candidates_total=len(all_results),
        success_count=sum(1 for r in all_results if r.final_status == "success"),
        failure_count=sum(1 for r in all_results if r.final_status != "success"),
        parse_failures=parse_failure_ids,
        anomaly_messages=anomaly_ids,
    )

    # Record outcome and check for persistent crediting failures. Only the
    # real click path produces a meaningful outcome; dry/extract runs are
    # skipped so they don't muddy the time series.
    degradation = None
    prior_balance_after: int | None = None
    if not dry_run and not extract_links:
        tracker = OutcomeTracker(cfg.outcome_path)
        # Look up the most recent prior outcome with a recorded
        # ``balance_after`` so the Slack summary can show inter-run
        # delta (= credits landed between the previous cron and this
        # one). Done BEFORE appending today's outcome so we don't pick
        # up the row we're about to write.
        for prior in reversed(tracker.recent(20)):
            prior_after = prior.get("balance_after")
            if isinstance(prior_after, int):
                prior_balance_after = prior_after
                break
        outcome = make_outcome(
            mode="click",
            messages_found=len(state_keys),
            click_success=summary.success_count,
            click_fail=summary.failure_count,
            expected_pt=estimated_pt_total,
            balance_before=balance_before,
            balance_after=balance_after,
        )
        tracker.append(outcome)
        degradation = tracker.detect_degradation(
            stagnation_window=cfg.adapter.stagnation_window,
        )
        if degradation is not None:
            logger.warning(
                "credit-ratio degradation detected over last %d runs (median=%.0f%%)",
                degradation.runs_inspected,
                degradation.median_ratio * 100,
            )

    extract_post_failed = False
    if notifier:
        if dry_run:
            notifier.send_dry_run(dry_run_view)
        elif extract_links:
            extract_ok = notifier.send_extract_links(
                extract_view,
                date_label=started_at.strftime("%Y-%m-%d"),
            )
            if not extract_ok:
                extract_post_failed = True
                logger.error(
                    "extract-links Slack delivery failed; the run will exit non-zero "
                    "so the workflow surfaces the failure"
                )
            elif extract_batches:
                # Slack delivery succeeded — reopen the source briefly to
                # mark each extracted batch as complete so the same
                # click-mails don't keep surfacing on the next cron run.
                # Uses ``clicked_label`` because semantically the run has
                # finished its work on the mail; the user does the actual
                # clicking in their browser. Without this, ``newer_than:Nd``
                # in the Gmail query would cause daily re-posting.
                try:
                    source.start(effective_cfg, http_session=http_session)
                    for extracted_batch in extract_batches:
                        try:
                            source.mark_complete(extracted_batch)
                        except Exception:
                            logger.exception(
                                "failed to mark extracted batch %s complete; URL may re-surface on next run",
                                extracted_batch.state_key,
                            )
                except GmailAuthError as exc:
                    logger.warning(
                        "could not reopen source for extract-mode labeling: %s; "
                        "extracted URLs may re-surface on next run",
                        exc,
                    )
                finally:
                    source.close()
        else:
            notifier.send_summary(
                summary,
                all_results,
                estimated_pt_total,
                balance_before=balance_before,
                balance_after=balance_after,
                prior_balance_after=prior_balance_after,
                degradation=degradation,
            )
        if parse_failure_ids:
            notifier.send_parse_failure(parse_failure_ids, "MIME/HTML decode")
        if anomaly_ids:
            notifier.send_parse_failure(anomaly_ids, "structural anomaly (template change?)")

    state.save()
    return 1 if extract_post_failed else 0


def cmd_click(cfg: Config, url: str) -> int:
    if not is_manual_url_allowed(url, cfg.adapter.allowed_hosts):
        print(f"refused: {url} is not under {cfg.adapter.site_label} hosts", file=sys.stderr)
        return 2
    cookies = _resolve_cookies(cfg)
    if cookies is None:
        print(
            f"refused: {cfg.adapter.cookies_env} is not set; anonymous clicks do not credit points. "
            f"Set {cfg.adapter.cookies_env} to enable credited single-URL clicks.",
            file=sys.stderr,
        )
        return 2
    candidate = ClickCandidate(
        url=url,  # type: ignore[arg-type]
        anchor_text="<manual>",
        extraction_reason="whitelist_url_pattern",
    )
    clicker = _build_clicker(cfg, cookies)
    if not _verify_login(clicker, cfg):
        print(
            f"refused: {cfg.adapter.site_label} login verification failed (stale or invalid cookies)",
            file=sys.stderr,
        )
        return 2
    _persist_cookies(clicker, cfg)
    result = clicker.click(candidate)
    _persist_cookies(clicker, cfg)
    print(
        f"status={result.final_status} http={result.http_status} "
        f"host={result.final_host or host_only(url)} duration={result.duration_ms}ms"
    )
    return 0 if result.final_status == "success" else 1


def cmd_state(cfg: Config, message_id: str) -> int:
    state = StateStore(cfg.state_path)
    msg = state._state.messages.get(message_id)
    if msg is None:
        print(f"no state for {message_id}")
        return 1
    print(msg.model_dump_json(indent=2))
    return 0


def cmd_discover(cfg: Config) -> int:
    """Crawl the daily-earn section read-only and dump a structural report.

    No clicks, no state mutation — guides which items can be added to the
    auto-click pipeline. Output goes to stdout (workflow log) rather than
    Slack so URLs don't end up in chat history.
    """
    cookies = _resolve_cookies(cfg)
    if cookies is None:
        print(f"refused: {cfg.adapter.cookies_env} is not set", file=sys.stderr)
        return 2
    clicker = _build_clicker(cfg, cookies)
    if not _verify_login(clicker, cfg):
        print(
            f"refused: {cfg.adapter.site_label} login verification failed (stale or invalid cookies)",
            file=sys.stderr,
        )
        return 2
    _persist_cookies(clicker, cfg)
    seeds = cfg.adapter.discover_seeds or (cfg.adapter.mypage_url,)
    reports = discover(clicker.session, seeds=seeds)
    _persist_cookies(clicker, cfg)
    print(render_report(reports))
    print()
    print("=== JSON ===")
    print(json.dumps([asdict(r) for r in reports], ensure_ascii=False, indent=2))
    return 0


def cmd_html(
    cfg: Config,
    url: str,
    *,
    force_browser: bool = False,
    cap: int = 80_000,
    wait_selector: str | None = None,
    wait_timeout_ms: int = 15_000,
    anonymous: bool = False,
    capture_network: bool = False,
    wait_until: str = "networkidle",
) -> int:
    """Fetch a single URL and dump its body to stdout.

    Used to plan automation for items whose interaction shape isn't
    obvious from discover's regex-based summary (e.g. JS-driven gacha
    where the 'play' button isn't an anchor with a recognizable text).
    The body is filtered to drop common header/footer/jQuery noise so
    the relevant markup fits within reasonable log size.

    For adapters with ``balance_uses_browser=True``, the body is
    fetched through Playwright Chromium so JS-rendered content is
    visible and anti-bot interstitials are bypassed.

    ``wait_selector`` (browser mode only) lets the caller wait for a
    specific DOM element before calling ``page.content()``. This is
    the workaround for fully-SPA sites (e.g. warau) whose server-side
    HTML is just a skeleton — Playwright still needs to wait for the
    client-side hydration to produce the actual content. Without this
    flag we capture an empty shell. ``wait_timeout_ms`` bounds the
    wait (default 15s) so a missing selector doesn't hang.

    ``wait_until`` (browser mode only) controls the goto wait condition.
    Default ``networkidle`` is safe for normal SPA pages, but ad-heavy
    sites (sugutama mypage, warau /game/list, pointtown /ptu) have
    long-poll ad iframes that never settle — passing
    ``domcontentloaded`` skips the wait and relies on ``wait_selector``
    (if set) to gate on real content.

    ``anonymous=True`` skips cookie loading and login verification so
    the request goes out unauthenticated. This is the only way to
    inspect login form pages (logged-in sessions redirect away from
    /login URLs) and other anonymous-only landing pages. URL host
    must still be in ``allowed_hosts``.
    """
    if not is_manual_url_allowed(url, cfg.adapter.allowed_hosts):
        print(f"refused: {url} is not under {cfg.adapter.site_label} hosts", file=sys.stderr)
        return 2

    if anonymous:
        # No cookies, no auth check — used to discover login-form
        # HTML that logged-in sessions can't see.
        if force_browser:
            from .common.browser import BrowserClicker

            captured: list[tuple[str, str, str]] = []
            with BrowserClicker(cookies=None) as bc:
                if capture_network:
                    bc.context.on(
                        "request",
                        lambda req: captured.append((req.method, req.url, req.resource_type)),
                    )
                page = bc.goto(url, wait_until=wait_until)
                try:
                    final_url = page.url
                    if wait_selector:
                        try:
                            page.wait_for_selector(wait_selector, timeout=wait_timeout_ms)
                        except Exception as exc:
                            logger.warning("wait_for_selector %r timed out: %s", wait_selector, exc)
                    body_text = page.content()
                finally:
                    page.close()
            original_len = len(body_text)
            print(f"=== {final_url} (browser anonymous, len={original_len}) ===")
            if capture_network and captured:
                print("\n=== captured network requests ===")
                for method, req_url, rtype in captured:
                    if rtype in ("image", "stylesheet", "font", "media"):
                        continue
                    print(f"  [{rtype}] {method} {req_url}")
        else:
            import requests

            resp = requests.get(
                url,
                timeout=(10.0, 30.0),
                allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; ai-workflows inspect)"},
            )
            body_text = resp.text
            original_len = len(body_text)
            print(f"=== {resp.url} (HTTP {resp.status_code} anonymous, len={original_len}) ===")
        body = re.sub(r"<script\b[^>]*>.*?</script>", "<!-- script removed -->", body_text, flags=re.DOTALL)
        body = re.sub(r"<style\b[^>]*>.*?</style>", "<!-- style removed -->", body, flags=re.DOTALL)
        body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
        body = re.sub(r"\n\s*\n+", "\n", body)
        print(body[:cap])
        if len(body) > cap:
            print(f"... (truncated, {len(body) - cap} more bytes)")
        return 0

    cookies = _resolve_cookies(cfg)
    if cookies is None:
        print(f"refused: {cfg.adapter.cookies_env} is not set", file=sys.stderr)
        return 2
    clicker = _build_clicker(cfg, cookies)
    if not _verify_login(clicker, cfg):
        print(f"refused: {cfg.adapter.site_label} login verification failed", file=sys.stderr)
        return 2
    _persist_cookies(clicker, cfg)

    if cfg.adapter.balance_uses_browser or force_browser:
        from urllib.parse import urlparse

        from .common.browser import BrowserClicker

        host = urlparse(cfg.adapter.mypage_url).hostname or ""
        default_domain = "." + (host.split(".", 1)[-1] if "." in host else host)
        captured_auth: list[tuple[str, str, str]] = []
        with BrowserClicker(
            cookies=_jar_to_cookies(clicker),
            default_cookie_domain=default_domain,
        ) as bc:
            if capture_network:
                bc.context.on(
                    "request",
                    lambda req: captured_auth.append((req.method, req.url, req.resource_type)),
                )
            page = bc.goto(url, wait_until=wait_until)
            try:
                final_url = page.url
                if wait_selector:
                    try:
                        page.wait_for_selector(wait_selector, timeout=wait_timeout_ms)
                    except Exception as exc:
                        # Don't abort the inspect — log the miss and
                        # capture whatever the DOM has at this point so
                        # the user can see how far hydration got.
                        logger.warning("wait_for_selector %r timed out: %s", wait_selector, exc)
                body_text = page.content()
            finally:
                page.close()
            _merge_browser_cookies(clicker, bc.export_cookies(), allowed_hosts=cfg.adapter.allowed_hosts)
        original_len = len(body_text)
        _persist_cookies(clicker, cfg)
        print(f"=== {final_url} (browser, len={original_len}) ===")
        if capture_network and captured_auth:
            print("\n=== captured network requests ===")
            for method, req_url, rtype in captured_auth:
                if rtype in ("image", "stylesheet", "font", "media"):
                    continue
                print(f"  [{rtype}] {method} {req_url}")
    else:
        resp = clicker.session.get(url, timeout=(10.0, 30.0), allow_redirects=True)
        _persist_cookies(clicker, cfg)
        body_text = resp.text
        original_len = len(body_text)
        print(f"=== {resp.url} (HTTP {resp.status_code}, len={original_len}) ===")

    body = re.sub(r"<script\b[^>]*>.*?</script>", "<!-- script removed -->", body_text, flags=re.DOTALL)
    body = re.sub(r"<style\b[^>]*>.*?</style>", "<!-- style removed -->", body, flags=re.DOTALL)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    body = re.sub(r"\n\s*\n+", "\n", body)
    print(body[:cap])
    if len(body) > cap:
        print(f"\n=== TRUNCATED (showed {cap} of {len(body)} stripped bytes; original {original_len}) ===")
    return 0


def cmd_balance(cfg: Config) -> int:
    """Print current point balance to stdout — useful for ad-hoc checks."""
    cookies = _resolve_cookies(cfg)
    if cookies is None:
        print(f"refused: {cfg.adapter.cookies_env} is not set", file=sys.stderr)
        return 2
    clicker = _build_clicker(cfg, cookies)
    if not _verify_login(clicker, cfg):
        print(
            f"refused: {cfg.adapter.site_label} login verification failed (stale or invalid cookies)",
            file=sys.stderr,
        )
        return 2
    _persist_cookies(clicker, cfg)
    balance = _fetch_balance(clicker, cfg)
    _persist_cookies(clicker, cfg)
    if balance is None:
        print("balance: <unknown> (parser failed; check logs for snippet)", file=sys.stderr)
        return 1
    print(f"balance: {balance} pt")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        adapter = get_adapter(args.site)
    except KeyError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    # One-shot migration: data/<file> → data/<site>/<file> if legacy
    # layout detected. Runs before Config so the file paths Config
    # generates point at the post-migration locations.
    _migrate_legacy_data_paths(adapter)

    try:
        cfg = Config.from_env(adapter)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    _setup_logging(cfg.log_level)

    if args.cmd == "run":
        env_extract = os.environ.get(f"{adapter.env_prefix}_EXTRACT_LINKS", "0") == "1"
        return cmd_run(
            cfg,
            dry_run=args.dry_run or cfg.dry_run,
            extract_links=args.extract_links or env_extract,
            max_messages=args.max_messages,
            notify=not args.no_notify,
        )
    if args.cmd == "click":
        return cmd_click(cfg, args.url)
    if args.cmd == "state":
        return cmd_state(cfg, args.message_id)
    if args.cmd == "balance":
        return cmd_balance(cfg)
    if args.cmd == "discover":
        return cmd_discover(cfg)
    if args.cmd == "html":
        return cmd_html(
            cfg,
            args.url,
            force_browser=getattr(args, "browser", False),
            cap=getattr(args, "cap", 80_000),
            wait_selector=getattr(args, "wait_selector", None),
            wait_timeout_ms=getattr(args, "wait_timeout_ms", 15_000),
            anonymous=getattr(args, "anonymous", False),
            capture_network=getattr(args, "capture_network", False),
            wait_until=getattr(args, "wait_until", "networkidle"),
        )
    parser.error(f"unknown subcommand: {args.cmd}")


if __name__ == "__main__":
    sys.exit(main())
