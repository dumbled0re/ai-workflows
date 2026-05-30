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
    build_up_gate_directive,
    compute_performance_stats,
    compute_recent_up_hit_rate,
    extract_few_shot_examples,
    format_few_shot_for_prompt,
    format_performance_feedback,
    review_predictions,
    save_new_predictions,
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


# ---------- source-aware review window ------------------------------------


def _pending_pred(source: str, prediction: str, entry_price: float, date: str) -> dict:
    """Build a pending prediction shaped like real save_new_predictions output."""
    return {
        "id": f"{date}_X_{source}",
        "date": date,
        "ticker": "X.T",
        "name": "X Corp",
        "prediction": prediction,
        "confidence": "MEDIUM",
        "entry_price": entry_price,
        "source": source,
        "status": "pending",
        "actual_price": None,
        "actual_return_pct": None,
        "reviewed_date": None,
        "days_held": None,
    }


def test_short_term_pick_expires_at_14_days() -> None:
    """A swing pick (short_term / holdings) hitting the 14-day window
    without a 3% move resolves as marginal win/loss — preserves the
    pre-source-aware behaviour for the 1-4 week horizon."""
    p = _pending_pred("short_term", "UP", 1000.0, "2026-01-01")
    history = {"predictions": [p]}
    # 14 days later, price barely moved (+1%) — under the 3% threshold
    # but past the window, so it expires as marginal win.
    review_predictions(history, current_prices={"X.T": 1010.0}, today="2026-01-15")
    assert p["status"] == "win"
    assert p["days_held"] == 14


def test_long_term_pick_still_pending_at_14_days() -> None:
    """A long_term pick at 14 days with a small move must stay pending —
    the 3-12 month thesis hasn't had time to play out. Resolving here
    is the bug this source-aware window fixes."""
    p = _pending_pred("long_term", "UP", 1000.0, "2026-01-01")
    history = {"predictions": [p]}
    review_predictions(history, current_prices={"X.T": 1010.0}, today="2026-01-15")
    assert p["status"] == "pending"


def test_long_term_pick_resolves_at_90_days() -> None:
    """The new long_term window kicks in at 90 days — past that, a
    marginal move expires the prediction so it eventually leaves
    pending limbo and gets counted in metrics."""
    p = _pending_pred("long_term", "UP", 1000.0, "2026-01-01")
    history = {"predictions": [p]}
    # 95 days later, price up 1% — under threshold but past 90-day window.
    review_predictions(history, current_prices={"X.T": 1010.0}, today="2026-04-06")
    assert p["status"] == "win"


def test_long_term_strong_move_resolves_immediately() -> None:
    """Even on long_term, a clear +3% / -3% move triggers resolution
    without waiting for the window. The window is only for marginal
    cases."""
    p = _pending_pred("long_term", "UP", 1000.0, "2026-01-01")
    history = {"predictions": [p]}
    # 10 days later, +5% move — past _MIN_REVIEW_DAYS, above 3%.
    review_predictions(history, current_prices={"X.T": 1050.0}, today="2026-01-11")
    assert p["status"] == "win"


def test_unknown_source_falls_back_to_default_window() -> None:
    """A prediction with a source we haven't catalogued uses the 14-day
    default — defensive against future schema additions."""
    p = _pending_pred("some_new_category", "UP", 1000.0, "2026-01-01")
    history = {"predictions": [p]}
    review_predictions(history, current_prices={"X.T": 1010.0}, today="2026-01-15")
    assert p["status"] == "win"


# ---------- drawdown stop --------------------------------------------------


def test_current_drawdown_reported_separately_from_max() -> None:
    """A run of wins followed by deeper losses should leave
    current_drawdown_pct = max_drawdown_pct (equity at the trough)."""
    history = {
        "predictions": [
            _pred("win", "UP", 5.0, reviewed_date="2026-01-01"),
            _pred("win", "UP", 5.0, reviewed_date="2026-01-02"),
            _pred("loss", "UP", -8.0, reviewed_date="2026-01-03"),
            _pred("loss", "UP", -10.0, reviewed_date="2026-01-04"),
        ],
    }
    stats = compute_performance_stats(history)
    # Cumulative: +5, +10, +2, -8 → peak 10, current -8 → DD 18
    assert stats["current_drawdown_pct"] == 18.0
    assert stats["max_drawdown_pct"] == 18.0


