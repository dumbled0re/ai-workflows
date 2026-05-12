"""Tests for counterfactual backtest — direction-aware sim over the
resolved predictions_history with composable filters.
"""

from __future__ import annotations

from stock_analyzer.backtest import (
    SimResult,
    combine_and,
    compare_gross_vs_net,
    format_counterfactuals_for_prompt,
    format_gross_vs_net_for_prompt,
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


# ---------- transaction-cost model --------------------------------------


def test_simulate_with_tc_reduces_each_trade_return() -> None:
    """A 0.4% TC per round-trip eats 0.4 off every trade's directional
    return. Five +5% wins gross-cumulate to +25%; net is +23%."""
    preds = [_pred("win", actual_return_pct=5.0, reviewed_date=f"2026-04-{i:02d}") for i in range(1, 6)]
    history = {"predictions": preds}
    gross = simulate(history, label="gross")
    net = simulate(history, label="net", tc_round_trip_pct=0.4)
    assert gross.total_return_pct == 25.0
    assert net.total_return_pct == 23.0
    # Per-trade mean and expectancy both shift by the TC.
    assert round(gross.mean_return_pct - net.mean_return_pct, 2) == 0.4


def test_simulate_tc_can_flip_marginal_win_to_net_loss() -> None:
    """A 1% gross win + 0.4% TC nets +0.6%; still positive. But a
    series of marginal wins post-TC can flatten the equity curve.
    Pin the per-trade arithmetic so future refactors don't drift."""
    history = {"predictions": [_pred("win", actual_return_pct=1.0, reviewed_date="2026-04-01")]}
    net = simulate(history, tc_round_trip_pct=0.4)
    assert net.total_return_pct == 0.6


def test_compare_gross_vs_net_reports_tc_drag_per_filter() -> None:
    """For each filter in the standard battery, the comparison
    returns paired gross/net metrics with TC drag spelled out."""
    history = {
        "predictions": [
            _pred("win", actual_return_pct=5.0, source="holdings", reviewed_date=f"2026-04-{i:02d}")
            for i in range(1, 7)
        ]
    }
    report = compare_gross_vs_net(history, tc_round_trip_pct=0.4)
    assert report["tc_round_trip_pct"] == 0.4
    rows = report["rows"]
    assert len(rows) >= 1
    baseline = next(r for r in rows if r["label"] == "baseline (all)")
    # 6 trades * 0.4 TC = 2.4 total drag
    assert baseline["tc_drag_pct"] == 2.4
    assert baseline["gross_total_return_pct"] > baseline["net_total_return_pct"]


def test_format_gross_vs_net_flags_negative_net() -> None:
    """When a filter is gross-positive but net-negative the rendered
    block must include the ⚠ flag so the AI sees it without reading
    the numbers itself."""
    report = {
        "tc_round_trip_pct": 0.4,
        "rows": [
            {
                "label": "marginal-strategy",
                "trades": 10,
                "gross_total_return_pct": 3.0,
                "net_total_return_pct": -1.0,
                "tc_drag_pct": 4.0,
                "gross_expectancy_pct": 0.3,
                "net_expectancy_pct": -0.1,
                "gross_sharpe": 0.2,
                "net_sharpe": -0.05,
                "gross_max_dd_pct": 5.0,
                "net_max_dd_pct": 6.0,
            }
        ],
    }
    text = format_gross_vs_net_for_prompt(report)
    assert "marginal-strategy" in text
    assert "net マイナス" in text
    assert "TC drag 4" in text


def test_format_gross_vs_net_empty_input_returns_empty() -> None:
    assert format_gross_vs_net_for_prompt({}) == ""
    assert format_gross_vs_net_for_prompt({"rows": []}) == ""
