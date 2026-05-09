"""Tests for discover.analyze_html and classify_interaction.

Live crawling can't be tested without real cookies, so we exercise the
classification logic against synthetic HTML that mimics each interaction
shape we expect to see (simple-click page, form-post page, JS-driven
page). The point isn't to mirror real Moppy markup byte-for-byte — it's
to lock in the rules that decide which items are safe to auto-click.
"""

from point_sites.discover import analyze_html, classify_interaction


def test_classify_form_wins_over_buttons() -> None:
    assert classify_interaction(buttons=5, forms=1, js=2) == "form_post"


def test_classify_js_only_means_js_required() -> None:
    assert classify_interaction(buttons=0, forms=0, js=3) == "js_required"


def test_classify_buttons_only_means_get_click() -> None:
    assert classify_interaction(buttons=1, forms=0, js=0) == "get_click"


def test_classify_nothing_means_unknown() -> None:
    assert classify_interaction(buttons=0, forms=0, js=0) == "unknown"


def test_analyze_simple_click_page() -> None:
    html = """
    <html><head><title>クリックでコイン</title></head><body>
      <p>毎日 1 コインプレゼント</p>
      <a href="https://pc.moppy.jp/cc/c?t=abc">クリックでコイン獲得</a>
    </body></html>
    """
    report = analyze_html("https://pc.moppy.jp/coin/click/", 200, html)
    assert report.title == "クリックでコイン"
    assert "1 コイン" in " ".join(report.point_hints)
    assert "https://pc.moppy.jp/cc/c?t=abc" in report.action_buttons
    assert report.forms_count == 0
    assert report.interaction_guess == "get_click"


def test_analyze_form_post_page() -> None:
    html = """
    <html><head><title>毎日ガチャ</title></head><body>
      <form action="/gacha/spin" method="post">
        <button type="submit">ガチャを回す</button>
      </form>
      <a href="/coin/garapon/result">結果を見る</a>
    </body></html>
    """
    report = analyze_html("https://pc.moppy.jp/gacha/", 200, html)
    assert report.forms_count == 1
    # Form_post wins even though there's an action-suggestive anchor.
    assert report.interaction_guess == "form_post"


def test_analyze_js_required_page() -> None:
    html = """
    <html><head><title>スロット</title></head><body>
      <div id="slot"></div>
      <script>
        document.addEventListener('DOMContentLoaded', () => {
          fetch('/api/slot/spin', {method: 'POST'});
        });
      </script>
    </body></html>
    """
    report = analyze_html("https://pc.moppy.jp/slot/", 200, html)
    assert report.forms_count == 0
    assert "addEventListener" in report.js_keywords
    assert "fetch(" in report.js_keywords
    assert report.interaction_guess == "js_required"


def test_analyze_extracts_point_hints_dedup_and_capped() -> None:
    fragments = " ".join(["1 ポイント"] * 20)
    html = f"<html><body>{fragments}</body></html>"
    report = analyze_html("https://x.example/", 200, html)
    # Dedup → only one unique hint, even though the literal occurs 20 times
    assert report.point_hints == ["1 ポイント"]


def test_analyze_unknown_page_no_signals() -> None:
    html = "<html><body>just text</body></html>"
    report = analyze_html("https://x.example/", 200, html)
    assert report.interaction_guess == "unknown"
    assert report.action_buttons == []
    assert report.forms_count == 0
