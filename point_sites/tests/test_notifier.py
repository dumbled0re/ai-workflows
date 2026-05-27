from __future__ import annotations

from point_sites.common.notifier import Notifier, _format_verification, _slack_escape


def _new_notifier() -> Notifier:
    return Notifier(bot_token="xoxb-fake", channel="#fake")


def test_slack_escape_handles_html_meta():
    assert _slack_escape("a&b<c>d") == "a&amp;b&lt;c&gt;d"


def test_build_extract_blocks_empty_returns_only_chrome():
    blocks = _new_notifier().build_extract_blocks([], date_label="2026-05-05")
    types = [b["type"] for b in blocks]
    assert types == ["header", "context", "divider"]


def test_build_extract_blocks_includes_full_urls_as_clickable():
    candidates = [
        (
            "msgA",
            "【モッピー】今日のクリックポイント",
            [
                "https://pc.moppy.jp/cc/c?token=abc",
                "https://pc.moppy.jp/cc/c?token=def",
            ],
        ),
        (
            "msgB",
            "別のメール件名",
            ["https://pc.moppy.jp/cc/c?token=ghi"],
        ),
    ]
    blocks = _new_notifier().build_extract_blocks(candidates, date_label="2026-05-05")

    header = blocks[0]
    assert header["type"] == "header"
    assert "3件" in header["text"]["text"]

    context = blocks[1]
    assert context["type"] == "context"
    text = context["elements"][0]["text"]
    assert "2026-05-05" in text
    assert "2メール" in text
    assert "ログイン" in text

    sections = [b for b in blocks if b["type"] == "section"]
    assert len(sections) == 2

    body0 = sections[0]["text"]["text"]
    assert "今日のクリックポイント" in body0
    assert "<https://pc.moppy.jp/cc/c?token=abc|🔗 click 1>" in body0
    assert "<https://pc.moppy.jp/cc/c?token=def|🔗 click 2>" in body0

    body1 = sections[1]["text"]["text"]
    assert "別のメール件名" in body1
    assert "<https://pc.moppy.jp/cc/c?token=ghi|🔗 click 1>" in body1


def test_build_extract_blocks_skips_messages_with_no_urls():
    candidates = [
        ("msgA", "subj A", []),
        ("msgB", "subj B", ["https://pc.moppy.jp/cc/c?t=1"]),
    ]
    blocks = _new_notifier().build_extract_blocks(candidates, date_label="2026-05-05")
    sections = [b for b in blocks if b["type"] == "section"]
    assert len(sections) == 1


def test_build_extract_blocks_uses_site_label_in_header():
    """2026-05-27 regression: pointincome channel に「モッピー クリックリンク」
    と通知される hard-code bug の修正確認。site_label を __init__ で渡したら
    header に反映される。"""
    notifier = Notifier(bot_token="xoxb-fake", channel="#fake", site_label="ポイントインカム")
    candidates = [("msgA", "subj", ["https://pointi.jp/al/x"])]
    blocks = notifier.build_extract_blocks(candidates, date_label="2026-05-27")
    header_text = blocks[0]["text"]["text"]
    assert "ポイントインカム" in header_text
    assert "モッピー" not in header_text


def test_build_extract_blocks_falls_back_to_moppy_when_site_label_empty():
    """site_label 無指定の旧 caller (test fixture 等) は legacy 「モッピー」
    fallback を保持。これは backward compatibility shim で、新規 caller
    は site_label を必ず渡す。"""
    notifier = Notifier(bot_token="xoxb-fake", channel="#fake")
    candidates = [("msgA", "subj", ["https://x/y"])]
    blocks = notifier.build_extract_blocks(candidates, date_label="2026-05-27")
    header_text = blocks[0]["text"]["text"]
    assert "モッピー" in header_text


def test_build_extract_blocks_escapes_subject():
    candidates = [
        ("msgA", "Title <script> & rest", ["https://pc.moppy.jp/cc/c?t=1"]),
    ]
    blocks = _new_notifier().build_extract_blocks(candidates, date_label="2026-05-05")
    body = next(b for b in blocks if b["type"] == "section")["text"]["text"]
    assert "&lt;script&gt;" in body
    assert "&amp;" in body
    assert "<script>" not in body


def _make_candidates(n: int) -> list[tuple[str, str, list[str]]]:
    return [(f"msg{i}", f"件名 {i}", [f"https://pc.moppy.jp/cc/c?t={i}"]) for i in range(n)]


def test_send_extract_links_single_page(monkeypatch):
    sent: list[dict] = []
    n = _new_notifier()
    monkeypatch.setattr(n, "_post", lambda payload: sent.append(payload))
    n.send_extract_links(_make_candidates(10), date_label="2026-05-05")
    assert len(sent) == 1
    blocks = sent[0]["blocks"]
    sections = [b for b in blocks if b["type"] == "section"]
    assert len(sections) == 10
    assert "(1/" not in blocks[1]["elements"][0]["text"]  # no page suffix when single
    # Slack must NOT auto-unfurl the click links (Slackbot would otherwise
    # fetch them anonymously and could consume the click on Moppy's side).
    assert sent[0]["unfurl_links"] is False
    assert sent[0]["unfurl_media"] is False


