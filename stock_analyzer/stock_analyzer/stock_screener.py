from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from stock_analyzer.config_loader import Settings
from stock_analyzer.data_fetcher import fetch_batch
from stock_analyzer.jpx400_components import JPX400_TICKERS
from stock_analyzer.nikkei225_components import NIKKEI_225_TICKERS
from stock_analyzer.technical_indicators import compute_indicators, compute_screening_score

logger = logging.getLogger(__name__)


def _pct_change_20d(close: pd.Series, window: int = 20) -> float | None:
    """20-day percent change for a close series, ``None`` when too short.

    Mirrors ``technical_indicators._pct_change`` but lives here so this
    module doesn't reach into the indicators package for one utility.
    """
    if len(close) <= window:
        return None
    try:
        old = float(close.iloc[-window - 1])
        if old == 0:
            return None
        return (float(close.iloc[-1]) - old) / old * 100
    except Exception:
        return None


def _compute_leading_sectors(
    data_dict: dict[str, pd.DataFrame],
    ticker_info: dict[str, dict[str, Any]],
    reference_close: pd.Series | None,
    window: int = 20,
    edge_pp: float = 3.0,
    min_tickers: int = 3,
) -> set[str]:
    """Identify sectors whose average member return beats the benchmark.

    For each sector with at least ``min_tickers`` price series of
    sufficient length, compute the mean 20-day return across members.
    Sectors whose mean return exceeds the benchmark's 20-day return by
    ``edge_pp`` percentage points enter the leading set; a stock in
    one of those sectors then receives the ``sector_rotation`` signal
    in screening.

    Returns an empty set when the benchmark is unavailable — a tailwind
    we cannot define cleanly is better not asserted than guessed at.
    """
    if reference_close is None:
        return set()
    ref_pct = _pct_change_20d(reference_close, window)
    if ref_pct is None:
        return set()

    sector_returns: dict[str, list[float]] = {}
    for ticker, df in data_dict.items():
        info = ticker_info.get(ticker) or {}
        sector = info.get("sector")
        if not sector or sector == "不明":
            continue
        try:
            close = df["Close"].astype(float)
        except Exception:
            continue
        pct = _pct_change_20d(close, window)
        if pct is None:
            continue
        sector_returns.setdefault(sector, []).append(pct)

    leading: set[str] = set()
    for sector, rets in sector_returns.items():
        if len(rets) < min_tickers:
            continue
        mean_ret = sum(rets) / len(rets)
        if (mean_ret - ref_pct) >= edge_pp:
            leading.add(sector)
    return leading


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

    # Sector momentum: pre-compute the set of leading sectors once per
    # run by aggregating member 20-day returns and comparing to the
    # benchmark. Stocks in those sectors get a tailwind tag in
    # ``signal_components`` and a score boost via ``sector_in_leading``.
    leading_sectors = _compute_leading_sectors(
        data_dict, ticker_info, reference_close=reference_close, window=20, edge_pp=3.0, min_tickers=3
    )
    logger.info("Leading sectors (vs benchmark + 3pp): %s", sorted(leading_sectors) or "(none)")

    # Score each stock. ``components`` records which signals fired so
    # the per-signal efficacy analyzer can group future outcomes by
    # active signal and report which ones actually correlate with wins.
    scored: list[tuple[str, float, dict[str, bool]]] = []
    for ticker, df in data_dict.items():
        try:
            sector = (ticker_info.get(ticker) or {}).get("sector")
            score, components = compute_screening_score(
                df,
                fundamentals=fundamentals.get(ticker),
                weights=screening_weights,
                reference_close=reference_close,
                sector_in_leading=bool(sector and sector in leading_sectors),
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
