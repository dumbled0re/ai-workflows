from __future__ import annotations

from stock_analyzer.position_sizing import (
    annotate_summary,
    compute_atr_pct,
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
