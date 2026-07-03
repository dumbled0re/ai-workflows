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
    build_recent_failure_block,
    build_up_gate_directive,
    compute_critic_efficacy,
    compute_performance_stats,
    compute_recent_up_hit_rate,
    extract_few_shot_examples,
    format_critic_efficacy,
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


def test_drawdown_stop_directive_emitted_above_15pp() -> None:
    """The format block must include the hard 'no new HIGH' directive
    once the recent-window DD crosses 15pp — the AI cannot miss this
    in scanning."""
    # Build a clear DD: +10, +10, -20, -10 → peak 20, current -10, DD 30
    # (4 trades — all inside the recent window)
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
    assert "drawdown" in feedback
    assert "15pp 閾値" in feedback
    assert "HIGH" in feedback


def test_drawdown_stop_silent_below_threshold() -> None:
    """Below 15pp the directive must not appear — operator should see
    'normal expectancy text' only, no false alarm."""
    history = {
        "predictions": [
            _pred("win", "UP", 5.0, reviewed_date="2026-01-01"),
            _pred("loss", "UP", -3.0, reviewed_date="2026-01-02"),
        ],
    }
    history["performance_stats"] = compute_performance_stats(history)
    feedback = format_performance_feedback(history)
    assert "15pp 閾値" not in feedback


def test_drawdown_preserves_file_order_within_same_day_ties() -> None:
    """Records reviewed the same day must keep their file order in the
    equity walk. The old ``wins + losses`` concatenation sorted every
    same-day tie as wins-first / losses-last, which fabricated a
    peak-then-crash pattern and inflated DD (audit 2026-07-04: 217pp
    reported vs 97pp actual)."""
    # File order alternates loss/win, all reviewed the same day.
    preds = [
        _pred("loss", "UP", -5.0, reviewed_date="2026-01-10"),
        _pred("win", "UP", 5.0, reviewed_date="2026-01-10"),
        _pred("loss", "UP", -5.0, reviewed_date="2026-01-10"),
        _pred("win", "UP", 5.0, reviewed_date="2026-01-10"),
    ]
    stats = compute_performance_stats({"predictions": preds})
    # Alternating walk: -5, 0, -5, 0 → max DD 5, ends back at the peak
    # (current DD 0). The wins-first ordering would walk +5, +10, +5, 0
    # → max DD 10 / current DD 10.
    assert stats["max_drawdown_pct"] == 5.0
    assert stats["current_drawdown_pct"] == 0.0


def test_drawdown_ban_recovers_after_losses_age_out_of_window() -> None:
    """An old crash must NOT ban HIGH forever. The all-time cumulative DD
    (current_drawdown_pct) stays huge after a deep loss, but once the
    last 30 trades are healthy the recent-window DD is ~0 and the
    directive must disappear. This pins the 2026-07-04 deadlock fix:
    with the old all-time rule, HIGH was permanently banned (DD 199.7pp)
    and the probation re-test could never start."""
    preds = [
        _pred("win", "UP", 10.0, reviewed_date="2026-01-01"),
        _pred("win", "UP", 10.0, reviewed_date="2026-01-02"),
        _pred("loss", "UP", -60.0, reviewed_date="2026-01-03"),  # crash
    ]
    # 30 modest wins afterwards — fill the entire recent window.
    for i in range(30):
        preds.append(_pred("win", "UP", 0.5, reviewed_date=f"2026-02-{i + 1:02d}"))
    history = {"predictions": preds}
    history["performance_stats"] = compute_performance_stats(history)
    stats = history["performance_stats"]
    # All-time DD is still way above 15pp (peak +20 → 20-(-40+15)=45pp)…
    assert stats["current_drawdown_pct"] >= 15.0
    # …but the recent window is a clean climb → no ban.
    assert stats["recent_drawdown_pct"] < 15.0
    feedback = format_performance_feedback(history)
    assert "15pp 閾値超過" not in feedback


# ---------- split adjustment ------------------------------------------------


