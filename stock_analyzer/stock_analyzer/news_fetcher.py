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

    Each headline is classified by ``news_classifier`` against the
    canonical TDnet-style urgent categories (TOB / 業績修正 / 自己
    株取得 / 大量保有 / M&A / 増資 / 配当 etc.). Urgent items are
    rendered with a category prefix so the AI sees them as material
    even when buried in a list of background headlines, mirroring the
    way a serious JP-equity research desk would highlight a TDnet
    notice over a generic news ticker headline.
    """
    from stock_analyzer.news_classifier import classify_news_list, format_for_prompt

    result: dict[str, str] = {}
    for ticker, items in stock_news.items():
        if items:
            classified = classify_news_list(items)
            text = format_for_prompt(classified)
            if text:
                result[ticker] = text
    return result


def fetch_margin_data(tickers: list[str]) -> dict[str, dict]:
    """Fetch margin trading data (信用残) from kabutan.jp.

    Returns dict mapping ticker to margin data:
        margin_ratio: 信用倍率 (buy_balance / sell_balance)
        buy_balance: 買い残 (shares)
        sell_balance: 売り残 (shares)
        margin_ratio_change: 前週比の変化方向
    """
    results: dict[str, dict] = {}

    for i, ticker in enumerate(tickers):
        if i > 0:
            time.sleep(random.uniform(0.3, 0.7))

        code = ticker.replace(".T", "")
        data = _fetch_kabutan_margin(code)
        if data:
            results[ticker] = data

    logger.info("Fetched margin data for %d/%d stocks", len(results), len(tickers))
    return results


def _fetch_kabutan_margin(code: str) -> dict | None:
    """Fetch margin data for a single stock from kabutan."""
    try:
        resp = requests.get(
            f"https://kabutan.jp/stock/kabuka?code={code}&ashi=shin",
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # Find the weekly margin data table (has 信用倍率 and 売り残 columns)
        for table in soup.find_all("table"):
            ths = [th.get_text(strip=True) for th in table.find_all("th")]
            if "信用倍率" in ths and "売り残(株)" in ths:
                rows = table.find_all("tr")[1:]  # skip header
                # Get the two most recent valid rows (skip rows with "－")
                valid_rows: list[list[str]] = []
                for row in rows:
                    cells = [td.get_text(strip=True) for td in row.find_all("td")]
                    if len(cells) >= 7 and cells[4] != "－":
                        valid_rows.append(cells)
                    if len(valid_rows) >= 2:
                        break

                if not valid_rows:
                    return None

                latest = valid_rows[0]
                # cells: [終値, 前週比率, 売買単価, 売買高, 売り残, 買い残, 信用倍率]
                sell_str = latest[4].replace(",", "")
                buy_str = latest[5].replace(",", "")
                ratio_str = latest[6]

                result: dict = {}
                try:
                    result["sell_balance"] = int(sell_str)
                    result["buy_balance"] = int(buy_str)
                    result["margin_ratio"] = float(ratio_str)
                except (ValueError, IndexError):
                    return None

                # Check trend: compare with previous week
                if len(valid_rows) >= 2:
                    prev = valid_rows[1]
                    try:
                        prev_ratio = float(prev[6])
                        if result["margin_ratio"] > prev_ratio:
                            result["margin_trend"] = "買い残増加（将来の売り圧力）"
                        elif result["margin_ratio"] < prev_ratio:
                            result["margin_trend"] = "買い残減少（売り圧力緩和）"
                        else:
                            result["margin_trend"] = "横ばい"
                    except (ValueError, IndexError):
                        pass

                # Interpretation
                ratio = result.get("margin_ratio", 0)
                if ratio > 5:
                    result["signal"] = "買い残過多（将来の売り圧力リスク高）"
                elif ratio > 3:
                    result["signal"] = "買い残やや多い"
                elif 1 < ratio <= 3:
                    result["signal"] = "需給バランス良好"
                elif 0.5 < ratio <= 1:
                    result["signal"] = "売り残優位（踏み上げの可能性）"
                elif ratio <= 0.5 and ratio > 0:
                    result["signal"] = "売り残過多（踏み上げリスク高）"

                return result

        return None
    except Exception as e:
        logger.debug("Failed to fetch margin data for %s: %s", code, e)
        return None
