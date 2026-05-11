"""Deterministic risk/reward parsing for free-form AI price strings.

The first-pass AI emits ``entry_price`` / ``stop_loss`` / ``target_price``
as Japanese free-form strings: "1100", "1100円", "1100 (8%)", "1100〜
1150" — anything readable to a human trader. Asking the critic AI to
re-parse those for the risk_reward rubric item turns a deterministic
arithmetic problem into a language-model task, which means flaky
answers. Worse, ``portfolio_risk`` can't enforce a minimum R/R hard
rule on un-parsed strings.

This module owns the parse: extract the first numeric token from any
of the AI's price formats, ignoring 円 / % / range separators / commas,
then compute R/R correctly per direction (UP vs DOWN). The result
flows two places:

1. Into the critic prompt as a pre-computed ``risk_reward_ratio``
   field on each pick, so the critic stops re-deriving it.
2. Into a new ``portfolio_risk.check_risk_reward`` violation that
   fires when the ratio is below an acceptable threshold (default
   1.5) — deterministic, doesn't depend on prompt compliance.

A failure to parse (missing field, ambiguous string, target on the
wrong side of entry) returns ``None`` and the downstream checks
silently skip — being silent on unparseable data is the right
fail-mode: we shouldn't flag a pick just because the AI's string was
unusual when we have no idea whether it's actually risky.
"""

from __future__ import annotations

import re

# A trade with R/R < this is structurally bad: even at 50% accuracy the
# trade is break-even. Default chosen lenient so we only flag the
# genuinely bad ones. Investment_rules.json's stop_loss policy and
# critic rubric both target >=2.0 for HIGH-quality picks; the
# portfolio-level deterministic floor is one notch below that to
# avoid double-flagging the merely-mediocre ones.
DEFAULT_MIN_RATIO = 1.5

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def parse_price_string(raw: object) -> float | None:
    """Best-effort numeric extraction from an AI-authored price string.

    Handles:
    - plain numbers (``"1000"`` / ``"1000.5"``)
    - numeric types (``int`` / ``float``)
    - currency suffix (``"1000円"``)
    - parenthesised commentary (``"1100 (8%)"``)
    - thousand separators (``"1,000"``)
    - range notation (``"1100-1150"``, ``"1100〜1150"`` — first value wins)
    - negative numbers (``"-50円"`` — unusual but stays well-defined)

    Returns ``None`` for missing / unparseable inputs. The function is
    intentionally tolerant: invalid strings just become "unknown
    value", they don't crash the cron.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        # bool is a subclass of int; treat True/False as malformed for
        # prices rather than silently returning 1.0 / 0.0.
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, str):
        return None
    s = raw.replace(",", "").strip()
    if not s:
        return None
    m = _NUMBER_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None


def compute_risk_reward(
    entry: float | None,
    stop: float | None,
    target: float | None,
    direction: str = "UP",
) -> float | None:
    """Risk/reward ratio for a long or short trade.

    For an UP trade: reward = target - entry, risk = entry - stop.
    For a DOWN trade: reward = entry - target, risk = stop - entry.

    Returns ``None`` when any input is missing, the direction is
    unrecognised, or the stop is on the wrong side of entry (which
    would imply a negative risk — a structurally malformed trade
    spec, not a low-quality one, so we want it to surface as
    "unknown" rather than artificially-large).
    """
    if entry is None or stop is None or target is None:
        return None
    if direction == "UP":
        reward = target - entry
        risk = entry - stop
    elif direction == "DOWN":
        reward = entry - target
        risk = stop - entry
    else:
        return None
    if risk <= 0:
        return None
    if reward <= 0:
        # Target on the wrong side of entry too: an inverted setup.
        # Returning 0.0 (not None) tells callers "we got a value, it's
        # just bad" so the portfolio_risk check flags it loudly rather
        # than silently skipping.
        return 0.0
    return reward / risk


def compute_for_pick(pick: dict) -> float | None:
    """Convenience wrapper: parse a pick's price strings and compute R/R.

    Reads ``entry_price`` / ``stop_loss`` / ``target_price`` /
    ``prediction``; returns ``None`` when the R/R cannot be derived
    (typical for holdings picks, which don't carry a target).
    """
    entry = parse_price_string(pick.get("entry_price"))
    stop = parse_price_string(pick.get("stop_loss"))
    target = parse_price_string(pick.get("target_price"))
    direction = str(pick.get("prediction", "UP"))
    return compute_risk_reward(entry, stop, target, direction)


def annotate_pick(pick: dict) -> None:
    """Set ``risk_reward_ratio`` on the pick in place.

    Convenience for callers (critic prompt builder, portfolio_risk)
    that want the numeric R/R alongside the original strings. ``None``
    is stored explicitly so a downstream ``"risk_reward_ratio" not in
    pick`` check never silently passes; rendering layers should
    interpret ``None`` as "not computable".
    """
    pick["risk_reward_ratio"] = compute_for_pick(pick)
