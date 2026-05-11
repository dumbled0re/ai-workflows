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
    extract_few_shot_examples,
    format_few_shot_for_prompt,
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


# ---------- few-shot example extraction ------------------------------------


def _pred_with_signals(
    status: str,
    prediction: str,
    actual_return_pct: float,
    fired: list[str],
    name: str = "X Corp",
    ticker: str = "1234.T",
    date: str = "2026-04-01",
) -> dict:
    """A resolved prediction that carries a ``signal_components`` payload.

    Few-shot extraction reads ``signal_components`` to surface which
    signals fired at entry — tests need this richer record because the
    minimal ``_pred`` above predates the signal-efficacy tracking work.
    """
    p = _pred(status, prediction, actual_return_pct, date=date)
    p["name"] = name
    p["ticker"] = ticker
    p["days_held"] = 7
    p["signal_components"] = {name: True for name in fired}
    return p


def test_few_shot_includes_down_wins_via_directional_ranking() -> None:
    """A DOWN-win at raw -10% (directional +10%) outranks an UP-win at +6%.

    This is the bug fix that motivated the redesign: ranking on raw
    ``actual_return_pct`` descending would have ordered the +6% UP-win
    first and dropped the DOWN-win out of the top-1. Direction-aware
    ranking flips that.
    """
    history = {
        "predictions": [
            _pred_with_signals("win", "UP", 6.0, ["volume"], ticker="A.T", date="2026-03-01"),
            _pred_with_signals("win", "DOWN", -10.0, ["macd"], ticker="B.T", date="2026-03-02"),
        ],
    }
    examples = extract_few_shot_examples(history, n_wins=2, n_losses=0)
    assert [e["ticker"] for e in examples["wins"]] == ["B.T", "A.T"]
    assert examples["wins"][0]["directional_return_pct"] == 10.0
    assert examples["losses"] == []


def test_few_shot_losses_ranked_by_directional_descending_negative() -> None:
    """Worst loss = most negative directional return = first in losses list."""
    history = {
        "predictions": [
            _pred_with_signals("loss", "UP", -4.0, ["rsi"], ticker="A.T"),
            _pred_with_signals("loss", "UP", -15.0, ["rsi"], ticker="B.T"),
            _pred_with_signals("loss", "DOWN", 8.0, ["macd"], ticker="C.T"),
        ],
    }
    examples = extract_few_shot_examples(history, n_wins=0, n_losses=3)
    # B (-15 dir), C (-8 dir), A (-4 below threshold and filtered out)
    assert [e["ticker"] for e in examples["losses"]] == ["B.T", "C.T"]


def test_few_shot_filters_marginal_magnitude() -> None:
    """Wins/losses below ``min_directional_return`` are dropped — those
    typically resolve on review-window timeout and don't carry strong
    signal-fingerprint lessons."""
    history = {
        "predictions": [
            _pred_with_signals("win", "UP", 3.5, ["volume"], ticker="A.T"),  # weak
            _pred_with_signals("win", "UP", 6.0, ["macd"], ticker="B.T"),  # crosses threshold
        ],
    }
    examples = extract_few_shot_examples(history, n_wins=5, min_directional_return=5.0)
    assert [e["ticker"] for e in examples["wins"]] == ["B.T"]


def test_few_shot_carries_signal_fingerprint_as_sorted_list() -> None:
    """Each example surfaces the signals that fired at entry — that
    fingerprint is what enables the AI to pattern-match a current pick
    against the examples. False-value signals are excluded; the list
    is sorted for stable rendering."""
    p = _pred_with_signals("win", "UP", 10.0, ["volume_spike"], ticker="A.T")
    p["signal_components"] = {"volume_spike": True, "rsi_extreme": False, "trend_aligned": True}
    history = {"predictions": [p]}
    examples = extract_few_shot_examples(history)
    assert examples["wins"][0]["fired_signals"] == ["trend_aligned", "volume_spike"]


def test_few_shot_handles_empty_history() -> None:
    assert extract_few_shot_examples({"predictions": []}) == {"wins": [], "losses": []}
    assert extract_few_shot_examples({}) == {"wins": [], "losses": []}


def test_few_shot_handles_missing_signal_components() -> None:
    # Old predictions saved before signal-efficacy tracking landed
    # have no ``signal_components`` key. They should still surface as
    # examples but with an empty fingerprint, not crash.
    p = _pred("win", "UP", 10.0)
    history = {"predictions": [p]}
    examples = extract_few_shot_examples(history)
    assert examples["wins"][0]["fired_signals"] == []


def test_format_few_shot_returns_empty_when_no_examples() -> None:
    assert format_few_shot_for_prompt({"wins": [], "losses": []}) == ""


def test_format_few_shot_renders_wins_and_losses_with_signals() -> None:
    examples = {
        "wins": [
            {
                "ticker": "A.T",
                "name": "Alpha",
                "direction": "UP",
                "confidence": "HIGH",
                "date": "2026-03-01",
                "actual_return_pct": 12.5,
                "directional_return_pct": 12.5,
                "days_held": 7,
                "fired_signals": ["volume_spike", "trend_aligned"],
            },
        ],
        "losses": [
            {
                "ticker": "B.T",
                "name": "Beta",
                "direction": "DOWN",
                "confidence": "HIGH",
                "date": "2026-02-15",
                "actual_return_pct": 8.0,
                "directional_return_pct": -8.0,
                "days_held": 10,
                "fired_signals": ["rsi_oversold"],
            },
        ],
    }
    out = format_few_shot_for_prompt(examples)
    # Both example tickers + section labels + signal fingerprints land
    # in the rendered block. Guarding all three at once so a single
    # split-message regression is caught here.
    assert "成功パターン" in out
    assert "失敗パターン" in out
    assert "A.T" in out and "B.T" in out
    assert "volume_spike" in out and "trend_aligned" in out
    assert "rsi_oversold" in out
    # The pattern-matching directive is what makes few-shot more than
    # decoration — pin it so a future edit doesn't quietly strip it.
    assert "似ているか" in out


def test_format_performance_feedback_includes_few_shot_block() -> None:
    """Integration: the feedback prompt embeds the few-shot section
    instead of the old direction-blind '直近の失敗 / 成功パターン' code."""
    history = {
        "predictions": [
            _pred_with_signals("win", "DOWN", -10.0, ["macd"], ticker="A.T"),
            _pred_with_signals("win", "UP", 7.0, ["volume"], ticker="B.T"),
            _pred_with_signals("loss", "UP", -8.0, ["rsi"], ticker="C.T"),
        ],
    }
    history["performance_stats"] = compute_performance_stats(history)
    feedback = format_performance_feedback(history)
    assert "成功パターン" in feedback
    assert "A.T" in feedback  # DOWN-win, would have been dropped pre-fix
    assert "macd" in feedback
    assert "失敗パターン" in feedback
    assert "C.T" in feedback
