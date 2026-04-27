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
    from src.market_context import fetch_market_context, format_market_context
    from src.news_fetcher import fetch_market_news, fetch_stock_news, format_market_news, format_stock_news
    from src.sector_analysis import compute_sector_rankings, format_sector_ranking
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

    # Performance tracking: load history and prepare for review
    from src.performance_tracker import (
        format_performance_feedback,
        get_current_prices_from_data,
        load_history,
        review_predictions,
        save_history,
    )

    perf_history = load_history()
    logger.info(
        "Loaded prediction history: %d predictions",
        len(perf_history.get("predictions", [])),
    )

    # Fetch market context (indices, forex, sentiment)
    logger.info("Fetching market context...")
    market_context = fetch_market_context()
    market_context_text = format_market_context(market_context)
    logger.info("Market context ready")

    # Fetch market news
    logger.info("Fetching market news...")
    market_news = fetch_market_news(max_items=10)
    market_news_text = format_market_news(market_news)
    logger.info("Fetched %d market news items", len(market_news))

    # Fetch and compute indicators for holdings
    holdings_summaries: list[dict] = []
    if config.holdings:
        tickers = [h.ticker for h in config.holdings]
        holdings_data, holdings_failed, holdings_fundamentals = fetch_batch(
            tickers, period=f"{config.settings.history_days}d", fetch_fundamentals=True
        )
        data_quality["success"] += len(holdings_data)
        data_quality["failed"] += len(holdings_failed)

        # Fetch news for holdings
        holdings_news = fetch_stock_news(tickers, max_per_stock=3)
        holdings_news_formatted = format_stock_news(holdings_news)

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
                # Attach news headlines
                news_text = holdings_news_formatted.get(holding.ticker)
                if news_text:
                    summary["recent_news"] = news_text
                holdings_summaries.append(summary)
            else:
                logger.warning("No data for holding: %s", holding.ticker)

    # Screen stocks (Nikkei 225 + JPX400)
    logger.info("Starting stock screening (Nikkei 225 + JPX400)")
    screened_candidates, screened_total, screened_failed, all_fundamentals, all_ticker_info = screen_stocks(
        config.settings
    )
    data_quality["success"] += screened_total
    data_quality["failed"] += screened_failed

    # Compute sector rankings
    logger.info("Computing sector rankings...")
    sector_rankings = compute_sector_rankings(all_fundamentals, all_ticker_info)
    for candidate in screened_candidates:
        ticker = candidate["ticker"]
        ranking = sector_rankings.get(ticker)
        if ranking:
            candidate["sector_ranking"] = format_sector_ranking(ranking)
            candidate["sector_score"] = ranking.get("sector_score", 0)

    # Fetch news for top candidates
    candidate_tickers = [c["ticker"] for c in screened_candidates[:20]]
    candidate_news = fetch_stock_news(candidate_tickers, max_per_stock=3)
    candidate_news_formatted = format_stock_news(candidate_news)
    for candidate in screened_candidates:
        news_text = candidate_news_formatted.get(candidate["ticker"])
        if news_text:
            candidate["recent_news"] = news_text

    # Review past predictions against current prices
    holdings_data_dict = {}
    if config.holdings:
        for h in config.holdings:
            df = holdings_data.get(h.ticker)  # type: ignore[possibly-undefined]
            if df is not None:
                holdings_data_dict[h.ticker] = df

    # screen_stocks already fetched all screening data; extract current prices
    # from the candidates we have (they include current_price)
    current_prices: dict[str, float] = {}
    for c in screened_candidates:
        if c.get("current_price"):
            current_prices[c["ticker"]] = c["current_price"]
    for s in holdings_summaries:
        if s.get("current_price"):
            current_prices[s["ticker"]] = s["current_price"]

    perf_history = review_predictions(perf_history, current_prices, date_str)
    performance_feedback = format_performance_feedback(perf_history)
    save_history(perf_history)
    logger.info("Performance review complete")

    # Save prompts for Claude Code Action
    prepare_prompts(
        holdings_summaries=holdings_summaries,
        candidates=screened_candidates,
        timing=timing,
        top_n=config.settings.discovery_top_n,
        market_context=market_context_text,
        market_news=market_news_text,
        performance_feedback=performance_feedback,
    )

    # Save current prices for Phase 3 (prediction tracking)
    meta_dir = Path("data")
    meta_dir.mkdir(exist_ok=True)
    with open(meta_dir / "current_prices.json", "w", encoding="utf-8") as f:
        json.dump(current_prices, f)

    # Save data quality and timing info for Phase 3
    with open(meta_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump({"timing": timing, "data_quality": data_quality}, f)

    logger.info("Phase 1 complete. Prompt file ready for Claude Code Action.")


def phase_notify() -> None:
    """Phase 3: Read Claude's analysis results, save predictions, send to Slack."""
    import json
    from pathlib import Path

    from src.ai_analyzer import load_analysis_results
    from src.performance_tracker import load_history, save_history, save_new_predictions
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

    # Save new predictions for tracking
    perf_history = load_history()
    # Load current prices from the analysis input (saved during prepare phase)
    current_prices: dict[str, float] = {}
    input_path = Path("data/analysis_input.json")
    if input_path.exists():
        try:
            with open(input_path, encoding="utf-8") as f:
                analysis_input = json.load(f)
            # Extract prices from the prompt text is unreliable;
            # instead, parse from holdings/discovery results
            for h in holdings_result.get("holdings_analysis", []):
                # We'll use entry prices from the prediction itself
                pass
        except Exception:
            pass

    # Extract current prices from meta or re-derive from results
    # The simplest approach: load from the previously saved candidates data
    prices_path = Path("data/current_prices.json")
    if prices_path.exists():
        try:
            with open(prices_path, encoding="utf-8") as f:
                current_prices = json.load(f)
        except Exception:
            pass

    if current_prices:
        perf_history = save_new_predictions(
            perf_history, holdings_result, discovery_result, current_prices
        )
        save_history(perf_history)
        logger.info("New predictions saved to tracking history")
    else:
        logger.warning("No current prices available; skipping prediction tracking")

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
