from __future__ import annotations

import logging

from src.config_loader import Settings
from src.data_fetcher import fetch_batch
from src.nikkei225_components import NIKKEI_225_TICKERS, get_tickers as get_nikkei225_tickers
from src.jpx400_components import JPX400_TICKERS, get_jpx400_tickers
from src.technical_indicators import compute_indicators, compute_screening_score

logger = logging.getLogger(__name__)


def _build_merged_universe() -> tuple[list[str], dict[str, dict]]:
    """Merge Nikkei 225 and JPX400 tickers, deduplicated.

    Returns:
        tuple of (unique ticker list, ticker->info dict)
    """
    ticker_info: dict[str, dict] = {}

    # Nikkei 225 first (has sector info)
    for t in NIKKEI_225_TICKERS:
        ticker_info[t["ticker"]] = t

    # JPX400 (add new ones, don't overwrite Nikkei 225 entries that have sector)
    for t in JPX400_TICKERS:
        if t["ticker"] not in ticker_info:
            ticker_info[t["ticker"]] = {
                "ticker": t["ticker"],
                "name": t["name"],
                "sector": "不明",
            }

    return list(ticker_info.keys()), ticker_info


def screen_stocks(settings: Settings) -> tuple[list[dict], int, int]:
    """Screen Nikkei 225 + JPX400 stocks and return top candidates.

    Two-phase approach:
    1. Fast screening with lightweight indicators (Python only, no Claude)
    2. Full indicator computation for top candidates

    Returns:
        tuple of (candidate summaries, total screened count, failed count)
    """
    all_tickers, ticker_info = _build_merged_universe()
    logger.info("Starting stock screening (%d tickers: Nikkei225 + JPX400)", len(all_tickers))

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
