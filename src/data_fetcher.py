from __future__ import annotations

import io
import logging
import random
import time

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BACKOFF_BASE_SEC = 5
_SLEEP_MIN = 0.5
_SLEEP_MAX = 1.5

_SESSION = requests.Session()
_SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }
)


def fetch_batch(
    tickers: list[str], period: str = "3mo"
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Fetch historical OHLCV data for multiple tickers one by one.

    Returns:
        tuple of (successful data dict, list of failed tickers)
    """
    results: dict[str, pd.DataFrame] = {}
    failed: list[str] = []

    period_seconds = _period_to_seconds(period)

    for i, ticker in enumerate(tickers):
        if i > 0:
            time.sleep(random.uniform(_SLEEP_MIN, _SLEEP_MAX))

        if (i + 1) % 20 == 0:
            logger.info("Progress: %d/%d tickers fetched", i + 1, len(tickers))

        data = _download_single(ticker, period_seconds)

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


def _period_to_seconds(period: str) -> int:
    """Convert period string like '3mo' to seconds."""
    unit = period[-1] if period[-1].isalpha() else period[-2:]
    num = int(period.replace(unit, ""))
    mapping = {"d": 86400, "mo": 86400 * 30, "y": 86400 * 365}
    return num * mapping.get(unit, 86400 * 90)


def _download_single(ticker: str, period_seconds: int) -> pd.DataFrame | None:
    """Download a single ticker from Yahoo Finance CSV endpoint."""
    end = int(time.time())
    start = end - period_seconds

    url = (
        f"https://query1.finance.yahoo.com/v7/finance/download/{ticker}"
        f"?period1={start}&period2={end}&interval=1d&events=history"
    )

    for attempt in range(_MAX_RETRIES):
        try:
            resp = _SESSION.get(url, timeout=10)
            if resp.status_code == 200:
                df = pd.read_csv(io.StringIO(resp.text), parse_dates=["Date"])
                df = df.set_index("Date")
                df = df.dropna(how="all")
                if not df.empty:
                    return df
            elif resp.status_code == 429:
                wait = _BACKOFF_BASE_SEC * (2 ** attempt) + random.uniform(0, 3)
                logger.warning(
                    "Rate limited for %s, retrying in %.0fs", ticker, wait
                )
                time.sleep(wait)
                continue
            else:
                logger.warning(
                    "HTTP %d for %s on attempt %d",
                    resp.status_code,
                    ticker,
                    attempt + 1,
                )
        except Exception:
            pass

        wait = _BACKOFF_BASE_SEC * (2 ** attempt) + random.uniform(0, 3)
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
