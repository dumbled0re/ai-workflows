from __future__ import annotations

from datetime import date, datetime

from stock_analyzer.earnings_calendar import (
    EarningsImminence,
    annotate_summary,
    collect_imminent,
    format_inline_for_summary,
    format_warnings_for_prompt,
    parse_earnings_date,
    trading_days_until,
)


def test_parse_earnings_date_handles_iso_string():
    assert parse_earnings_date("2026-05-13") == date(2026, 5, 13)


def test_parse_earnings_date_passthrough_for_date_object():
    assert parse_earnings_date(date(2026, 5, 13)) == date(2026, 5, 13)


def test_parse_earnings_date_strips_datetime_to_date():
    assert parse_earnings_date(datetime(2026, 5, 13, 15, 0)) == date(2026, 5, 13)


def test_parse_earnings_date_returns_none_for_garbage():
    # None / empty / malformed / wrong-type — all collapse to None so
    # the ticker is silently skipped rather than crashing the run.
    for bad in (None, "", "   ", "not-a-date", 12345, ["2026-05-13"]):
        assert parse_earnings_date(bad) is None


def test_trading_days_until_skips_weekend():
    # 2026-05-11 is a Monday. The following Friday 2026-05-15 is 4
    # trading days away (Tue/Wed/Thu/Fri), no weekend in between.
    today = date(2026, 5, 11)
    target = date(2026, 5, 15)
    assert trading_days_until(target, today) == 4


def test_trading_days_until_returns_zero_for_today():
    # An earnings date that *is* today still counts as imminent — the
    # caller treats 0 as "report happens during today's session".
    today = date(2026, 5, 13)
    assert trading_days_until(today, today) == 0


def test_trading_days_until_returns_none_for_past():
    # yfinance hands back the most-recent earnings date, which is
    # often in the past. Past must return None so we don't surface a
    # stale "upcoming earnings" warning.
    assert trading_days_until(date(2026, 4, 24), date(2026, 5, 11)) is None


def test_trading_days_until_skips_japanese_holiday():
    # 2026-05-04 / 05-05 / 05-06 are GW holidays (みどりの日 / こども
    # の日 / 振替休日). From Fri 2026-05-01 (today) to Thu 2026-05-07
    # (target), the only trading day between is Thu 5/7 itself = 1.
    today = date(2026, 5, 1)
    target = date(2026, 5, 7)
    assert trading_days_until(target, today) == 1


def test_trading_days_until_falls_back_to_one_for_weekend_target():
    # Earnings printed on a Saturday should still warn ("Monday is
    # effectively the event"). Without the fallback we'd report 0
    # which the caller would render as "today" — wrong day-label.
    today = date(2026, 5, 8)  # Friday
    target = date(2026, 5, 9)  # Saturday
    assert trading_days_until(target, today) == 1


def test_annotate_summary_marks_imminent_within_threshold():
    summary = {"ticker": "9984.T", "name": "SoftBank Group", "next_earnings_date": "2026-05-13"}
    rec = annotate_summary(summary, today=date(2026, 5, 11))
    assert summary["earnings_imminent"] is True
    assert summary["days_until_earnings"] == 2
    assert summary["earnings_date_parsed"] == date(2026, 5, 13)
    assert rec == EarningsImminence(
        ticker="9984.T", name="SoftBank Group", earnings_date=date(2026, 5, 13), trading_days_until=2
    )


def test_annotate_summary_skips_when_outside_threshold():
    summary = {"ticker": "7203.T", "name": "Toyota", "next_earnings_date": "2026-08-06"}
    rec = annotate_summary(summary, today=date(2026, 5, 11))
    assert rec is None
    assert summary["earnings_imminent"] is False
    assert summary["days_until_earnings"] is not None and summary["days_until_earnings"] > 3


def test_annotate_summary_drops_stale_past_date():
    # yfinance can hand back the most-recent (past) earnings as
    # "Earnings Date". annotate must strip ``next_earnings_date`` so
    # the prompt doesn't tell the AI to avoid an event that's done.
    summary = {"ticker": "6861.T", "name": "Keyence", "next_earnings_date": "2026-04-24"}
    rec = annotate_summary(summary, today=date(2026, 5, 11))
    assert rec is None
    assert summary["earnings_imminent"] is False
    assert summary["next_earnings_date"] is None


def test_annotate_summary_none_when_field_missing():
    summary: dict = {"ticker": "1234.T", "name": "X"}
    rec = annotate_summary(summary, today=date(2026, 5, 11))
    assert rec is None
    assert summary["earnings_imminent"] is False
    assert summary["days_until_earnings"] is None
    assert summary["earnings_date_parsed"] is None


def test_collect_imminent_sorts_soonest_first():
    today = date(2026, 5, 11)
    summaries = [
        {"ticker": "A", "name": "A", "next_earnings_date": "2026-05-14"},  # 3 days
        {"ticker": "B", "name": "B", "next_earnings_date": "2026-05-13"},  # 2 days
        {"ticker": "C", "name": "C", "next_earnings_date": "2026-08-06"},  # far future
        {"ticker": "D", "name": "D", "next_earnings_date": "2026-04-24"},  # past
        {"ticker": "E", "name": "E"},  # no date
    ]
    imminent = collect_imminent(summaries, today=today)
    assert [r.ticker for r in imminent] == ["B", "A"]
    assert imminent[0].trading_days_until == 2
    assert imminent[1].trading_days_until == 3


def test_format_warnings_for_prompt_returns_empty_when_no_warnings():
    assert format_warnings_for_prompt([]) == ""


def test_format_warnings_for_prompt_renders_block_with_each_ticker():
    warnings = [
        EarningsImminence("9984.T", "SoftBank Group", date(2026, 5, 13), 2),
        EarningsImminence("8306.T", "Mitsubishi UFJ", date(2026, 5, 15), 4),
    ]
    out = format_warnings_for_prompt(warnings, threshold=4)
    assert "決算発表 4 営業日以内" in out
    assert "9984.T" in out
    assert "SoftBank Group" in out
    assert "2026-05-13" in out
    assert "2 営業日後" in out
    assert "8306.T" in out
    # The "must address" instruction line is what forces the AI to
    # respond explicitly — guard against accidental removal.
    assert "entry は回避" in out


def test_format_warnings_for_prompt_uses_today_label_for_zero_days():
    warnings = [EarningsImminence("X", "X Corp", date(2026, 5, 11), 0)]
    out = format_warnings_for_prompt(warnings)
    assert "本日発表" in out


def test_format_inline_for_summary_empty_when_not_imminent():
    assert format_inline_for_summary(None) == ""
    assert format_inline_for_summary(10) == ""
    # Exact threshold boundary: at threshold = imminent (boundary
    # inclusive); above threshold = silent. Guard the boundary so we
    # don't accidentally over- or under-fire after a future tweak.
    assert format_inline_for_summary(3) != ""
    assert format_inline_for_summary(4) == ""


def test_format_inline_for_summary_today_uses_explicit_label():
    out = format_inline_for_summary(0)
    assert "本日発表" in out
    assert "entry 回避必須" in out


def test_format_inline_for_summary_future_includes_count():
    out = format_inline_for_summary(2)
    assert "残り 2 営業日" in out
    assert "entry 回避必須" in out