def test_current_drawdown_zero_when_at_new_peak() -> None:
    """A series ending on a new high → current DD is 0 even if there
    was a historical drawdown along the way."""
    history = {
        "predictions": [
            _pred("win", "UP", 5.0, reviewed_date="2026-01-01"),
            _pred("loss", "UP", -3.0, reviewed_date="2026-01-02"),
            _pred("win", "UP", 10.0, reviewed_date="2026-01-03"),
        ],
    }
    stats = compute_performance_stats(history)
    # Cumulative: +5, +2, +12 → peak 12 = current → DD 0
    assert stats["current_drawdown_pct"] == 0.0
    # max DD was the 3-point dip from peak 5
    assert stats["max_drawdown_pct"] == 3.0


def test_drawdown_stop_directive_emitted_above_15pct() -> None:
    """The format block must include the hard 'no new HIGH' directive
    once current DD crosses 15% — the AI cannot miss this in scanning."""
    # Build a clear DD: +10, +10, -20, -10 → peak 20, current -10, DD 30
    history = {
        "predictions": [
            _pred("win", "UP", 10.0, reviewed_date="2026-01-01"),
            _pred("win", "UP", 10.0, reviewed_date="2026-01-02"),
            _pred("loss", "UP", -20.0, reviewed_date="2026-01-03"),
            _pred("loss", "UP", -10.0, reviewed_date="2026-01-04"),
        ],
    }
    history["performance_stats"] = compute_performance_stats(history)
    feedback = format_performance_feedback(history)
    assert "累計DD" in feedback
    assert "15% 閾値" in feedback
    assert "HIGH" in feedback


def test_drawdown_stop_silent_below_threshold() -> None:
    """Below 15% the directive must not appear — operator should see
    'normal expectancy text' only, no false alarm."""
    history = {
        "predictions": [
            _pred("win", "UP", 5.0, reviewed_date="2026-01-01"),
            _pred("loss", "UP", -3.0, reviewed_date="2026-01-02"),
        ],
    }
    history["performance_stats"] = compute_performance_stats(history)
    feedback = format_performance_feedback(history)
    assert "15% 閾値" not in feedback


# ---------- drift indicator ------------------------------------------------


def _resolved_with_dates(returns_with_dates: list[tuple[float, str]]) -> list[dict]:
    """Build a resolved-trade list where each entry has a directional
    return and a reviewed_date for chronological ordering."""
    preds = []
    for i, (ret, rev_date) in enumerate(returns_with_dates):
        prediction = "UP" if ret >= 0 else "DOWN"
        status = "win" if ret > 0 else "loss"
        raw = ret if prediction == "UP" else -ret
        p = _pred(status, prediction, raw, date="2026-01-01", reviewed_date=rev_date)
        p["ticker"] = f"T{i}.T"
        preds.append(p)
    return preds


def test_drift_fires_when_recent_expectancy_drops_versus_baseline() -> None:
    """Baseline averaging ~+3% with small variance, recent 14 averaging
    ~-3% — clear, statistically significant decay should set
    is_drift=True via Welch's t-test (p < 0.10)."""
    # Add small noise (±0.5) around the means so the t-test has
    # non-zero variance to work with. Identical values give zero
    # variance and the test (correctly) returns no p-value.
    baseline_pairs = [(3.0 + (i % 3 - 1) * 0.5, f"2026-01-{i:02d}") for i in range(1, 21)]
    recent_pairs = [(-3.0 + (i % 3 - 1) * 0.5, f"2026-02-{i:02d}") for i in range(1, 15)]
    history = {"predictions": _resolved_with_dates(baseline_pairs + recent_pairs)}
    stats = compute_performance_stats(history)
    drift = stats["drift_indicator"]
    assert drift["recent_n"] == 14
    assert drift["baseline_n"] == 20
    assert drift["delta_pp"] < -5.0
    assert drift["is_drift"] is True
    # p-value should be tiny on a 6pp delta with ~0.5 stdev
    assert drift["p_value"] is not None
    assert drift["p_value"] < 0.01


