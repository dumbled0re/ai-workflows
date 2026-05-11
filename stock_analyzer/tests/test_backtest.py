"""Tests for counterfactual backtest — direction-aware sim over the
resolved predictions_history with composable filters.
"""

from __future__ import annotations

from stock_analyzer.backtest import (
    SimResult,
    combine_and,
    format_counterfactuals_for_prompt,
    from_source,
    has_signal,
    only_confidence,
    only_direction,
    simulate,
    standard_counterfactuals,
)


def _pred(
    status: str,
    prediction: str = "UP",
    actual_return_pct: float = 5.0,
    confidence: str = "MEDIUM",
    source: str = "holdings",
    components: dict[str, bool] | None = None,
    reviewed_date: str = "2026-04-15",
) -> dict:
    return {
        "ticker": "0000.T",
        "name": "test",
        "prediction": prediction,
        "confidence": confidence,
        "status": status,
        "actual_return_pct": actual_return_pct,
        "source": source,
        "signal_components": components or {},
        "reviewed_date": reviewed_date,
    }


def test_simulate_empty_history_returns_empty_result() -> None:
    result = simulate({"predictions": []})
    assert result.trades == 0
    assert result.equity_curve == []


def test_simulate_unfiltered_aggregates_directional_returns() -> None:
    """A DOWN-win with raw -8% should contribute +8% to the equity curve."""
    history = {
        "predictions": [
            _pred("win", prediction="UP", actual_return_pct=5.0, reviewed_date="2026-04-10"),
            _pred("win", prediction="DOWN", actual_return_pct=-8.0, reviewed_date="2026-04-11"),
            _pred("loss", prediction="UP", actual_return_pct=-3.0, reviewed_date="2026-04-12"),
        ],
    }
    result = simulate(history)
    assert result.trades == 3
    assert result.wins == 2
    assert result.losses == 1
    # cumulative: +5 → +13 → +10
    assert result.equity_curve == [5.0, 13.0, 10.0]
    assert result.total_return_pct == 10.0


def test_simulate_with_filter_excludes_non_matching_trades() -> None:
    """Filter to HIGH-only — only those trades enter the sim."""
    history = {
        "predictions": [
            _pred("win", confidence="HIGH"),
            _pred("loss", confidence="MEDIUM"),
            _pred("win", confidence="HIGH"),
        ],
    }
    result = simulate(history, filter_fn=only_confidence("HIGH"), label="HIGH only")
    assert result.trades == 2
    assert result.wins == 2
    assert result.losses == 0


def test_simulate_max_drawdown_walks_peak_to_trough() -> None:
    history = {
        "predictions": [
            _pred("win", actual_return_pct=10.0, reviewed_date="2026-04-01"),
            _pred("loss", actual_return_pct=-5.0, reviewed_date="2026-04-02"),
            _pred("loss", actual_return_pct=-5.0, reviewed_date="2026-04-03"),
            _pred("loss", actual_return_pct=-5.0, reviewed_date="2026-04-04"),
        ],
    }
    result = simulate(history)
    # cumulative: +10 → +5 → 0 → -5. Peak = 10, trough = -5. DD = 15.
    assert result.max_drawdown_pct == 15.0


def test_has_signal_filter_excludes_predictions_without_signal() -> None:
    history = {
        "predictions": [
            _pred("win", components={"volume_spike": True}),
            _pred("win", components={"volume_spike": False}),
            _pred("loss", components={}),
        ],
    }
    result = simulate(history, filter_fn=has_signal("volume_spike"), label="vs")
    assert result.trades == 1
    assert result.wins == 1


def test_combine_and_requires_all_predicates() -> None:
    history = {
        "predictions": [
            _pred("win", confidence="HIGH", prediction="UP"),
            _pred("win", confidence="HIGH", prediction="DOWN", actual_return_pct=-5.0),
            _pred("win", confidence="MEDIUM", prediction="UP"),
        ],
    }
    filt = combine_and(only_confidence("HIGH"), only_direction("UP"))
    result = simulate(history, filter_fn=filt, label="HIGH+UP")
    assert result.trades == 1
    assert result.wins == 1


def test_simulate_sharpe_undefined_for_constant_returns() -> None:
    """Stdev=0 → sharpe None (avoids division by zero)."""
    history = {
        "predictions": [
            _pred("win", actual_return_pct=5.0),
            _pred("win", actual_return_pct=5.0),
            _pred("win", actual_return_pct=5.0),
        ],
    }
    result = simulate(history)
    assert result.sharpe_like is None


def test_standard_counterfactuals_drops_buckets_below_threshold() -> None:
    """Sims with <5 trades are filtered out so noise doesn't dominate the report."""
    history = {
        "predictions": [
            _pred("win", confidence="HIGH"),
            _pred("loss", confidence="MEDIUM"),
            _pred("win", confidence="MEDIUM"),
        ],
    }
    sims = standard_counterfactuals(history)
    # Only "baseline (all)" might qualify — but baseline has 3 trades,
    # below the 5-trade threshold → empty.
    assert all(s.trades >= 5 for s in sims)


def test_standard_counterfactuals_returns_baseline_when_enough_trades() -> None:
    """6 trades → baseline survives."""
    history = {
        "predictions": [_pred("win") for _ in range(3)] + [_pred("loss") for _ in range(3)],
    }
    sims = standard_counterfactuals(history)
    labels = [s.label for s in sims]
    assert "baseline (all)" in labels


def test_format_counterfactuals_sorts_by_sharpe_descending() -> None:
    """Sims are sorted so the highest-Sharpe filter floats to the top."""
    sims = [
        SimResult(
            label="A_low",
            trades=10,
            wins=5,
            losses=5,
            win_rate_pct=50.0,
            mean_return_pct=0.0,
            expectancy_per_trade_pct=0.0,
            profit_factor=1.0,
            sharpe_like=0.1,
            max_drawdown_pct=5.0,
            equity_curve=[],
        ),
        SimResult(
            label="B_high",
            trades=10,
            wins=8,
            losses=2,
            win_rate_pct=80.0,
            mean_return_pct=3.0,
            expectancy_per_trade_pct=3.0,
            profit_factor=4.0,
            sharpe_like=1.5,
            max_drawdown_pct=2.0,
            equity_curve=[],
        ),
    ]
    rendered = format_counterfactuals_for_prompt(sims)
    assert rendered.index("B_high") < rendered.index("A_low")


def test_from_source_filter() -> None:
    history = {
        "predictions": [
            _pred("win", source="holdings"),
            _pred("loss", source="short_term"),
            _pred("win", source="holdings"),
        ],
    }
    result = simulate(history, filter_fn=from_source("holdings"), label="h")
    assert result.trades == 2
