from __future__ import annotations

from datetime import date

from stock_analyzer.tdnet_fetcher import (
    Disclosure,
    _parse_rows,
    fetch_tdnet_today,
    format_disclosures_for_summary,
    format_urgent_summary,
)

# Minimal TDnet HTML snippet — two disclosure rows, header preserved
# so the parser exercises the "7-cell sliding window" path. Tags lifted
# verbatim from the live page format.
_SAMPLE_HTML = """
<html><body>
<table>
<tr><td>時刻</td><td>コード</td><td>会社名</td><td>表題</td><td>XBRL</td><td>上場取引所</td><td>更新履歴</td></tr>
<tr>
  <td>13:40</td>
  <td>59840</td>
  <td>兼房</td>
  <td><a href="140120260511522766.pdf">2026年３月期 決算短信〔日本基準〕（連結）</a></td>
  <td><a href="081220260511522766.zip">XBRL</a></td>
  <td>東名</td>
  <td></td>
</tr>
<tr>
  <td>15:00</td>
  <td>72030</td>
  <td>トヨタ自動車</td>
  <td><a href="140120260511500001.pdf">業績予想の上方修正に関するお知らせ</a></td>
  <td></td>
  <td>東名</td>
  <td></td>
</tr>
</table>
</body></html>
"""


def test_parse_rows_extracts_disclosures_with_4digit_ticker() -> None:
    """TDnet's 5-digit codes (59840 / 72030) must be truncated to the
    standard TSE 4-digit format (.T suffix) so they join our existing
    holdings / candidate dicts."""
    rows = _parse_rows(_SAMPLE_HTML)
    assert len(rows) == 2
    tickers = [r.ticker for r in rows]
    assert "5984.T" in tickers
    assert "7203.T" in tickers
    # Code5 preserved verbatim for debug
    assert rows[0].code5 == "59840"


def test_parse_rows_captures_pdf_url() -> None:
    """The title cell holds a relative PDF link; the parser must
    resolve it to an absolute URL so the AI / Slack rendering can
    cite the source."""
    rows = _parse_rows(_SAMPLE_HTML)
    pdf_url = rows[0].pdf_url
    assert pdf_url.startswith("https://www.release.tdnet.info/inbs/")
    assert pdf_url.endswith(".pdf")


def test_parse_rows_keeps_title_japanese_intact() -> None:
    """Multi-byte JP titles must survive — earlier scrapers stripped
    accidentally. Pin a representative title."""
    rows = _parse_rows(_SAMPLE_HTML)
    titles = [r.title for r in rows]
    assert "決算短信" in titles[0]
    assert "上方修正" in titles[1]


def test_parse_rows_returns_empty_for_no_disclosures() -> None:
    """A page with header-only / no real disclosure rows returns []."""
    rows = _parse_rows("<html><body><table><tr><td>foo</td></tr></table></body></html>")
    assert rows == []


def test_format_disclosures_for_summary_renders_lines() -> None:
    """The per-stock summary line must be multi-disclosure-friendly:
    header + one line per disclosure with time + truncated title."""
    items = [
        Disclosure("13:40", "59840", "5984.T", "兼房", "2026年３月期 決算短信", "https://example.com/x.pdf"),
        Disclosure("15:00", "59840", "5984.T", "兼房", "業績予想の上方修正", "https://example.com/y.pdf"),
    ]
    text = format_disclosures_for_summary(items)
    assert "本日適時開示" in text
    assert "13:40" in text
    assert "15:00" in text
    assert "決算短信" in text
    assert "上方修正" in text


def test_format_disclosures_empty_when_no_items() -> None:
    """No filings → empty string so the prompt renderer's
    ``if s.get('tdnet_disclosures_text')`` skip works."""
    assert format_disclosures_for_summary([]) == ""


def test_format_urgent_summary_matches_canonical_categories() -> None:
    """Urgent block is filtered against the canonical JP equity
    disclosure categories. A normal 決算短信 doesn't qualify but
    business-forecast revisions and TOB filings do."""
    by_ticker = {
        "5984.T": [
            Disclosure("13:40", "59840", "5984.T", "兼房", "決算短信", "url1"),
        ],
        "7203.T": [
            Disclosure("15:00", "72030", "7203.T", "トヨタ", "業績予想の上方修正", "url2"),
        ],
        "9999.T": [
            Disclosure("10:00", "99990", "9999.T", "Acme", "公開買付に関するお知らせ", "url3"),
        ],
    }
    urgent = format_urgent_summary(by_ticker)
    cats = [u["category"] for u in urgent]
    tickers = [u["ticker"] for u in urgent]
    # 上方修正 (mapped to '業績予想' kw match) and 公開買付 surface;
    # generic 決算短信 doesn't.
    assert "業績予想" in cats or "上方修正" in cats
    assert "公開買付" in cats
    assert "7203.T" in tickers
    assert "9999.T" in tickers
    assert "5984.T" not in tickers


def test_format_urgent_summary_dedups_one_match_per_disclosure() -> None:
    """A single disclosure shouldn't double-fire even if its title
    contains two urgent keywords (e.g. '業績予想の修正 (下方修正)' has
    both 業績予想 and 下方修正). The first-match rule keeps it to one
    entry per source disclosure."""
    by_ticker = {
        "1234.T": [
            Disclosure("09:00", "12340", "1234.T", "X Corp", "業績予想の修正（下方修正）", "url"),
        ],
    }
    urgent = format_urgent_summary(by_ticker)
    assert len(urgent) == 1
    # The match keyword is whichever appears first in the canonical list;
    # 業績予想 comes before 下方修正 → that's what tags this entry.
    assert urgent[0]["category"] == "業績予想"


def test_fetch_tdnet_today_smoke_via_monkeypatch(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """End-to-end with the network layer monkeypatched. Verifies the
    target_tickers filter actually narrows the result down to the
    requested set even when the daily list contains far more."""
    import stock_analyzer.tdnet_fetcher as tdf

    def fake_fetch_page(d: date, page: int) -> str | None:
        if page == 1:
            return _SAMPLE_HTML
        return None

    monkeypatch.setattr(tdf, "_fetch_page", fake_fetch_page)
    out = fetch_tdnet_today(target_tickers={"7203.T"}, target_date=date(2026, 5, 12))
    assert list(out.keys()) == ["7203.T"]
    assert out["7203.T"][0].title == "業績予想の上方修正に関するお知らせ"
    assert "5984.T" not in out  # filtered out by target_tickers
