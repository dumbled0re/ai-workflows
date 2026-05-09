"""Parser golden tests against sanitized real moppy email fixtures.

Tokens in fixtures are masked (X/A/B/C/D/E placeholders) but URL structure
and surrounding text match production.
"""

from pathlib import Path

import pytest

from point_sites.adapters.moppy.parser import (
    CALLOUT_RE,
    CLICK_COIN_URL_RE,
    parse,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_url_regex_matches_real_pattern():
    text = "https://pc.moppy.jp/cc/c?t=ABCdef123+/-_=xyz and more text"
    matches = CLICK_COIN_URL_RE.findall(text)
    assert matches == ["https://pc.moppy.jp/cc/c?t=ABCdef123+/-_=xyz"]


def test_url_regex_does_not_match_other_paths():
    text = "https://pc.moppy.jp/guide/#page3 https://pc.moppy.jp/ad/detail.php?s_id=1"
    assert CLICK_COIN_URL_RE.findall(text) == []


def test_callout_regex_extracts_coin_count():
    text = "▲5日以内に上記URLアクセスで【1コイン】GET！"
    match = CALLOUT_RE.search(text)
    assert match is not None
    assert match.group(1) == "1"


def test_callout_regex_extracts_two_digit_count():
    text = "▲上記URLアクセスで【10コイン】GET！"
    match = CALLOUT_RE.search(text)
    assert match is not None
    assert match.group(1) == "10"


def test_parse_1coin_fixture_yields_single_dedup_candidate():
    body = _load("sample_1coin_ahamo.txt")
    candidates, anomalies = parse(body)
    assert len(candidates) == 1
    assert anomalies == []
    assert candidates[0].estimated_points == 1
    url = str(candidates[0].url)
    assert url.startswith("https://pc.moppy.jp/cc/c?t=")
    assert candidates[0].extraction_reason == "whitelist_url_pattern_and_anchor"


def test_parse_5coin_fixture_yields_five_distinct_candidates():
    body = _load("sample_5coin_recommend.txt")
    candidates, anomalies = parse(body)
    assert len(candidates) == 5
    assert anomalies == []
    urls = [str(c.url) for c in candidates]
    assert len(set(urls)) == 5  # all distinct
    assert all(c.estimated_points == 1 for c in candidates)
    assert all(u.startswith("https://pc.moppy.jp/cc/c?t=") for u in urls)


def test_parse_skips_non_clickcoin_moppy_urls():
    """guide/sns/edit_mail_flg URLs are not click-coin endpoints."""
    body = _load("sample_1coin_ahamo.txt")
    candidates, _ = parse(body)
    # 1coin fixture has many non-/cc/c URLs (guide, sns, edit) — none should be candidates
    urls = [str(c.url) for c in candidates]
    assert not any("guide" in u or "edit_mail_flg" in u for u in urls)
    assert not any("youtube.com" in u or "twitter.com" in u for u in urls)


def test_parse_url_without_callout_flagged_as_anomaly():
    body = "https://pc.moppy.jp/cc/c?t=ABCDEF12345\n\n別の文章で callout なし\n"
    candidates, anomalies = parse(body)
    assert candidates == []
    assert any("url_without_callout" in a for a in anomalies)


def test_parse_empty_body():
    candidates, anomalies = parse("")
    assert candidates == []
    assert anomalies and "empty_body" in anomalies[0]


def test_parse_html_body_stripped_then_extracted():
    """Future-proofing: HTML emails should also work via tag-stripping."""
    html = """
    <html><body>
      <p><a href="https://pc.moppy.jp/cc/c?t=HTMLTESTtoken12345">click</a></p>
      <p>▲5日以内に上記URLアクセスで【1コイン】GET！</p>
    </body></html>
    """
    candidates, _ = parse(html, is_html=True)
    assert len(candidates) == 1
    assert candidates[0].estimated_points == 1


def test_parse_dedupes_same_url_appearing_twice():
    body = (
        "https://pc.moppy.jp/cc/c?t=DUPLICATEtoken\n"
        "▲5日以内に上記URLアクセスで【1コイン】GET！\n"
        "（中略）\n"
        "https://pc.moppy.jp/cc/c?t=DUPLICATEtoken\n"
        "▲5日以内に上記URLアクセスで【1コイン】GET！\n"
    )
    candidates, _ = parse(body)
    assert len(candidates) == 1


@pytest.mark.parametrize(
    "callout_variant",
    [
        "▲5日以内に上記URLアクセスで【1コイン】GET！",
        "▲明日までに上記URLアクセスで【1コイン】GET！",
        "▲上記URLアクセスで【3コイン】GET",
    ],
)
def test_parse_accepts_callout_variants(callout_variant):
    body = f"https://pc.moppy.jp/cc/c?t=VAR12345\n{callout_variant}\n"
    candidates, _ = parse(body)
    assert len(candidates) == 1
