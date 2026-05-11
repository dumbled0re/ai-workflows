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


# ---------- relative-strength signal ---------------------------------------


def test_relative_strength_fires_when_stock_outperforms_benchmark() -> None:
    """A stock up 10% over 20 days against a flat N225 should fire RS."""
    # 25 bars so the 20-day pct_change is well-defined. Start at 100,
    # end at 110 → +10%. Benchmark stays flat.
    stock_closes = [100.0 + i * 0.5 for i in range(25)]
    bench = pd.Series([1000.0] * 25)
    score, components = compute_screening_score(_df(stock_closes), reference_close=bench)
    assert components.get("relative_strength") is True
    # Score should include the weight (default 15).
    assert score >= 15


def test_relative_strength_silent_when_benchmark_keeps_pace() -> None:
    """If the benchmark moves with the stock, RS must NOT fire — same
    return = zero relative edge."""
    stock_closes = [100.0 + i * 0.5 for i in range(25)]
    bench = pd.Series([1000.0 + i * 5.0 for i in range(25)])  # also ~+10%
    _, components = compute_screening_score(_df(stock_closes), reference_close=bench)
    assert components.get("relative_strength") is not True


def test_relative_strength_silent_when_stock_underperforms() -> None:
    """Stock flat vs benchmark up — RS must not fire on a laggard."""
    stock_closes = [100.0] * 25
    bench = pd.Series([1000.0 + i * 5.0 for i in range(25)])
    _, components = compute_screening_score(_df(stock_closes), reference_close=bench)
    assert components.get("relative_strength") is not True


def test_relative_strength_skipped_when_history_too_short() -> None:
    """A 10-bar history can't support a 20-day RS calc — fail to None,
    not crash."""
    stock_closes = [100.0 + i for i in range(10)]
    bench = pd.Series([1000.0] * 10)
    _, components = compute_screening_score(_df(stock_closes), reference_close=bench)
    assert components.get("relative_strength") is not True


def test_relative_strength_signal_disabled_without_benchmark() -> None:
    """Backward compat: callers passing no reference_close must not see
    a spurious False key — the signal is simply absent."""
    stock_closes = [100.0 + i * 0.5 for i in range(25)]
    _, components = compute_screening_score(_df(stock_closes))
    assert "relative_strength" not in components


# ---------- sector-rotation signal -----------------------------------------


def test_sector_rotation_fires_when_caller_marks_in_leading_sector() -> None:
    """The signal is gated purely on the ``sector_in_leading`` parameter;
    the score function trusts the caller's sector-aggregation result."""
    df = _df([100.0] * 30)
    score, components = compute_screening_score(df, sector_in_leading=True)
    assert components.get("sector_rotation") is True
    assert score >= 15


def test_sector_rotation_silent_by_default() -> None:
    """Backward compat: default ``sector_in_leading=False`` means no key
    appears — same shape as before this signal landed."""
    df = _df([100.0] * 30)
    _, components = compute_screening_score(df)
    assert "sector_rotation" not in components


# ---------- leading-sectors aggregation ------------------------------------


def test_compute_leading_sectors_returns_sector_beating_benchmark_by_edge() -> None:
    """Two sectors: tech up 10% over 20 days, autos up 1%. Benchmark
    flat → tech beats by 10pp (>=3pp edge), autos by 1pp (silent)."""
    from stock_analyzer.stock_screener import _compute_leading_sectors

    def up_series(start: float, end: float, n: int = 25) -> pd.Series:
        step = (end - start) / (n - 1)
        return pd.Series([start + i * step for i in range(n)])

    data = {
        "T1.T": _df([100.0 + i * 0.5 for i in range(25)]),  # +12%
        "T2.T": _df([100.0 + i * 0.4 for i in range(25)]),  # +10%
        "T3.T": _df([100.0 + i * 0.5 for i in range(25)]),  # +12%
        "A1.T": _df([100.0 + i * 0.04 for i in range(25)]),  # +1%
        "A2.T": _df([100.0 + i * 0.04 for i in range(25)]),  # +1%
        "A3.T": _df([100.0 + i * 0.04 for i in range(25)]),  # +1%
    }
    info = {
        "T1.T": {"sector": "情報通信"},
        "T2.T": {"sector": "情報通信"},
        "T3.T": {"sector": "情報通信"},
        "A1.T": {"sector": "自動車"},
        "A2.T": {"sector": "自動車"},
        "A3.T": {"sector": "自動車"},
    }
    bench = up_series(1000.0, 1000.0)  # flat

    leading = _compute_leading_sectors(data, info, reference_close=bench, edge_pp=3.0, min_tickers=3)
    assert "情報通信" in leading
    assert "自動車" not in leading