def test_drift_quiet_when_recent_matches_baseline() -> None:
    """Identical means but small natural variance → not statistically
    significant → is_drift=False even though the indicator reports
    the (near-zero) delta so the operator can see stability."""
    baseline_pairs = [(2.0 + (i % 3 - 1) * 0.5, f"2026-01-{i:02d}") for i in range(1, 21)]
    recent_pairs = [(2.0 + (i % 3 - 1) * 0.5, f"2026-02-{i:02d}") for i in range(1, 15)]
    history = {"predictions": _resolved_with_dates(baseline_pairs + recent_pairs)}
    stats = compute_performance_stats(history)
    drift = stats["drift_indicator"]
    assert drift["is_drift"] is False
    # The means are equal so delta ~0; p-value should be near 0.5
    assert abs(drift["delta_pp"]) < 0.3


def test_drift_quiet_on_small_delta_with_high_variance() -> None:
    """Big delta but huge variance → not significant. This is the
    key reason for t-test over fixed 2pp threshold: scale-aware
    decision-making."""
    # Baseline: mean ~+1%, ±10% noise → very high variance
    baseline_pairs = [(1.0 + (i % 4 - 2) * 10.0, f"2026-01-{i:02d}") for i in range(1, 21)]
    # Recent: mean ~-1%, same noise structure
    recent_pairs = [(-1.0 + (i % 4 - 2) * 10.0, f"2026-02-{i:02d}") for i in range(1, 15)]
    history = {"predictions": _resolved_with_dates(baseline_pairs + recent_pairs)}
    stats = compute_performance_stats(history)
    drift = stats["drift_indicator"]
    # 2pp delta on ~10% stdev is noise — t-test correctly stays silent.
    assert drift["is_drift"] is False
    assert drift["p_value"] is not None and drift["p_value"] > 0.10


def test_drift_indicator_absent_with_insufficient_samples() -> None:
    """Below the min-recent / min-baseline thresholds the indicator is
    suppressed entirely — a noisy 2-vs-3 comparison would be worse
    than no signal."""
    history = {"predictions": _resolved_with_dates([(3.0, "2026-01-01"), (-2.0, "2026-02-01")])}
    stats = compute_performance_stats(history)
    assert "drift_indicator" not in stats


def test_drift_indicator_skips_entries_without_reviewed_date() -> None:
    """Entries missing reviewed_date can't be chrono-ordered — they
    must be silently excluded, not crash. Common for the most-recently
    captured predictions still in pending status that somehow got
    flagged win/loss elsewhere."""
    rows = _resolved_with_dates([(3.0, f"2026-01-{i:02d}") for i in range(1, 21)])
    rows += _resolved_with_dates([(-2.0, f"2026-02-{i:02d}") for i in range(1, 15)])
    # Inject a no-reviewed_date stray that would otherwise corrupt ordering.
    stray = _pred("win", "UP", 50.0, date="2026-01-01", reviewed_date="")
    rows.insert(5, stray)
    history = {"predictions": rows}
    stats = compute_performance_stats(history)
    drift = stats["drift_indicator"]
    # Stray excluded, so baseline still has 20 entries (not 21).
    assert drift["baseline_n"] == 20


def test_format_feedback_emits_drift_warning_block() -> None:
    """When drift is statistically significant, the feedback block
    must include the ⚠ line + the t-test p-value so the AI sees the
    rigour of the warning, not just the heuristic threshold."""
    baseline_pairs = [(3.0 + (i % 3 - 1) * 0.5, f"2026-01-{i:02d}") for i in range(1, 21)]
    recent_pairs = [(-3.0 + (i % 3 - 1) * 0.5, f"2026-02-{i:02d}") for i in range(1, 15)]
    history = {"predictions": _resolved_with_dates(baseline_pairs + recent_pairs)}
    history["performance_stats"] = compute_performance_stats(history)
    feedback = format_performance_feedback(history)
    assert "戦略ドリフト" in feedback
    assert "統計的に有意" in feedback
    # The p-value must appear in the rendered text so the operator
    # can see the strength of evidence at a glance.
    assert "t-test p=" in feedback
    # Means should still appear in the line
    assert "+3.00%/件" in feedback or "3.0" in feedback


