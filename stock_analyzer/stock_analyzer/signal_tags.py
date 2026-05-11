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
