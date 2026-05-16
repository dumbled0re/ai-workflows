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


def test_sugutama_js_placeholder_returns_none() -> None:
    """sugutama mypage は server-side では ``------`` placeholder のみで JS render。

    DEFAULT_BALANCE_PATTERNS を当てると embedded ``<script>`` 内の "class=...point..."
    風文字列にノイズマッチして年号 (e.g. 2026) を拾うバグがあった (2026-05-16)。
    sugutama 専用の strict pattern では placeholder のため None を返すことを確認。
    """
    from point_sites.adapters.sugutama import _SUGUTAMA_BALANCE_PATTERNS

    # 実際の sugutama mypage HTML の構造を再現
    html = """
    <div class="mile add_mile js-user_point">------</div>
    <script>
      // ノイズ: copyright や config 等で 2026 が現れがち
      var meta = {copyright: 2026, class: "point-config"};
    </script>
    <div class="some-point-class">2026</div>
    """
    assert parse_balance(html, _SUGUTAMA_BALANCE_PATTERNS) is None


def test_sugutama_pattern_matches_when_server_renders() -> None:
    """将来 sugutama が server-side render に切り替わった場合は拾えること。"""
    from point_sites.adapters.sugutama import _SUGUTAMA_BALANCE_PATTERNS

    html = '<div class="mile add_mile js-user_point">1,234</div>'
    assert parse_balance(html, _SUGUTAMA_BALANCE_PATTERNS) == 1234
