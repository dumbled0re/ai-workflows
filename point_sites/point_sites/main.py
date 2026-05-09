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
from .common.cookie_store import load as load_persisted_cookies
from .common.cookie_store import save_jar as save_cookie_jar
from .common.discover import discover, render_report
from .common.gmail_client import GmailAuthError
from .common.models import ClickCandidate, RunSummary
from .common.notifier import Notifier
from .common.outcome_tracker import OutcomeTracker, make_outcome
from .common.redaction import host_only, redact_subject, redact_url
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
    """
    try:
        n = save_cookie_jar(clicker.session.cookies, cfg.cookie_store_path)
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
    """Wrapper that injects adapter-specific mypage URL and login keyword."""
    return clicker.verify_login(cfg.adapter.mypage_url, cfg.adapter.login_keyword)


def _fetch_balance(clicker: Clicker, cfg: Config) -> int | None:
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

    # Extract-links mode skips the click path entirely, so we don't need a
    # Clicker (and don't need cookies). Only construct it when actually clicking.
    clicker: Clicker | None = None
    if not extract_links:
        clicker = _build_clicker(cfg, _resolve_cookies(cfg) or [])
        if _resolve_cookies(cfg) is None:
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
    if not dry_run and not extract_links and clicker is not None and clicker.authenticated:
        if not _verify_login(clicker, cfg):
            msg = (
                f"{cfg.adapter.site_label} login verification failed: cookies are stale or invalid. "
                f"Re-export them from the browser and update the {cfg.adapter.cookies_env} secret."
            )
            logger.error(msg)
            if notifier:
                notifier.send_auth_error(msg)
            return 1
        logger.info("%s login verified — clicks will be credited to the account", cfg.adapter.site_label)
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
    if not dry_run and not extract_links:
        tracker = OutcomeTracker(cfg.outcome_path)
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
        degradation = tracker.detect_degradation()
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
        else:
            notifier.send_summary(
                summary,
                all_results,
                estimated_pt_total,
                balance_before=balance_before,
                balance_after=balance_after,
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


def cmd_html(cfg: Config, url: str) -> int:
    """Fetch a single URL and dump its body to stdout.

    Used to plan automation for items whose interaction shape isn't
    obvious from discover's regex-based summary (e.g. JS-driven gacha
    where the 'play' button isn't an anchor with a recognizable text).
    The body is filtered to drop common header/footer/jQuery noise so
    the relevant markup fits within reasonable log size.
    """
    if not is_manual_url_allowed(url, cfg.adapter.allowed_hosts):
        print(f"refused: {url} is not under {cfg.adapter.site_label} hosts", file=sys.stderr)
        return 2
    cookies = _resolve_cookies(cfg)
    if cookies is None:
        print(f"refused: {cfg.adapter.cookies_env} is not set", file=sys.stderr)
        return 2
    clicker = _build_clicker(cfg, cookies)
    if not _verify_login(clicker, cfg):
        print(f"refused: {cfg.adapter.site_label} login verification failed", file=sys.stderr)
        return 2
    _persist_cookies(clicker, cfg)
    resp = clicker.session.get(url, timeout=(10.0, 30.0), allow_redirects=True)
    _persist_cookies(clicker, cfg)
    print(f"=== {resp.url} (HTTP {resp.status_code}, len={len(resp.text)}) ===")
    body = re.sub(r"<script\b[^>]*>.*?</script>", "<!-- script removed -->", resp.text, flags=re.DOTALL)
    body = re.sub(r"<style\b[^>]*>.*?</style>", "<!-- style removed -->", body, flags=re.DOTALL)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    body = re.sub(r"\n\s*\n+", "\n", body)
    cap = 80_000
    print(body[:cap])
    if len(body) > cap:
        print(f"\n=== TRUNCATED (showed {cap} of {len(body)} stripped bytes; original {len(resp.text)}) ===")
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
        return cmd_html(cfg, args.url)
    parser.error(f"unknown subcommand: {args.cmd}")


if __name__ == "__main__":
    sys.exit(main())