def test_review_split_adjusts_entry_price_on_large_move() -> None:
    """A 4:1 split makes a flat stock look like -75%. With a
    split_factor_fn reporting the split, the entry is rescaled and the
    outcome is judged on the true economic move."""
    p = _pending_pred("short_term", "UP", 6000.0, "2026-01-01")
    history = {"predictions": [p]}
    review_predictions(
        history,
        current_prices={"X.T": 1550.0},
        today="2026-01-10",
        split_factor_fn=lambda ticker, since: 4.0,
    )
    # Adjusted entry 6000/4 = 1500 → +3.33% → clean UP win.
    assert p["status"] == "win"
    assert p["split_factor"] == 4.0
    assert p["entry_price_split_adjusted"] == 1500.0
    assert abs(p["actual_return_pct"] - 3.33) < 0.01


def test_review_split_lookup_skipped_for_small_moves() -> None:
    """The split lookup is an external call — it must not fire for
    ordinary moves below the 20% threshold."""

    def _explode(ticker: str, since: str) -> float:
        raise AssertionError("split_factor_fn must not be called for small moves")

    p = _pending_pred("short_term", "UP", 1000.0, "2026-01-01")
    history = {"predictions": [p]}
    review_predictions(history, current_prices={"X.T": 1050.0}, today="2026-01-10", split_factor_fn=_explode)
    assert p["status"] == "win"
    assert "split_factor" not in p


def test_review_split_fn_failure_falls_back_to_raw_prices() -> None:
    """A lookup failure must never block the review — fall back to the
    raw comparison (pre-fix behaviour) instead of crashing the cron."""

    def _broken(ticker: str, since: str) -> float:
        raise RuntimeError("yahoo down")

    p = _pending_pred("short_term", "DOWN", 1000.0, "2026-01-01")
    history = {"predictions": [p]}
    review_predictions(history, current_prices={"X.T": 700.0}, today="2026-01-10", split_factor_fn=_broken)
    assert p["status"] == "win"  # raw -30% → DOWN win
    assert p["actual_return_pct"] == -30.0
    assert "split_factor" not in p


# ---------- stale-pending expiry --------------------------------------------


def test_stale_pending_expires_when_price_unavailable() -> None:
    """A pending prediction whose ticker no longer gets a price must be
    force-closed after window + grace — otherwise it sits pending
    forever and silently drops out of every metric (survivorship
    bias)."""
    p = _pending_pred("short_term", "UP", 1000.0, "2026-01-01")
    history = {"predictions": [p]}
    # 50 days > 14 (window) + 30 (grace), no price for X.T
    review_predictions(history, current_prices={}, today="2026-02-20")
    assert p["status"] == "expired"
    assert p["expire_reason"] == "price_unavailable"
    assert p["actual_return_pct"] is None


def test_stale_pending_waits_out_the_grace_period() -> None:
    """Within window + grace the prediction stays pending — the ticker
    may just be temporarily missing from the fetch."""
    p = _pending_pred("short_term", "UP", 1000.0, "2026-01-01")
    history = {"predictions": [p]}
    review_predictions(history, current_prices={}, today="2026-02-01")  # 31 days < 44
    assert p["status"] == "pending"


def test_expired_predictions_excluded_from_accuracy() -> None:
    """Expired records count in stats['expired'] but never as win/loss/
    pending — they must not move accuracy in either direction."""
    expired = _pred("expired", "UP", None, reviewed_date="2026-02-20")
    history = {
        "predictions": [
            _pred("win", "UP", 5.0),
            _pred("loss", "UP", -5.0),
            expired,
        ],
    }
    stats = compute_performance_stats(history)
    assert stats["expired"] == 1
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["pending"] == 0
    assert stats["accuracy_pct"] == 50.0


# ---------- episode-level dedup ---------------------------------------------


