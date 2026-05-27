"""Tests for the 前日比 P&L 表示 helpers in slack_notifier.

2026-05-27 added so the Slack message can show portfolio + per-holding
day-over-day movement plus 含み損益 vs avg_cost. The helpers must:

  - silently fall back to empty output when meta is missing / unusable
    (preserves legacy AI-summary-only behaviour)
  - render exact 円 values (no truncation hiding meaningful losses)
  - sign positives with "+" so red/green isn't the only differentiator
    (Slack's mrkdwn doesn't carry color; sign + arrow emoji handle it)
"""

from __future__ import annotations

from stock_analyzer.slack_notifier import _aggregate_pnl, _format_holding_pnl_line


def test_format_holding_pnl_line_full_meta() -> None:
    meta = {
        "current_price": 1000.0,
        "prev_close": 950.0,
        "shares": 100,
        "avg_cost": 800.0,
        "unrealized_pnl_pct": 25.0,
    }
    line = _format_holding_pnl_line(meta)
    assert "評価額: ¥100,000" in line
    # day change: (1000 - 950) * 100 = +5,000、+5.26%
    assert "+¥5,000" in line
    assert "+5.26%" in line
    # unrealized: (1000 - 800) * 100 = +20,000、+25.00%
    assert "+¥20,000" in line
    assert "+25.00%" in line


def test_format_holding_pnl_line_loss_signs() -> None:
    meta = {
        "current_price": 900.0,
        "prev_close": 1000.0,
        "shares": 100,
        "avg_cost": 1200.0,
        "unrealized_pnl_pct": -25.0,
    }
    line = _format_holding_pnl_line(meta)
    # day change negative — no "+" prefix on negatives so 円 just reads "-¥…"
    assert "-¥10,000" in line
    assert "-10.00%" in line
    assert "-¥30,000" in line
    assert "-25.00%" in line


def test_format_holding_pnl_line_no_avg_cost_skips_unrealized() -> None:
    meta = {"current_price": 500.0, "prev_close": 490.0, "shares": 200}
    line = _format_holding_pnl_line(meta)
    assert "評価額: ¥100,000" in line
    assert "前日比" in line
    assert "含み損益" not in line


def test_format_holding_pnl_line_missing_meta_returns_empty() -> None:
    assert _format_holding_pnl_line(None) == ""
    assert _format_holding_pnl_line({}) == ""
    # shares=0 → no meaningful aggregate
    assert _format_holding_pnl_line({"current_price": 100, "shares": 0}) == ""
    # current_price missing → can't compute
    assert _format_holding_pnl_line({"shares": 100}) == ""


def test_format_holding_pnl_line_no_prev_close_skips_day_change() -> None:
    """If prev_close is None (first run, single-row history), the line
    should still show 評価額 + 含み損益 but skip 前日比."""
    meta = {"current_price": 1000.0, "shares": 50, "avg_cost": 800.0, "unrealized_pnl_pct": 25.0}
    line = _format_holding_pnl_line(meta)
    assert "評価額" in line
    assert "前日比" not in line
    assert "含み損益" in line


def test_aggregate_pnl_sums_across_holdings() -> None:
    holdings_analysis = [{"ticker": "A.T"}, {"ticker": "B.T"}]
    holdings_meta = {
        "A.T": {"current_price": 1000, "prev_close": 950, "shares": 100, "avg_cost": 800},
        "B.T": {"current_price": 500, "prev_close": 510, "shares": 200, "avg_cost": 600},
    }
    text = _aggregate_pnl(holdings_analysis, holdings_meta)
    assert text is not None
    # 評価額: 1000*100 + 500*200 = 100,000 + 100,000 = 200,000
    assert "¥200,000" in text
    # 前日比: (1000-950)*100 + (500-510)*200 = 5,000 - 2,000 = +3,000
    assert "+¥3,000" in text
    # 含み損益: (1000-800)*100 + (500-600)*200 = 20,000 - 20,000 = 0
    assert "+¥0" in text  # ties go positive sign
    assert "2 銘柄" in text


def test_aggregate_pnl_no_meta_returns_none() -> None:
    """Empty holdings or no matching meta → notifier shouldn't insert a stub
    line (caller treats None as "skip aggregate block")."""
    assert _aggregate_pnl([], {}) is None
    assert _aggregate_pnl([{"ticker": "A.T"}], {}) is None
    # Meta present but missing current_price → still no usable data.
    assert _aggregate_pnl([{"ticker": "A.T"}], {"A.T": {"shares": 100}}) is None


def test_aggregate_pnl_partial_unrealized_omits_when_no_avg_cost() -> None:
    """A holding with no avg_cost shouldn't break aggregation — sum the
    others and omit 含み損益 if zero holdings have avg_cost."""
    holdings_analysis = [{"ticker": "A.T"}, {"ticker": "B.T"}]
    holdings_meta = {
        "A.T": {"current_price": 1000, "prev_close": 950, "shares": 100},  # no avg_cost
        "B.T": {"current_price": 500, "prev_close": 510, "shares": 200},  # no avg_cost
    }
    text = _aggregate_pnl(holdings_analysis, holdings_meta)
    assert text is not None
    assert "前日比" in text
    assert "含み損益" not in text  # neither has avg_cost
