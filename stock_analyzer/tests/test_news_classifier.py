from __future__ import annotations

import pytest

from stock_analyzer.news_classifier import (
    classify_headline,
    classify_news_list,
    extract_urgent,
    format_for_prompt,
)


@pytest.mark.parametrize(
    "title,expected_category,expected_severity,expected_direction",
    [
        ("TOB成立、X社が完全子会社化を発表", "TOB", "urgent", "bullish"),
        ("公開買付けを実施へ", "TOB", "urgent", "bullish"),
        ("業績予想の上方修正を発表", "業績修正", "urgent", "bullish"),
        ("通期業績予想の下方修正", "業績修正", "urgent", "bearish"),
        ("自己株式取得枠の設定を決議", "自己株取得", "urgent", "bullish"),
        ("自社株買いを再開", "自己株取得", "urgent", "bullish"),
        ("株式分割 1:3 を実施", "株式分割", "watch", "bullish"),
        ("大量保有報告書の提出を確認", "大量保有", "urgent", ""),
        ("X社とのM&Aで基本合意", "M&A", "urgent", ""),
        ("経営統合に向けた協議開始", "M&A", "urgent", ""),
        ("第三者割当増資による資金調達", "増資", "urgent", "bearish"),
        ("公募増資の実施を決定", "増資", "urgent", "bearish"),
        ("配当予想の上方修正を発表", "業績修正", "urgent", "bullish"),
        ("年間配当の増配を発表", "配当", "watch", "bullish"),
        ("業績悪化により減配を決定", "配当", "watch", "bearish"),
        ("無配転落、再開時期未定", "配当", "watch", "bearish"),
        ("株主優待新設のお知らせ", "株主優待", "watch", "bullish"),
    ],
)
def test_classify_headline_recognises_canonical_categories(
    title: str, expected_category: str, expected_severity: str, expected_direction: str
) -> None:
    """Pin the canonical TDnet-style categories so a keyword tweak
    doesn't silently lose a class. The set covers every category the
    JP-equity research community treats as material on disclosure
    day."""
    result = classify_headline(title)
    assert result.category == expected_category
    assert result.severity == expected_severity
    assert result.direction_hint == expected_direction


def test_classify_headline_unmatched_returns_empty() -> None:
    """Background headlines (industry op-eds, generic moves) get no
    classification and the caller treats absence as 'no urgent
    bucket'."""
    result = classify_headline("業界の景気動向について")
    assert result.category == ""
    assert result.severity == ""


def test_classify_headline_empty_string_safe() -> None:
    """A blank title must not crash the classifier — common in
    headline-fetch failure cases."""
    result = classify_headline("")
    assert result.category == ""


def test_downward_revision_checked_before_generic_revision() -> None:
    """Keyword ordering matters: 下方修正 must be matched before the
    generic 業績予想の修正 row so the bearish direction survives.
    Pin this to guard against future ordering refactors."""
    result = classify_headline("業績予想の修正(下方修正)を発表")
    assert result.direction_hint == "bearish"


def test_classify_news_list_mutates_items_in_place() -> None:
    """The list-wrapper annotates each item in place AND returns the
    list — pin both behaviours so callers can either mutate or
    chain."""
    items = [
        {"title": "TOB成立"},
        {"title": "経常的な月次データの開示"},
        {"title": "自己株式取得を発表"},
    ]
    returned = classify_news_list(items)
    assert returned is items
    assert items[0]["category"] == "TOB"
    assert items[0]["severity"] == "urgent"
    # Background headlines untouched — no spurious empty fields.
    assert "category" not in items[1]
    assert items[2]["category"] == "自己株取得"


def test_extract_urgent_filters_to_severity_urgent_only() -> None:
    """Watch-level items (株式分割 / 配当 / 株主優待) shouldn't bleed
    into the urgent extraction — the top-of-prompt urgent block must
    stay focused on disclosures that gap the stock."""
    items = [
        {"title": "TOB成立", "category": "TOB", "severity": "urgent"},
        {"title": "株式分割", "category": "株式分割", "severity": "watch"},
        {"title": "雑記事", "title2": "no class"},
    ]
    urgent = extract_urgent(items)
    assert len(urgent) == 1
    assert urgent[0]["category"] == "TOB"


def test_format_for_prompt_renders_urgent_with_category_prefix() -> None:
    """Urgent items must show the category bracket in the rendered
    text so the AI parses category alongside the headline. The
    direction hint is also surfaced when present."""
    items = [
        {"title": "下方修正発表", "category": "業績修正", "severity": "urgent", "direction_hint": "bearish"},
        {"title": "経常データ"},
    ]
    text = format_for_prompt(items)
    assert "[業績修正(bearish)] 下方修正発表" in text
    # Background headline still rendered (in a separate group).
    assert "経常データ" in text


def test_format_for_prompt_empty_input_returns_empty_string() -> None:
    assert format_for_prompt([]) == ""


def test_format_for_prompt_handles_missing_title_field() -> None:
    """A malformed item without a ``title`` key should not crash and
    should not appear in the output."""
    items = [{"category": "TOB", "severity": "urgent"}]
    text = format_for_prompt(items)
    assert text == ""