def test_episode_stats_collapse_daily_repredictions() -> None:
    """holdings re-predicted daily resolve to the same outcome many
    times. Episode stats must count that position once."""
    dup1 = _pred("win", "DOWN", -30.0, date="2026-06-22")
    dup2 = _pred("win", "DOWN", -30.0, date="2026-06-23")
    dup3 = _pred("win", "DOWN", -30.0, date="2026-06-24")
    for d in (dup1, dup2, dup3):
        d["ticker"] = "3133.T"
        d["actual_price"] = 71.0
    other = _pred("loss", "UP", -5.0, date="2026-06-22")
    other["ticker"] = "9999.T"
    other["actual_price"] = 95.0
    history = {"predictions": [dup1, dup2, dup3, other]}
    stats = compute_performance_stats(history)
    ep = stats["episode_stats"]
    assert ep["n_raw"] == 4
    assert ep["n_episodes"] == 2  # 3133.T collapsed to one episode
    assert ep["accuracy_pct"] == 50.0  # 1 win episode / 2 episodes
    # feedback surfaces the inflation warning (75% nominal vs 50% episode)
    history["performance_stats"] = stats
    feedback = format_performance_feedback(history)
    assert "重複補正後" in feedback
    assert "名目勝率が重複計上で上振れ" in feedback


def test_episode_stats_keep_distinct_outcomes_separate() -> None:
    """Same ticker resolving at a *different* actual price is a new
    episode — dedup must key on the outcome, not just the ticker."""
    a = _pred("win", "DOWN", -10.0, date="2026-06-01")
    b = _pred("win", "DOWN", -20.0, date="2026-06-10")
    a["ticker"] = b["ticker"] = "3133.T"
    a["actual_price"] = 90.0
    b["actual_price"] = 80.0
    history = {"predictions": [a, b]}
    stats = compute_performance_stats(history)
    assert stats["episode_stats"]["n_episodes"] == 2


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


def _resolved_dir(
    prediction: str,
    status: str,
    actual_return_pct: float,
    reviewed_date: str,
    ticker: str = "X.T",
) -> dict:
    """Helper that decouples prediction direction from the realised
    return sign — needed for direction-level win-rate tests where we
    deliberately set UP picks that lose (raw return < 0) or DOWN picks
    that lose (raw return > 0)."""
    return _pred(status, prediction, actual_return_pct, date="2026-01-01", reviewed_date=reviewed_date) | {
        "ticker": ticker
    }


def test_recent_direction_winrate_splits_up_and_down() -> None:
    """Per-direction win rate over the most recent window matches a
    hand-computed split. The all-time bucket and the recent direction
    bucket can diverge; this is the recent half."""
    # Baseline 20 wins (alternating UP/DOWN) to clear drift sample-size gates.
    rows = []
    for i in range(1, 21):
        rows.append(_resolved_dir("UP", "win", 2.0, f"2026-01-{i:02d}", ticker=f"B{i}.T"))
    # Recent window: 6 UP picks 2-of-6 win, 5 DOWN picks 3-of-5 win.
    recent_specs = [
        ("UP", "win", 3.0),
        ("UP", "loss", -2.0),
        ("UP", "loss", -3.5),
        ("UP", "loss", -4.0),
        ("UP", "win", 1.5),
        ("UP", "loss", -2.0),
        ("DOWN", "win", -4.0),
        ("DOWN", "win", -2.0),
        ("DOWN", "loss", 1.0),
        ("DOWN", "loss", 2.5),
        ("DOWN", "win", -1.5),
    ]
    for i, (pred, status, ret) in enumerate(recent_specs, start=1):
        rows.append(_resolved_dir(pred, status, ret, f"2026-02-{i:02d}", ticker=f"R{i}.T"))

    stats = compute_performance_stats({"predictions": rows})
    rdw = stats.get("recent_direction_winrate")
    assert rdw is not None, "expected recent_direction_winrate stat to be populated"
    assert rdw["recent_n"] == 14  # window cap
    # The window may include older baseline rows since recent_n=14 includes
    # the last 14 chronologically — but the 11 recent specs are guaranteed
    # in the tail. Validate counts allow that.
    up = rdw["UP"]
    down = rdw["DOWN"]
    assert up is not None and down is not None
    # All 11 recent specs land in the window; the remaining 3 slots come
    # from baseline UP wins. Net: UP n = 6 + 3 = 9, wins = 2 + 3 = 5;
    # DOWN n = 5, wins = 3.
    assert up["n"] == 9
    assert up["wins"] == 5
    assert up["winrate_pct"] == 55.6
    assert down["n"] == 5
    assert down["wins"] == 3
    assert down["winrate_pct"] == 60.0


