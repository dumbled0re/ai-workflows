from __future__ import annotations

import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


def compute_sector_rankings(
    fundamentals: dict[str, dict],
    ticker_info: dict[str, dict],
) -> dict[str, dict]:
    """Compute sector-relative rankings for each stock.

    Args:
        fundamentals: Dict mapping ticker to fundamental data from yfinance
        ticker_info: Dict mapping ticker to info dict with 'sector' key

    Returns:
        Dict mapping ticker to sector ranking info
    """
    # Group stocks by sector
    sector_stocks: dict[str, list[str]] = defaultdict(list)
    for ticker in fundamentals:
        info = ticker_info.get(ticker, {})
        sector = info.get("sector", "不明")
        if sector != "不明":
            sector_stocks[sector].append(ticker)

    rankings: dict[str, dict] = {}

    for sector, tickers in sector_stocks.items():
        if len(tickers) < 2:
            continue

        # Collect metrics for the sector
        sector_per = []
        sector_pbr = []
        sector_roe = []

        for t in tickers:
            f = fundamentals.get(t, {})
            per = f.get("trailingPE")
            pbr = f.get("priceToBook")
            roe = f.get("returnOnEquity")

            if per is not None and per > 0:
                sector_per.append((t, per))
            if pbr is not None and pbr > 0:
                sector_pbr.append((t, pbr))
            if roe is not None:
                sector_roe.append((t, roe))

        # Compute medians
        per_median = _median([v for _, v in sector_per]) if sector_per else None
        pbr_median = _median([v for _, v in sector_pbr]) if sector_pbr else None
        roe_median = _median([v for _, v in sector_roe]) if sector_roe else None

        # Assign rankings
        for t in tickers:
            f = fundamentals.get(t, {})
            ranking: dict = {
                "sector": sector,
                "sector_size": len(tickers),
            }

            per_val = f.get("trailingPE")
            if per_val is not None and per_val > 0 and per_median:
                ranking["per_vs_sector"] = _relative_label(per_val, per_median, lower_is_better=True)
                ranking["sector_per_median"] = round(per_median, 1)

            pbr_val = f.get("priceToBook")
            if pbr_val is not None and pbr_val > 0 and pbr_median:
                ranking["pbr_vs_sector"] = _relative_label(pbr_val, pbr_median, lower_is_better=True)
                ranking["sector_pbr_median"] = round(pbr_median, 2)

            roe_val = f.get("returnOnEquity")
            if roe_val is not None and roe_median is not None:
                ranking["roe_vs_sector"] = _relative_label(roe_val, roe_median, lower_is_better=False)
                ranking["sector_roe_median"] = round(roe_median * 100, 1)

            # Overall sector attractiveness score
            score = 0
            if ranking.get("per_vs_sector") == "割安":
                score += 1
            if ranking.get("pbr_vs_sector") == "割安":
                score += 1
            if ranking.get("roe_vs_sector") == "優秀":
                score += 1
            ranking["sector_score"] = score  # 0-3

            rankings[t] = ranking

    logger.info(
        "Computed sector rankings for %d stocks across %d sectors",
        len(rankings),
        len(sector_stocks),
    )
    return rankings


def format_sector_ranking(ranking: dict | None) -> str:
    """Format a single stock's sector ranking into text for display."""
    if not ranking:
        return ""

    parts = [f"セクター: {ranking['sector']} ({ranking['sector_size']}社)"]

    if "per_vs_sector" in ranking:
        parts.append(f"PER: {ranking['per_vs_sector']} (業界中央値: {ranking['sector_per_median']})")
    if "pbr_vs_sector" in ranking:
        parts.append(f"PBR: {ranking['pbr_vs_sector']} (業界中央値: {ranking['sector_pbr_median']})")
    if "roe_vs_sector" in ranking:
        parts.append(f"ROE: {ranking['roe_vs_sector']} (業界中央値: {ranking['sector_roe_median']}%)")

    return " | ".join(parts)


def _median(values: list[float]) -> float:
    """Compute median of a list of values."""
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2 == 0:
        return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2
    return sorted_vals[mid]


def _relative_label(value: float, median: float, lower_is_better: bool) -> str:
    """Return a label based on value vs median comparison."""
    if median == 0:
        return "平均"

    ratio = value / median

    if lower_is_better:
        if ratio < 0.7:
            return "割安"
        elif ratio > 1.3:
            return "割高"
        else:
            return "平均"
    else:
        if ratio > 1.3:
            return "優秀"
        elif ratio < 0.7:
            return "低い"
        else:
            return "平均"
