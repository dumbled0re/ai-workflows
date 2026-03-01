from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import jpholiday

from src.ai_analyzer import AIAnalyzer
from src.config_loader import load_config
from src.data_fetcher import fetch_batch
from src.slack_notifier import (
    send_analysis_to_slack,
    send_error_to_slack,
    send_market_closed_to_slack,
)
from src.stock_screener import screen_nikkei225
from src.technical_indicators import compute_indicators

JST = timezone(timedelta(hours=9))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def is_market_day(date: datetime) -> bool:
    """Check if the given date is a Tokyo Stock Exchange trading day."""
    d = date.date()
    # Weekend check
    if d.weekday() >= 5:
        return False
    # Japanese holiday check
    if jpholiday.is_holiday(d):
        return False
    return True


def main() -> None:
    now_jst = datetime.now(JST)
    timing = "morning" if now_jst.hour < 12 else "evening"
    date_str = now_jst.strftime("%Y-%m-%d")

    logger.info("Starting stock analysis: %s (%s)", date_str, timing)

    # Get required environment variables
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    slack_webhook = os.environ.get("SLACK_WEBHOOK_URL")

    if not anthropic_key:
        logger.error("ANTHROPIC_API_KEY not set")
        sys.exit(1)
    if not slack_webhook:
        logger.error("SLACK_WEBHOOK_URL not set")
        sys.exit(1)

    # Market calendar check
    if not is_market_day(now_jst):
        logger.info("Market is closed today (%s)", date_str)
        send_market_closed_to_slack(slack_webhook, date_str)
        return

    # Load config
    config = load_config("stocks.yml")
    logger.info("Loaded %d holdings", len(config.holdings))

    # Track data quality for Slack notification
    data_quality: dict = {"success": 0, "failed": 0}

    # Fetch and analyze holdings
    holdings_summaries: list[dict] = []
    if config.holdings:
        tickers = [h.ticker for h in config.holdings]
        holdings_data, holdings_failed = fetch_batch(
            tickers, period=f"{config.settings.history_days}d"
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
                )
                holdings_summaries.append(summary)
            else:
                logger.warning("No data for holding: %s", holding.ticker)

    # Screen Nikkei 225
    logger.info("Starting Nikkei 225 screening")
    screened_candidates, screened_total, screened_failed = screen_nikkei225(
        config.settings
    )
    data_quality["success"] += screened_total
    data_quality["failed"] += screened_failed

    # AI Analysis
    analyzer = AIAnalyzer(api_key=anthropic_key, model=config.settings.claude_model)

    logger.info("Analyzing %d holdings with Claude", len(holdings_summaries))
    holdings_result = analyzer.analyze_holdings(holdings_summaries, timing)

    logger.info("Analyzing %d discovery candidates with Claude", len(screened_candidates))
    discovery_result = analyzer.discover_stocks(
        screened_candidates, config.settings.discovery_top_n
    )

    # Send to Slack
    logger.info("Sending results to Slack")
    success = send_analysis_to_slack(
        webhook_url=slack_webhook,
        holdings_analysis=holdings_result,
        discovery_results=discovery_result,
        timing=timing,
        data_quality=data_quality,
    )

    if success:
        logger.info("Analysis complete and sent to Slack successfully")
    else:
        logger.error("Failed to send results to Slack")
        # Print to stdout as fallback (captured in GitHub Actions logs)
        print("=== Holdings Analysis ===")
        print(holdings_result)
        print("=== Discovery Results ===")
        print(discovery_result)
        sys.exit(1)


if __name__ == "__main__":
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
