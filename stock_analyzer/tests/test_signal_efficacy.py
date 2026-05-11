"""Tests for compute_signal_efficacy — per-screening-signal win rate
analysis that turns predictions_history into screening_weight evidence.

The function correlates which screening signals fired at entry with
the eventual win/loss outcome. The weekly review prompt consumes the
lift_pct (with-signal accuracy minus without-signal accuracy) to tune
screening_weights data-driven instead of intuition-driven.
"""

from __future__ import annotations

from stock_analyzer.performance_tracker import (
    compute_signal_efficacy,
    format_signal_efficacy,
)


def _pred(status: str, components: dict[str, bool]) -> dict:
    """Build a resolved-prediction stub carrying signal_components."""
    return {
        "ticker": "0000.T",
        "prediction": "UP",
        "status": status,
        "actual_return_pct": 5.0 if status == "win" else -5.0,
        "signal_components": components,
    }


def test_empty_history_returns_empty() -> None:
    assert compute_signal_efficacy({"predictions": []}) == {}


def test_skips_unresolved_predictions() -> None:
    """Pending predictions don't have an outcome → excluded from the count."""
    history = {
        "predictions": [
            {"status": "pending", "signal_components": {"volume_spike": True}},
            {"status": "pending", "signal_components": {"volume_spike": True}},
            {"status": "pending", "signal_components": {"volume_spike": True}},
            {"status": "pending", "signal_components": {"volume_spike": True}},
            {"status": "pending", "signal_components": {"volume_spike": True}},
        ],
    }
    assert compute_signal_efficacy(history) == {}


def test_signal_with_positive_lift() -> None:
    """A signal that fires only on wins → 100% with vs 0% without → lift +100."""
    history = {
        "predictions": [
            _pred("win", {"volume_spike": True}),
            _pred("win", {"volume_spike": True}),
            _pred("win", {"volume_spike": True}),
            _pred("win", {"volume_spike": True}),
            _pred("win", {"volume_spike": True}),
            _pred("loss", {"volume_spike": False}),
            _pred("loss", {"volume_spike": False}),
            _pred("loss", {"volume_spike": False}),
            _pred("loss", {"volume_spike": False}),
            _pred("loss", {"volume_spike": False}),
        ],
    }
    result = compute_signal_efficacy(history)
    assert "volume_spike" in result
    assert result["volume_spike"]["with_signal"]["accuracy_pct"] == 100.0
    assert result["volume_spike"]["without_signal"]["accuracy_pct"] == 0.0
    assert result["volume_spike"]["lift_pct"] == 100.0


def test_signal_with_negative_lift() -> None:
    """A signal that fires only on losses → big negative lift → flag for removal."""
    history = {
        "predictions": [
            _pred("loss", {"rsi_oversold_recovery": True}),
            _pred("loss", {"rsi_oversold_recovery": True}),
            _pred("loss", {"rsi_oversold_recovery": True}),
            _pred("loss", {"rsi_oversold_recovery": True}),
            _pred("loss", {"rsi_oversold_recovery": True}),
            _pred("win", {"rsi_oversold_recovery": False}),
            _pred("win", {"rsi_oversold_recovery": False}),
            _pred("win", {"rsi_oversold_recovery": False}),
            _pred("win", {"rsi_oversold_recovery": False}),
            _pred("win", {"rsi_oversold_recovery": False}),
        ],
    }
    result = compute_signal_efficacy(history)
    assert result["rsi_oversold_recovery"]["lift_pct"] == -100.0


def test_signal_suppressed_below_min_samples() -> None:
    """Only 4 with-signal predictions → below min_samples=5 → suppressed."""
    history = {
        "predictions": [
            _pred("win", {"volume_spike": True}),
            _pred("win", {"volume_spike": True}),
            _pred("loss", {"volume_spike": True}),
            _pred("loss", {"volume_spike": True}),
            _pred("win", {"volume_spike": False}),
            _pred("win", {"volume_spike": False}),
            _pred("win", {"volume_spike": False}),
            _pred("win", {"volume_spike": False}),
            _pred("win", {"volume_spike": False}),
        ],
    }
    assert compute_signal_efficacy(history) == {}


def test_signal_suppressed_when_no_without_population() -> None:
    """Every prediction has the signal → no comparison baseline → suppressed."""
    history = {
        "predictions": [
            _pred("win", {"volume_spike": True}),
            _pred("win", {"volume_spike": True}),
            _pred("loss", {"volume_spike": True}),
            _pred("loss", {"volume_spike": True}),
            _pred("win", {"volume_spike": True}),
            _pred("win", {"volume_spike": True}),
        ],
    }
    assert compute_signal_efficacy(history) == {}


def test_format_signal_efficacy_renders_block() -> None:
    """The formatted block sorts signals by lift descending."""
    history = {
        "predictions": [
            # macd_crossover: 5 wins, 0 losses → +100 lift
            _pred("win", {"macd_crossover": True, "bollinger_lower": False}),
            _pred("win", {"macd_crossover": True, "bollinger_lower": False}),
            _pred("win", {"macd_crossover": True, "bollinger_lower": False}),
            _pred("win", {"macd_crossover": True, "bollinger_lower": False}),
            _pred("win", {"macd_crossover": True, "bollinger_lower": False}),
            # bollinger_lower: 0 wins, 5 losses → -100 lift
            _pred("loss", {"macd_crossover": False, "bollinger_lower": True}),
            _pred("loss", {"macd_crossover": False, "bollinger_lower": True}),
            _pred("loss", {"macd_crossover": False, "bollinger_lower": True}),
            _pred("loss", {"macd_crossover": False, "bollinger_lower": True}),
            _pred("loss", {"macd_crossover": False, "bollinger_lower": True}),
        ],
    }
    efficacy = compute_signal_efficacy(history)
    rendered = format_signal_efficacy(efficacy)
    assert "シグナル別 実勝率" in rendered
    assert "macd_crossover" in rendered
    assert "bollinger_lower" in rendered
    # macd_crossover with +100 lift should appear before bollinger_lower with -100
    assert rendered.index("macd_crossover") < rendered.index("bollinger_lower")


def test_format_returns_empty_for_no_efficacy() -> None:
    assert format_signal_efficacy({}) == ""
