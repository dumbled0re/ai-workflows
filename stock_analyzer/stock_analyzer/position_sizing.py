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

_KELLY_FRACTION = 0.25  # Quarter-Kelly. Full-Kelly is mathematically optimal
# for long-run geometric growth but catastrophically volatile — most
# professional desks run quarter or half. Quarter is the conservative
# choice given that win_rate and avg-return estimates are themselves
# noisy on ~100-sample history.
_KELLY_CAP_PCT = 5.0  # Even when the math says 20%+, never allocate more
# than 5% to one pick. Single-stock idiosyncratic risk dominates above
# that point.
_KELLY_MIN_SAMPLES = 8  # Below this resolved-trade count per bucket,
# Kelly estimates are too noisy — fall back to the heuristic base.


def compute_kelly_size(
    win_rate: float,
    avg_win_pct: float,
    avg_loss_pct_abs: float,
    fraction: float = _KELLY_FRACTION,
    cap: float = _KELLY_CAP_PCT,
) -> float | None:
    """Quarter-Kelly position size from empirical win rate + R/R.

    Kelly formula: f* = (b * p - q) / b, where p = win prob, q = 1-p,
    b = avg_win / avg_loss (R:R ratio in $). Returns the fraction-
    scaled, percentage-units, capped result. None when inputs are
    degenerate (zero loss size, negative win rate).

    Why quarter-Kelly: full-Kelly is the theoretical growth-optimal
    fraction but has catastrophic drawdown profiles when win-rate
    estimates are wrong (which they always are with sample <1000).
    Quarter-Kelly trades off ~5% of growth for half the volatility.
    """
    if avg_loss_pct_abs <= 0 or win_rate <= 0 or win_rate >= 1:
        return None
    if avg_win_pct <= 0:
        return None
    b = avg_win_pct / avg_loss_pct_abs
    q = 1.0 - win_rate
    full_kelly = (b * win_rate - q) / b
    if full_kelly <= 0:
        return 0.0  # negative-EV bucket — recommend NO position
    scaled = full_kelly * fraction * 100  # convert to % units
    return round(min(scaled, cap), 2)


def derive_kelly_bases(history: dict) -> dict[str, float] | None:
    """Compute per-confidence Kelly sizes from predictions_history.

    Returns ``{"HIGH": x, "MEDIUM": y, "LOW": z}`` where each value is
    the quarter-Kelly position size in percent of capital. A bucket
    with fewer than ``_KELLY_MIN_SAMPLES`` resolved trades is omitted;
    the caller falls back to the heuristic base for that confidence.

    Returns None when nothing in history is usable — also handled
    by falling back to the heuristic bases.
    """
    predictions = history.get("predictions") or []
    resolved = [p for p in predictions if p.get("status") in ("win", "loss")]
    if not resolved:
        return None
    bases: dict[str, float] = {}
    for conf in ("HIGH", "MEDIUM", "LOW"):
        bucket = [p for p in resolved if (p.get("confidence") or "").upper() == conf]
        if len(bucket) < _KELLY_MIN_SAMPLES:
            continue
        wins_in_bucket = [p for p in bucket if p["status"] == "win"]
        losses_in_bucket = [p for p in bucket if p["status"] == "loss"]
        if not wins_in_bucket or not losses_in_bucket:
            # Need at least one of each to compute b.
            continue
        win_rate = len(wins_in_bucket) / len(bucket)
        # Direction-aware returns so DOWN-wins count as positive gain.
        win_returns_dir = []
        loss_returns_dir = []
        for p in wins_in_bucket:
            r = p.get("actual_return_pct")
            if r is None:
                continue
            dir_r = float(r) if (p.get("prediction") or "").upper() == "UP" else -float(r)
            win_returns_dir.append(dir_r)
        for p in losses_in_bucket:
            r = p.get("actual_return_pct")
            if r is None:
                continue
            dir_r = float(r) if (p.get("prediction") or "").upper() == "UP" else -float(r)
            loss_returns_dir.append(dir_r)
        if not win_returns_dir or not loss_returns_dir:
            continue
        avg_win = sum(win_returns_dir) / len(win_returns_dir)
        avg_loss_abs = abs(sum(loss_returns_dir) / len(loss_returns_dir))
        kelly = compute_kelly_size(win_rate, avg_win, avg_loss_abs)
        if kelly is not None:
            bases[conf] = kelly
    return bases or None


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


def annotate_summary(
    summary: dict,
    default_confidence: str = "MEDIUM",
    kelly_bases: dict[str, float] | None = None,
) -> None:
    """Mutate the summary with ``suggested_position_pct``.

    Computes three candidate sizes and picks the smallest (most
    conservative):
    1. **Kelly** (when kelly_bases provided): empirical quarter-Kelly
       derived from the bucket's historical win-rate × R:R. This is
       the size a professional desk would compute from track record.
    2. **Vol-targeted** (always): heuristic confidence-base scaled by
       inverse daily ATR. Falls back to the confidence base when no
       ATR data.
    3. **Stop-aware** (when AI specified stop_loss): 1%-account-risk
       rule combined with distance to stop.

    Taking the minimum across all available guards against any
    single method's over-estimation. ``kelly_bases`` is typically
    derived once per cron via ``derive_kelly_bases(history)``.
    """
    confidence = (summary.get("confidence") or default_confidence).upper()
    atr_pct = summary.get("daily_atr_pct")
    vol_size = vol_targeted_size(confidence, atr_pct if isinstance(atr_pct, (int, float)) else None)

    # Kelly path: use the empirical base when available, otherwise the
    # heuristic base flowed through vol_targeted. Kelly base overrides
    # the heuristic *only when present* — never inflates if the
    # heuristic is more conservative.
    kelly_size: float | None = None
    if kelly_bases and confidence in kelly_bases:
        kelly_base = kelly_bases[confidence]
        # Same inverse-vol scaling as vol_targeted_size, with the
        # Kelly base substituted in.
        if isinstance(atr_pct, (int, float)) and atr_pct > 0:
            kelly_size = round(min(kelly_base, kelly_base * (_TARGET_DAILY_VOL_PCT / atr_pct)), 2)
        else:
            kelly_size = kelly_base
        summary["kelly_base_pct"] = kelly_base
        summary["kelly_position_pct"] = kelly_size

    candidates: list[float] = [c for c in (vol_size, kelly_size) if c is not None]
    summary["suggested_position_pct"] = min(candidates) if candidates else vol_size
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