def test_send_extract_links_chunks_above_45(monkeypatch):
    """Each page must stay <= 50 blocks (3 chrome + <= 45 sections)."""
    sent: list[dict] = []
    n = _new_notifier()
    monkeypatch.setattr(n, "_post", lambda payload: sent.append(payload))
    n.send_extract_links(_make_candidates(100), date_label="2026-05-05")
    # 100 / 45 → 3 pages
    assert len(sent) == 3
    for payload in sent:
        assert len(payload["blocks"]) <= 50
    # Page labels show progress
    assert "(1/3)" in sent[0]["blocks"][1]["elements"][0]["text"]
    assert "(2/3)" in sent[1]["blocks"][1]["elements"][0]["text"]
    assert "(3/3)" in sent[2]["blocks"][1]["elements"][0]["text"]


def test_send_extract_links_empty_posts_text_only(monkeypatch):
    sent: list[dict] = []
    n = _new_notifier()
    monkeypatch.setattr(n, "_post", lambda payload: (sent.append(payload), True)[1])
    ok = n.send_extract_links([], date_label="2026-05-05")
    assert ok is True
    assert len(sent) == 1
    assert "blocks" not in sent[0]
    assert "ありませんでした" in sent[0]["text"]


def test_send_extract_links_skips_messages_with_no_urls(monkeypatch):
    sent: list[dict] = []
    n = _new_notifier()
    monkeypatch.setattr(n, "_post", lambda payload: (sent.append(payload), True)[1])
    ok = n.send_extract_links(
        [
            ("msgA", "件名A", []),
            ("msgB", "件名B", []),
        ],
        date_label="2026-05-05",
    )
    assert ok is True
    assert len(sent) == 1
    assert "blocks" not in sent[0]
    assert "ありませんでした" in sent[0]["text"]


def test_send_extract_links_returns_false_when_post_fails(monkeypatch):
    """Caller (cmd_run) needs the failure signal to exit non-zero."""
    n = _new_notifier()
    monkeypatch.setattr(n, "_post", lambda payload: False)
    ok = n.send_extract_links(_make_candidates(3), date_label="2026-05-05")
    assert ok is False


def test_send_extract_links_returns_false_when_any_chunk_fails(monkeypatch):
    """Multi-page send: failure on any page must propagate."""
    n = _new_notifier()
    calls = {"i": 0}

    def fake_post(payload: dict) -> bool:
        calls["i"] += 1
        return calls["i"] != 2  # second call fails

    monkeypatch.setattr(n, "_post", fake_post)
    ok = n.send_extract_links(_make_candidates(100), date_label="2026-05-05")
    assert ok is False
    assert calls["i"] == 3  # all chunks attempted despite failure


def test_format_verification_returns_none_for_quiet_run():
    assert _format_verification(0, None, None) is None


def test_format_verification_warns_when_balance_unavailable_but_clicks_happened():
    msg = _format_verification(5, None, None)
    assert msg is not None
    assert "残高取得失敗" in msg


def test_format_verification_shows_full_credit_without_flag():
    msg = _format_verification(5, 100, 105)
    assert msg is not None
    assert "100→105" in msg
    assert "+5pt" in msg
    assert "100%" in msg
    assert "⚠" not in msg


def test_format_verification_flags_zero_credit():
    msg = _format_verification(11, 100, 100)
    assert msg is not None
    assert "0%" in msg
    assert "⚠" in msg


def test_format_verification_no_estimate_just_shows_delta():
    msg = _format_verification(0, 100, 110)
    assert msg is not None
    assert "100→110" in msg
    assert "推定なし" in msg


def test_format_verification_shows_inter_run_delta_when_prior_differs():
    """User-facing test: when prior cron's balance_after differs from this
    run's balance_before, the line should surface that inter-run delta so
    a flat within-run Δ doesn't hide day-over-day credits."""
    msg = _format_verification(0, 55, 55, prior_balance_after=54)
    assert msg is not None
    assert "55→55" in msg
    assert "前回比 +1pt (54→55)" in msg


def test_format_verification_omits_inter_run_when_prior_matches_before():
    """No inter-run delta means no extra noise on the line."""
    msg = _format_verification(0, 55, 55, prior_balance_after=55)
    assert msg is not None
    assert "前回比" not in msg


def test_format_verification_omits_inter_run_when_prior_is_none():
    msg = _format_verification(0, 55, 55, prior_balance_after=None)
    assert msg is not None
    assert "前回比" not in msg


def test_format_verification_inter_run_delta_negative_visible():
    """Redemption / point conversion between cron runs should be visible too."""
    msg = _format_verification(0, 100, 100, prior_balance_after=150)
    assert msg is not None
    assert "前回比 -50pt (150→100)" in msg


def test_format_verification_inter_run_alongside_estimated():
    """Inter-run delta should append even on the 加算確認 (estimated_total>0) line."""
    msg = _format_verification(5, 100, 105, prior_balance_after=98)
    assert msg is not None
    assert "100→105" in msg
    assert "前回比 +2pt (98→100)" in msg