def test_compute_leading_sectors_skips_below_min_tickers() -> None:
    """A 1-ticker sector is statistical noise — must not enter the
    leading set even if that one ticker is up enormously."""
    from stock_analyzer.stock_screener import _compute_leading_sectors

    data = {"T1.T": _df([100.0 + i * 2.0 for i in range(25)])}  # +50%
    info = {"T1.T": {"sector": "情報通信"}}
    bench = pd.Series([1000.0] * 25)
    leading = _compute_leading_sectors(data, info, reference_close=bench, min_tickers=3)
    assert leading == set()


def test_compute_leading_sectors_returns_empty_without_benchmark() -> None:
    """No benchmark → cannot define 'leading'. Must return an empty set,
    not raise."""
    from stock_analyzer.stock_screener import _compute_leading_sectors

    data = {"T1.T": _df([100.0 + i for i in range(25)])}
    info = {"T1.T": {"sector": "情報通信"}}
    assert _compute_leading_sectors(data, info, reference_close=None) == set()


# ---------- analyst-consensus signals --------------------------------------


def test_analyst_target_upside_fires_at_or_above_15pct() -> None:
    """Mean target 1300 / current 1000 → +30% upside, well above the
    15% threshold. Both the score and the components fingerprint must
    reflect the firing signal."""
    df = _df([100.0] * 30 + [1000.0])  # final close is 1000
    fund = {"targetMeanPrice": 1300.0}
    score, comps = compute_screening_score(df, fundamentals=fund)
    assert comps.get("analyst_target_upside") is True
    assert score >= 15


def test_analyst_target_upside_silent_below_threshold() -> None:
    """Mean target only 10% above current → under the 15% threshold,
    no fire. The signal is gating, not graded."""
    df = _df([100.0] * 30 + [1000.0])
    fund = {"targetMeanPrice": 1100.0}
    _, comps = compute_screening_score(df, fundamentals=fund)
    assert "analyst_target_upside" not in comps


def test_analyst_consensus_buy_fires_when_rating_strong_and_sample_ok() -> None:
    """Rating 2.0 (= aggregate Buy) with 5 analysts → bullish enough."""
    df = _df([100.0] * 30)
    fund = {"recommendationMean": 2.0, "numberOfAnalystOpinions": 5}
    _, comps = compute_screening_score(df, fundamentals=fund)
    assert comps.get("analyst_consensus_buy") is True


def test_analyst_consensus_buy_requires_min_analysts() -> None:
    """2 analysts is too thin for a meaningful aggregate — must not fire
    even on a Strong Buy mean. The signal is meant to surface
    institutional consensus, not the view of a couple of shops."""
    df = _df([100.0] * 30)
    fund = {"recommendationMean": 1.5, "numberOfAnalystOpinions": 2}
    _, comps = compute_screening_score(df, fundamentals=fund)
    assert "analyst_consensus_buy" not in comps


def test_analyst_consensus_buy_silent_on_hold_or_worse() -> None:
    """Rating 3.0 (Hold) → no signal even with many analysts."""
    df = _df([100.0] * 30)
    fund = {"recommendationMean": 3.0, "numberOfAnalystOpinions": 10}
    _, comps = compute_screening_score(df, fundamentals=fund)
    assert "analyst_consensus_buy" not in comps


def test_analyst_signals_absent_without_fundamentals() -> None:
    """Backward compat: no fundamentals → both analyst keys absent."""
    df = _df([100.0] * 30)
    _, comps = compute_screening_score(df)
    assert "analyst_target_upside" not in comps
    assert "analyst_consensus_buy" not in comps


