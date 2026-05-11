"""Tests for sector_analysis — per-sector relative ranking of PER/PBR/ROE.

The ranking feeds Claude's "this stock is cheap vs sector average"
context, which the AI uses to differentiate value picks. Bugs here
silently degrade the discovery section quality.
"""

from __future__ import annotations

from stock_analyzer.sector_analysis import (
    _median,
    _relative_label,
    compute_sector_rankings,
    format_sector_ranking,
)


def test_median_odd_length() -> None:
    assert _median([1.0, 3.0, 5.0]) == 3.0


def test_median_even_length_averages_middle_two() -> None:
    assert _median([1.0, 2.0, 4.0, 5.0]) == 3.0


def test_median_empty_returns_zero() -> None:
    assert _median([]) == 0.0


def test_relative_label_lower_is_better_buckets() -> None:
    """For PER/PBR style metrics, lower vs median is undervalued."""
    median = 20.0
    # < 70% of median → 割安
    assert _relative_label(10.0, median, lower_is_better=True) == "割安"
    # > 130% of median → 割高
    assert _relative_label(30.0, median, lower_is_better=True) == "割高"
    # In between → 平均
    assert _relative_label(20.0, median, lower_is_better=True) == "平均"


def test_relative_label_higher_is_better_buckets() -> None:
    """For ROE-style metrics, higher vs median is better."""
    median = 0.10
    # > 130% of median → 優秀
    assert _relative_label(0.15, median, lower_is_better=False) == "優秀"
    # < 70% of median → 低い
    assert _relative_label(0.05, median, lower_is_better=False) == "低い"
    # In between → 平均
    assert _relative_label(0.10, median, lower_is_better=False) == "平均"


def test_compute_sector_rankings_attaches_relative_labels() -> None:
    """Two stocks in the same sector with different PER → one should be 割安."""
    fundamentals = {
        "A": {"trailingPE": 10.0, "priceToBook": 1.0, "returnOnEquity": 0.15},
        "B": {"trailingPE": 30.0, "priceToBook": 2.0, "returnOnEquity": 0.08},
    }
    ticker_info = {
        "A": {"sector": "Tech"},
        "B": {"sector": "Tech"},
    }
    rankings = compute_sector_rankings(fundamentals, ticker_info)
    assert "A" in rankings
    assert "B" in rankings
    # A has PER=10, B has PER=30; median=20. A is 0.5x median → 割安
    assert rankings["A"]["per_vs_sector"] == "割安"
    # B is 1.5x median → 割高
    assert rankings["B"]["per_vs_sector"] == "割高"


def test_compute_sector_rankings_skips_single_member_sectors() -> None:
    """Sectors with only 1 stock can't compute meaningful median → skip."""
    fundamentals = {
        "A": {"trailingPE": 10.0, "priceToBook": 1.0, "returnOnEquity": 0.15},
    }
    ticker_info = {"A": {"sector": "Tech"}}
    rankings = compute_sector_rankings(fundamentals, ticker_info)
    assert rankings == {}


def test_compute_sector_rankings_excludes_unknown_sector() -> None:
    """'不明' sector tags are excluded from grouping (can't compare meaningfully)."""
    fundamentals = {
        "A": {"trailingPE": 10.0, "priceToBook": 1.0, "returnOnEquity": 0.15},
        "B": {"trailingPE": 30.0, "priceToBook": 2.0, "returnOnEquity": 0.08},
    }
    ticker_info = {
        "A": {"sector": "不明"},
        "B": {"sector": "不明"},
    }
    rankings = compute_sector_rankings(fundamentals, ticker_info)
    assert rankings == {}


def test_sector_score_aggregates_three_dimensions() -> None:
    """A stock that's cheap on PER+PBR and high on ROE scores 3/3."""
    fundamentals = {
        "A": {"trailingPE": 10.0, "priceToBook": 0.5, "returnOnEquity": 0.20},
        "B": {"trailingPE": 30.0, "priceToBook": 2.0, "returnOnEquity": 0.05},
    }
    ticker_info = {
        "A": {"sector": "Tech"},
        "B": {"sector": "Tech"},
    }
    rankings = compute_sector_rankings(fundamentals, ticker_info)
    assert rankings["A"]["sector_score"] == 3
    assert rankings["B"]["sector_score"] == 0


def test_format_sector_ranking_renders_full_context() -> None:
    ranking = {
        "sector": "Tech",
        "sector_size": 5,
        "per_vs_sector": "割安",
        "sector_per_median": 18.5,
        "pbr_vs_sector": "平均",
        "sector_pbr_median": 1.5,
        "roe_vs_sector": "優秀",
        "sector_roe_median": 12.0,
    }
    rendered = format_sector_ranking(ranking)
    assert "Tech" in rendered
    assert "割安" in rendered
    assert "18.5" in rendered  # sector PER median


def test_format_sector_ranking_empty_for_no_data() -> None:
    assert format_sector_ranking(None) == ""
    assert format_sector_ranking({}) == ""
