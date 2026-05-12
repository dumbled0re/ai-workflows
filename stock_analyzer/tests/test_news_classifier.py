from __future__ import annotations

import pytest

from stock_analyzer.news_classifier import (
    aggregate_sentiment,
    classify_headline,
    classify_news_list,
    extract_urgent,
    format_for_prompt,
    score_sentiment,
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


# ---------- sentiment scoring -------------------------------------------


def test_score_sentiment_positive_terms_sum() -> None:
    """Multiple bullish terms in one headline accumulate. The
    granularity is intentionally low (±1 per term) — a single
    headline with three positives is meaningfully more bullish
    than one with one."""
    assert score_sentiment("好調な決算で増益、最高益更新") == 3
    assert score_sentiment("上方修正と増配を発表") == 2


def test_score_sentiment_negative_terms_sum() -> None:
    assert score_sentiment("下方修正、減益で赤字転落") == -3
    assert score_sentiment("急落して業績下振れ懸念") == -3


def test_score_sentiment_mixed_terms_cancel() -> None:
    """Bullish minus bearish — a headline with both '増益' and
    'リスク' nets to zero (or close)."""
    score = score_sentiment("増益だが為替リスクが懸念")
    # +1 (増益) -1 (リスク) -1 (懸念) = -1
    assert score == -1


def test_score_sentiment_neutral_headline_returns_zero() -> None:
    """A factual headline without lexicon terms scores 0 — the
    sentiment system silently passes neutral news through."""
    assert score_sentiment("代表取締役人事に関するお知らせ") == 0
    assert score_sentiment("月次売上高のお知らせ") == 0


def test_score_sentiment_empty_input_returns_zero() -> None:
    assert score_sentiment("") == 0
    assert score_sentiment(None) == 0  # type: ignore[arg-type]


def test_classify_news_list_adds_sentiment_field() -> None:
    """classify_news_list annotates each item with sentiment when
    non-zero. Items with zero sentiment don't get the field added —
    same pattern as category (truthy-check works as 'present'
    detector at the rendering layer)."""
    items = [
        {"title": "好調で増益発表"},  # +2
        {"title": "代表取締役人事のお知らせ"},  # 0
        {"title": "赤字転落"},  # -1 (赤字 + 赤字転落 are separate matches actually)
    ]
    classify_news_list(items)
    assert items[0]["sentiment"] == 2
    assert "sentiment" not in items[1]  # neutral → no field
    assert items[2]["sentiment"] < 0  # negative


def test_aggregate_sentiment_summarises_list() -> None:
    """For a 5-headline list with mixed sentiment, the aggregate
    counts positive/negative/total and the net signed sum."""
    items = [
        {"title": "好調", "sentiment": 1},
        {"title": "増益", "sentiment": 1},
        {"title": "悪材料", "sentiment": -1},
        {"title": "下方修正", "sentiment": -1},
        {"title": "通常開示"},  # no sentiment field
    ]
    agg = aggregate_sentiment(items)
    assert agg["count"] == 4
    assert agg["net_sentiment"] == 0
    assert agg["positive_count"] == 2
    assert agg["negative_count"] == 2


def test_aggregate_sentiment_empty_list() -> None:
    agg = aggregate_sentiment([])
    assert agg["count"] == 0
    assert agg["net_sentiment"] == 0


def test_format_for_prompt_includes_sentiment_tag() -> None:
    """The rendered prompt block shows a [sent=+N] tag inline so
    the AI sees per-headline sentiment without needing to re-classify."""
    items = [{"title": "好調な決算", "sentiment": 1}]
    text = format_for_prompt(items)
    assert "[sent=+1]" in text
