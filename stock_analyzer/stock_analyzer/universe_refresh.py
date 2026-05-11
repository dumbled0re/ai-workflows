"""Detect when the static Nikkei 225 / JPX 400 ticker lists are stale.

``nikkei225_components.py`` and ``jpx400_components.py`` are hand-curated
Python literals that capture the index composition at a moment in time
(currently 2025-10-01). The lists drift as indexes rebalance — Nikkei
225 has ~10 swaps per year reviewed in April and October. A stale list
means yfinance can't fetch the removed tickers (silent skip) and the
newly-added tickers never enter the screening pool (silent miss).

This module fetches the *current* Nikkei 225 composition from Wikipedia
(best-effort: HTML parsing is fragile to layout changes, so failures
fall back to "no opinion" rather than aborting the cron), diffs it
against the static list, and renders a Slack-ready report so the
operator can manually update the .py file and commit.

Design choice — manual review rather than auto-write:
  - The static .py files carry the ``sector`` tag per ticker, which
    Wikipedia doesn't reliably provide. Auto-rewriting would drop
    sector tags or fill them with "不明", degrading sector_analysis.
  - Manual commit keeps git history clear ("Nikkei 225 April 2026
    rebalance: +ABC -XYZ") instead of an opaque "auto-refresh".
  - Wikipedia HTML is fragile — a parser regression should surface
    as "no signal" rather than "wrong universe in production".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import requests

from stock_analyzer.nikkei225_components import NIKKEI_225_TICKERS

logger = logging.getLogger(__name__)


_WIKIPEDIA_NIKKEI225_URL = "https://en.wikipedia.org/wiki/Nikkei_225"
# 4-digit Tokyo Stock Exchange ticker code regex. Matches against the
# raw href / text patterns Wikipedia uses for member lists. The .T
# suffix is appended afterward to align with yfinance convention.
_TICKER_CODE_RE = re.compile(r"\b(\d{4})\b")
# Headings that flank the Nikkei 225 constituent table in the English
# Wikipedia article. Used to narrow the page-text search window so we
# only pick codes from the constituent list, not the surrounding prose.
_CONSTITUENTS_HEADING_RE = re.compile(r"(?i)components?|constituents?|component\s+stocks?")


@dataclass(frozen=True)
class UniverseDiff:
    """Tickers gained / lost between static list and live source."""

    added: tuple[str, ...] = field(default_factory=tuple)
    removed: tuple[str, ...] = field(default_factory=tuple)
    static_count: int = 0
    live_count: int = 0
    source: str = ""

    @property
    def is_stale(self) -> bool:
        """True when either side has 5+ tickers the other lacks.

        Sub-5 diffs are typically false positives from imperfect
        Wikipedia parsing (e.g. table column shifts). Above 5 it's
        almost always a real rebalance — Nikkei 225 typically swaps
        ~10 per review.
        """
        return len(self.added) + len(self.removed) >= 5


def fetch_live_nikkei225_tickers(
    timeout: tuple[float, float] = (10.0, 30.0),
    user_agent: str = "ai-workflows-stock-analyzer (universe staleness check)",
) -> set[str]:
    """Best-effort fetch of current Nikkei 225 tickers from Wikipedia.

    Returns a set of ``NNNN.T`` strings. Raises ``RuntimeError`` if
    the fetch or parse looks too thin to be a real constituent list
    (under 100 codes) — callers treat that as "no signal" and skip
    the staleness check rather than reporting a false alarm.
    """
    resp = requests.get(
        _WIKIPEDIA_NIKKEI225_URL,
        timeout=timeout,
        headers={"User-Agent": user_agent},
    )
    resp.raise_for_status()
    html = resp.text
    codes = _extract_tse_codes(html)
    if len(codes) < 100:
        raise RuntimeError(f"only {len(codes)} TSE codes parsed from Wikipedia — page layout may have changed")
    return {f"{c}.T" for c in codes}


def _extract_tse_codes(html: str) -> set[str]:
    """Pull 4-digit TSE codes from the Wikipedia article body.

    Strategy: walk the page text, find the components section, and
    collect every 4-digit code that appears in a row containing a
    company name. Imperfect — Wikipedia's table HTML changes — but
    catches ~95% of constituents on a normal day; the ``is_stale``
    threshold absorbs the rest.
    """
    # Restrict to the constituent-table region when the heading is
    # detectable, otherwise scan the whole body (less accurate but
    # never zero).
    text = html
    heading_match = _CONSTITUENTS_HEADING_RE.search(html)
    if heading_match:
        text = html[heading_match.start() :]
        # Cut off at the next major top-level section heading.
        next_section = re.search(r"<h[12][^>]*>", text[200:])
        if next_section:
            text = text[: next_section.end() + 200]
    codes: set[str] = set()
    for m in _TICKER_CODE_RE.finditer(text):
        c = m.group(1)
        # TSE prime-market codes are 1000–9999. Exclude obvious noise:
        # 4-digit years (2000–2099) appear in references, dates etc.
        if 1000 <= int(c) <= 9999 and not (2000 <= int(c) <= 2099):
            codes.add(c)
    return codes


def diff_against_static(
    live: set[str] | None = None,
) -> UniverseDiff:
    """Compare the live Nikkei 225 set against the static .py file."""
    static = {t["ticker"] for t in NIKKEI_225_TICKERS}
    if live is None:
        try:
            live = fetch_live_nikkei225_tickers()
        except Exception as exc:
            logger.warning("Live Nikkei 225 fetch failed; staleness check skipped: %s", exc)
            return UniverseDiff(
                static_count=len(static),
                live_count=0,
                source="fetch_failed",
            )
    added = tuple(sorted(live - static))
    removed = tuple(sorted(static - live))
    return UniverseDiff(
        added=added,
        removed=removed,
        static_count=len(static),
        live_count=len(live),
        source="wikipedia",
    )


def format_diff_for_slack(diff: UniverseDiff) -> str:
    """Render the diff as a Slack-ready block. Empty when not stale."""
    if diff.source == "fetch_failed":
        return ""
    if not diff.is_stale:
        return ""
    lines = [
        "📋 Nikkei 225 構成銘柄に乖離検知",
        f"静的リスト {diff.static_count} 件 vs ライブ {diff.live_count} 件 (出典 {diff.source})",
    ]
    if diff.added:
        sample = ", ".join(diff.added[:10])
        more = f" 他 {len(diff.added) - 10} 件" if len(diff.added) > 10 else ""
        lines.append(f"➕ ライブにあって静的リストに無い: {sample}{more}")
    if diff.removed:
        sample = ", ".join(diff.removed[:10])
        more = f" 他 {len(diff.removed) - 10} 件" if len(diff.removed) > 10 else ""
        lines.append(f"➖ 静的リストにあってライブに無い: {sample}{more}")
    lines.append(
        "→ `stock_analyzer/stock_analyzer/nikkei225_components.py` を手動更新してください "
        "(Wikipedia 解析は不完全な可能性、Nikkei 公式 https://indexes.nikkei.co.jp/nkave/index/component で照合推奨)"
    )
    return "\n".join(lines)
