from __future__ import annotations

from stock_analyzer.position_sizing import (
    annotate_summary,
    compute_atr_pct,
    compute_kelly_size,
    derive_kelly_bases,
    stop_aware_size,
    vol_targeted_size,
)


def test_compute_atr_pct_returns_typical_jp_equity_range() -> None:
    """A series with ~1% daily moves should produce ATR % around 1-2%."""
    closes = [100.0 + i * 0.1 for i in range(20)]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    atr = compute_atr_pct(highs, lows, closes)
    assert atr is not None
    # H-L = 2 on a ~102 close → ~2%
    assert 1.5 < atr < 2.5


def test_compute_atr_pct_none_on_short_history() -> None:
    """A 10-bar series is too short for a 14-day ATR — return None
    so the caller falls back to the confidence base."""
    closes = [100.0] * 10
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    assert compute_atr_pct(highs, lows, closes) is None


def test_vol_targeted_size_caps_at_base_when_low_vol() -> None:
    """A quiet stock (<= target vol) gets the full base size, not
    8% — the cap prevents over-allocation in calm markets."""
    # HIGH base = 4%, vol = 0.5% (very quiet) → uncapped formula
    # would suggest 4% * (2/0.5) = 16%. Capped at 4%.
    assert vol_targeted_size("HIGH", daily_vol_pct=0.5) == 4.0
    assert vol_targeted_size("HIGH", daily_vol_pct=2.0) == 4.0  # equal to target


def test_vol_targeted_size_shrinks_for_volatile() -> None:
    """A 4% daily-vol stock at HIGH confidence gets half the base
    (4 * 2/4 = 2)."""
    assert vol_targeted_size("HIGH", daily_vol_pct=4.0) == 2.0
    assert vol_targeted_size("MEDIUM", daily_vol_pct=4.0) == 1.0


def test_vol_targeted_size_returns_base_when_no_vol() -> None:
    """No vol data → fall back to confidence base. Better than
    skipping the recommendation entirely."""
    assert vol_targeted_size("MEDIUM", daily_vol_pct=None) == 2.0


def test_vol_targeted_size_none_for_unknown_confidence() -> None:
    assert vol_targeted_size("UNCLEAR", daily_vol_pct=2.0) is None
    assert vol_targeted_size("", daily_vol_pct=2.0) is None


def test_stop_aware_size_up_trade() -> None:
    """1% account risk, entry 1000, stop 950 (5% away). Per-trade
    capital risk / 5% = 0.2 → 0.2% of capital. With round.
    Wait — the formula is account_risk / risk_pct_of_price * 100,
    where risk_pct = 5% → 1/5 * 100 = 20% → no that's wrong too.
    Let me re-derive:
    - Account = 1,000,000円. account_risk_pct=1% → ¥10,000 at risk
    - risk_per_share = entry - stop = 50円
    - shares = 10,000 / 50 = 200 shares
    - position size = 200 * 1000 = 200,000円 = 20% of account
    So a 5% stop with 1% account risk → 20% position. That's
    aggressive but matches the formula. Real desks pair this with
    a hard position-size cap (e.g. 25%) which we don't enforce here.
    """
    size = stop_aware_size(entry=1000.0, stop=950.0, direction="UP")
    assert size == 20.0


def test_stop_aware_size_down_trade() -> None:
    """Symmetric for DOWN: entry 1000, stop 1050 → 5% wrong-way move."""
    assert stop_aware_size(entry=1000.0, stop=1050.0, direction="DOWN") == 20.0


def test_stop_aware_size_returns_none_for_inverted_stop() -> None:
    """An inverted-stop setup (UP with stop > entry) returns None
    so we don't fabricate a size for malformed input. portfolio_risk
    flags it elsewhere."""
    assert stop_aware_size(entry=1000.0, stop=1100.0, direction="UP") is None
    assert stop_aware_size(entry=1000.0, stop=950.0, direction="DOWN") is None


def test_stop_aware_size_returns_none_for_missing_inputs() -> None:
    assert stop_aware_size(entry=None, stop=950.0, direction="UP") is None
    assert stop_aware_size(entry=1000.0, stop=None, direction="UP") is None


def test_annotate_summary_picks_more_conservative_of_two() -> None:
    """When both vol-target and stop-aware are available, the
    annotation takes the smaller (more conservative) result. This
    guards against over-allocation when one method is permissive."""
    summary: dict = {
        "confidence": "HIGH",
        "daily_atr_pct": 2.0,  # vol-target → 4%
        "current_price": 1000.0,
        "stop_loss": "990",  # 1% stop → stop-aware = 100% (huge)
        "prediction": "UP",
    }
    annotate_summary(summary)
    # vol-target 4% should win over the absurd stop-aware 100%
    assert summary["suggested_position_pct"] == 4.0
    assert summary["stop_aware_position_pct"] == 100.0


def test_annotate_summary_no_atr_falls_back_to_base() -> None:
    summary: dict = {"confidence": "MEDIUM"}
    annotate_summary(summary)
    assert summary["suggested_position_pct"] == 2.0


# ---------- Kelly criterion sizing ----------------------------------------


