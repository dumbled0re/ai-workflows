"""TDnet 適時開示 daily-list scraping.

TDnet is the canonical real-time disclosure system for JP-listed
companies. Filing categories like TOB (公開買付) / 業績修正 / 自己株
取得 / 株式分割 / 大量保有 directly move the stock on next session.
Serious JP-equity desks monitor TDnet via paid API (FactSet, QUICK);
the free public web page at ``https://www.release.tdnet.info/inbs/
I_list_001_YYYYMMDD.html`` exposes the same data with a one-day lag
at most.

Scraping strategy:
- Daily-list page is paginated 100 items per page (I_list_001 / 002 /
  003 / ...). Walk pages until empty or fetch fails.
- Each row has 7 td cells: time / 5-digit code / company / title /
  XBRL / exchange / update. The 5-digit code is the 4-digit TSE
  ticker + one market segment digit; we map by truncating the last
  digit and appending ``.T``.
- Disclosure title is the high-signal field — TDnet uses standard
  Japanese category prefixes ("(訂正)業績予想の修正" etc.) that the
  existing ``news_classifier`` regex can classify.

Fail-soft: TDnet page structure changes occasionally; any parse error
returns an empty result rather than crashing the cron. The AI prompt
just gets no TDnet block in that case, same as if no relevant ticker
filed today.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_JST = timezone(timedelta(hours=9))
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ai-workflows/0.1; tdnet daily-list reader)"}
_TIMEOUT = 15
_TDNET_BASE = "https://www.release.tdnet.info/inbs"


@dataclass(frozen=True)
class Disclosure:
    """A single TDnet daily-list row.

    ``ticker`` is the .T-suffixed standard 4-digit TSE code so it
    joins cleanly against our holdings / candidates dicts. The
    original 5-digit TDnet code is preserved on ``code5`` for
    operator debugging if needed.
    """

    time_jst: str  # "HH:MM"
    code5: str  # 5-digit TDnet code
    ticker: str  # 4-digit + ".T"
    company: str
    title: str
    pdf_url: str  # absolute URL


def _today_jst() -> date:
    return datetime.now(_JST).date()


def _fetch_page(d: date, page: int) -> str | None:
    """Fetch one page of TDnet daily list. Returns HTML text or None."""
    url = f"{_TDNET_BASE}/I_list_{page:03d}_{d:%Y%m%d}.html"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        if resp.status_code != 200:
            return None
        return resp.text
    except Exception as e:
        logger.debug("TDnet page %d fetch failed for %s: %s", page, d, e)
        return None


def _parse_rows(html: str) -> list[Disclosure]:
    """Parse one TDnet page's HTML into Disclosure rows.

    The daily-list table flattens every disclosure to 7 ``<td>``
    cells in order: time / 5-digit code / company / title / XBRL /
    exchange / update-history. We walk the cells grouping in chunks
    of 7; rows where the 2nd cell is a 5-digit numeric are real
    disclosures, others are header / pager rows.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[Disclosure] = []
    cells = soup.find_all("td")
    code_re = re.compile(r"^\d{5}$")
    time_re = re.compile(r"^\d{2}:\d{2}$")

    for i in range(0, len(cells) - 6):
        c_time = cells[i].get_text(strip=True)
        c_code = cells[i + 1].get_text(strip=True)
        if not time_re.match(c_time) or not code_re.match(c_code):
            continue
        company = cells[i + 2].get_text(strip=True)
        title_cell = cells[i + 3]
        title = title_cell.get_text(strip=True)
        link = title_cell.find("a")
        pdf_href = link.get("href", "") if link else ""
        pdf_url = f"{_TDNET_BASE}/{pdf_href}" if pdf_href and not pdf_href.startswith("http") else pdf_href
        # 5-digit → 4-digit by dropping last digit (market segment).
        ticker_4 = c_code[:4]
        out.append(
            Disclosure(
                time_jst=c_time,
                code5=c_code,
                ticker=f"{ticker_4}.T",
                company=company,
                title=title,
                pdf_url=pdf_url,
            )
        )
    return out


def fetch_tdnet_today(
    target_tickers: set[str] | None = None,
    target_date: date | None = None,
    max_pages: int = 10,
) -> dict[str, list[Disclosure]]:
    """Fetch all TDnet disclosures for ``target_date`` (default = today JST).

    When ``target_tickers`` is provided, only disclosures whose ticker
    is in that set are kept — typical use is "holdings + top-20
    candidates" so we don't drag every TSE filing into the prompt.
    None means "keep everything", useful for debug runs.

    Result is grouped by ticker so callers can attach to per-stock
    summaries; empty list per ticker when nothing filed.
    """
    if target_date is None:
        target_date = _today_jst()
    result: dict[str, list[Disclosure]] = {}
    for page in range(1, max_pages + 1):
        html = _fetch_page(target_date, page)
        if html is None:
            break
        rows = _parse_rows(html)
        if not rows:
            break
        for d in rows:
            if target_tickers is not None and d.ticker not in target_tickers:
                continue
            result.setdefault(d.ticker, []).append(d)
        # Page is full only when 100 rows present; less means we've hit the tail.
        if len(rows) < 100:
            break
    total = sum(len(v) for v in result.values())
    logger.info("TDnet: %d disclosures across %d tickers for %s", total, len(result), target_date)
    return result


def format_disclosures_for_summary(disclosures: list[Disclosure]) -> str:
    """Per-stock prompt line(s) for the disclosures the ticker filed
    today. Empty when no filings."""
    if not disclosures:
        return ""
    lines = [f"  {d.time_jst} {d.title[:120]}" for d in disclosures]
    return "本日適時開示:\n" + "\n".join(lines)


def format_urgent_summary(
    by_ticker: dict[str, list[Disclosure]],
    urgent_categories: tuple[str, ...] = (
        "公開買付",
        "TOB",
        "業績予想",
        "上方修正",
        "下方修正",
        "自己株式取得",
        "自社株買い",
        "株式分割",
        "大量保有",
        "M&A",
        "合併",
        "経営統合",
        "第三者割当",
        "公募増資",
    ),
) -> list[dict]:
    """Filter to disclosures that match urgent categories.

    Returns a flat list of dicts compatible with the existing
    ``news_classifier``-style urgent block rendering. Same canonical
    JP equity research desk shortlist as the news classifier so the
    two sources de-duplicate naturally at the AI summarisation step.
    """
    out: list[dict] = []
    for ticker, items in by_ticker.items():
        for d in items:
            for kw in urgent_categories:
                if kw in d.title:
                    out.append(
                        {
                            "ticker": ticker,
                            "company": d.company,
                            "time": d.time_jst,
                            "category": kw,
                            "title": d.title,
                            "url": d.pdf_url,
                        }
                    )
                    break
    return out
