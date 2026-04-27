from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import jpholiday

JST = timezone(timedelta(hours=9))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def is_market_day(date: datetime) -> bool:
    """Check if the given date is a Tokyo Stock Exchange trading day."""
    d = date.date()
    if d.weekday() >= 5:
        return False
    if jpholiday.is_holiday(d):
        return False
    return True


def phase_prepare() -> None:
    """Phase 1: Fetch data, compute indicators, prepare prompts for Claude."""
    from src.ai_analyzer import prepare_prompts
    from src.config_loader import load_config
    from src.data_fetcher import fetch_batch
    from src.slack_notifier import send_market_closed_to_slack
    from src.stock_screener import screen_stocks
    from src.technical_indicators import compute_indicators

    now_jst = datetime.now(JST)
    timing = "morning" if now_jst.hour < 12 else "evening"
    date_str = now_jst.strftime("%Y-%m-%d")

    logger.info("Phase 1 (Prepare): %s (%s)", date_str, timing)

    slack_webhook = os.environ.get("SLACK_WEBHOOK_URL")

    # Market calendar check
    if not is_market_day(now_jst):
        logger.info("Market is closed today (%s)", date_str)
        if slack_webhook:
            send_market_closed_to_slack(slack_webhook, date_str)
        sys.exit(0)

    # Load config
    config = load_config("stocks.yml")
    logger.info("Loaded %d holdings", len(config.holdings))

    # Track data quality
    import json
    from pathlib import Path

    data_quality: dict = {"success": 0, "failed": 0}

    # Fetch and compute indicators for holdings
    holdings_summaries: list[dict] = []
    if config.holdings:
        tickers = [h.ticker for h in config.holdings]
        holdings_data, holdings_failed, holdings_fundamentals = fetch_batch(
            tickers, period=f"{config.settings.history_days}d", fetch_fundamentals=True
        )
        data_quality["success"] += len(holdings_data)
        data_quality["failed"] += len(holdings_failed)

        for holding in config.holdings:
            df = holdings_data.get(holding.ticker)
            if df is not None:
                summary = compute_indicators(
                    df=df,
                    ticker=holding.ticker,
                    name=holding.name,
                    shares=holding.shares,
                    avg_cost=holding.avg_cost,
                    fundamentals=holdings_fundamentals.get(holding.ticker),
                )
                holdings_summaries.append(summary)
            else:
                logger.warning("No data for holding: %s", holding.ticker)

    # Screen stocks (Nikkei 225 + JPX400)
    logger.info("Starting stock screening (Nikkei 225 + JPX400)")
    screened_candidates, screened_total, screened_failed = screen_stocks(
        config.settings
    )
    data_quality["success"] += screened_total
    data_quality["failed"] += screened_failed

    # Save prompts for Claude Code Action
    prepare_prompts(
        holdings_summaries=holdings_summaries,
        candidates=screened_candidates,
        timing=timing,
        top_n=config.settings.discovery_top_n,
    )

    # Save data quality and timing info for Phase 3
    meta_dir = Path("data")
    meta_dir.mkdir(exist_ok=True)
    with open(meta_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump({"timing": timing, "data_quality": data_quality}, f)

    logger.info("Phase 1 complete. Prompt file ready for Claude Code Action.")


def phase_notify() -> None:
    """Phase 3: Read Claude's analysis results and send to Slack."""
    import json
    from pathlib import Path

    from src.ai_analyzer import load_analysis_results
    from src.slack_notifier import send_analysis_to_slack

    logger.info("Phase 3 (Notify): Sending results to Slack")

    slack_webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not slack_webhook:
        logger.error("SLACK_WEBHOOK_URL not set")
        sys.exit(1)

    # Load metadata
    meta_path = Path("data/meta.json")
    if meta_path.exists():
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    else:
        meta = {"timing": "morning", "data_quality": None}

    # Load Claude's analysis results
    holdings_result, discovery_result = load_analysis_results()

    # Send to Slack
    success = send_analysis_to_slack(
        webhook_url=slack_webhook,
        holdings_analysis=holdings_result,
        discovery_results=discovery_result,
        timing=meta["timing"],
        data_quality=meta.get("data_quality"),
    )

    if success:
        logger.info("Results sent to Slack successfully")
    else:
        logger.error("Failed to send results to Slack")
        print("=== Holdings Analysis ===")
        print(holdings_result)
        print("=== Discovery Results ===")
        print(discovery_result)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="JP Stock Analyzer")
    parser.add_argument(
        "phase",
        choices=["prepare", "notify"],
        help="Phase to run: 'prepare' (fetch data & build prompts) or 'notify' (send results to Slack)",
    )
    args = parser.parse_args()

    if args.phase == "prepare":
        phase_prepare()
    elif args.phase == "notify":
        phase_notify()


if __name__ == "__main__":
    from src.slack_notifier import send_error_to_slack

    try:
        main()
    except Exception as e:
        logger.exception("Fatal error in stock analysis")
        webhook = os.environ.get("SLACK_WEBHOOK_URL")
        if webhook:
            try:
                send_error_to_slack(webhook, str(e))
            except Exception:
                pass
        sys.exit(1)