def test_compute_kelly_size_positive_edge() -> None:
    """60% win rate with 6%/4% R:R ratio:
    b = 6/4 = 1.5, p = 0.6, q = 0.4
    f* = (1.5*0.6 - 0.4) / 1.5 = 0.5/1.5 = 0.333
    Quarter-Kelly = 0.333 * 0.25 = 0.0833 = 8.3%
    Capped at 5% → 5%"""
    kelly = compute_kelly_size(win_rate=0.6, avg_win_pct=6.0, avg_loss_pct_abs=4.0)
    assert kelly == 5.0  # Hit the cap


def test_compute_kelly_size_moderate_edge() -> None:
    """55% win rate, even R:R (4%/4%): low edge → small position.
    f* = (1.0*0.55 - 0.45) / 1.0 = 0.10
    Quarter-Kelly = 2.5%"""
    kelly = compute_kelly_size(win_rate=0.55, avg_win_pct=4.0, avg_loss_pct_abs=4.0)
    assert kelly == 2.5


def test_compute_kelly_size_negative_edge_returns_zero() -> None:
    """40% win rate with even R:R → negative EV → recommend NO
    position. This is the key safety mechanism: empirical Kelly
    pulls capital out of buckets that don't have an edge."""
    kelly = compute_kelly_size(win_rate=0.4, avg_win_pct=4.0, avg_loss_pct_abs=4.0)
    assert kelly == 0.0


def test_compute_kelly_size_degenerate_returns_none() -> None:
    """Zero or negative inputs → None so caller falls back to base."""
    assert compute_kelly_size(0.0, 4.0, 4.0) is None
    assert compute_kelly_size(0.6, 0.0, 4.0) is None
    assert compute_kelly_size(0.6, 4.0, 0.0) is None
    assert compute_kelly_size(1.0, 4.0, 4.0) is None  # 100% win = degenerate


def test_derive_kelly_bases_skips_low_sample_buckets() -> None:
    """A bucket with fewer than _KELLY_MIN_SAMPLES resolved trades
    must be omitted — Kelly on noise gives garbage. The caller then
    falls back to the heuristic base for that confidence."""
    # 5 HIGH + 8 MEDIUM = below threshold for HIGH, above for MEDIUM.
    preds = []
    for _ in range(5):
        preds.append({"status": "win", "confidence": "HIGH", "prediction": "UP", "actual_return_pct": 6.0})
    for _ in range(5):
        preds.append({"status": "win", "confidence": "MEDIUM", "prediction": "UP", "actual_return_pct": 4.0})
    for _ in range(3):
        preds.append({"status": "loss", "confidence": "MEDIUM", "prediction": "UP", "actual_return_pct": -4.0})
    history = {"predictions": preds}
    bases = derive_kelly_bases(history)
    assert bases is not None
    assert "HIGH" not in bases  # too few samples
    assert "MEDIUM" in bases


def test_derive_kelly_bases_direction_aware() -> None:
    """DOWN-wins (raw negative actual_return) must count as positive
    Kelly returns. A DOWN prediction with -8% actual is an 8% win
    in directional terms; if Kelly used raw, this would crash the
    avg_win calculation."""
    preds = []
    # 8 wins: 4 UP +6%, 4 DOWN -6% (= +6% directional)
    for _ in range(4):
        preds.append({"status": "win", "confidence": "HIGH", "prediction": "UP", "actual_return_pct": 6.0})
    for _ in range(4):
        preds.append({"status": "win", "confidence": "HIGH", "prediction": "DOWN", "actual_return_pct": -6.0})
    # 4 losses with similar pattern
    for _ in range(2):
        preds.append({"status": "loss", "confidence": "HIGH", "prediction": "UP", "actual_return_pct": -4.0})
    for _ in range(2):
        preds.append({"status": "loss", "confidence": "HIGH", "prediction": "DOWN", "actual_return_pct": 4.0})
    bases = derive_kelly_bases({"predictions": preds})
    assert bases is not None
    assert bases["HIGH"] > 0  # positive edge → positive Kelly


def test_annotate_summary_uses_kelly_when_provided() -> None:
    """When kelly_bases passed, position size uses the empirical
    base instead of the heuristic. The final number is still the
    minimum across vol/Kelly/stop methods."""
    kelly_bases = {"HIGH": 1.5, "MEDIUM": 0.8}  # Conservative empirical
    summary: dict = {
        "confidence": "HIGH",
        "daily_atr_pct": 2.0,  # vol-target = base = 4%
    }
    annotate_summary(summary, kelly_bases=kelly_bases)
    # Kelly base 1.5% is smaller than vol-target 4% → Kelly wins
    assert summary["kelly_position_pct"] == 1.5
    assert summary["suggested_position_pct"] == 1.5


def test_annotate_summary_kelly_inverse_vol_scales() -> None:
    """Kelly base also scales by inverse volatility: a 4%-vol stock
    at Kelly base 2% gets 1% (2 * 2/4)."""
    kelly_bases = {"HIGH": 2.0}
    summary: dict = {"confidence": "HIGH", "daily_atr_pct": 4.0}
    annotate_summary(summary, kelly_bases=kelly_bases)
    assert summary["kelly_position_pct"] == 1.0
