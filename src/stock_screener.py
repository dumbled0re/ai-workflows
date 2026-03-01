from __future__ import annotations

import logging

from src.config_loader import Settings
from src.data_fetcher import fetch_batch
from src.nikkei225_components import get_tickers
from src.technical_indicators import compute_indicators, compute_screening_score

logger = logging.getLogger(__name__)


def screen_nikkei225(settings: Settings) -> tuple[list[dict], int, int]:
    """Screen Nikkei 225 stocks and return top candidates with full indicators.

    Two-phase approach:
    1. Fast screening with lightweight indicators (Python only, no Claude)
    2. Full indicator computation for top candidates

    Returns:
        tuple of (candidate summaries, total screened count, failed count)
    """
    all_tickers = get_tickers()
    logger.info("Starting Nikkei 225 screening (%d tickers)", len(all_tickers))

    # Phase 1: Fetch data and fast screen
    data_dict, failed_tickers = fetch_batch(all_tickers, period="3mo")
    total_screened = len(data_dict)
    failed_count = len(failed_tickers)

    logger.info(
        "Data fetched: %d successful, %d failed", total_screened, failed_count
    )

    # Score each stock
    scored: list[tuple[str, float]] = []
    for ticker, df in data_dict.items():
        try:
            score = compute_screening_score(df)
            scored.append((ticker, score))
        except Exception:
            logger.warning("Scoring failed for %s", ticker, exc_info=True)

    # Sort by score descending, take top N
    scored.sort(key=lambda x: x[1], reverse=True)
    top_candidates = scored[: settings.screener_pool_size]

    logger.info(
        "Top %d candidates selected (score range: %.0f - %.0f)",
        len(top_candidates),
        top_candidates[0][1] if top_candidates else 0,
        top_candidates[-1][1] if top_candidates else 0,
    )

    # Phase 2: Compute full indicators for top candidates
    from src.nikkei225_components import NIKKEI_225_TICKERS

    ticker_info = {t["ticker"]: t for t in NIKKEI_225_TICKERS}

    candidates: list[dict] = []
    for ticker, score in top_candidates:
        df = data_dict.get(ticker)
        if df is None:
            continue
        info = ticker_info.get(ticker, {})
        try:
            summary = compute_indicators(
                df=df,
                ticker=ticker,
                name=info.get("name", ticker),
            )
            summary["screening_score"] = score
            summary["sector"] = info.get("sector", "不明")
            candidates.append(summary)
        except Exception:
            logger.warning(
                "Full indicator computation failed for %s", ticker, exc_info=True
            )

    logger.info("Screening complete: %d candidates with full indicators", len(candidates))
    return candidates, total_screened, failed_count
