"""moppy_clicker CLI entry point.

Subcommands:
  run           fetch → parse → click → notify
  click <URL>   manual single-URL click (moppy hosts only)
  state         dump state for a single message_id
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import asdict
from datetime import UTC, datetime

from .balance import fetch_balance
from .clicker import Clicker, is_manual_url_allowed
from .config import Config, ConfigError
from .cookie_store import load as load_persisted_cookies
from .cookie_store import save_jar as save_cookie_jar
from .discover import discover, render_report
from .gmail_client import GmailAuthError, GmailClient, GmailParseError
from .models import ClickCandidate, RunSummary
from .moppy_parser import parse as parse_email
from .notifier import Notifier
from .outcome_tracker import OutcomeTracker, make_outcome
from .redaction import host_only, redact_subject, redact_url
from .state_store import StateStore

logger = logging.getLogger("moppy_clicker")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _resolve_cookies(cfg: Config) -> list[dict[str, object]] | None:
    """Prefer persisted post-rotation cookies over the bootstrap Secret.

    Moppy rotates session cookies on each request; submitting the stale
    Secret value on a subsequent run gets the session killed. The
    persisted jar from the previous run carries the latest rotation.
    """
    persisted = load_persisted_cookies(cfg.cookie_store_path)
    if persisted is not None:
        logger.info("using persisted cookie jar (%d cookies)", len(persisted))
        return persisted
    if cfg.moppy_cookies is not None:
        logger.info("no persisted cookie jar; bootstrapping from MOPPY_COOKIES (%d cookies)", len(cfg.moppy_cookies))
        return cfg.moppy_cookies
    return None


def _persist_cookies(clicker: Clicker, cfg: Config) -> None:
    """Save the live jar so the next process picks up Moppy's rotated values.

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


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="moppy_clicker")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="fetch and click")
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

    p_click = sub.add_parser("click", help="manual single-URL click (moppy hosts only)")
    p_click.add_argument("url")

    p_state = sub.add_parser("state", help="dump state for a message_id")
    p_state.add_argument("--message-id", required=True)

    sub.add_parser("balance", help="fetch and print current Moppy coin balance")
    sub.add_parser(
        "discover",
        help="read-only crawl of 毎日貯める section; prints a structural report (no clicks)",
    )
    p_html = sub.add_parser(
        "html",
        help="GET a Moppy URL with auth and print its body (debug, capped at 50KB)",
    )
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

    try:
        gmail = GmailClient(cfg.gmail_user, cfg.gmail_app_password)
    except GmailAuthError as exc:
        logger.error("auth error: %s", exc)
        if notifier:
            notifier.send_auth_error(str(exc))
        return 1

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
        clicker = Clicker(
            interval_min=cfg.click_interval_min,
            interval_max=cfg.click_interval_max,
            cookies=_resolve_cookies(cfg),
        )

    if not dry_run and not extract_links and clicker is not None and not clicker.authenticated:
        # Anonymous clicks return HTTP 200 but Moppy does NOT credit points.
        # Recording these as "clicked" would also block later credited retries
        # because the email would already be labeled `moppy-clicked` and skipped.
        msg = (
            "MOPPY_COOKIES is not set; refusing to run. Anonymous clicks would "
            "be marked as completed without crediting points, blocking future "
            "credited retries. Set MOPPY_COOKIES or use --dry-run."
        )
        logger.error(msg)
        if notifier:
            notifier.send_auth_error(msg)
        return 1
    if not dry_run and not extract_links and clicker is not None and clicker.authenticated:
        if not clicker.verify_login():
            msg = (
                "Moppy login verification failed: cookies are stale or invalid. "
                "Re-export them from the browser and update the MOPPY_COOKIES secret."
            )
            logger.error(msg)
            if notifier:
                notifier.send_auth_error(msg)
            return 1
        logger.info("Moppy login verified — clicks will be credited to the account")
        # Persist immediately after the verify_login GET so the rotated
        # cookies survive even if a later step crashes.
        _persist_cookies(clicker, cfg)

    # Capture the pre-click balance so the post-run summary can prove
    # whether points actually credited. Only meaningful in real click mode;
    # dry-run / extract-links don't trigger any click side-effects.
    balance_before: int | None = None
    if not dry_run and not extract_links and clicker is not None and clicker.authenticated:
        balance_before = fetch_balance(clicker.session)
        if balance_before is not None:
            logger.info("balance before clicks: %d pt", balance_before)

    try:
        msg_ids = gmail.search_messages(
            cfg.gmail_query,
            max_results=max_messages or cfg.max_messages,
        )
        logger.info("found %d candidate messages", len(msg_ids))

        for msg_id in msg_ids:
            # Extract mode bypasses state — its job is to surface every URL so
            # the user can click manually. Stale anonymous-success records and
            # exhausted-attempt records would otherwise hide URLs the user
            # never actually got credit for.
            if not extract_links and state.is_message_complete(msg_id, cfg.max_attempts):
                continue
            try:
                parsed = gmail.get_message(msg_id)
            except GmailParseError as exc:
                logger.warning("get_message failed for %s: %s", msg_id, exc)
                parse_failure_ids.append(msg_id)
                continue

            if not parsed.has_body:
                parse_failure_ids.append(msg_id)
                continue

            if parsed.plaintext_body:
                body, is_html = parsed.plaintext_body, False
            else:
                assert parsed.html_body is not None
                body, is_html = parsed.html_body, True
            candidates, anomalies = parse_email(body, is_html=is_html)
            if anomalies:
                logger.warning(
                    "anomalous parse for %s: anomalies=%s candidates=%d",
                    msg_id,
                    anomalies,
                    len(candidates),
                )
                anomaly_ids.append(msg_id)
                continue
            if not candidates:
                # No click-coin URLs: legitimate non-coin email (newsletter,
                # confirmation, etc.). Mark so future runs skip it.
                # Skip labeling in dry_run / extract_links to keep state pristine.
                if not dry_run and not extract_links:
                    gmail.add_label(msg_id, "moppy-no-coins")
                continue

            new_candidates: list[ClickCandidate] = [
                c for c in candidates if not state.is_url_done(msg_id, str(c.url), cfg.max_attempts)
            ]

            if dry_run:
                dry_run_view.append(
                    (
                        msg_id,
                        redact_subject(parsed.subject),
                        [redact_url(str(c.url)) for c in new_candidates],
                    )
                )
                continue

            if extract_links:
                # Post full URLs so the user can click in their logged-in browser.
                # Subjects are NOT redacted here (private channel, user needs
                # to triage). State is intentionally untouched so re-runs after
                # a manual click won't be blocked.
                #
                # Use the unfiltered ``candidates`` list (NOT ``new_candidates``):
                # state_store may contain historical anonymous HTTP-200 entries
                # from before login was implemented, which would cause real
                # unclicked URLs to be silently dropped here.
                extract_view.append(
                    (
                        msg_id,
                        parsed.subject,
                        [str(c.url) for c in candidates],
                    )
                )
                continue

            if not new_candidates:
                continue

            assert clicker is not None  # narrowed by the extract_links branch above
            state.increment_attempt(msg_id)
            for idx, candidate in enumerate(new_candidates):
                if idx > 0:
                    clicker.sleep_between()
                result = clicker.click(candidate)
                state.record_attempt(msg_id, result)
                all_results.append(result)
                if result.final_status == "success" and candidate.estimated_points:
                    estimated_pt_total += candidate.estimated_points
            state.save()

            if state.is_message_complete(msg_id, cfg.max_attempts) and all(
                r.final_status == "success" for r in all_results if r.candidate.url in {c.url for c in new_candidates}
            ):
                gmail.mark_as_read(msg_id)
                gmail.add_label(msg_id, cfg.moppy_label)
    finally:
        gmail.close()

    # Snapshot the balance again *after* the click loop so we can compare
    # against ``balance_before``. Skipped on dry/extract runs (no clicks
    # were issued) and on failed-auth paths (we already returned earlier).
    balance_after: int | None = None
    if not dry_run and not extract_links and clicker is not None and clicker.authenticated:
        balance_after = fetch_balance(clicker.session)
        if balance_after is not None:
            logger.info("balance after clicks: %d pt", balance_after)
        # Save the jar one more time at the very end so all rotation
        # from the click loop is captured for the next run.
        _persist_cookies(clicker, cfg)

    finished_at = datetime.now(UTC)
    summary = RunSummary(
        started_at=started_at,
        finished_at=finished_at,
        messages_processed=len(msg_ids),
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
            messages_found=len(msg_ids),
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
    if not is_manual_url_allowed(url):
        print(f"refused: {url} is not under moppy.jp", file=sys.stderr)
        return 2
    cookies = _resolve_cookies(cfg)
    if cookies is None:
        print(
            "refused: MOPPY_COOKIES is not set; anonymous clicks do not credit points. "
            "Set MOPPY_COOKIES to enable credited single-URL clicks.",
            file=sys.stderr,
        )
        return 2
    candidate = ClickCandidate(
        url=url,  # type: ignore[arg-type]
        anchor_text="<manual>",
        extraction_reason="whitelist_url_pattern",
    )
    clicker = Clicker(
        interval_min=cfg.click_interval_min,
        interval_max=cfg.click_interval_max,
        cookies=cookies,
    )
    if not clicker.verify_login():
        print(
            "refused: Moppy login verification failed (stale or invalid cookies)",
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
        print("refused: MOPPY_COOKIES is not set", file=sys.stderr)
        return 2
    clicker = Clicker(
        interval_min=cfg.click_interval_min,
        interval_max=cfg.click_interval_max,
        cookies=cookies,
    )
    if not clicker.verify_login():
        print("refused: Moppy login verification failed (stale or invalid cookies)", file=sys.stderr)
        return 2
    _persist_cookies(clicker, cfg)
    reports = discover(clicker.session)
    _persist_cookies(clicker, cfg)
    print(render_report(reports))
    print()
    print("=== JSON ===")
    print(json.dumps([asdict(r) for r in reports], ensure_ascii=False, indent=2))
    return 0


def cmd_html(cfg: Config, url: str) -> int:
    """Fetch a single Moppy URL and dump its body to stdout.

    Used to plan automation for items whose interaction shape isn't
    obvious from discover's regex-based summary (e.g. JS-driven gacha
    where the 'play' button isn't an anchor with a recognizable text).
    The body is filtered to drop common header/footer/jQuery noise so
    the relevant markup fits within reasonable log size; full body is
    dumped as-is when it's already small.
    """
    if not is_manual_url_allowed(url):
        print(f"refused: {url} is not under moppy.jp", file=sys.stderr)
        return 2
    cookies = _resolve_cookies(cfg)
    if cookies is None:
        print("refused: MOPPY_COOKIES is not set", file=sys.stderr)
        return 2
    clicker = Clicker(
        interval_min=cfg.click_interval_min,
        interval_max=cfg.click_interval_max,
        cookies=cookies,
    )
    if not clicker.verify_login():
        print("refused: Moppy login verification failed", file=sys.stderr)
        return 2
    _persist_cookies(clicker, cfg)
    resp = clicker.session.get(url, timeout=(10.0, 30.0), allow_redirects=True)
    _persist_cookies(clicker, cfg)
    print(f"=== {resp.url} (HTTP {resp.status_code}, len={len(resp.text)}) ===")
    # Strip <script> and <style> blocks: they're either jQuery/UI noise
    # or rendering details we don't need to plan automation. Keeps the
    # markup-of-interest (the actual gacha UI / forms / anchors)
    # inside a manageable log slice without losing the meaningful HTML.
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
    """Print current Moppy balance to stdout — useful for ad-hoc checks
    and for verifying that the cookies + parser combo still work without
    having to schedule a workflow run."""
    cookies = _resolve_cookies(cfg)
    if cookies is None:
        print("refused: MOPPY_COOKIES is not set", file=sys.stderr)
        return 2
    clicker = Clicker(
        interval_min=cfg.click_interval_min,
        interval_max=cfg.click_interval_max,
        cookies=cookies,
    )
    if not clicker.verify_login():
        print("refused: Moppy login verification failed (stale or invalid cookies)", file=sys.stderr)
        return 2
    _persist_cookies(clicker, cfg)
    balance = fetch_balance(clicker.session)
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
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    _setup_logging(cfg.log_level)

    if args.cmd == "run":
        env_extract = os.environ.get("MOPPY_EXTRACT_LINKS", "0") == "1"
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
