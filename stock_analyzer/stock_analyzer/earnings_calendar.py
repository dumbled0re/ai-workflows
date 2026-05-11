"""Per-ticker earnings-imminence detection and prompt injection.

``calendar_context`` warns Claude that the market is *in* the annual or
mid-year earnings concentration window. That signal is true for thousands
of 3月期 stocks for five weeks, which is too coarse: a holding whose
earnings is 14 days out is in the same window as one reporting tomorrow,
yet only the second one has a 5-10% gap-risk priced for the next session.

Per-ticker earnings dates already flow through ``data_fetcher`` (via
``yfinance.Ticker.calendar``) into each summary as ``next_earnings_date``.
This module does *not* re-fetch — it reads the string back, parses it,
computes trading-days until the event, and exposes:

- a per-summary annotation (``earnings_imminent`` / ``days_until_earnings``
  / ``earnings_date_parsed``) so ``ai_analyzer`` can render a per-stock
  inline warning instead of just a date string,
- a top-of-prompt warning block listing tickers within the imminent
  threshold so the AI is forced to address them explicitly rather than
  hoping it notices the date.

yfinance can hand back *past* earnings dates (the "most recent" rather
than "next future"). Filtering those out is the main correctness job
here; without it we would inject a stale warning that pushes the AI to
avoid a stock that already reported clean.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import jpholiday

DEFAULT_IMMINENT_THRESHOLD_DAYS = 3
"""Trading days. AI's existing rule says '決算 3 営業日前以内は entry 回避'."""


@dataclass(frozen=True)
class EarningsImminence:
    """A single ticker that is within ``threshold`` trading days of earnings.

    Used both as the per-summary annotation payload and as the top-level
    warning list entry. ``trading_days_until`` is 0 when earnings is
    today, 1 when tomorrow's session reports, etc.
    """

    ticker: str
    name: str
    earnings_date: date
    trading_days_until: int


def parse_earnings_date(raw: object) -> date | None:
    """Best-effort parse of the ``next_earnings_date`` field on a summary.

    ``data_fetcher`` stores it as ``str(date(...))`` so the canonical
    shape is ``"YYYY-MM-DD"``. Accept ``date``/``datetime`` directly
    too — if upstream ever stops stringifying, we still work. Anything
    else (None, malformed strings) returns None and the ticker simply
    gets no earnings annotation.
    """
    if raw is None:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def trading_days_until(target: date, today: date) -> int | None:
    """Trading days from ``today`` (exclusive) to ``target`` (inclusive).

    Returns None when ``target`` is in the past — callers want a forward
    horizon, and yfinance routinely hands back stale "most recent"
    earnings dates that must not be surfaced as upcoming warnings.

    Returns 0 when earnings is today (still imminent: the AI shouldn't
    enter on the morning of an after-close report). Weekend or holiday
    earnings dates fall back to a calendar-day count so a Sunday print
    still counts as "tomorrow" if Monday is a trading day.
    """
    if target < today:
        return None
    if target == today:
        return 0
    days = 0
    d = today + timedelta(days=1)
    while d <= target:
        if d.weekday() < 5 and not jpholiday.is_holiday(d):
            days += 1
        d += timedelta(days=1)
    # Earnings on a weekend/holiday: target itself isn't a trading day,
    # but we should still treat it as "imminent". Counting the next
    # trading-day session as the effective event is the conservative
    # read for entry-avoidance.
    if days == 0:
        # All days in (today, target] were non-trading. Fall back to 1
        # so the warning still fires — never 0, since 0 means "today".
        return 1
    return days


def annotate_summary(
    summary: dict,
    today: date,
    threshold: int = DEFAULT_IMMINENT_THRESHOLD_DAYS,
) -> EarningsImminence | None:
    """Set imminent flags on ``summary`` in place; return the imminence record.

    Three mutations may happen on the summary dict:
    - ``earnings_date_parsed`` (date | None): parsed date or None
    - ``days_until_earnings`` (int | None): trading days from today or None
    - ``earnings_imminent`` (bool): True iff within threshold

    Returns the ``EarningsImminence`` payload when imminent, else None.
    Callers aggregating warnings collect non-None returns directly.
    """
    parsed = parse_earnings_date(summary.get("next_earnings_date"))
    summary["earnings_date_parsed"] = parsed
    if parsed is None:
        summary["days_until_earnings"] = None
        summary["earnings_imminent"] = False
        return None

    days = trading_days_until(parsed, today)
    summary["days_until_earnings"] = days
    if days is None:
        # Past date — drop the stale string so the prompt doesn't claim
        # an upcoming event that already happened.
        summary["earnings_imminent"] = False
        summary["next_earnings_date"] = None
        return None

    imminent = days <= threshold
    summary["earnings_imminent"] = imminent
    if not imminent:
        return None
    return EarningsImminence(
        ticker=str(summary.get("ticker", "")),
        name=str(summary.get("name") or summary.get("ticker", "")),
        earnings_date=parsed,
        trading_days_until=days,
    )


def collect_imminent(
    summaries: Iterable[dict],
    today: date,
    threshold: int = DEFAULT_IMMINENT_THRESHOLD_DAYS,
) -> list[EarningsImminence]:
    """Annotate each summary in place and return imminent ones, soonest first.

    Stable sort: ties on ``trading_days_until`` keep input order so the
    holdings block (which is iterated before candidates upstream) shows
    first within a day-bucket — operator-facing readability.
    """
    out: list[EarningsImminence] = []
    for s in summaries:
        rec = annotate_summary(s, today, threshold)
        if rec is not None:
            out.append(rec)
    out.sort(key=lambda r: r.trading_days_until)
    return out


def format_warnings_for_prompt(
    warnings: list[EarningsImminence],
    threshold: int = DEFAULT_IMMINENT_THRESHOLD_DAYS,
) -> str:
    """Render the top-of-prompt mandatory-avoid block. Empty when no warnings.

    Mirrors the formatting style of ``calendar_context`` / ``portfolio_risk``
    so all three "things the AI must explicitly address" blocks read with
    the same shape in the prompt stream.
    """
    if not warnings:
        return ""
    lines = [
        f"=== 決算発表 {threshold} 営業日以内の銘柄 (本日アクティブ) ===",
    ]
    for w in warnings:
        when = "本日発表" if w.trading_days_until == 0 else f"{w.trading_days_until} 営業日後"
        lines.append(f"🔴 {w.ticker} {w.name} — {w.earnings_date.isoformat()} ({when})")
    lines.append(
        "これらの銘柄は決算サプライズによる gap-up/gap-down リスクが極めて高く、"
        "テクニカル分析が無効化される。entry は回避するか、HIGH 信頼度の UP/DOWN を付与しないこと"
    )
    return "\n".join(lines)


def format_inline_for_summary(
    days_until: int | None,
    threshold: int = DEFAULT_IMMINENT_THRESHOLD_DAYS,
) -> str:
    """Per-ticker line suffix indicating imminence. Empty when not imminent.

    Returned string is appended after the date in the per-stock prompt
    block by ``ai_analyzer``. Kept here so the human-readable wording
    stays consistent with the top block when either is tuned.
    """
    if days_until is None or days_until > threshold:
        return ""
    if days_until == 0:
        return " (⚠ 本日発表 — entry 回避必須)"
    return f" (⚠ 残り {days_until} 営業日 — entry 回避必須)"