def test_recent_direction_winrate_returns_none_below_min_dir_n() -> None:
    """Direction with fewer than min_dir_n samples in the window → that
    side is None (not enough data to gate on it)."""
    rows = []
    # 20 UP baseline. No DOWN at all.
    for i in range(1, 21):
        rows.append(_resolved_dir("UP", "win", 1.0, f"2026-01-{i:02d}", ticker=f"U{i}.T"))
    stats = compute_performance_stats({"predictions": rows})
    rdw = stats.get("recent_direction_winrate")
    assert rdw is not None
    assert rdw["UP"] is not None
    assert rdw["DOWN"] is None


def test_by_regime_splits_trades_by_regime_field() -> None:
    """Each prediction's stamped regime drives the by_regime bucket;
    UP/DOWN sub-buckets reflect within-regime direction accuracy."""
    rows = []
    # Regime A: 6 trades (4 UP wins, 2 UP losses) → 66.7%
    for i in range(1, 7):
        status = "win" if i <= 4 else "loss"
        ret = 3.0 if status == "win" else -2.0
        p = _resolved_dir("UP", status, ret, f"2026-01-{i:02d}", ticker=f"A{i}.T")
        p["regime"] = "上昇トレンド"
        rows.append(p)
    # Regime B: 5 trades (2 wins, 3 losses) → 40%
    for i in range(1, 6):
        status = "win" if i <= 2 else "loss"
        ret = 2.5 if status == "win" else -3.0
        p = _resolved_dir("UP", status, ret, f"2026-02-{i:02d}", ticker=f"B{i}.T")
        p["regime"] = "高ボラティリティ"
        rows.append(p)
    # Regime C: 3 trades — should be filtered by min_n
    for i in range(1, 4):
        p = _resolved_dir("UP", "win", 1.5, f"2026-03-{i:02d}", ticker=f"C{i}.T")
        p["regime"] = "レンジ相場"
        rows.append(p)

    stats = compute_performance_stats({"predictions": rows})
    by_regime = stats.get("by_regime")
    assert by_regime is not None
    assert "上昇トレンド" in by_regime
    assert "高ボラティリティ" in by_regime
    assert "レンジ相場" not in by_regime  # below min_n=5
    assert by_regime["上昇トレンド"]["n"] == 6
    assert by_regime["上昇トレンド"]["accuracy_pct"] == 66.7
    assert by_regime["高ボラティリティ"]["accuracy_pct"] == 40.0
    # UP sub-bucket present.
    assert "UP" in by_regime["上昇トレンド"]["by_direction"]
    assert by_regime["上昇トレンド"]["by_direction"]["UP"]["wins"] == 4


def test_critic_efficacy_splits_resolved_by_verdict() -> None:
    """compute_critic_efficacy returns per-verdict accuracy and average
    directional return. ``keep`` picks should outperform ``reject``
    picks if the critic is calibrated; this test just pins the
    bucketing math."""
    rows = []
    # 6 keep entries: 4 wins / 2 losses
    for i, status in enumerate(["win", "win", "win", "win", "loss", "loss"], start=1):
        ret = 3.0 if status == "win" else -2.0
        r = _resolved_dir("UP", status, ret, f"2026-01-{i:02d}", ticker=f"K{i}.T")
        r["critic_verdict"] = "keep"
        rows.append(r)
    # 5 reject entries: 1 win / 4 losses
    for i, status in enumerate(["win", "loss", "loss", "loss", "loss"], start=1):
        ret = 2.5 if status == "win" else -3.0
        r = _resolved_dir("UP", status, ret, f"2026-02-{i:02d}", ticker=f"R{i}.T")
        r["critic_verdict"] = "reject"
        rows.append(r)
    # 3 downgrade — below min_samples=5 default, should be filtered
    for i in range(1, 4):
        r = _resolved_dir("UP", "win", 1.0, f"2026-03-{i:02d}", ticker=f"D{i}.T")
        r["critic_verdict"] = "downgrade"
        rows.append(r)

    eff = compute_critic_efficacy({"predictions": rows})
    assert eff is not None
    assert "keep" in eff
    assert "reject" in eff
    assert "downgrade" not in eff  # below min_samples
    assert eff["keep"]["n"] == 6 and eff["keep"]["accuracy_pct"] == 66.7
    assert eff["reject"]["n"] == 5 and eff["reject"]["accuracy_pct"] == 20.0


