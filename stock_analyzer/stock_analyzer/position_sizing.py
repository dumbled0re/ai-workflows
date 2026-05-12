"""ATR-based volatility-targeted position sizing recommendations.

Real trading desks size positions inversely with volatility so a
high-vol stock doesn't consume disproportionate capital risk. The
canonical formula is:

    position_pct = base_pct_for_confidence * (target_vol / stock_vol)

where stock_vol is approximated by ATR(14) / current_price (daily
percent move) and target_vol is the desk-level constant (~2% daily
for swing trading). Base pct comes from confidence:

    HIGH   → 4% of capital (high conviction, high size)
    MEDIUM → 2% of capital
    LOW    → 0.5% of capital (monitor, not a sizing decision)

The output is a *recommended* size — the system tracks predictions
rather than real positions, so this is operator-facing guidance,
not an automated trade. It's printed alongside each pick so the
operator can scale their entry by conviction × volatility rather
than uniformly across the discovery list.

ATR computation uses the standard 14-day Welles Wilder method.
``stop_aware_size`` is an alternative formula when the AI has set
an explicit stop_loss: size by the max-loss-per-trade rule
(``account_risk_pct / (entry - stop) * entry``) which respects
the AI's own risk preference rather than ATR's statistical estimate.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_BASE_PCT_BY_CONFIDENCE = {
    "HIGH": 4.0,
    "MEDIUM": 2.0,
    "LOW": 0.5,
}
_TARGET_DAILY_VOL_PCT = 2.0
_DEFAULT_ACCOUNT_RISK_PCT = 1.0  # 1% of capital risked per trade — Kelly-ish conservative


def compute_atr_pct(highs: list[float], lows: list[float], closes: list[float], window: int = 14) -> float | None:
    """Average True Range as a percentage of the latest close.

    Returns None when there's not enough data for a meaningful 14-day
    average (need 15+ bars to compute 14 true-ranges with a prev-close
    anchor). Stocks with insufficient history just skip the size
    calculation and the operator falls back to the AI's free-form
    suggestion.
    """
    if len(highs) < window + 1 or len(lows) < window + 1 or len(closes) < window + 1:
        return None
    true_ranges: list[float] = []
    for i in range(len(closes) - window, len(closes)):
        h = highs[i]
        low = lows[i]
        prev_c = closes[i - 1]
        tr = max(h - low, abs(h - prev_c), abs(low - prev_c))
        true_ranges.append(tr)
    if not true_ranges:
        return None
    atr = sum(true_ranges) / len(true_ranges)
    last_close = closes[-1]
    if last_close <= 0:
        return None
    return atr / last_close * 100


def vol_targeted_size(
    confidence: str,
    daily_vol_pct: float | None,
    target_vol_pct: float = _TARGET_DAILY_VOL_PCT,
) -> float | None:
    """Recommend a position size as % of capital, scaled by volatility.

    A stock at the target_vol gets the base size for its confidence;
    a stock at 2x the target gets half the base, etc. The result is
    capped at the confidence base — a low-vol stock shouldn't get an
    aggregious 8% allocation just because it's quiet.

    Returns None when confidence is unrecognised or vol is missing,
    so the caller renders nothing rather than guessing.
    """
    if not confidence:
        return None
    base = _BASE_PCT_BY_CONFIDENCE.get(confidence.upper())
    if base is None:
        return None
    if daily_vol_pct is None or daily_vol_pct <= 0:
        return base  # no vol info → fall back to base
    sized = base * (target_vol_pct / daily_vol_pct)
    return round(min(base, sized), 2)


def stop_aware_size(
    entry: float | None,
    stop: float | None,
    direction: str,
    account_risk_pct: float = _DEFAULT_ACCOUNT_RISK_PCT,
) -> float | None:
    """Position size when the AI specified a stop_loss.

    The rule "risk 1% of capital per trade" combined with the
    distance to stop gives a deterministic size. For an UP trade:
    risk_per_share = entry - stop. Capital risk = account * 1%.
    Shares = capital_risk / risk_per_share. Position % = (shares *
    entry) / capital. Account size cancels out when we report just
    the percentage.

    Returns None when either price is missing or the stop is on the
    wrong side (handled separately by portfolio_risk's
    check_stop_loss_consistency).
    """
    if entry is None or stop is None or entry <= 0:
        return None
    direction_u = (direction or "").upper()
    if direction_u == "UP":
        risk_per_share = entry - stop
    elif direction_u == "DOWN":
        risk_per_share = stop - entry
    else:
        return None
    if risk_per_share <= 0:
        return None
    risk_pct_of_price = risk_per_share / entry * 100
    if risk_pct_of_price <= 0:
        return None
    # position_pct = (capital_risk / risk_per_share) * entry / capital
    #             = (account_risk_pct / risk_pct_of_price) * 100
    return round(account_risk_pct / risk_pct_of_price * 100, 2)


def annotate_summary(summary: dict, default_confidence: str = "MEDIUM") -> None:
    """Mutate the summary with ``suggested_position_pct``.

    Computes both vol-targeted (using ATR) and stop-aware (using
    AI's stop_loss when present) sizes; the result is the more
    conservative of the two so we never over-allocate. ATR is
    pulled from the existing summary fields populated by
    ``compute_indicators`` (we maintain the close / high / low
    series upstream, but here we read pre-computed daily_atr_pct
    when available, otherwise just use the confidence base).

    The recommendation is appended to the per-stock prompt block
    via ``ai_analyzer._format_stock_data`` so the AI sees a
    concrete size suggestion alongside its own prediction.
    """
    confidence = (summary.get("confidence") or default_confidence).upper()
    atr_pct = summary.get("daily_atr_pct")
    vol_size = vol_targeted_size(confidence, atr_pct if isinstance(atr_pct, (int, float)) else None)
    summary["suggested_position_pct"] = vol_size
    # Stop-aware size only when both entry and stop are numeric. The
    # AI's free-form strings are parsed by the existing risk_reward
    # module; we reuse that helper to keep the parsing consistent.
    from stock_analyzer.risk_reward import parse_price_string

    entry = parse_price_string(summary.get("current_price"))
    stop = parse_price_string(summary.get("stop_loss"))
    stop_size = stop_aware_size(entry, stop, summary.get("prediction") or "UP")
    if stop_size is not None:
        # Take the more conservative (smaller) of the two so we
        # never over-allocate when one method is permissive.
        if vol_size is not None:
            summary["suggested_position_pct"] = min(vol_size, stop_size)
        else:
            summary["suggested_position_pct"] = stop_size
        summary["stop_aware_position_pct"] = stop_size
