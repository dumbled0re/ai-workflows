from __future__ import annotations

from stock_analyzer.signal_tags import annotate_margin_signals


def test_low_pressure_fires_below_threshold() -> None:
    """margin_ratio < 1.5 sets the low-pressure tag — short-squeeze setup
    territory. Existing signal_components keys are preserved."""
    summary: dict = {"margin_ratio": 0.8, "signal_components": {"existing": True}}
    annotate_margin_signals(summary)
    assert summary["signal_components"]["margin_low_pressure"] is True
    assert summary["signal_components"]["existing"] is True
    assert "margin_overhang" not in summary["signal_components"]


def test_overhang_fires_above_high_threshold() -> None:
    """margin_ratio > 5.0 maps to overhang (investment_rules avoid_entry)."""
    summary: dict = {"margin_ratio": 6.5}
    annotate_margin_signals(summary)
    assert summary["signal_components"] == {"margin_overhang": True}


def test_middle_band_emits_no_tag() -> None:
    """The 1.5-5.0 band is the 'ordinary' zone — margin doesn't usefully
    predict on its own. No tag should be added, so signal_components
    is created (empty) rather than left missing."""
    summary: dict = {"margin_ratio": 3.0}
    annotate_margin_signals(summary)
    assert summary.get("signal_components") == {}


def test_no_margin_ratio_leaves_summary_untouched() -> None:
    """Older summaries built before margin tracking landed must not
    crash. Without ``margin_ratio`` we make zero mutations — not even
    creating an empty signal_components dict."""
    summary: dict = {"ticker": "X.T"}
    annotate_margin_signals(summary)
    assert "signal_components" not in summary
    assert summary == {"ticker": "X.T"}


def test_unparseable_margin_ratio_is_silent() -> None:
    """A string or None for margin_ratio (corrupt upstream data) is
    skipped, not crashed on."""
    summary: dict = {"margin_ratio": "N/A"}
    annotate_margin_signals(summary)
    assert "signal_components" not in summary


def test_boundary_at_low_pressure_threshold_is_exclusive() -> None:
    """1.5 itself is NOT low pressure (threshold is strict <). Pin the
    boundary so a future tweak doesn't quietly shift behaviour."""
    summary: dict = {"margin_ratio": 1.5}
    annotate_margin_signals(summary)
    assert summary["signal_components"] == {}


def test_boundary_at_overhang_threshold_is_exclusive() -> None:
    """5.0 itself is the avoid_entry cutoff in investment_rules but not
    'overhang' for the tag — only strictly > 5.0 fires."""
    summary: dict = {"margin_ratio": 5.0}
    annotate_margin_signals(summary)
    assert summary["signal_components"] == {}
