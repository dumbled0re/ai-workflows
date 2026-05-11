"""Tests for calendar_context — seasonality detection that feeds Claude
the active Japan-equity timing context (earnings concentration, dividend
ex-date approach, year-end thinning, GW, summer doldrums)."""

from __future__ import annotations

from datetime import date

from stock_analyzer.calendar_context import (
    detect_calendar_signals,
    format_signals_for_prompt,
)


def _kinds(today: date) -> set[str]:
    return {s.kind for s in detect_calendar_signals(today)}


def test_earnings_q4_active_late_april() -> None:
    """4/15 – 5/20 is annual-earnings concentration for 3月期 firms."""
    assert "earnings_q4" in _kinds(date(2026, 4, 25))
    assert "earnings_q4" in _kinds(date(2026, 5, 10))


def test_earnings_q4_silent_outside_window() -> None:
    assert "earnings_q4" not in _kinds(date(2026, 4, 14))
    assert "earnings_q4" not in _kinds(date(2026, 5, 21))


def test_earnings_q2_active_late_october_to_mid_november() -> None:
    assert "earnings_q2" in _kinds(date(2026, 10, 30))
    assert "earnings_q2" in _kinds(date(2026, 11, 10))


def test_year_end_window_wraps_across_year_boundary() -> None:
    """Dec 25 through Jan 8 wraps the year change."""
    assert "year_end" in _kinds(date(2026, 12, 28))
    assert "year_end" in _kinds(date(2027, 1, 3))
    assert "year_end" not in _kinds(date(2026, 12, 24))
    assert "year_end" not in _kinds(date(2027, 1, 9))


def test_golden_week_window() -> None:
    """Late April + first week of May."""
    assert "golden_week" in _kinds(date(2026, 4, 28))
    assert "golden_week" in _kinds(date(2026, 5, 3))
    assert "golden_week" not in _kinds(date(2026, 4, 24))


def test_summer_doldrums_window() -> None:
    assert "summer_doldrums" in _kinds(date(2026, 8, 1))
    assert "summer_doldrums" not in _kinds(date(2026, 7, 15))
    assert "summer_doldrums" not in _kinds(date(2026, 8, 26))


def test_dividend_ex_signal_within_10_trading_days() -> None:
    """Within 10 trading days of 3/31 or 9/30 the dividend_ex flag lights up.

    2026-03-31 is a Tuesday → trading day; 10 trading days before is
    2026-03-17. So dates from 2026-03-17 onward should flag.
    """
    # Just before the 10-trading-day boundary
    far_before = date(2026, 3, 10)
    assert "dividend_ex" not in _kinds(far_before)
    # Inside the warning horizon
    close_before = date(2026, 3, 25)
    assert "dividend_ex" in _kinds(close_before)


def test_dividend_ex_signal_silent_after_record_day_passes() -> None:
    """Day after Sep 30 — next record date is 6 months out, well outside the horizon."""
    after = date(2026, 10, 1)
    assert "dividend_ex" not in _kinds(after)


def test_signals_stack_when_multiple_active() -> None:
    """Late April hits both earnings_q4 AND golden_week."""
    kinds = _kinds(date(2026, 4, 28))
    assert "earnings_q4" in kinds
    assert "golden_week" in kinds


def test_format_signals_returns_empty_when_no_signals() -> None:
    assert format_signals_for_prompt([]) == ""


def test_format_signals_renders_active_signals() -> None:
    signals = detect_calendar_signals(date(2026, 4, 28))
    rendered = format_signals_for_prompt(signals)
    assert "暦・季節要因" in rendered
    assert "earnings_q4" in rendered or "本決算" in rendered
