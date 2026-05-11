"""Post-screening signal tags written into ``signal_components``.

The primary screening signals live in ``technical_indicators.compute_
screening_score`` because they affect the score that ranks the
universe. A second category of signals — like margin-balance pressure
— is only available *after* screening (margin_data is fetched only
for the top-20 candidates + holdings, not the whole universe). Wiring
those into the pre-screen would mean an extra two-thousand-request
fetch every cron.

Instead, this module mutates the ``signal_components`` dict on each
summary in place after the data is attached. The fingerprint is still
saved with the prediction so ``compute_signal_efficacy`` can answer
"does margin_low_pressure correlate with wins?" in the weekly review,
but the screening score is unaffected — these are diagnostic-only
signals, not ranking inputs.
"""

from __future__ import annotations

# 信用倍率 (margin_ratio = 信用買残 / 信用売残) interpretation:
#   < 1.0  → 売り長 (sellers > buyers) — short-squeeze setup possible
#   1.0-1.5 → balanced, slight buyer-side
#   > 3.0  → overhang (overhead supply from forced unwinds)
#   > 5.0  → high-risk overhang (investment_rules avoid_entry threshold)
_LOW_PRESSURE_THRESHOLD = 1.5
_OVERHANG_THRESHOLD = 5.0


_EARNINGS_GROWTH_THRESHOLD = 10.0  # percent YoY
_EARNINGS_DECLINE_THRESHOLD = -5.0  # percent YoY
_SURPRISE_BEAT_THRESHOLD = 5.0  # percent above estimate
_SURPRISE_MISS_THRESHOLD = -5.0  # percent below estimate


def annotate_earnings_surprise(summary: dict) -> None:
    """Mutate ``summary['signal_components']`` with PEAD-derived tags.

    Post-Earnings-Announcement-Drift is among the most-documented
    predictive signals in academic finance: a stock beating estimates
    by >= 5% tends to drift up for weeks afterwards, scaling with the
    surprise magnitude, and the effect compounds when beats are
    consecutive (a sign the company is structurally outperforming
    expectations rather than a one-off).

    Three mutually exclusive tags can fire:
    - ``earnings_beat``: latest quarter surprise >= +5%
    - ``earnings_consistent_beat``: 3+ consecutive beats (stronger)
    - ``earnings_miss``: latest quarter surprise <= -5%

    Same post-screening pattern as ``annotate_earnings_momentum``:
    affects predictions_history fingerprints (and signal_efficacy
    reports) but not the pre-screen score. Tickers without surprise
    data skip silently.
    """
    surprise = summary.get("latest_surprise_pct")
    if not isinstance(surprise, (int, float)):
        return
    components = summary.setdefault("signal_components", {})
    consecutive = summary.get("consecutive_beats") or 0
    if surprise >= _SURPRISE_BEAT_THRESHOLD:
        # Promote to consistent_beat when the streak is long — that's
        # the stronger sub-signal a serious analyst would weight more.
        if isinstance(consecutive, int) and consecutive >= 3:
            components["earnings_consistent_beat"] = True
        else:
            components["earnings_beat"] = True
    elif surprise <= _SURPRISE_MISS_THRESHOLD:
        components["earnings_miss"] = True


_ANALYST_DRIFT_UPGRADE_THRESHOLD = 5.0  # percentage points
_ANALYST_DRIFT_DOWNGRADE_THRESHOLD = -5.0


_FWD_ESTIMATE_RAISE_THRESHOLD = 5.0  # percent — quarter-on-quarter growth
_FWD_ESTIMATE_CUT_THRESHOLD = -5.0


def annotate_forward_estimates(summary: dict) -> None:
    """Mutate ``signal_components`` with forward-estimate tags.

    Looks at current-quarter and next-quarter consensus growth. When
    *both* are >= +5% the company has aggregated positive forward
    expectations (= analysts are quietly raising) which historically
    precedes the strongest forward returns. When either is <= -5%,
    fire the cut tag for symmetry.
    """
    cq = summary.get("current_q_growth_pct")
    nq = summary.get("next_q_growth_pct")
    if not isinstance(cq, (int, float)) and not isinstance(nq, (int, float)):
        return
    components = summary.setdefault("signal_components", {})
    cq_ok = isinstance(cq, (int, float)) and cq >= _FWD_ESTIMATE_RAISE_THRESHOLD
    nq_ok = isinstance(nq, (int, float)) and nq >= _FWD_ESTIMATE_RAISE_THRESHOLD
    cq_bad = isinstance(cq, (int, float)) and cq <= _FWD_ESTIMATE_CUT_THRESHOLD
    nq_bad = isinstance(nq, (int, float)) and nq <= _FWD_ESTIMATE_CUT_THRESHOLD
    if cq_ok and nq_ok:
        components["forward_estimate_raise"] = True
    elif cq_bad or nq_bad:
        components["forward_estimate_cut"] = True


