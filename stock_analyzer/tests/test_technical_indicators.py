"""Tests for technical_indicators — the heart of the screening logic.

The screening score and indicator summary are what land in Claude's
prompt, so regressions here propagate directly into the AI's analysis
quality. Cover the score breakdown (which signals fire on which
patterns) and the indicator helpers (RSI / SMA / MACD / Bollinger /
volume-ratio) on synthetic OHLCV data so the tests don't need yfinance.
"""

from __future__ import annotations

import pandas as pd

from stock_analyzer.technical_indicators import (
    compute_indicators,
    compute_screening_score,
)


def _df(closes: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame from a close-price series."""
    n = len(closes)
    if volumes is None:
        volumes = [1_000_000.0] * n
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c * 1.01 for c in closes],
            "Low": [c * 0.99 for c in closes],
            "Close": closes,
            "Volume": volumes,
        }
    )


def test_score_returns_tuple_of_score_and_components() -> None:
    """The new signature is ``(float, dict)`` — score + per-signal breakdown."""
    df = _df([100.0] * 30)
    score, components = compute_screening_score(df)
    assert isinstance(score, float)
    assert isinstance(components, dict)


def test_volume_spike_fires_when_recent_volume_exceeds_1_5x_average() -> None:
    """Latest day volume = 3x average → volume_spike component True."""
    closes = [100.0] * 30
    volumes = [1_000_000.0] * 29 + [3_000_000.0]
    df = _df(closes, volumes)
    _, components = compute_screening_score(df)
    assert components.get("volume_spike") is True


def test_volume_spike_silent_on_flat_volume() -> None:
    closes = [100.0] * 30
    volumes = [1_000_000.0] * 30
    df = _df(closes, volumes)
    _, components = compute_screening_score(df)
    assert "volume_spike" not in components


def test_score_increases_when_a_signal_fires() -> None:
    """Volume spike on otherwise-flat data → score > 0."""
    closes = [100.0] * 30
    volumes = [1_000_000.0] * 29 + [5_000_000.0]
    df_flat = _df(closes, [1_000_000.0] * 30)
    df_spike = _df(closes, volumes)
    flat_score, _ = compute_screening_score(df_flat)
    spike_score, components = compute_screening_score(df_spike)
    assert spike_score > flat_score
    assert components.get("volume_spike") is True


def test_fundamental_signals_fire_for_value_stock() -> None:
    """A stock with low PER + low PBR + high ROE fires all 3 fundamental signals."""
    df = _df([100.0] * 30)
    fundamentals = {
        "trailingPE": 10.0,  # < 15 → per_value
        "priceToBook": 0.8,  # < 1.0 → pbr_undervalued
        "returnOnEquity": 0.15,  # > 0.10 → roe_profitable
        "dividendYield": 4.0,  # > 3.0 → dividend_yield
        "revenueGrowth": 0.08,  # > 0.05 → revenue_growth
    }
    _, components = compute_screening_score(df, fundamentals=fundamentals)
    assert components.get("per_value") is True
    assert components.get("pbr_undervalued") is True
    assert components.get("roe_profitable") is True
    assert components.get("dividend_yield") is True
    assert components.get("revenue_growth") is True


def test_fundamental_signals_silent_for_growth_stock() -> None:
    """High PER + high PBR + low ROE → no value-fundamental signal fires."""
    df = _df([100.0] * 30)
    fundamentals = {
        "trailingPE": 50.0,
        "priceToBook": 5.0,
        "returnOnEquity": 0.05,
    }
    _, components = compute_screening_score(df, fundamentals=fundamentals)
    assert "per_value" not in components
    assert "pbr_undervalued" not in components
    assert "roe_profitable" not in components


def test_custom_weights_shift_score_total() -> None:
    """Increasing a weight on a fired signal proportionally lifts total score."""
    df = _df([100.0] * 30, volumes=[1_000_000.0] * 29 + [10_000_000.0])
    default_score, _ = compute_screening_score(df)
    boosted_score, _ = compute_screening_score(df, weights={"volume_spike": 100})
    assert boosted_score > default_score


def test_compute_indicators_returns_required_fields() -> None:
    """compute_indicators must produce the keys Claude's prompt references."""
    df = _df([100.0 + i * 0.1 for i in range(80)])
    summary = compute_indicators(df=df, ticker="0000.T", name="test")
    required = {
        "ticker",
        "name",
        "current_price",
        "price_change_1d",
        "price_change_5d",
        "price_change_1m",
        "trend_signal",
        "rsi_14",
        "macd_histogram",
        "bb_position_pct",
        "volume_ratio",
        "distance_from_52w_high",
        "distance_from_52w_low",
    }
    assert required.issubset(summary.keys())
    assert summary["ticker"] == "0000.T"


def test_compute_indicators_attaches_unrealized_pnl_when_avg_cost_given() -> None:
    df = _df([100.0] * 30)
    summary = compute_indicators(df=df, ticker="0000.T", name="x", avg_cost=80.0)
    # current 100, cost 80 → +25%
    assert summary["unrealized_pnl_pct"] == 25.0


def test_compute_indicators_handles_short_history() -> None:
    """A 5-bar history still returns a summary (with None for long-window indicators)."""
    df = _df([100.0, 101.0, 102.0, 103.0, 104.0])
    summary = compute_indicators(df=df, ticker="0000.T", name="x")
    assert summary["current_price"] == 104.0
    # SMA_75 should be None (only 5 bars)
    assert summary["sma_75"] is None