# ---------- calibration zone (issue #46 Phase 1) ---------------------------


def _bucket_pred(conf: str, status: str, ret: float, idx: int) -> dict:
    """Build a resolved prediction for a specific confidence bucket."""
    prediction = "UP" if ret >= 0 else "DOWN"
    raw = ret if prediction == "UP" else -ret
    p = _pred(
        status,
        prediction,
        raw,
        confidence=conf,
        date="2026-01-01",
        reviewed_date=f"2026-04-{(idx % 28) + 1:02d}",
    )
    p["ticker"] = f"T{idx}.T"
    return p


def test_calibration_zone_red_when_high_accuracy_below_medium_ratio() -> None:
    """HIGH 51% / MEDIUM 67% → ratio 0.76 < 0.9 → red zone."""
    preds: list[dict] = []
    # HIGH: 30 件、win 15 (50%)
    for i in range(30):
        preds.append(_bucket_pred("HIGH", "win" if i < 15 else "loss", 5.0 if i < 15 else -5.0, i))
    # MEDIUM: 30 件、win 20 (66.7%)
    for i in range(30):
        preds.append(_bucket_pred("MEDIUM", "win" if i < 20 else "loss", 5.0 if i < 20 else -5.0, i + 100))
    stats = compute_performance_stats({"predictions": preds})
    zone = stats.get("calibration_zone")
    assert zone is not None
    assert zone["zone"] == "red"
    assert any("calibration 逆転" in r for r in zone["reasons"])


def test_calibration_zone_green_when_high_outperforms_medium() -> None:
    """HIGH 70% / MEDIUM 60% → ratio > 0.9 → green zone (normal)."""
    preds: list[dict] = []
    for i in range(30):
        preds.append(_bucket_pred("HIGH", "win" if i < 21 else "loss", 5.0 if i < 21 else -5.0, i))
    for i in range(30):
        preds.append(_bucket_pred("MEDIUM", "win" if i < 18 else "loss", 5.0 if i < 18 else -5.0, i + 100))
    stats = compute_performance_stats({"predictions": preds})
    zone = stats.get("calibration_zone")
    assert zone is not None
    assert zone["zone"] == "green"


def test_calibration_zone_yellow_when_high_sample_insufficient() -> None:
    """HIGH n=8 < min(15) → yellow downgrade (uncertainty 高い)。"""
    preds: list[dict] = []
    # HIGH: 8 件のみ (min 15 未満)
    for i in range(8):
        preds.append(_bucket_pred("HIGH", "win" if i < 4 else "loss", 5.0 if i < 4 else -5.0, i))
    # MEDIUM: 25 件、healthy 65%
    for i in range(25):
        preds.append(_bucket_pred("MEDIUM", "win" if i < 16 else "loss", 5.0 if i < 16 else -5.0, i + 100))
    stats = compute_performance_stats({"predictions": preds})
    zone = stats.get("calibration_zone")
    assert zone is not None
    assert zone["zone"] in {"yellow", "red"}  # サンプル不足は最低 yellow
    # high_n が低いことが理由として記録されてる
    assert any("HIGH サンプル不足" in r or "calibration" in r for r in zone["reasons"])


