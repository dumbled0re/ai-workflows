from __future__ import annotations

from stock_analyzer.trailing_stop import annotate_holding, compute_trailing_stop


def test_no_suggestion_when_below_3pct_gain() -> None:
    """At +2% PnL the gain is too thin to lock in. We don't want to
    spam holdings with trailing-stop suggestions when there's barely
    any cushion."""
    s = compute_trailing_stop(entry_price=1000.0, current_price=1020.0, direction="UP")
    assert s is None


def test_suggestion_at_low_band_3_to_5pct_pulls_stop_to_entry_minus_2() -> None:
    """+4% PnL → tighten stop to entry -2% (= 980 on 1000 entry).
    This caps the downside at 2% loss instead of the original wider stop."""
    s = compute_trailing_stop(entry_price=1000.0, current_price=1040.0, direction="UP")
    assert s is not None
    assert s.new_stop_pct == -2.0
    assert s.new_stop_price == 980.0
    assert "+3%" in s.rationale


def test_suggestion_at_mid_band_5_to_10pct_pulls_stop_to_entry() -> None:
    """+7% PnL → no-loss exit (stop at entry exactly)."""
    s = compute_trailing_stop(entry_price=1000.0, current_price=1070.0, direction="UP")
    assert s is not None
    assert s.new_stop_pct == 0.0
    assert s.new_stop_price == 1000.0


def test_suggestion_at_high_band_10pct_plus_locks_half() -> None:
    """+12% PnL → entry +5% stop (half-profit lock-in)."""
    s = compute_trailing_stop(entry_price=1000.0, current_price=1120.0, direction="UP")
    assert s is not None
    assert s.new_stop_pct == 5.0
    assert s.new_stop_price == 1050.0
    assert "半分" in s.rationale


def test_down_position_mirrors_logic() -> None:
    """DOWN holding at -12% PnL (= +12% directional gain for short
    setup) gets the same half-lock treatment but on the short side:
    stop at entry -5% (price moved further down)."""
    s = compute_trailing_stop(entry_price=1000.0, current_price=880.0, direction="DOWN")
    assert s is not None
    assert s.new_stop_pct == 5.0
    # For DOWN: new_stop_price = entry * (1 - new_stop_rel) = 950
    assert s.new_stop_price == 950.0


def test_returns_none_on_missing_inputs() -> None:
    assert compute_trailing_stop(None, 1000.0) is None
    assert compute_trailing_stop(1000.0, None) is None
    assert compute_trailing_stop(0.0, 1000.0) is None
    assert compute_trailing_stop(1000.0, 1100.0, direction="SIDEWAYS") is None


def test_annotate_holding_skips_when_no_avg_cost() -> None:
    """Candidates don't carry avg_cost — annotate should silently
    skip rather than crash."""
    summary = {"current_price": 1100.0, "prediction": "UP"}
    annotate_holding(summary)
    assert "trailing_stop_suggestion" not in summary


def test_annotate_holding_writes_dict_when_gain_present() -> None:
    """A holding with avg_cost + winning current_price gets the
    suggestion dict written. The shape matches what ai_analyzer
    expects (rationale / new_stop_pct / new_stop_price keys)."""
    summary = {
        "avg_cost": 1000.0,
        "current_price": 1120.0,
        "prediction": "UP",
    }
    annotate_holding(summary)
    ts = summary["trailing_stop_suggestion"]
    assert ts["new_stop_pct"] == 5.0
    assert ts["new_stop_price"] == 1050.0
    assert "rationale" in ts
