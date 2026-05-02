"""moppy_clicker CLI entry point.

Subcommands:
  run           fetch → parse → click → notify
  click <URL>   manual single-URL click (moppy hosts only)
  state         dump state for a single message_id
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime

from .clicker import Clicker, is_manual_url_allowed
from .config import Config, ConfigError
from .gmail_client import GmailAuthError, GmailClient, GmailParseError
from .models import ClickCandidate, RunSummary
from .moppy_parser import parse as parse_email
from .notifier import Notifier
from .redaction import host_only, redact_subject, redact_url
from .state_store import StateStore

logger = logging.getLogger("moppy_clicker")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="moppy_clicker")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="fetch and click")
    p_run.add_argument("--dry-run", action="store_true", help="extract only, no click")
    p_run.add_argument("--max-messages", type=int, default=None)
    p_run.add_argument("--no-notify", action="store_true")

    p_click = sub.add_parser("click", help="manual single-URL click (moppy hosts only)")
    p_click.add_argument("url")

    p_state = sub.add_parser("state", help="dump state for a message_id")
    p_state.add_argument("--message-id", required=True)
    return parser


def cmd_run(cfg: Config, dry_run: bool, max_messages: int | None, notify: bool) -> int:
    started_at = datetime.now(UTC)
    notifier = Notifier(cfg.slack_webhook_url) if notify else None

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

    clicker = Clicker(
        interval_min=cfg.click_interval_min,
        interval_max=cfg.click_interval_max,
    )

    try:
        msg_ids = gmail.search_messages(
            cfg.gmail_query,
            max_results=max_messages or cfg.max_messages,
        )
        logger.info("found %d candidate messages", len(msg_ids))

        for msg_id in msg_ids:
            if state.is_message_complete(msg_id, cfg.max_attempts):
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
                if not dry_run:
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

            if not new_candidates:
                continue

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
                r.final_status == "success"
                for r in all_results
                if r.candidate.url in {c.url for c in new_candidates}
            ):
                gmail.mark_as_read(msg_id)
                gmail.add_label(msg_id, cfg.moppy_label)
    finally:
        gmail.close()

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

    if notifier:
        if dry_run:
            notifier.send_dry_run(dry_run_view)
        else:
            notifier.send_summary(summary, all_results, estimated_pt_total)
        if parse_failure_ids:
            notifier.send_parse_failure(parse_failure_ids, "MIME/HTML decode")
        if anomaly_ids:
            notifier.send_parse_failure(anomaly_ids, "structural anomaly (template change?)")

    state.save()
    return 0


def cmd_click(cfg: Config, url: str) -> int:
    if not is_manual_url_allowed(url):
        print(f"refused: {url} is not under moppy.jp", file=sys.stderr)
        return 2
    candidate = ClickCandidate(
        url=url,  # type: ignore[arg-type]
        anchor_text="<manual>",
        extraction_reason="whitelist_url_pattern",
    )
    clicker = Clicker(
        interval_min=cfg.click_interval_min,
        interval_max=cfg.click_interval_max,
    )
    result = clicker.click(candidate)
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
        return cmd_run(
            cfg,
            dry_run=args.dry_run or cfg.dry_run,
            max_messages=args.max_messages,
            notify=not args.no_notify,
        )
    if args.cmd == "click":
        return cmd_click(cfg, args.url)
    if args.cmd == "state":
        return cmd_state(cfg, args.message_id)
    parser.error(f"unknown subcommand: {args.cmd}")


if __name__ == "__main__":
    sys.exit(main())