def test_brier_score_computed_per_confidence_bucket() -> None:
    """Brier = avg((predicted_prob - outcome)^2)。
    HIGH (predicted 0.75): wins → low Brier、losses → high Brier。
    """
    preds: list[dict] = []
    # HIGH: 15 wins (Brier=(0.75-1)^2=0.0625), 5 losses (0.5625) → avg=(15*0.0625+5*0.5625)/20=0.1875
    for i in range(15):
        preds.append(_bucket_pred("HIGH", "win", 5.0, i))
    for i in range(5):
        preds.append(_bucket_pred("HIGH", "loss", -5.0, i + 100))
    # MEDIUM: 20 wins (Brier=(0.65-1)^2=0.1225) → avg=0.1225
    for i in range(20):
        preds.append(_bucket_pred("MEDIUM", "win", 5.0, i + 200))
    stats = compute_performance_stats({"predictions": preds})
    high = stats["by_confidence"]["HIGH"]
    medium = stats["by_confidence"]["MEDIUM"]
    assert high["predicted_prob"] == 0.75
    assert medium["predicted_prob"] == 0.65
    # HIGH 75% accuracy (15/20) closer to predicted 75% → moderate Brier
    assert abs(high["brier_score"] - 0.1875) < 0.01
    # MEDIUM 100% accuracy >>> predicted 65% → low Brier (severe underconfidence)
    assert abs(medium["brier_score"] - 0.1225) < 0.01


def test_reliability_diagram_records_gap_per_bucket() -> None:
    """reliability_diagram は predicted vs observed の gap を bucket 別に出力。"""
    preds: list[dict] = []
    # HIGH overconfident: predicted 75% / observed 50%
    for i in range(15):
        preds.append(_bucket_pred("HIGH", "win", 5.0, i))
    for i in range(15):
        preds.append(_bucket_pred("HIGH", "loss", -5.0, i + 100))
    # MEDIUM well-calibrated: predicted 65% / observed ~65%
    for i in range(13):
        preds.append(_bucket_pred("MEDIUM", "win", 5.0, i + 200))
    for i in range(7):
        preds.append(_bucket_pred("MEDIUM", "loss", -5.0, i + 300))
    stats = compute_performance_stats({"predictions": preds})
    rel = stats["reliability_diagram"]
    high_bin = next(b for b in rel if b["confidence"] == "HIGH")
    medium_bin = next(b for b in rel if b["confidence"] == "MEDIUM")
    # HIGH: 0.50 - 0.75 = -25pp (severely overconfident)
    assert high_bin["gap_pp"] == -25.0
    # MEDIUM: 0.65 - 0.65 = 0pp (ish, depending on rounding)
    assert abs(medium_bin["gap_pp"]) < 1.0


def test_calibration_zone_red_on_brier_inversion() -> None:
    """HIGH Brier > MEDIUM Brier だけでも Red (accuracy ratio が境界でも独立 fire)。"""
    preds: list[dict] = []
    # HIGH: 15 wins, 12 losses → 55.6% accuracy → ratio vs MEDIUM 61.5% = 0.90 ぎりぎり
    # でも Brier は HIGH=(0.75-outcomes mean)^2 で MEDIUM より悪い
    for i in range(15):
        preds.append(_bucket_pred("HIGH", "win", 5.0, i))
    for i in range(12):
        preds.append(_bucket_pred("HIGH", "loss", -5.0, i + 100))
    # MEDIUM: 16 wins, 10 losses → 61.5%
    for i in range(16):
        preds.append(_bucket_pred("MEDIUM", "win", 5.0, i + 200))
    for i in range(10):
        preds.append(_bucket_pred("MEDIUM", "loss", -5.0, i + 300))
    stats = compute_performance_stats({"predictions": preds})
    zone = stats["calibration_zone"]
    # accuracy ratio 55.6/61.5 = 0.903 → ratio 単独では Red 未満 (boundary)
    # でも Brier 0.305 vs 0.255 で HIGH > MEDIUM → Red
    assert zone["zone"] == "red"
    assert any("Brier 逆転" in r for r in zone["reasons"])


def test_net_expectancy_subtracts_round_trip_cost() -> None:
    """gross 1.0%/trade、cost 0.2% (片道) → 往復 0.4% → net 0.6%。"""
    from stock_analyzer.performance_tracker import compute_net_expectancy

    history = {
        "predictions": [
            _pred("win", "UP", 6.0, reviewed_date="2026-01-01"),
            _pred("win", "UP", 4.0, reviewed_date="2026-01-02"),
            _pred("loss", "UP", -8.0, reviewed_date="2026-01-03"),
        ],
    }
    net = compute_net_expectancy(history, transaction_cost_pct=0.2)
    assert net is not None
    # gross = (6+4-8)/3 = 0.67
    assert abs(net["gross_expectancy_pct"] - 0.67) < 0.01
    # net = gross - 0.4 = 0.27
    assert abs(net["net_expectancy_pct"] - 0.27) < 0.01


