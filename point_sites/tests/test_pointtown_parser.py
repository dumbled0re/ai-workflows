"""Tests for the pointtown parser.

Regression coverage for the 2026-05-15 fix: pointtown click-mails
routinely contain duplicate CTAs (multiple ``/mail/click?t=...&u=...``
URLs that all redirect to the same credit endpoint). The parser must
keep the first and drop the rest WITHOUT raising an anomaly, because
``main.py``'s anomaly path triggers ``continue`` and would swallow
the credit entirely (verified live on msg 4512 — a 5-coin click-mail
that was silently skipped for multiple days).
"""

from __future__ import annotations

from point_sites.adapters.pointtown.parser import parse_message


def test_duplicate_cta_keeps_first_no_anomaly() -> None:
    """The actual shape of msg 4512: two ``/mail/click`` URLs differing
    only in the ``t=`` tracking token; ``u=`` (user hash) identical."""
    body = (
        "このメールの【コイン付】URLをクリックすると、5コインが獲得できます。\n"
        "▼infoQに登録する\n"
        "【コイン付】 https://www.pointtown.com/mail/click?t=knI&u=a9c96ec1\n"
        "（infoQ の説明）\n"
        "▼infoQに登録する\n"
        "【コイン付】 https://www.pointtown.com/mail/click?t=knV&u=a9c96ec1\n"
    )
    candidates, anomalies = parse_message(body)
    assert len(candidates) == 1, "should keep only the first CTA"
    # Order in document → first match kept.
    assert str(candidates[0].url).startswith("https://www.pointtown.com/mail/click?t=knI")
    assert anomalies == [], "duplicate-CTA pattern must NOT raise an anomaly (would block click)"


def test_single_click_url_no_anomaly() -> None:
    body = "ご紹介キャンペーン参加！クリックで1コイン。\nhttps://www.pointtown.com/mail/click?t=single&u=abc123\n"
    candidates, anomalies = parse_message(body)
    assert len(candidates) == 1
    assert anomalies == []


def test_empty_body() -> None:
    candidates, anomalies = parse_message("")
    assert candidates == []
    assert "empty message body" in anomalies[0]


def test_no_click_url_long_body_raises_anomaly() -> None:
    """When body is non-trivial but no click URLs match, the parser
    should raise the "regex may be stale" anomaly so the operator
    refines the URL pattern."""
    body = "お知らせ\n" + ("x" * 1000)  # long body, no click URLs
    candidates, anomalies = parse_message(body)
    assert candidates == []
    assert any("no click-coin URLs matched" in a for a in anomalies)
