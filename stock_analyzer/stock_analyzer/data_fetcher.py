from __future__ import annotations

import logging
import random
import time

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_SLEEP_MIN = 0.5
_SLEEP_MAX = 1.5
_MAX_RETRIES = 3
_BACKOFF_BASE_SEC = 5

# Fundamental keys to extract from yfinance .info
_FUNDAMENTAL_KEYS = [
    "trailingPE",
    "forwardPE",
    "priceToBook",
    "returnOnEquity",
    "returnOnAssets",
    "dividendYield",
    "marketCap",
    "profitMargins",
    "revenueGrowth",
    "earningsGrowth",
    "earningsQuarterlyGrowth",
    "debtToEquity",
    "currentRatio",
    "fiftyTwoWeekHigh",
    "fiftyTwoWeekLow",
    "sector",
    "industry",
]


def fetch_batch(
    tickers: list[str],
    period: str = "3mo",
    fetch_fundamentals: bool = False,
) -> tuple[dict[str, pd.DataFrame], list[str], dict[str, dict]]:
    """Fetch historical OHLCV data for multiple tickers one by one.

    Returns:
        tuple of (successful data dict, list of failed tickers, fundamentals dict)
    """
    results: dict[str, pd.DataFrame] = {}
    failed: list[str] = []
    fundamentals: dict[str, dict] = {}

    for i, ticker in enumerate(tickers):
        if i > 0:
            time.sleep(random.uniform(_SLEEP_MIN, _SLEEP_MAX))

        if (i + 1) % 20 == 0:
            logger.info("Progress: %d/%d tickers fetched", i + 1, len(tickers))

        data, info = _download_ticker(ticker, period, fetch_fundamentals)

        if data is not None and len(data) >= 20:
            results[ticker] = data
            if info:
                fundamentals[ticker] = info
        else:
            if data is not None and not data.empty:
                logger.warning("Insufficient data for %s (%d rows)", ticker, len(data))
            failed.append(ticker)

    logger.info("Data fetch complete: %d succeeded, %d failed", len(results), len(failed))
    return results, failed, fundamentals


def _download_ticker(ticker: str, period: str, fetch_fundamentals: bool) -> tuple[pd.DataFrame | None, dict | None]:
    """Download a single ticker from Yahoo Finance via yfinance."""
    for attempt in range(_MAX_RETRIES):
        try:
            tk = yf.Ticker(ticker)
            df = tk.history(period=period, auto_adjust=True)

            if df is None or df.empty:
                logger.warning("No data returned for %s", ticker)
                return None, None

            # Keep only OHLCV columns
            expected_cols = ["Open", "High", "Low", "Close", "Volume"]
            available = [c for c in expected_cols if c in df.columns]
            if "Close" not in available:
                logger.warning("Missing Close column for %s", ticker)
                return None, None

            df = df[available]
            df = df.dropna(how="all")
            df = df.sort_index()

            info = None
            if fetch_fundamentals and not df.empty:
                info = _extract_fundamentals(tk, ticker)

            if not df.empty:
                return df, info

        except Exception as e:
            logger.warning(
                "Download attempt %d/%d failed for %s: %s",
                attempt + 1,
                _MAX_RETRIES,
                ticker,
                e,
            )

        wait = _BACKOFF_BASE_SEC * (2**attempt) + random.uniform(0, 3)
        time.sleep(wait)

    logger.error("All %d download attempts failed for %s", _MAX_RETRIES, ticker)
    return None, None


def _extract_fundamentals(tk: yf.Ticker, ticker: str) -> dict | None:
    """Extract fundamental data from a yfinance Ticker object."""
    try:
        raw = tk.info
        info: dict = {}
        for key in _FUNDAMENTAL_KEYS:
            val = raw.get(key)
            if val is not None:
                info[key] = val

        # Earnings date from calendar
        try:
            cal = tk.calendar
            if cal and "Earnings Date" in cal:
                dates = cal["Earnings Date"]
                if dates:
                    info["next_earnings_date"] = str(dates[0])
        except Exception:
            pass

        return info if info else None
    except Exception as e:
        logger.debug("Failed to fetch fundamentals for %s: %s", ticker, e)
        return None