def test_critic_efficacy_none_when_no_verdicts() -> None:
    """Legacy data (no critic_verdict field) → None."""
    rows = [_resolved_dir("UP", "win", 2.0, f"2026-01-{i:02d}", ticker=f"L{i}.T") for i in range(1, 7)]
    assert compute_critic_efficacy({"predictions": rows}) is None


def test_format_critic_efficacy_emits_block_with_known_order() -> None:
    """Render order is keep → downgrade → reject regardless of dict key
    insertion order, so the weekly review prompt reads top-down."""
    eff = {
        "reject": {"n": 5, "wins": 1, "accuracy_pct": 20.0, "mean_dir_return_pct": -3.0},
        "keep": {"n": 6, "wins": 4, "accuracy_pct": 66.7, "mean_dir_return_pct": 1.5},
    }
    text = format_critic_efficacy(eff)
    assert "Critic 二次評価" in text
    keep_idx = text.index("keep")
    reject_idx = text.index("reject")
    assert keep_idx < reject_idx


def test_format_critic_efficacy_empty_when_no_data() -> None:
    assert format_critic_efficacy(None) == ""
    assert format_critic_efficacy({}) == ""


def test_by_regime_absent_when_no_regime_stamped() -> None:
    """Legacy predictions without a regime field → no bucket emitted."""
    rows = [_resolved_dir("UP", "win", 2.0, f"2026-01-{i:02d}", ticker=f"L{i}.T") for i in range(1, 7)]
    # Don't stamp regime field.
    stats = compute_performance_stats({"predictions": rows})
    assert "by_regime" not in stats


def test_recent_failure_block_empty_when_no_actionable_signals() -> None:
    """No drift, no negative-lift signals, no bad regimes → block is
    empty so it can be unconditionally concatenated."""
    history = {"performance_stats": {}}
    assert build_recent_failure_block(history) == ""


def test_recent_failure_block_emits_drift_and_direction_lines() -> None:
    """Drift + recent_direction_winrate both populated → both lines
    appear under the header."""
    history = {
        "predictions": [],
        "performance_stats": {
            "drift_indicator": {
                "is_drift": True,
                "recent_n": 14,
                "recent_expectancy_pct": -3.66,
                "baseline_expectancy_pct": 1.49,
                "p_value": 0.004,
            },
            "recent_direction_winrate": {
                "UP": {"n": 9, "wins": 1, "winrate_pct": 11.1},
                "DOWN": {"n": 5, "wins": 3, "winrate_pct": 60.0},
            },
        },
    }
    block = build_recent_failure_block(history)
    assert "直近の失敗パターン" in block
    assert "-3.66%" in block
    assert "1.49%" in block
    assert "p=0.004" in block
    assert "UP 11.1%" in block
    assert "DOWN 60.0%" in block


