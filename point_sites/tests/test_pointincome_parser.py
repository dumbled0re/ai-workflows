"""Tests for the pointincome parser.

Regression coverage for the 2026-05-27 fix: pointincome's actual
click-mail format places the callout (「クリックで3ptゲット」) on the
**line above** the URL, not after it. The original parser only searched
the 200 chars after the URL match, causing every legitimate click mail
to be silently dropped (no candidates → batch skipped → user sees
「抽出対象のメールはありませんでした」 despite real mails arriving).

URL path also turned out to be ``/al/click_mail_magazine.php?...``
rather than the speculated ``/click/...`` / ``/cc/...`` patterns; the
regex was extended to accept ``/al/`` paths as well.
"""

from __future__ import annotations

from point_sites.adapters.pointincome.parser import parse


def test_callout_before_url_with_al_path() -> None:
    """User fixture (2026-05-22 mail): callout on the line above the URL,
    URL path is ``/al/click_mail_magazine.php`` (new pattern).

    Both the URL regex extension (``al`` added to the allowed paths) and
    the callout-window expansion (search before the URL, not just after)
    are required for this to extract one candidate cleanly."""
    body = (
        "■━━━━━━━━━━━━━━━━━━━━■\n"
        "  マネーのミカタ　　　【 ３pt 】\n"
        "■━━━━━━━━━━━━━━━━━━━━■\n"
        "  ★承認期間 ：即追加\n"
        "  ★獲得条件 ：YouTubeクイズ回答\n"
        "  ……………………………………………………………………\n"
        "▼クリックで3ptゲット（※有効期限：05月29日まで）\n"
        "https://pointi.jp/al/click_mail_magazine.php?no=117467&hash=4b887105b7fdf72ecc95f81e13d6f27f&html=1&a=9883946846i5dvi30l11qtcsicgu\n"
    )
    candidates, anomalies = parse(body, is_html=False)
    assert len(candidates) == 1, f"expected 1 candidate, got {len(candidates)} (anomalies={anomalies})"
    assert candidates[0].estimated_points == 3
    assert "/al/click_mail_magazine.php" in str(candidates[0].url)
    assert anomalies == []


def test_callout_after_url_legacy_position() -> None:
    """Defensive: if pointincome ever moves the callout back to after the URL,
    the parser should still pick it up (window now covers both sides)."""
    body = (
        "▼マネーのミカタを試す\n"
        "https://pointi.jp/al/click_mail_magazine.php?no=999&hash=abc&html=1&a=xyz\n"
        "クリックで5ptプレゼント！\n"
    )
    candidates, _anomalies = parse(body, is_html=False)
    assert len(candidates) == 1
    assert candidates[0].estimated_points == 5


def test_url_without_callout_is_anomaly_not_candidate() -> None:
    """A bare pointi.jp tracking URL with no nearby callout must not be
    promoted to a candidate (would credit phantom clicks)."""
    body = (
        "ポイントインカム TOP https://pointi.jp/\n"
        "■マイページ https://pointi.jp/my/my_page.php\n"
        # The /al/ URL appears with no callout in either direction within 200 chars.
        + ("...filler... " * 30)
        + "\nhttps://pointi.jp/al/click_mail_magazine.php?no=1&hash=x&html=1&a=y\n"
        + ("...filler... " * 30)
    )
    candidates, anomalies = parse(body, is_html=False)
    assert candidates == []
    # The unconfirmed URL should be reported as an anomaly so main.py logs it.
    assert any("url_without_callout" in a for a in anomalies)


def test_excluded_paths_dropped() -> None:
    """login/logout/etc URLs must not be extracted even if a callout text
    happens to sit nearby."""
    body = "クリックで3ptゲット\nhttps://pointi.jp/login?redir=/al/x\n"
    candidates, _ = parse(body, is_html=False)
    assert candidates == []


def test_sns_campaign_mail_with_only_footer_pointi_urls_is_clean() -> None:
    """2026-05-28 user fixture (X 引用ポスト & リプライキャンペーン): click-mail
    ではなく Google Forms 経由の SNS 投稿必須キャンペーン。``forms.gle`` URL
    が本文の核で、pointi.jp の URL は footer (``/my/my_page.php`` 等) のみ。

    旧 parser (``/(al|click|cc|access|c)/``) は footer の ``/my/`` を URL
    match して callout が無いので anomaly を発火していた。narrow regex
    (``/al/click_mail_magazine.php`` のみ) + EXCLUSION 強化 (``/my`` 追加)
    で、anomaly 0 / candidate 0 のクリーンな出力になる事を保証する。
    """
    body = (
        "◆━━━━━━━━━━━━◆\n"
        "  X引用ポスト&リプライキャンペーン\n"
        "◆━━━━━━━━━━━━◆\n"
        "参加者の中から先着1,500名様に1,000pt(100円分）プレゼント！\n"
        "▼キャンペーンへの参加はこちら\n"
        "https://forms.gle/rU8QgdXE6pVLH8M8A\n"
        "※こちらのキャンペーンはSNS(X/TwitterやFacebook、Instagram、ブログ等)\n"
        "　での拡散は禁止となります。\n"
        "※クリックポイントの付与はございません。\n"
        "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "┃■ポイントインカム TOP　https://pointi.jp/\n"
        "┃■マイページ　　　　　　https://pointi.jp/my/my_page.php\n"
        "┃■よくある質問　　　　　https://pointi.jp/help/\n"
        "┃■お問い合わせ　　　　　https://pointi.jp/contactus/form.php\n"
        "┃■メルマガ設定の変更　　https://pointi.jp/my/my_profile.php\n"
        "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )
    candidates, anomalies = parse(body, is_html=False)
    assert candidates == [], "SNS-only campaign mails must not yield click candidates"
    assert anomalies == [], "footer URLs must not trigger url_without_callout anomalies"
