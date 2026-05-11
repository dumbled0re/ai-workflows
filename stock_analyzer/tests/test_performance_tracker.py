"""Tests for performance_tracker — direction-aware return computation
and the risk-adjusted P&L metrics (expectancy / profit factor / Sharpe /
max drawdown) that feed back into Claude's analysis prompt.

The original ``compute_performance_stats`` summed raw signed returns,
which produced ``avg_return_wins`` < 0 whenever the resolved-trade
population had a non-trivial DOWN-win count. That bled into the
feedback prompt as a false signal. These tests pin the directional
fix and the new metric calculations.
"""

from __future__ import annotations

from stock_analyzer.performance_tracker import (
    _directional_return,
    compute_performance_stats,
    format_performance_feedback,
)


def _pred(
    status: str,
    prediction: str,
    actual_return_pct: float | None,
    confidence: str = "MEDIUM",
    source: str = "holdings",
    date: str = "2026-04-01",
    reviewed_date: str = "2026-04-15",
) -> dict:
    return {
        "id": f"{date}_T_{status}_{prediction}_{actual_return_pct}",
        "date": date,
        "ticker": "0000.T",
        "name": "test",
        "prediction": prediction,
        "confidence": confidence,
        "entry_price": 100.0,
        "status": status,
        "actual_return_pct": actual_return_pct,
        "reviewed_date": reviewed_date,
        "source": source,
    }


def test_directional_return_flips_down_predictions() -> None:
    """A DOWN-win with -10% raw return is a +10% directional gain."""
    up_win = _pred("win", "UP", 5.0)
    down_win = _pred("win", "DOWN", -10.0)
    assert _directional_return(up_win) == 5.0
    assert _directional_return(down_win) == 10.0


def test_directional_return_none_for_missing_or_unknown() -> None:
    assert _directional_return(_pred("pending", "UP", None)) is None
    p = _pred("win", "SIDEWAYS", 1.0)  # unknown direction
    assert _directional_return(p) is None


def test_avg_return_wins_is_positive_even_when_population_skews_down() -> None:
    """The original bug: avg_return_wins came out negative when DOWN-wins
    (with negative raw return) dominated. After the fix wins must average
    positive — i.e. "predicting correctly" gets credit regardless of
    direction."""
    history = {
        "predictions": [
            _pred("win", "UP", 5.0),
            _pred("win", "DOWN", -8.0),  # correct DOWN call, raw -8 → +8 directional
            _pred("win", "DOWN", -6.0),
            _pred("loss", "UP", -4.0),  # wrong UP call, raw -4 → -4 directional
            _pred("loss", "DOWN", 3.0),  # wrong DOWN call, raw +3 → -3 directional
        ],
    }
    stats = compute_performance_stats(history)
    assert stats["wins"] == 3
    assert stats["losses"] == 2
    # 3 wins: +5, +8, +6 → avg 6.33
    assert stats["avg_return_wins"] == 6.33
    # 2 losses: -4, -3 → avg -3.5
    assert stats["avg_return_losses"] == -3.5


def test_expectancy_and_profit_factor() -> None:
    """Expectancy = win_rate * avg_win - loss_rate * |avg_loss|.
    Profit factor = sum(wins) / |sum(losses)|."""
    history = {
        "predictions": [
            _pred("win", "UP", 10.0),
            _pred("win", "UP", 8.0),
            _pred("loss", "UP", -5.0),
            _pred("loss", "UP", -3.0),
        ],
    }
    stats = compute_performance_stats(history)
    # wins=2 losses=2 win_rate=0.5
    # avg_win=9, avg_loss_abs=4
    # expectancy = 0.5*9 - 0.5*4 = 2.5
    assert stats["expectancy_per_trade_pct"] == 2.5
    # profit factor = (10+8) / |(-5)+(-3)| = 18/8 = 2.25
    assert stats["profit_factor"] == 2.25