def test_recent_failure_block_lists_previously_rejected_tickers(tmp_path, monkeypatch) -> None:
    """data/critic_decisions.json (last cron's verdicts) → block lists
    reject + downgrade tickers so the AI doesn't reissue them without
    fresh catalyst."""
    # Build a fake critic_decisions.json under tmp_path mirroring the
    # real layout, then point the module's _DATA_DIR-derived path at it.
    fake_data_dir = tmp_path / "data"
    fake_data_dir.mkdir()
    (fake_data_dir / "critic_decisions.json").write_text(
        '{"A.T": "reject", "B.T": "reject", "C.T": "downgrade", "D.T": "keep"}',
        encoding="utf-8",
    )
    # The block resolves path via Path(__file__).parent.parent / "data";
    # monkeypatch the module-level __file__ resolution by tweaking the
    # CWD-relative join inside the function isn't easy. Simpler:
    # we monkeypatch a synthetic Path constructor — but the simplest is
    # to override the resolved path at runtime via env. The block reads
    # straight from the project's data dir, so we substitute via a
    # symlink-style trick: temporarily set the project's data file.
    import stock_analyzer.performance_tracker as pt

    real_path = pt.Path(pt.__file__).parent.parent / "data" / "critic_decisions.json"
    backed_up = None
    try:
        if real_path.exists():
            backed_up = real_path.read_text(encoding="utf-8")
        real_path.parent.mkdir(parents=True, exist_ok=True)
        real_path.write_text(
            '{"A.T": "reject", "B.T": "reject", "C.T": "downgrade", "D.T": "keep"}',
            encoding="utf-8",
        )
        history = {
            "predictions": [],
            "performance_stats": {
                "drift_indicator": {
                    "is_drift": True,
                    "recent_n": 14,
                    "recent_expectancy_pct": -1.0,
                    "baseline_expectancy_pct": 1.0,
                    "p_value": 0.04,
                }
            },
        }
        block = build_recent_failure_block(history)
        assert "前回 cron で critic が落とした" in block
        assert "A.T" in block and "B.T" in block
        assert "C.T" in block
        assert "D.T" not in block  # keep verdicts are not surfaced
    finally:
        if backed_up is not None:
            real_path.write_text(backed_up, encoding="utf-8")
        elif real_path.exists():
            real_path.unlink()


def test_recent_failure_block_emits_bad_regimes_only_when_threshold_breached() -> None:
    """Regime under 50 % with n ≥ 5 is surfaced; healthy regimes are
    not."""
    history = {
        "predictions": [],
        "performance_stats": {
            "by_regime": {
                "強気トレンド": {"n": 30, "accuracy_pct": 65.0},
                "高ボラティリティ": {"n": 10, "accuracy_pct": 40.0},
                "レンジ相場": {"n": 3, "accuracy_pct": 30.0},  # below n threshold
            }
        },
    }
    block = build_recent_failure_block(history)
    assert "高ボラティリティ: 40.0% (n=10)" in block
    assert "強気トレンド" not in block  # healthy
    assert "レンジ相場" not in block  # sample too small


def test_recent_direction_winrate_absent_when_history_tiny() -> None:
    """Below the min_dir_n threshold across the whole history, the stat
    is suppressed entirely."""
    rows = [_resolved_dir("UP", "win", 2.0, "2026-01-01")]
    stats = compute_performance_stats({"predictions": rows})
    assert "recent_direction_winrate" not in stats


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


def test_high_status_suppressed_when_recent_high_below_medium_ratio() -> None:
    """HIGH 50% / MEDIUM 66.7% (双方 直近 window 内・十分なサンプル) →
    high_status=suppressed (HIGH 出力禁止)。circuit-breaker zone は HIGH
    ラベルの傷では発火しない (2026-06-24 の 2 軸分離)。"""
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
    assert zone["high_status"] == "suppressed"
    assert any("ratio" in r for r in zone["high_reasons"])


def test_high_status_ok_when_high_outperforms_medium() -> None:
    """HIGH 70% / MEDIUM 60% → ratio > 0.9 → high_status=ok (通常運用)。"""
    preds: list[dict] = []
    for i in range(30):
        preds.append(_bucket_pred("HIGH", "win" if i < 21 else "loss", 5.0 if i < 21 else -5.0, i))
    for i in range(30):
        preds.append(_bucket_pred("MEDIUM", "win" if i < 18 else "loss", 5.0 if i < 18 else -5.0, i + 100))
    stats = compute_performance_stats({"predictions": preds})
    zone = stats.get("calibration_zone")
    assert zone is not None
    assert zone["high_status"] == "ok"


