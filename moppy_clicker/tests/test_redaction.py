from moppy_clicker.redaction import host_only, redact_subject, redact_url


def test_redact_url_strips_query_and_fragment():
    raw = "https://pc.moppy.jp/redirect/abc?uid=123&token=xyz#frag"
    assert redact_url(raw) == "https://pc.moppy.jp/redirect/abc"


def test_redact_url_handles_empty_query():
    assert redact_url("https://pc.moppy.jp/redirect/abc") == "https://pc.moppy.jp/redirect/abc"


def test_redact_url_unparseable():
    assert redact_url("not a url at all") == "not a url at all"


def test_host_only():
    assert host_only("https://pc.moppy.jp/redirect/abc?u=1") == "pc.moppy.jp"


def test_redact_subject_truncates():
    assert redact_subject("【モッピー】クリックで1pt獲得") == "【モッピー…"


def test_redact_subject_short_unchanged():
    assert redact_subject("短い") == "短い"


def test_redact_subject_empty():
    assert redact_subject("") == ""