def test_max_drawdown_chronological_peak_to_trough() -> None:
    """Three losses in a row after one gain → drawdown = sum of those losses."""
    history = {
        "predictions": [
            _pred("win", "UP", 5.0, reviewed_date="2026-04-01"),
            _pred("loss", "UP", -3.0, reviewed_date="2026-04-02"),
            _pred("loss", "UP", -4.0, reviewed_date="2026-04-03"),
            _pred("loss", "UP", -2.0, reviewed_date="2026-04-04"),
        ],
    }
    stats = compute_performance_stats(history)
    # Cumulative path: 0 → +5 → +2 → -2 → -4. Peak = 5, trough = -4.
    # Max drawdown = peak (5) - trough (-4) = 9
    assert stats["max_drawdown_pct"] == 9.0


def test_confidence_direction_cross_tab_requires_min_sample() -> None:
    """Below the minimum-N threshold the cross-tab bucket is suppressed."""
    # Only 4 HIGH-UP predictions — below _MIN_BUCKET_N (5)
    history = {
        "predictions": [
            _pred("win", "UP", 5.0, confidence="HIGH"),
            _pred("win", "UP", 5.0, confidence="HIGH"),
            _pred("loss", "UP", -5.0, confidence="HIGH"),
            _pred("loss", "UP", -5.0, confidence="HIGH"),
        ],
    }
    stats = compute_performance_stats(history)
    # by_confidence still emitted (it has lower bar)
    assert "by_confidence" in stats
    # by_confidence_direction should be suppressed for small N
    assert "by_confidence_direction" not in stats


def test_confidence_direction_cross_tab_above_threshold() -> None:
    """6 HIGH-UP predictions → bucket appears."""
    preds = [_pred("win", "UP", 5.0, confidence="HIGH")] * 6
    stats = compute_performance_stats({"predictions": preds})
    assert "by_confidence_direction" in stats
    assert "HIGH_UP" in stats["by_confidence_direction"]
    bucket = stats["by_confidence_direction"]["HIGH_UP"]
    assert bucket["total"] == 6
    assert bucket["accuracy_pct"] == 100.0


def test_calibration_inversion_warning_fires() -> None:
    """When HIGH < MEDIUM accuracy and both have N>=5, the format
    helper must emit the inversion warning so the AI sees it every run."""
    # HIGH: 2/6 = 33% (5 fails + 1 win, sample size 6 ≥ 5)
    # MEDIUM: 5/6 = 83%
    high = [_pred("loss", "UP", -5.0, confidence="HIGH")] * 5 + [_pred("win", "UP", 5.0, confidence="HIGH")]
    medium = [_pred("win", "UP", 5.0, confidence="MEDIUM")] * 5 + [_pred("loss", "UP", -5.0, confidence="MEDIUM")]
    history = {"predictions": high + medium}
    history["performance_stats"] = compute_performance_stats(history)
    feedback = format_performance_feedback(history)
    assert "キャリブレーション逆転" in feedback
    assert "HIGH" in feedback
    assert "MEDIUM" in feedback


def test_calibration_warning_silent_when_correctly_ordered() -> None:
    """HIGH > MEDIUM → no warning."""
    # HIGH 5/6 (= 83%), MEDIUM 3/6 (= 50%)
    high = [_pred("win", "UP", 5.0, confidence="HIGH")] * 5 + [_pred("loss", "UP", -5.0, confidence="HIGH")]
    medium = [_pred("win", "UP", 5.0, confidence="MEDIUM")] * 3 + [_pred("loss", "UP", -5.0, confidence="MEDIUM")] * 3
    history = {"predictions": high + medium}
    history["performance_stats"] = compute_performance_stats(history)
    feedback = format_performance_feedback(history)
    assert "キャリブレーション逆転" not in feedback


def test_format_warns_when_expectancy_non_positive() -> None:
    """Expectancy <= 0 → warning to the AI to widen risk-reward."""
    history = {
        "predictions": [
            _pred("win", "UP", 2.0),
            _pred("win", "UP", 2.0),
            _pred("loss", "UP", -5.0),
            _pred("loss", "UP", -5.0),
        ],
    }
    history["performance_stats"] = compute_performance_stats(history)
    feedback = format_performance_feedback(history)
    assert "期待値が 0 以下" in feedback


def test_empty_history_returns_empty_stats() -> None:
    assert compute_performance_stats({"predictions": []}) == {}
