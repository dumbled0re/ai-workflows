from __future__ import annotations

import io
import logging
import random
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BACKOFF_BASE_SEC = 5
_SLEEP_MIN = 0.3
_SLEEP_MAX = 1.0

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


def _ticker_to_stooq(ticker: str) -> str:
    """Convert Yahoo Finance ticker (e.g. '7203.T') to stooq format ('7203.JP')."""
    return ticker.replace(".T", ".JP")


def _period_to_start_date(period: str) -> str:
    """Convert period string like '3mo' to start date string 'YYYYMMDD'."""
    now = datetime.now()
    if period.endswith("mo"):
        months = int(period[:-2])
        start = now - timedelta(days=months * 30)
    elif period.endswith("y"):
        years = int(period[:-1])
        start = now - timedelta(days=years * 365)
    else:
        days = int(period[:-1])
        start = now - timedelta(days=days)
    return start.strftime("%Y%m%d")


def fetch_batch(
    tickers: list[str], period: str = "3mo"
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Fetch historical OHLCV data for multiple tickers one by one.

    Returns:
        tuple of (successful data dict, list of failed tickers)
    """
    results: dict[str, pd.DataFrame] = {}
    failed: list[str] = []

    start_date = _period_to_start_date(period)
    end_date = datetime.now().strftime("%Y%m%d")

    for i, ticker in enumerate(tickers):
        if i > 0:
            time.sleep(random.uniform(_SLEEP_MIN, _SLEEP_MAX))

        if (i + 1) % 20 == 0:
            logger.info("Progress: %d/%d tickers fetched", i + 1, len(tickers))

        data = _download_from_stooq(ticker, start_date, end_date)

        if data is not None and len(data) >= 20:
            results[ticker] = data
        else:
            if data is not None and not data.empty:
                logger.warning(
                    "Insufficient data for %s (%d rows)", ticker, len(data)
                )
            failed.append(ticker)

    logger.info(
        "Data fetch complete: %d succeeded, %d failed", len(results), len(failed)
    )
    return results, failed


def _download_from_stooq(
    ticker: str, start_date: str, end_date: str
) -> pd.DataFrame | None:
    """Download a single ticker from stooq.com."""
    stooq_ticker = _ticker_to_stooq(ticker)
    url = (
        f"https://stooq.com/q/d/l/"
        f"?s={stooq_ticker}&d1={start_date}&d2={end_date}&i=d"
    )

    for attempt in range(_MAX_RETRIES):
        try:
            resp = _SESSION.get(url, timeout=15)
            if resp.status_code == 200:
                text = resp.text.strip()
                if "No data" in text or len(text) < 50:
                    logger.warning("No data available for %s on stooq", ticker)
                    return None
                df = pd.read_csv(io.StringIO(text), parse_dates=["Date"])
                df = df.set_index("Date")
                df = df.sort_index()
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
        except Exception as e:
            logger.warning(
                "Download attempt %d/%d failed for %s: %s",
                attempt + 1,
                _MAX_RETRIES,
                ticker,
                e,
            )

        wait = _BACKOFF_BASE_SEC * (2 ** attempt) + random.uniform(0, 3)
        time.sleep(wait)

    logger.error("All %d download attempts failed for %s", _MAX_RETRIES, ticker)
    return None