def test_low_peg_ratio_fires_when_growth_outpaces_pe() -> None:
    """Forward P/E 12 + earnings growth 20% → PEG 0.6 → fire. Classic
    Lynch GARP setup that serious value-growth investors hunt for."""
    df = _df([100.0] * 30)
    fund = {"forwardPE": 12.0, "earningsGrowth": 0.20}
    _, comps = compute_screening_score(df, fundamentals=fund)
    assert comps.get("low_peg_ratio") is True


def test_peg_silent_at_or_above_one() -> None:
    """P/E 25 with 20% growth → PEG 1.25, no fire. Boundary at 1.0
    is strict (<), pinning the gating semantics."""
    df = _df([100.0] * 30)
    fund = {"forwardPE": 25.0, "earningsGrowth": 0.20}
    _, comps = compute_screening_score(df, fundamentals=fund)
    assert "low_peg_ratio" not in comps


def test_peg_silent_on_negative_growth() -> None:
    """A shrinking company shouldn't fire a GARP signal even if its
    P/E is low — negative earningsGrowth disqualifies."""
    df = _df([100.0] * 30)
    fund = {"forwardPE": 8.0, "earningsGrowth": -0.10}
    _, comps = compute_screening_score(df, fundamentals=fund)
    assert "low_peg_ratio" not in comps


def test_near_52w_high_fires_within_5pct() -> None:
    """Close at 100, 252d high at 102 → within 2% of high → fire.
    George & Hwang (2004) 52WH effect. Need >=60 bars for the
    rolling-max signal to engage."""
    # 65-bar series: build up to a 102 peak then back near it.
    closes = [50.0 + i * 0.5 for i in range(30)] + [102.0] + [99.0] * 33 + [100.0]
    df = _df(closes)
    _, comps = compute_screening_score(df)
    assert comps.get("near_52w_high") is True


def test_near_52w_high_silent_when_far_below() -> None:
    """Close at 80 with rolling high of 120 → 33% below → no fire."""
    closes = [80.0] * 30 + [120.0] * 5 + [80.0] * 30
    df = _df(closes)
    _, comps = compute_screening_score(df)
    assert "near_52w_high" not in comps


def test_near_52w_high_silent_on_short_history() -> None:
    """A 40-bar history is too short for a 52WH signal — must abstain
    rather than fire on a meaningless 'rolling max'."""
    df = _df([100.0] * 40)
    _, comps = compute_screening_score(df)
    assert "near_52w_high" not in comps


def test_volume_trend_up_fires_on_sustained_elevation() -> None:
    """Last 5 sessions averaging 25%+ above the 20-day average →
    institutional accumulation pattern. volume_spike was a single-day
    measure; this catches the multi-session build-up."""
    # 20 bars of low volume, then 5 bars of elevated volume → 5-day
    # avg significantly above the 20-day rolling average.
    volumes = [1_000_000.0] * 15 + [2_000_000.0] * 5
    df = _df([100.0] * 20, volumes=volumes)
    _, comps = compute_screening_score(df)
    assert comps.get("volume_trend_up") is True


def test_volume_trend_silent_when_flat() -> None:
    """Steady volume → no fire even if absolute level is high."""
    df = _df([100.0] * 30, volumes=[2_000_000.0] * 30)
    _, comps = compute_screening_score(df)
    assert "volume_trend_up" not in comps


def test_compute_leading_sectors_ignores_unknown_sector_tickers() -> None:
    """Tickers with sector='不明' (universe merging fallback) must not
    pollute a real sector's average."""
    from stock_analyzer.stock_screener import _compute_leading_sectors

    data = {
        "T1.T": _df([100.0 + i * 0.5 for i in range(25)]),
        "X.T": _df([100.0] * 25),  # flat, but '不明'
    }
    info = {
        "T1.T": {"sector": "情報通信"},
        "X.T": {"sector": "不明"},
    }
    bench = pd.Series([1000.0] * 25)
    leading = _compute_leading_sectors(data, info, reference_close=bench, min_tickers=1, edge_pp=3.0)
    # T1 alone is enough at min_tickers=1; the 不明 ticker stays out.
    assert "情報通信" in leading
    assert "不明" not in leading
