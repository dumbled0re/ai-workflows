from __future__ import annotations

import logging
import random
import time

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}
_TIMEOUT = 10


def fetch_market_news(max_items: int = 10) -> list[dict]:
    """Fetch latest market news headlines from kabutan.jp.

    Returns list of dicts with 'title' and optional 'url' keys.
    """
    try:
        resp = requests.get(
            "https://kabutan.jp/news/marketnews/",
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("kabutan market news returned %d", resp.status_code)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        news: list[dict] = []

        # Try multiple selectors
        for a in soup.select("a[href*='/news/']"):
            title = a.get_text(strip=True)
            if len(title) > 15 and len(news) < max_items:
                href = a.get("href", "")
                url = f"https://kabutan.jp{href}" if href.startswith("/") else href
                if not any(n["title"] == title for n in news):
                    news.append({"title": title, "url": url})

        logger.info("Fetched %d market news items", len(news))
        return news
    except Exception as e:
        logger.warning("Failed to fetch market news: %s", e)
        return []


def fetch_stock_news(tickers: list[str], max_per_stock: int = 3) -> dict[str, list[dict]]:
    """Fetch recent news for specific stocks from kabutan.jp.

    Args:
        tickers: List of ticker symbols (e.g., ['7203.T', '6758.T'])
        max_per_stock: Maximum news items per stock

    Returns:
        Dict mapping ticker to list of news dicts.
    """
    results: dict[str, list[dict]] = {}

    for i, ticker in enumerate(tickers):
        if i > 0:
            time.sleep(random.uniform(0.5, 1.0))

        code = ticker.replace(".T", "")
        news = _fetch_kabutan_stock_news(code, max_per_stock)
        if news:
            results[ticker] = news

    logger.info("Fetched news for %d/%d stocks", len(results), len(tickers))
    return results


def _fetch_kabutan_stock_news(code: str, max_items: int) -> list[dict]:
    """Fetch news for a single stock from kabutan.jp/stock/?code=XXXX."""
    try:
        resp = requests.get(
            f"https://kabutan.jp/stock/?code={code}",
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        news: list[dict] = []

        # Find news links on the stock page
        for a in soup.select("a[href*='/news/']"):
            title = a.get_text(strip=True)
            if len(title) > 10 and len(news) < max_items:
                href = a.get("href", "")
                url = f"https://kabutan.jp{href}" if href.startswith("/") else href
                if not any(n["title"] == title for n in news):
                    news.append({"title": title, "url": url})

        return news
    except Exception as e:
        logger.debug("Failed to fetch news for %s: %s", code, e)
        return []


def format_market_news(news: list[dict]) -> str:
    """Format market news into text for Claude prompt."""
    if not news:
        return ""

    lines = ["=== 最新マーケットニュース ==="]
    for item in news:
        lines.append(f"- {item['title']}")
    return "\n".join(lines)


def format_stock_news(stock_news: dict[str, list[dict]]) -> dict[str, str]:
    """Format per-stock news into text snippets.

    Returns dict mapping ticker to formatted news string.
    """
    result: dict[str, str] = {}
    for ticker, items in stock_news.items():
        if items:
            headlines = " / ".join(item["title"] for item in items)
            result[ticker] = headlines
    return result
