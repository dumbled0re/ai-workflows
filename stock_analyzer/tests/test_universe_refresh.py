"""Tests for universe_refresh — staleness detection between the static
Nikkei 225 ticker list and the live source. The fetch path itself is
best-effort (Wikipedia HTML changes), so tests focus on the diff +
rendering logic rather than the HTTP fetch."""

from __future__ import annotations

from unittest.mock import patch

from stock_analyzer.universe_refresh import (
    UniverseDiff,
    diff_against_static,
    format_diff_for_slack,
)


def test_diff_marks_clean_when_lists_match() -> None:
    """A live set equal to the static set → empty diff, not stale."""
    from stock_analyzer.nikkei225_components import NIKKEI_225_TICKERS

    static_set = {t["ticker"] for t in NIKKEI_225_TICKERS}
    diff = diff_against_static(live=static_set)
    assert diff.added == ()
    assert diff.removed == ()
    assert diff.is_stale is False


def test_diff_marks_stale_on_significant_change() -> None:
    """5+ tickers different → stale."""
    diff = UniverseDiff(
        added=("9999.T", "9998.T", "9997.T"),
        removed=("1001.T", "1002.T", "1003.T"),
        static_count=225,
        live_count=225,
        source="wikipedia",
    )
    assert diff.is_stale is True


def test_diff_not_stale_below_threshold() -> None:
    """4 tickers different = within parser-noise tolerance → not stale."""
    diff = UniverseDiff(
        added=("9999.T", "9998.T"),
        removed=("1001.T", "1002.T"),
        static_count=225,
        live_count=225,
        source="wikipedia",
    )
    assert diff.is_stale is False


def test_format_for_slack_empty_when_not_stale() -> None:
    """Below-threshold diff renders to empty string (no Slack post)."""
    diff = UniverseDiff(static_count=225, live_count=225, source="wikipedia")
    assert format_diff_for_slack(diff) == ""


def test_format_for_slack_empty_when_fetch_failed() -> None:
    """fetch_failed source → no Slack post (don't alert on transient errors)."""
    diff = UniverseDiff(static_count=225, live_count=0, source="fetch_failed")
    assert format_diff_for_slack(diff) == ""


def test_format_for_slack_renders_added_and_removed() -> None:
    diff = UniverseDiff(
        added=("9999.T", "9998.T", "9997.T"),
        removed=("1001.T", "1002.T", "1003.T"),
        static_count=225,
        live_count=225,
        source="wikipedia",
    )
    rendered = format_diff_for_slack(diff)
    assert "Nikkei 225" in rendered
    assert "9999.T" in rendered
    assert "1001.T" in rendered
    assert "nikkei225_components.py" in rendered  # next-action instruction


def test_format_for_slack_truncates_long_lists() -> None:
    """Diff with >10 entries on a side shows "他 N 件" suffix."""
    added = tuple(f"99{i:02d}.T" for i in range(15))
    diff = UniverseDiff(added=added, static_count=225, live_count=240, source="wikipedia")
    rendered = format_diff_for_slack(diff)
    assert "他 5 件" in rendered


def test_diff_falls_back_silently_when_fetch_raises() -> None:
    """If the live fetcher raises, diff_against_static returns a
    fetch_failed-source diff rather than propagating the exception."""
    with patch("stock_analyzer.universe_refresh.fetch_live_nikkei225_tickers") as mock_fetch:
        mock_fetch.side_effect = RuntimeError("network down")
        diff = diff_against_static(live=None)
    assert diff.source == "fetch_failed"
    assert diff.live_count == 0
    assert diff.is_stale is False