def annotate_liquidity(summary: dict, max_spread_pct: float = 1.0) -> None:
    """Add a low-liquidity flag when bid-ask spread is too wide.

    Wide-spread stocks have high implicit transaction cost and are
    poor swing candidates regardless of the technical setup. The
    flag goes into ``signal_components`` so the AI can downgrade or
    drop the pick and signal_efficacy can verify the heuristic.
    """
    bid = summary.get("bid")
    ask = summary.get("ask")
    price = summary.get("current_price")
    if not isinstance(bid, (int, float)) or not isinstance(ask, (int, float)):
        return
    if not isinstance(price, (int, float)) or price <= 0:
        return
    if bid <= 0 or ask <= bid:
        return
    spread_pct = (ask - bid) / price * 100
    if spread_pct > max_spread_pct:
        components = summary.setdefault("signal_components", {})
        components["wide_spread_liquidity"] = True


def annotate_analyst_drift(summary: dict) -> None:
    """Mutate ``summary['signal_components']`` with analyst-consensus drift tags.

    Consensus drift = bullish_share(current) - bullish_share(3m ago), in
    percentage points. A 5pp+ upward drift across the 4 monthly snapshots
    means coverage shops are net-upgrading — which itself is a well-
    documented leading indicator independent of the absolute rating.

    Two mutually exclusive tags:
    - ``analyst_upgrade_drift`` when drift_pp >= +5
    - ``analyst_downgrade_drift`` when drift_pp <= -5

    Tickers without drift data (only 1 period available, or all-zero
    analyst counts) skip silently.
    """
    drift = summary.get("analyst_drift_pp")
    if not isinstance(drift, (int, float)):
        return
    components = summary.setdefault("signal_components", {})
    if drift >= _ANALYST_DRIFT_UPGRADE_THRESHOLD:
        components["analyst_upgrade_drift"] = True
    elif drift <= _ANALYST_DRIFT_DOWNGRADE_THRESHOLD:
        components["analyst_downgrade_drift"] = True


def annotate_earnings_momentum(summary: dict) -> None:
    """Mutate ``summary['signal_components']`` with YoY momentum tags.

    Two mutually exclusive tags can fire from the quarterly YoY data:
    - ``earnings_yoy_growth``: revenue or net income up ≥ 10% YoY
      (a meaningful tailwind for fundamentals-driven swings)
    - ``earnings_yoy_decline``: revenue or net income down ≥ 5% YoY
      (deteriorating fundamentals, common precursor to guidance cuts)

    The tags are post-screening (added in main.py after fetch_earnings_
    momentum_batch attaches the data) so they affect predictions_history
    fingerprints and signal_efficacy without changing the pre-screen
    score. Tickers without the YoY data are silently skipped.
    """
    rev_yoy = summary.get("revenue_yoy_pct")
    ni_yoy = summary.get("net_income_yoy_pct")
    candidates = [v for v in (rev_yoy, ni_yoy) if isinstance(v, (int, float))]
    if not candidates:
        return
    components = summary.setdefault("signal_components", {})
    # Best-case wins: a stock whose revenue OR net income is growing
    # is "growing", even if the other is flat. Symmetric for decline.
    best = max(candidates)
    worst = min(candidates)
    if best >= _EARNINGS_GROWTH_THRESHOLD:
        components["earnings_yoy_growth"] = True
    elif worst <= _EARNINGS_DECLINE_THRESHOLD:
        components["earnings_yoy_decline"] = True


def annotate_margin_signals(summary: dict) -> None:
    """Mutate ``summary['signal_components']`` with margin-based tags.

    Two mutually exclusive tags can fire:
    - ``margin_low_pressure``: ratio below 1.5 (light overhead, room
      for the stock to absorb buy demand without margin liquidation)
    - ``margin_overhang``: ratio above 5.0 (lots of margin buyers
      sitting on losses — vulnerable to forced selling)

    A summary with no ``margin_ratio`` is left untouched. The function
    is silent on the in-between zone (1.5-5.0) since that's the
    "ordinary" band where margin doesn't usefully predict anything
    on its own. ``signal_components`` is created if missing so older
    summaries built before the signal-tracking work landed don't
    crash on first annotation.
    """
    ratio = summary.get("margin_ratio")
    if ratio is None:
        return
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        return
    components = summary.setdefault("signal_components", {})
    if r < _LOW_PRESSURE_THRESHOLD:
        components["margin_low_pressure"] = True
    elif r > _OVERHANG_THRESHOLD:
        components["margin_overhang"] = True
