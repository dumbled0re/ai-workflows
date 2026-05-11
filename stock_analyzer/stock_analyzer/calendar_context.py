"""Japan-equity calendar context: seasonality + earnings-season detection.

The market context already in ``market_context.py`` captures price-based
regime (bull / bear / volatility), but ignores time-of-year effects that
materially move yields on Japanese stocks:

- 本決算集中 (April 15 – May 20): Most 3月期 companies release annual
  earnings here. Surprise risk dominates technicals; ``investment_rules``
  already says "決算 3 営業日前以内は entry 回避" — the AI sees that as
  a rule, but doesn't know we're *in* that window unless told.
- 中間決算 (October 15 – November 15): Same dynamic at half-year mark.
- 配当権利確定 (~March 28 / September 28): Last trading day with rights
  draws buy-pressure; the next session opens ex-dividend (gap down).
- Year-end / Year-start (Dec 25 – Jan 8): Thin liquidity, tax-loss
  selling pressure December, new-NISA buying pressure January.
- GW (April 25 – May 6): Pre-holiday position cuts.
- Summer doldrums (July 20 – August 25): Low ADV, volatility-strategy
  ineffective.

This module turns those windows into a deterministic check fired in
``phase_prepare``; the resulting block is injected into Claude's
analysis prompt so the AI accounts for the season explicitly instead
of relying on its prior training to "remember" Japanese calendar
quirks. The check is intentionally cheap (just date arithmetic with
``jpholiday`` for trading-day alignment).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import jpholiday


@dataclass(frozen=True)
class CalendarSignal:
    """One seasonality event active for ``today``.

    ``kind`` is a short stable id (``earnings_q4`` / ``dividend_ex`` /
    ``year_end`` ...) so downstream code can pattern-match without
    parsing the human-readable ``message``.
    """

    kind: str
    severity: str  # "info" | "warning"
    message: str
    advice: str = ""
    affected_sectors: tuple[str, ...] = field(default_factory=tuple)


def detect_calendar_signals(today: date) -> list[CalendarSignal]:
    """Return all calendar signals active on ``today``.

    Multiple signals can stack (e.g. earnings season + GW approach in
    late April). Each signal independently contributes to the prompt
    block. Empty list means no notable calendar context.
    """
    signals: list[CalendarSignal] = []

    if _in_earnings_season_q4(today):
        signals.append(
            CalendarSignal(
                kind="earnings_q4",
                severity="warning",
                message=(
                    "本決算発表シーズン (3月期企業の通期決算が 4/15〜5/20 に集中)。"
                    "決算サプライズで gap-up / gap-down が発生しやすく、テクニカル分析が無効化される"
                ),
                advice=(
                    "決算発表日が確認できる銘柄は entry を回避するか、発表後の "
                    "反応を見てから入る。決算前の HIGH 信頼度 UP/DOWN は付与しない"
                ),
            )
        )

    if _in_earnings_season_q2(today):
        signals.append(
            CalendarSignal(
                kind="earnings_q2",
                severity="warning",
                message=(
                    "中間決算発表シーズン (3月期企業の上期決算が 10/15〜11/15 に集中)。"
                    "決算サプライズリスクが通常期より高い"
                ),
                advice="本決算シーズン同様、決算前の銘柄は entry 回避を優先",
            )
        )

    div_days = _days_until_dividend_ex(today)
    if div_days is not None and 0 <= div_days <= 7:
        signals.append(
            CalendarSignal(
                kind="dividend_ex",
                severity="info",
                message=(
                    f"配当権利確定日まで残り {div_days} 営業日。"
                    "権利付き最終日に向けて高配当株に買い圧力、翌営業日に配当落ち gap-down が発生"
                ),
                advice=(
                    "短期スイング: 配当落ちの gap-down を見越して権利日前の利確を検討。"
                    "保有 holdings の配当落ち分はパフォーマンス計算で「外れ」とカウントしないよう注意"
                ),
            )
        )

    if _in_year_end_window(today):
        signals.append(
            CalendarSignal(
                kind="year_end",
                severity="warning",
                message=(
                    "年末年始 (12/25〜1/8): 出来高薄、ボラ低、税金対策の年末損出し売りと "
                    "新 NISA 枠リセットの年初買いが交錯する"
                ),
                advice=(
                    "ポジションサイズを縮小。年末は損切り銘柄の安値拾い、年初は "
                    "新 NISA 買い需要を見越した大型バリュー株が有利"
                ),
            )
        )

    if _in_gw_window(today):
        signals.append(
            CalendarSignal(
                kind="golden_week",
                severity="info",
                message=(
                    "GW 接近 (4/25〜5/6): 連休前のリスク回避ポジション解消で売り圧力。"
                    "連休中の海外市場変動を受けた連休明けの gap も注意"
                ),
                advice="連休跨ぎのポジションは縮小推奨。新規 entry は連休明けまで保留も選択肢",
            )
        )

    if _in_summer_doldrums(today):
        signals.append(
            CalendarSignal(
                kind="summer_doldrums",
                severity="info",
                message=(
                    "夏枯れ相場 (7/20〜8/25): 機関投資家の夏休みで出来高低下、"
                    "値動きはレンジ寄りでブレイクアウトが起きにくい"
                ),
                advice=(
                    "モメンタム戦略の有効性低下。レンジ逆張り or 信頼度を一段下げて様子見。"
                    "出来高 spike も騙しのリスク高い"
                ),
            )
        )

    return signals


def format_signals_for_prompt(signals: list[CalendarSignal]) -> str:
    """Render signals as a prompt-injection block. Empty when no signals."""
    if not signals:
        return ""
    lines = ["=== 暦・季節要因 (本日アクティブ) ==="]
    for s in signals:
        icon = "🔴" if s.severity == "warning" else "🟡"
        lines.append(f"{icon} [{s.kind}] {s.message}")
        if s.advice:
            lines.append(f"   → 推奨対応: {s.advice}")
    lines.append("これらの暦要因を必ず銘柄選定 + 信頼度設定 + entry timing に反映してください")
    return "\n".join(lines)


# --- window predicates --------------------------------------------------------


def _in_earnings_season_q4(today: date) -> bool:
    """4/15 ≤ today ≤ 5/20 (annual earnings concentration for 3月期 firms)."""
    return _in_window(today, (4, 15), (5, 20))


def _in_earnings_season_q2(today: date) -> bool:
    """10/15 ≤ today ≤ 11/15 (mid-year earnings concentration)."""
    return _in_window(today, (10, 15), (11, 15))


def _in_year_end_window(today: date) -> bool:
    """Dec 25 – Jan 8 (the window wraps across year boundary)."""
    md = (today.month, today.day)
    return md >= (12, 25) or md <= (1, 8)


def _in_gw_window(today: date) -> bool:
    """Late-April through Golden Week (4/25 – 5/6)."""
    return _in_window(today, (4, 25), (5, 6))


def _in_summer_doldrums(today: date) -> bool:
    """7/20 – 8/25."""
    return _in_window(today, (7, 20), (8, 25))


def _in_window(today: date, start_md: tuple[int, int], end_md: tuple[int, int]) -> bool:
    md = (today.month, today.day)
    return start_md <= md <= end_md


def _days_until_dividend_ex(today: date) -> int | None:
    """Trading days until the next 3月末 or 9月末 dividend ex-date.

    Japanese fiscal-year dividends concentrate on March 31 / September
    30 record dates. The ex-dividend day is the next trading day after
    the record date. To stay within a useful warning horizon we report
    only when the next record date is within ~10 trading days.
    """
    candidates: list[date] = []
    for month, day in ((3, 31), (9, 30)):
        year = today.year
        d = _last_trading_day_on_or_before(date(year, month, day))
        if d < today:
            d = _last_trading_day_on_or_before(date(year + (1 if month == 9 else 0), month, day))
        candidates.append(d)
    next_record = min(candidates)
    delta = _trading_days_between(today, next_record)
    return delta if delta is not None and delta <= 10 else None


def _last_trading_day_on_or_before(d: date) -> date:
    """Walk backward off weekends / national holidays."""
    while d.weekday() >= 5 or jpholiday.is_holiday(d):
        d -= timedelta(days=1)
    return d


def _trading_days_between(start: date, end: date) -> int | None:
    """Count trading days from ``start`` (exclusive) to ``end`` (inclusive).

    Returns ``None`` when ``end`` is in the past — the caller wants
    "days until" so a negative delta is meaningless here.
    """
    if end < start:
        return None
    days = 0
    d = start + timedelta(days=1)
    while d <= end:
        if d.weekday() < 5 and not jpholiday.is_holiday(d):
            days += 1
        d += timedelta(days=1)
    return days


def detect_for_now(now: datetime | None = None) -> list[CalendarSignal]:
    """Convenience entry point — calls ``detect_calendar_signals`` with today."""
    return detect_calendar_signals((now or datetime.now()).date())
