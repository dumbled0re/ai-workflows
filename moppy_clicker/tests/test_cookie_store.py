"""Tests for cookie persistence across runs.

The store is the bridge between processes — within one process,
``requests.Session`` already tracks Set-Cookie. Between runs, the file
on disk is the only thing carrying Moppy's rotated values forward.
These tests pin down that bridge: round-trip identity, atomic writes,
and graceful fallback to the bootstrap path when the file is missing
or corrupt.
"""

from requests.cookies import RequestsCookieJar

from moppy_clicker import cookie_store


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