def test_signal_correlation_pairs_detects_co_firing() -> None:
    """volume_spike と volume_surge は階層化で常に co-fire → 強相関 r≈1.0。"""
    from stock_analyzer.performance_tracker import compute_signal_correlation_pairs

    # Build 20 predictions where 10 fire both vol_a and vol_b, 5 only vol_a, 5 neither
    rows = []
    for i in range(10):
        p = _pred("win", "UP", 5.0, reviewed_date=f"2026-04-{i + 1:02d}")
        p["ticker"] = f"T{i}.T"
        p["signal_components"] = {"vol_a": True, "vol_b": True}
        rows.append(p)
    for i in range(5):
        p = _pred("loss", "UP", -5.0, reviewed_date=f"2026-04-{i + 15:02d}")
        p["ticker"] = f"T{i + 10}.T"
        p["signal_components"] = {"vol_a": True}
        rows.append(p)
    for i in range(5):
        p = _pred("loss", "UP", -5.0, reviewed_date=f"2026-04-{i + 20:02d}")
        p["ticker"] = f"T{i + 15}.T"
        p["signal_components"] = {"other": True}
        rows.append(p)
    pairs = compute_signal_correlation_pairs({"predictions": rows})
    # vol_a/vol_b の組は強正相関であるべき
    vol_pair = next((p for p in pairs if set(p["pair"]) == {"vol_a", "vol_b"}), None)
    assert vol_pair is not None
    assert vol_pair["correlation"] > 0.5


def test_walkforward_cv_returns_none_when_data_insufficient() -> None:
    """少量データ (< n_folds * min_test_n * 2) は None で suppression。"""
    from stock_analyzer.performance_tracker import evaluate_weights_walkforward_cv

    history = {"predictions": []}
    result = evaluate_weights_walkforward_cv(history, weights={"a": 10})
    assert result is None


def test_walkforward_cv_returns_fold_results_with_enough_data() -> None:
    """十分なデータで複数 fold の accuracy を返す。signal を持つ予測必須。"""
    from stock_analyzer.performance_tracker import evaluate_weights_walkforward_cv

    # 100 件、signal "good" が true なら win 偏重、false ならランダム
    rows = []
    for i in range(100):
        good_sig = i % 3 == 0
        is_win = good_sig if i % 7 != 0 else not good_sig  # noise
        ret = 5.0 if is_win else -5.0
        status = "win" if is_win else "loss"
        date_str = f"2026-{(i // 30) + 1:02d}-{(i % 28) + 1:02d}"
        p = _pred(status, "UP", ret, date=date_str, reviewed_date=date_str)
        p["ticker"] = f"T{i}.T"
        p["signal_components"] = {"good": good_sig, "other": True}
        rows.append(p)
    result = evaluate_weights_walkforward_cv({"predictions": rows}, weights={"good": 20, "other": 5}, n_folds=4)
    assert result is not None
    assert result["n_folds_used"] >= 2
    assert "mean_top_quantile_acc_pct" in result


def test_format_feedback_emits_red_zone_block() -> None:
    """Red zone は feedback に🟥 ブロック + 「HIGH 出力は禁止」 directive。"""
    preds: list[dict] = []
    for i in range(30):
        preds.append(_bucket_pred("HIGH", "win" if i < 15 else "loss", 5.0 if i < 15 else -5.0, i))
    for i in range(30):
        preds.append(_bucket_pred("MEDIUM", "win" if i < 20 else "loss", 5.0 if i < 20 else -5.0, i + 100))
    history = {"predictions": preds}
    history["performance_stats"] = compute_performance_stats(history)
    feedback = format_performance_feedback(history)
    assert "🟥" in feedback
    assert "Red zone" in feedback
    assert "HIGH 出力は禁止" in feedback


# ---------- NO_TRADE / UP gate ---------------------------------------------


