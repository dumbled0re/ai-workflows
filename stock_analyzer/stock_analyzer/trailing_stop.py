"""Trailing-stop suggestion logic for holdings positions.

The discipline pros apply: as a position moves favorably, raise the
stop-loss so accumulated gains can't reverse to losses. Specifically:

- Unrealized P&L >= +10% → raise stop to entry +5% (lock in half)
- Unrealized P&L >= +5%  → raise stop to entry (no-loss exit)
- Unrealized P&L >= +3%  → raise stop to entry -2% (cut downside)
- Unrealized P&L <  +3%  → keep AI's original stop

The system tracks predictions, not real positions, so this is
operator-facing guidance — the AI's stop_loss recommendation gets
augmented with a "trailing suggestion" so the operator can decide
to tighten the stop on a winner. investment_rules.json's
``trailing_stop`` rule is the corresponding policy.

This is recommendation-only — no execution, no auto-adjustment of
predictions_history.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TrailingStopSuggestion:
    """Recommendation for tightening a holding's stop-loss.

    ``new_stop_pct`` is expressed relative to entry (avg_cost):
    +0.05 = "stop at entry + 5%". Positive values mean "stop is
    above entry" = locking in profit; zero means "stop at entry"
    = no-loss exit; negative means "stop below entry but tighter
    than original".
    """

    rationale: str
    new_stop_pct: float  # fraction relative to entry
    new_stop_price: float  # absolute price


def compute_trailing_stop(
    entry_price: float | None,
    current_price: float | None,
    direction: str = "UP",
) -> TrailingStopSuggestion | None:
    """Recommend a tightened stop for a holding with unrealized gain.

    Returns None when:
    - entry_price or current_price missing / non-positive
    - direction unrecognised
    - position isn't sufficiently in the green (< +3% PnL)

    The thresholds (3 / 5 / 10) are calibrated to common JP-equity
    swing target ranges. For tighter holding profiles (intraday or
    high-vol scalping) the bands would shrink; for longer-horizon
    holds they would widen.
    """
    if entry_price is None or current_price is None or entry_price <= 0:
        return None
    direction_u = (direction or "UP").upper()
    if direction_u not in {"UP", "DOWN"}:
        return None

    # Directional unrealised PnL %.
    if direction_u == "UP":
        pnl_pct = (current_price - entry_price) / entry_price * 100
    else:
        pnl_pct = (entry_price - current_price) / entry_price * 100

    if pnl_pct < 3.0:
        return None  # Not enough cushion to tighten

    # Determine band.
    if pnl_pct >= 10.0:
        new_stop_rel = 0.05  # entry + 5% (UP) / entry - 5% (DOWN)
        rationale = "含み益+10%超 — 利益の半分を確定する位置まで stop 引き上げ"
    elif pnl_pct >= 5.0:
        new_stop_rel = 0.0  # entry exactly (no-loss exit)
        rationale = "含み益+5%超 — 損失ゼロを確定する建値 stop に引き上げ"
    else:
        new_stop_rel = -0.02  # entry - 2% (UP) / entry + 2% (DOWN)
        rationale = "含み益+3%超 — 損失を最小化する位置まで stop 引き上げ"

    # Convert relative to absolute price, respecting direction.
    new_stop_price = entry_price * (1.0 + new_stop_rel) if direction_u == "UP" else entry_price * (1.0 - new_stop_rel)

    return TrailingStopSuggestion(
        rationale=rationale,
        new_stop_pct=round(new_stop_rel * 100, 2),
        new_stop_price=round(new_stop_price, 1),
    )


def annotate_holding(summary: dict) -> None:
    """Set ``trailing_stop_suggestion`` on a holding summary.

    Reads ``avg_cost`` / ``current_price`` / ``prediction`` from the
    summary. holdings_summaries built by compute_indicators carry
    avg_cost only when the user has actually entered the position;
    screened candidates don't carry it. So this annotation is
    effectively no-op for candidates.
    """
    entry = summary.get("avg_cost")
    current = summary.get("current_price")
    direction = summary.get("prediction") or "UP"
    suggestion = compute_trailing_stop(
        float(entry) if isinstance(entry, (int, float)) else None,
        float(current) if isinstance(current, (int, float)) else None,
        direction=direction,
    )
    if suggestion is None:
        return
    summary["trailing_stop_suggestion"] = {
        "rationale": suggestion.rationale,
        "new_stop_pct": suggestion.new_stop_pct,
        "new_stop_price": suggestion.new_stop_price,
    }
