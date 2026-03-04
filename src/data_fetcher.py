from __future__ import annotations

import logging
import random
import time

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BACKOFF_BASE_SEC = 15
_SLEEP_MIN = 1.0
_SLEEP_MAX = 3.0


def fetch_batch(
    tickers: list[str], period: str = "3mo"
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Fetch historical OHLCV data for multiple tickers one by one.

    Returns:
        tuple of (successful data dict, list of failed tickers)
    """
    results: dict[str, pd.DataFrame] = {}
    failed: list[str] = []

    for i, ticker in enumerate(tickers):
        if i > 0:
            time.sleep(random.uniform(_SLEEP_MIN, _SLEEP_MAX))

        if (i + 1) % 10 == 0:
            logger.info("Progress: %d/%d tickers fetched", i + 1, len(tickers))

        data = _download_single(ticker, period)

        if data is not None and len(data) >= 20:
            results[ticker] = data
        else:
            if data is not None:
                logger.warning(
                    "Insufficient data for %s (%d rows)", ticker, len(data)
                )
            failed.append(ticker)

    logger.info(
        "Data fetch complete: %d succeeded, %d failed", len(results), len(failed)
    )
    return results, failed


def _download_single(ticker: str, period: str) -> pd.DataFrame | None:
    """Download a single ticker with exponential backoff retry."""
    for attempt in range(_MAX_RETRIES):
        try:
            t = yf.Ticker(ticker)
            data = t.history(period=period, interval="1d")
            if data is not None and not data.empty:
                return data
        except Exception:
            pass

        wait = _BACKOFF_BASE_SEC * (2 ** attempt) + random.uniform(0, 5)
        logger.warning(
            "Download attempt %d/%d failed for %s, retrying in %.0fs",
            attempt + 1,
            _MAX_RETRIES,
            ticker,
            wait,
        )
        time.sleep(wait)

    logger.error("All %d download attempts failed for %s", _MAX_RETRIES, ticker)
    return None