def test_save_new_predictions_skips_no_trade_holdings() -> None:
    """holdings entries with prediction='NO_TRADE' (and the legacy
    NEUTRAL spelling) must not enter the history — there is no
    directional bet to verify and including them dilutes calibration."""
    history: dict = {"predictions": []}
    holdings_result = {
        "holdings_analysis": [
            {"ticker": "A.T", "name": "A", "prediction": "NO_TRADE", "confidence": "MEDIUM"},
            {"ticker": "B.T", "name": "B", "prediction": "NEUTRAL", "confidence": "LOW"},
            {"ticker": "C.T", "name": "C", "prediction": "UP", "confidence": "HIGH"},
        ]
    }
    save_new_predictions(
        history,
        holdings_result,
        {"short_term_picks": [], "long_term_picks": []},
        current_prices={"A.T": 100.0, "B.T": 100.0, "C.T": 100.0},
        today="2026-05-15",
    )
    tickers = [p["ticker"] for p in history["predictions"]]
    # Only the UP holding is tracked.
    assert tickers == ["C.T"]


def test_save_new_predictions_skips_no_trade_discovery() -> None:
    """short_term_picks / long_term_picks NO_TRADE entries also skipped."""
    history: dict = {"predictions": []}
    discovery = {
        "short_term_picks": [
            {"ticker": "A.T", "name": "A", "prediction": "NO_TRADE", "confidence": "MEDIUM"},
            {"ticker": "B.T", "name": "B", "prediction": "UP", "confidence": "HIGH"},
        ],
        "long_term_picks": [
            {"ticker": "C.T", "name": "C", "prediction": "NO_TRADE", "confidence": "LOW"},
        ],
    }
    save_new_predictions(
        history,
        {"holdings_analysis": []},
        discovery,
        current_prices={"A.T": 100.0, "B.T": 100.0, "C.T": 100.0},
        today="2026-05-15",
    )
    tickers = [(p["ticker"], p["source"]) for p in history["predictions"]]
    assert tickers == [("B.T", "short_term")]


def _up_pred(status: str, reviewed_date: str, raw: float = 5.0) -> dict:
    """Build a resolved UP prediction with a reviewed_date for ordering."""
    p = _pred(status, "UP", raw, date="2026-01-01", reviewed_date=reviewed_date)
    return p


def test_compute_recent_up_hit_rate_none_below_min_samples() -> None:
    """Below the configured min sample (12) the function returns None
    so a tiny coin-flip sample doesn't gate anything."""
    history = {"predictions": [_up_pred("win", f"2026-02-{i:02d}") for i in range(1, 8)]}
    assert compute_recent_up_hit_rate(history) is None


def test_compute_recent_up_hit_rate_returns_stat_above_threshold() -> None:
    """20 UP predictions, 14 wins → 70% hit rate, above threshold so
    below_threshold=False. recent_n reflects the actual sample used."""
    preds = [_up_pred("win", f"2026-02-{i:02d}") for i in range(1, 15)]
    preds += [_up_pred("loss", f"2026-02-{i:02d}", -5.0) for i in range(15, 21)]
    history = {"predictions": preds}
    stat = compute_recent_up_hit_rate(history)
    assert stat is not None
    assert stat["recent_n"] == 20
    assert stat["wins"] == 14
    assert stat["hit_rate_pct"] == 70.0
    assert stat["below_threshold"] is False


def test_compute_recent_up_hit_rate_flags_below_threshold() -> None:
    """A 40% (8/20) UP hit rate sits below the 50% threshold and the
    flag must be true so the gate downstream knows to fire."""
    preds = [_up_pred("win", f"2026-02-{i:02d}") for i in range(1, 9)]
    preds += [_up_pred("loss", f"2026-02-{i:02d}", -5.0) for i in range(9, 21)]
    history = {"predictions": preds}
    stat = compute_recent_up_hit_rate(history)
    assert stat is not None
    assert stat["hit_rate_pct"] == 40.0
    assert stat["below_threshold"] is True


