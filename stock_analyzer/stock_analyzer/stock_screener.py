from __future__ import annotations

import logging

from stock_analyzer.config_loader import Settings
from stock_analyzer.data_fetcher import fetch_batch
from stock_analyzer.jpx400_components import JPX400_TICKERS
from stock_analyzer.nikkei225_components import NIKKEI_225_TICKERS
from stock_analyzer.technical_indicators import compute_indicators, compute_screening_score

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


def screen_stocks(
    settings: Settings,
    screening_weights: dict | None = None,
) -> tuple[list[dict], int, int, dict[str, dict], dict[str, dict]]:
    """Screen Nikkei 225 + JPX400 stocks and return top candidates.

    Two-phase approach:
    1. Fast screening with lightweight indicators (Python only, no Claude)
    2. Full indicator computation for top candidates

    Returns:
        tuple of (candidate summaries, total screened count, failed count,
                  fundamentals dict, ticker_info dict)
    """
    all_tickers, ticker_info = _build_merged_universe()
    logger.info("Starting stock screening (%d tickers: Nikkei225 + JPX400)", len(all_tickers))

    # Phase 1: Fetch data and fast screen
    data_dict, failed_tickers, fundamentals = fetch_batch(all_tickers, period="3mo", fetch_fundamentals=True)
    total_screened = len(data_dict)
    failed_count = len(failed_tickers)

    logger.info("Data fetched: %d successful, %d failed", total_screened, failed_count)

    # Benchmark series for the relative-strength signal. Fetched once
    # and reused across every scoring call so RS evaluation costs ~0
    # extra. A fetch failure just disables the signal — the rest of
    # the screening proceeds and the signal_components dict simply
    # won't contain ``relative_strength`` for any candidate.
    n225_data, _failed_n225, _ = fetch_batch(["^N225"], period="3mo", fetch_fundamentals=False)
    reference_close = None
    if "^N225" in n225_data:
        try:
            reference_close = n225_data["^N225"]["Close"].astype(float)
        except Exception:
            logger.warning("N225 close extraction failed; relative_strength signal disabled", exc_info=True)
    else:
        logger.warning("N225 fetch failed; relative_strength signal disabled")

    # Score each stock. ``components`` records which signals fired so
    # the per-signal efficacy analyzer can group future outcomes by
    # active signal and report which ones actually correlate with wins.
    scored: list[tuple[str, float, dict[str, bool]]] = []
    for ticker, df in data_dict.items():
        try:
            score, components = compute_screening_score(
                df,
                fundamentals=fundamentals.get(ticker),
                weights=screening_weights,
                reference_close=reference_close,
            )
            scored.append((ticker, score, components))
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
    for ticker, score, components in top_candidates:
        df = data_dict.get(ticker)
        if df is None:
            continue
        info = ticker_info.get(ticker, {})
        try:
            summary = compute_indicators(
                df=df,
                ticker=ticker,
                name=info.get("name", ticker),
                fundamentals=fundamentals.get(ticker),
            )
            summary["screening_score"] = score
            summary["signal_components"] = components
            summary["sector"] = info.get("sector", "不明")
            candidates.append(summary)
        except Exception:
            logger.warning("Full indicator computation failed for %s", ticker, exc_info=True)

    logger.info("Screening complete: %d candidates with full indicators", len(candidates))
    return candidates, total_screened, failed_count, fundamentals, ticker_info
