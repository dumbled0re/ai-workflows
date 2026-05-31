"""Tests for cookie persistence across runs.

The store is the bridge between processes — within one process,
``requests.Session`` already tracks Set-Cookie. Between runs, the file
on disk is the only thing carrying Moppy's rotated values forward.
These tests pin down that bridge: round-trip identity, atomic writes,
and graceful fallback to the bootstrap path when the file is missing
or corrupt.
"""

from requests.cookies import RequestsCookieJar

from point_sites.common import cookie_store


def _populate_jar(jar: RequestsCookieJar, **kwargs: str) -> None:
    for name, value in kwargs.items():
        jar.set(name, value, domain=".moppy.jp", path="/")


def test_load_returns_none_when_file_missing(tmp_path) -> None:
    assert cookie_store.load(tmp_path / "missing.json") is None


def test_load_returns_none_for_invalid_json(tmp_path) -> None:
    p = tmp_path / "cookies.json"
    p.write_text("not json", encoding="utf-8")
    assert cookie_store.load(p) is None


def test_load_returns_none_for_empty_list(tmp_path) -> None:
    p = tmp_path / "cookies.json"
    p.write_text("[]", encoding="utf-8")
    assert cookie_store.load(p) is None


def test_load_returns_none_when_shape_invalid(tmp_path) -> None:
    p = tmp_path / "cookies.json"
    # Missing 'value' key
    p.write_text('[{"name": "x"}]', encoding="utf-8")
    assert cookie_store.load(p) is None


def test_save_and_load_roundtrip(tmp_path) -> None:
    jar = RequestsCookieJar()
    _populate_jar(jar, PHPSESSID="abc123", user_token="xyz")
    path = tmp_path / "cookies.json"
    n = cookie_store.save_jar(jar, path)
    assert n == 2

    loaded = cookie_store.load(path)
    assert loaded is not None
    by_name = {c["name"]: c for c in loaded}
    assert by_name["PHPSESSID"]["value"] == "abc123"
    assert by_name["PHPSESSID"]["domain"] == ".moppy.jp"
    assert by_name["PHPSESSID"]["path"] == "/"
    assert by_name["user_token"]["value"] == "xyz"


def test_save_is_atomic_no_tmp_leftover(tmp_path) -> None:
    jar = RequestsCookieJar()
    _populate_jar(jar, x="1")
    path = tmp_path / "cookies.json"
    cookie_store.save_jar(jar, path)
    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == []


def test_save_overwrites_existing_atomically(tmp_path) -> None:
    """A second save with rotated values must replace the file completely."""
    path = tmp_path / "cookies.json"

    jar1 = RequestsCookieJar()
    _populate_jar(jar1, PHPSESSID="old")
    cookie_store.save_jar(jar1, path)

    jar2 = RequestsCookieJar()
    _populate_jar(jar2, PHPSESSID="rotated")
    cookie_store.save_jar(jar2, path)

    loaded = cookie_store.load(path)
    assert loaded is not None
    assert len(loaded) == 1
    assert loaded[0]["value"] == "rotated"


def test_save_creates_parent_dir(tmp_path) -> None:
    path = tmp_path / "nested" / "dir" / "cookies.json"
    jar = RequestsCookieJar()
    _populate_jar(jar, x="1")
    cookie_store.save_jar(jar, path)
    assert path.exists()


def test_domain_matches_hosts_exact_match() -> None:
    assert cookie_store.domain_matches_hosts("pc.moppy.jp", {"pc.moppy.jp"})


def test_domain_matches_hosts_dot_domain_covers_subdomain() -> None:
    """``.moppy.jp`` cookie should be sent to pc.moppy.jp etc."""
    assert cookie_store.domain_matches_hosts(".moppy.jp", {"pc.moppy.jp"})
    assert cookie_store.domain_matches_hosts(".moppy.jp", {"mail.moppy.jp"})


def test_domain_matches_hosts_no_match_on_unrelated_third_party() -> None:
    """Analytics / ad / tracker cookies must be filtered out."""
    allowed = {"pc.moppy.jp", "moppy.jp"}
    assert not cookie_store.domain_matches_hosts("googleads.com", allowed)
    assert not cookie_store.domain_matches_hosts(".google-analytics.com", allowed)
    assert not cookie_store.domain_matches_hosts(".doubleclick.net", allowed)


def test_domain_matches_hosts_empty_domain_rejected() -> None:
    """Domain-less cookies are rejected (they'd default to the request
    host, which we can't validate here)."""
    assert not cookie_store.domain_matches_hosts("", {"pc.moppy.jp"})
    assert not cookie_store.domain_matches_hosts(".", {"pc.moppy.jp"})


def test_save_jar_filters_third_party_cookies_when_allowed_hosts_set(tmp_path) -> None:
    """Reproduce the 2026-05-15 pointtown bloat: 16 first-party +
    322 third-party cookies → with allowed_hosts filter, only the 16
    first-party should be persisted."""
    path = tmp_path / "cookies.json"
    jar = RequestsCookieJar()
    jar.set("session", "abc", domain=".pointtown.com", path="/")
    jar.set("user_id", "42", domain="www.pointtown.com", path="/")
    # Bring in third-party tracker bloat — what a Playwright wizard
    # would leave behind after visiting a page with analytics+ads.
    jar.set("_ga", "GA1.2.x", domain=".google-analytics.com", path="/")
    jar.set("_gid", "GA1.2.y", domain=".googleads.com", path="/")
    jar.set("__doubleclick", "abc", domain=".doubleclick.net", path="/")

    n = cookie_store.save_jar(jar, path, allowed_hosts={"pointtown.com", "www.pointtown.com"})
    assert n == 2
    loaded = cookie_store.load(path)
    assert loaded is not None
    names = {c["name"] for c in loaded}
    assert names == {"session", "user_id"}


def test_save_jar_drops_trackers_even_with_no_domain_filter(tmp_path) -> None:
    """Tracker name filter applies regardless of ``allowed_hosts``.

    Even when callers opt out of domain filtering by passing ``None``,
    known third-party tracker cookies (`_ga` etc.) are still dropped —
    they never contribute to auth state and only bloat the jar.
    """
    path = tmp_path / "cookies.json"
    jar = RequestsCookieJar()
    jar.set("session", "abc", domain=".pointtown.com", path="/")
    jar.set("_ga", "GA1.2.x", domain=".google-analytics.com", path="/")

    n = cookie_store.save_jar(jar, path, allowed_hosts=None)
    assert n == 1
    loaded = cookie_store.load(path)
    assert loaded is not None
    assert {c["name"] for c in loaded} == {"session"}


def test_is_tracker_cookie_recognises_common_patterns() -> None:
    """Spot-check the name patterns the filter is supposed to catch."""
    from point_sites.common.cookie_store import is_tracker_cookie

    for name in (
        "_ga",
        "_ga_ABC123",
        "_gid",
        "_gat",
        "__utma",
        "_clck",
        "_clsk",
        "_hjSession",
        "_hjid",
        "_uetsid",
        "_uetvid",
        "_fbp",
        "_fbc",
        "AMCV_ABC",
        "s_cc",
        "_ttp",
        "gcl_au",
    ):
        assert is_tracker_cookie(name), f"{name!r} should be a tracker"
    for name in ("session_id", "csrf_token", "JSESSIONID", "auth_token", "user_id", "PHPSESSID"):
        assert not is_tracker_cookie(name), f"{name!r} should NOT be a tracker"