def test_compute_recent_up_hit_rate_takes_most_recent_only() -> None:
    """When the sample exceeds recent_n, the function trims to the
    *most recent* UP predictions (sorted by reviewed_date desc) — old
    wins shouldn't mask a recent losing streak."""
    # 12 old wins, 12 recent losses → if we took everything we'd see
    # 50%, but the trimmed-to-recent_n=20 view should show much worse.
    old_wins = [_up_pred("win", f"2026-01-{i:02d}") for i in range(1, 13)]
    recent_losses = [_up_pred("loss", f"2026-03-{i:02d}", -5.0) for i in range(1, 13)]
    history = {"predictions": old_wins + recent_losses}
    stat = compute_recent_up_hit_rate(history, recent_n=20)
    # Most-recent 20 = 12 losses + 8 old wins → 8/20 = 40%
    assert stat is not None
    assert stat["recent_n"] == 20
    assert stat["wins"] == 8


def test_build_up_gate_directive_silent_when_above_threshold() -> None:
    """Gate text is empty when hit rate is above threshold so phase_prepare
    can unconditionally concatenate without polluting the prompt."""
    stats = {
        "recent_up_hit_rate": {
            "recent_n": 20,
            "wins": 14,
            "hit_rate_pct": 70.0,
            "threshold_pct": 50.0,
            "below_threshold": False,
        }
    }
    assert build_up_gate_directive(stats) == ""


def test_build_up_gate_directive_emits_block_when_below_threshold() -> None:
    """Below-threshold stat → directive includes the hit-rate number,
    the ban on short_term UP, and the NO_TRADE option for holdings."""
    stats = {
        "recent_up_hit_rate": {
            "recent_n": 20,
            "wins": 8,
            "hit_rate_pct": 40.0,
            "threshold_pct": 50.0,
            "below_threshold": True,
        }
    }
    text = build_up_gate_directive(stats)
    assert text != ""
    # Headline metric is visible
    assert "40.0%" in text
    assert "20 件" in text
    # The gate's two core directives are present (pin to catch refactors
    # that silently drop one rule).
    assert "short_term_picks の UP 推奨は原則禁止" in text
    assert "NO_TRADE" in text
    # long_term remains exempt
    assert "long_term_picks" in text


def test_build_up_gate_directive_silent_when_no_stat() -> None:
    """Missing stat (e.g. brand-new history) → no directive. The gate
    must not fire on insufficient data."""
    assert build_up_gate_directive({}) == ""
    assert build_up_gate_directive({"recent_up_hit_rate": None}) == ""


def test_format_performance_feedback_includes_recent_up_hit_rate() -> None:
    """Once enough UP samples exist, format_performance_feedback emits
    the recent UP rate line regardless of gate state — the AI sees the
    trajectory even when above threshold so it can self-monitor."""
    preds = [_up_pred("win", f"2026-02-{i:02d}") for i in range(1, 15)]
    preds += [_up_pred("loss", f"2026-02-{i:02d}", -5.0) for i in range(15, 21)]
    history = {"predictions": preds}
    history["performance_stats"] = compute_performance_stats(history)
    feedback = format_performance_feedback(history)
    assert "直近 UP 予測勝率" in feedback
    assert "70.0%" in feedback
    # The ⚠ marker is reserved for below-threshold; this rate is OK
    # so the marker must not appear on the UP-rate line.
    up_line = next(line for line in feedback.splitlines() if "直近 UP 予測勝率" in line)
    assert "⚠" not in up_line


def test_format_performance_feedback_marks_below_threshold_up_rate() -> None:
    """Below-threshold UP rate gets the ⚠ marker on the rate line so
    the AI can see at a glance which metrics need correction."""
    preds = [_up_pred("loss", f"2026-02-{i:02d}", -5.0) for i in range(1, 16)]
    preds += [_up_pred("win", f"2026-02-{i:02d}") for i in range(16, 21)]
    history = {"predictions": preds}
    history["performance_stats"] = compute_performance_stats(history)
    feedback = format_performance_feedback(history)
    up_line = next(line for line in feedback.splitlines() if "直近 UP 予測勝率" in line)
    assert "⚠" in up_line


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