def test_high_status_ok_when_high_sample_insufficient_without_lifetime_inversion() -> None:
    """HIGH n=8 < min(15) かつ通算でも逆転実績なし → high_status=ok。
    旧実装は『サンプル不足 → yellow』で weight 学習まで凍結していたが、
    証拠の無い不確実性で circuit breaker を止めるのは過剰。"""
    preds: list[dict] = []
    # HIGH: 8 件のみ (min 15 未満)、50%
    for i in range(8):
        preds.append(_bucket_pred("HIGH", "win" if i < 4 else "loss", 5.0 if i < 4 else -5.0, i))
    # MEDIUM: 25 件、healthy 64%
    for i in range(25):
        preds.append(_bucket_pred("MEDIUM", "win" if i < 16 else "loss", 5.0 if i < 16 else -5.0, i + 100))
    stats = compute_performance_stats({"predictions": preds})
    zone = stats.get("calibration_zone")
    assert zone is not None
    assert zone["high_status"] == "ok"


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


def test_high_status_suppressed_on_brier_inversion() -> None:
    """HIGH Brier > MEDIUM Brier だけでも suppressed (accuracy ratio が境界でも独立 fire)。"""
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
    # accuracy ratio 55.6/61.5 = 0.903 → ratio 単独では境界未満
    # でも Brier 0.285 vs 0.238 で HIGH > MEDIUM → suppressed
    assert zone["high_status"] == "suppressed"
    assert any("Brier" in r for r in zone["high_reasons"])


def test_high_status_probation_breaks_absorbing_red_deadlock() -> None:
    """回帰テスト (2026-06 デッドロック): 通算では HIGH 逆転だが直近 window
    で HIGH が枯れている (= かつて red で抑制された) 場合、永久 red になら
    ず high_status=probation で再試験経路を残す。circuit-breaker zone は
    直近 performance が健全なので red にならない (weight 学習が復帰できる)。
    """
    preds: list[dict] = []
    # 古い HIGH (90 日より前、2026-01): 8 win / 12 loss = 40% → 通算で逆転
    for i in range(20):
        preds.append(
            _pred(
                "win" if i < 8 else "loss",
                "UP" if i < 8 else "DOWN",
                5.0,
                confidence="HIGH",
                date="2026-01-05",
                reviewed_date=f"2026-01-{(i % 28) + 1:02d}",
            )
        )
    # 直近 MEDIUM (window 内、健全 ~67% / interleaved で recent Sharpe 正) —
    # HIGH は一切出ていない (suppression 後の現実を再現)
    for i in range(30):
        win = i % 3 != 0  # 20 win / 10 loss、日付に対して均す
        preds.append(
            _pred(
                "win" if win else "loss",
                "UP" if win else "DOWN",
                5.0,
                confidence="MEDIUM",
                date="2026-06-05",
                reviewed_date=f"2026-06-{(i % 28) + 1:02d}",
            )
        )
    stats = compute_performance_stats({"predictions": preds})
    zone = stats["calibration_zone"]
    assert zone["high_status"] == "probation"
    assert zone["high_recent_n"] == 0  # 直近 window に HIGH は無い
    assert zone["zone"] != "red"  # 通算 HIGH の傷で circuit breaker を止めない
    assert any("probation" in r for r in zone["high_reasons"])


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


def test_overpriced_bias_returns_none_without_pre_entry_metrics() -> None:
    """pre_entry_metrics 無い予測のみだと overpriced_bias は None。"""
    from stock_analyzer.performance_tracker import compute_performance_stats

    rows = [_pred("win", "UP", 5.0, confidence="HIGH", reviewed_date=f"2026-04-{i + 1:02d}") for i in range(20)]
    stats = compute_performance_stats({"predictions": rows})
    assert stats.get("overpriced_bias") is None


