"""Tests for point_sites.balance.

We can't hit the real mypage, so tests use synthetic HTML that exercises
each parser pattern. The list of variants doubles as a contract: if Moppy
ever ships a redesign, the failing test makes the breakage obvious.
"""

from point_sites.common.balance import parse_balance


def test_data_attribute() -> None:
    html = '<div data-points="1234" class="balance-widget"></div>'
    assert parse_balance(html) == 1234


def test_data_attribute_with_thousand_separator() -> None:
    html = "<div data-balance='12,345'></div>"
    assert parse_balance(html) == 12345


def test_japanese_label_with_p_unit() -> None:
    html = """
    <p>保有ポイント
        <span class="num">5,678</span> P
    </p>
    """
    assert parse_balance(html) == 5678


def test_japanese_label_with_full_width_p() -> None:
    html = "<p>保有コイン: 42 Ｐ</p>"
    assert parse_balance(html) == 42


def test_genzai_no_point() -> None:
    html = "<div>現在のポイント： 999</div>"
    assert parse_balance(html) == 999


def test_shoji_point() -> None:
    html = "<span>所持ポイント 100 P</span>"
    assert parse_balance(html) == 100


def test_class_with_point_keyword() -> None:
    html = '<div class="header-point-balance">7777</div>'
    assert parse_balance(html) == 7777


def test_returns_none_when_no_pattern_matches() -> None:
    html = "<html><body>Welcome to the site</body></html>"
    assert parse_balance(html) is None


def test_returns_none_for_empty_html() -> None:
    assert parse_balance("") is None


def test_first_pattern_wins() -> None:
    """When multiple patterns could match, the most-specific one (data-*) wins."""
    html = """
    <div data-points="1000"></div>
    <p>保有ポイント 9999 P</p>
    """
    assert parse_balance(html) == 1000
