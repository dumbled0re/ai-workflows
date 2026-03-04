from __future__ import annotations

import logging
import time

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_BATCH_SIZE = 10
_BATCH_SLEEP_SEC = 5
_MAX_RETRIES = 3
_BACKOFF_BASE_SEC = 10


def fetch_batch(
    tickers: list[str], period: str = "3mo"
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Fetch historical OHLCV data for multiple tickers.

    Returns:
        tuple of (successful data dict, list of failed tickers)
    """
    results: dict[str, pd.DataFrame] = {}
    failed: list[str] = []

    batches = [tickers[i : i + _BATCH_SIZE] for i in range(0, len(tickers), _BATCH_SIZE)]

    for batch_idx, batch in enumerate(batches):
        if batch_idx > 0:
            time.sleep(_BATCH_SLEEP_SEC)

        logger.info(
            "Fetching batch %d/%d (%d tickers)", batch_idx + 1, len(batches), len(batch)
        )

        data = _download_with_retry(batch, period)

        if data is None or data.empty:
            logger.warning("Batch %d returned no data", batch_idx + 1)
            failed.extend(batch)
            continue

        if len(batch) == 1:
            ticker = batch[0]
            if len(data) >= 20:
                results[ticker] = data
            else:
                logger.warning(
                    "Insufficient data for %s (%d rows)", ticker, len(data)
                )
                failed.append(ticker)
        else:
            for ticker in batch:
                try:
                    if isinstance(data.columns, pd.MultiIndex):
                        ticker_data = data.xs(ticker, level="Ticker", axis=1)
                    else:
                        ticker_data = data
                    ticker_data = ticker_data.dropna(how="all")
                    if len(ticker_data) >= 20:
                        results[ticker] = ticker_data
                    else:
                        logger.warning(
                            "Insufficient data for %s (%d rows)",
                            ticker,
                            len(ticker_data),
                        )
                        failed.append(ticker)
                except (KeyError, ValueError):
                    logger.warning("No data returned for %s", ticker)
                    failed.append(ticker)

    logger.info(
        "Data fetch complete: %d succeeded, %d failed", len(results), len(failed)
    )
    return results, failed


def _download_with_retry(
    tickers: list[str], period: str
) -> pd.DataFrame | None:
    """Download with exponential backoff retry."""
    for attempt in range(_MAX_RETRIES):
        try:
            data = yf.download(
                tickers=tickers,
                period=period,
                interval="1d",
                group_by="ticker" if len(tickers) > 1 else "column",
                threads=True,
                progress=False,
                timeout=10,
            )
            if data is not None and not data.empty:
                return data
        except Exception:
            wait = _BACKOFF_BASE_SEC * (2 ** attempt)
            logger.warning(
                "Download attempt %d/%d failed, retrying in %ds",
                attempt + 1,
                _MAX_RETRIES,
                wait,
                exc_info=True,
            )
            time.sleep(wait)

    logger.error("All %d download attempts failed for %s", _MAX_RETRIES, tickers)
    return None