def test_overpriced_bias_records_mean_returns_per_bucket() -> None:
    """pre_entry_metrics が十分にある場合、bucket 別 mean returns を出力。"""
    from stock_analyzer.performance_tracker import compute_performance_stats

    rows = []
    # HIGH n=15、事前 21d +8% (overpriced 候補)
    for i in range(15):
        status = "win" if i < 8 else "loss"
        ret = 5.0 if i < 8 else -5.0
        p = _pred(status, "UP", ret, confidence="HIGH", reviewed_date=f"2026-04-{i + 1:02d}")
        p["ticker"] = f"H{i}.T"
        p["pre_entry_metrics"] = {"price_change_5d": 3.0, "price_change_1m": 8.0, "price_change_3m": 15.0}
        rows.append(p)
    # MEDIUM n=15、事前 21d +1% (well-priced)
    for i in range(15):
        status = "win" if i < 10 else "loss"
        ret = 5.0 if i < 10 else -5.0
        p = _pred(status, "UP", ret, confidence="MEDIUM", reviewed_date=f"2026-04-{i + 16:02d}")
        p["ticker"] = f"M{i}.T"
        p["pre_entry_metrics"] = {"price_change_5d": 1.0, "price_change_1m": 1.0, "price_change_3m": 3.0}
        rows.append(p)
    stats = compute_performance_stats({"predictions": rows})
    bias = stats.get("overpriced_bias")
    assert bias is not None
    assert bias["HIGH"]["mean_1m_ret_pct"] == 8.0
    assert bias["MEDIUM"]["mean_1m_ret_pct"] == 1.0


def test_effective_vol_returns_max_of_atr_and_realized() -> None:
    """compute_effective_vol_pct は max(ATR, realized vol) を返す。"""
    from stock_analyzer.position_sizing import compute_atr_pct, compute_effective_vol_pct, compute_realized_vol_pct

    # 直近 spike を仕込んだ価格列
    closes = [100.0] * 14 + [98.0, 102.0, 95.0, 108.0, 100.0]
    highs = [c * 1.02 for c in closes]
    lows = [c * 0.98 for c in closes]
    atr = compute_atr_pct(highs, lows, closes)
    realized = compute_realized_vol_pct(closes)
    effective = compute_effective_vol_pct(highs, lows, closes)
    assert atr is not None
    assert realized is not None
    assert effective is not None
    assert effective == max(atr, realized)


def test_format_feedback_emits_high_suppressed_block() -> None:
    """直近 HIGH 校正逆転 → feedback に🟥 HIGH 校正不良 + 「HIGH 出力は禁止」 directive。"""
    preds: list[dict] = []
    for i in range(30):
        preds.append(_bucket_pred("HIGH", "win" if i < 15 else "loss", 5.0 if i < 15 else -5.0, i))
    for i in range(30):
        preds.append(_bucket_pred("MEDIUM", "win" if i < 20 else "loss", 5.0 if i < 20 else -5.0, i + 100))
    history = {"predictions": preds}
    history["performance_stats"] = compute_performance_stats(history)
    feedback = format_performance_feedback(history)
    assert "🟥" in feedback
    assert "HIGH 校正不良" in feedback
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


def test_compute_recent_up_hit_rate_flags_negative_ev_at_50pct() -> None:
    """勝率がちょうど閾値 (50%) でも期待値が負なら gate は発火する。
    2026-07-04 監査: UP は 50% に張り付いたまま EV 負で gate が開いて
    いたのが実害だった。"""
    preds = [_up_pred("win", f"2026-02-{i:02d}", 1.0) for i in range(1, 11)]
    preds += [_up_pred("loss", f"2026-02-{i:02d}", -3.0) for i in range(11, 21)]
    history = {"predictions": preds}
    stat = compute_recent_up_hit_rate(history)
    assert stat is not None
    assert stat["hit_rate_pct"] == 50.0
    assert stat["mean_dir_return_pct"] == -1.0
    assert stat["ev_negative"] is True
    assert stat["below_threshold"] is True


def test_build_up_gate_directive_mentions_ev_when_ev_triggered() -> None:
    """EV 起因で発火した場合、hit rate は閾値以上なので「勝率が低い」
    ではなく期待値の説明を出す。"""
    stats = {
        "recent_up_hit_rate": {
            "recent_n": 20,
            "wins": 10,
            "hit_rate_pct": 50.0,
            "threshold_pct": 50.0,
            "mean_dir_return_pct": -1.0,
            "ev_negative": True,
            "below_threshold": True,
        }
    }
    text = build_up_gate_directive(stats)
    assert "期待値" in text
    assert "-1.00%" in text
    assert "short_term_picks の UP 推奨は原則禁止" in text


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
