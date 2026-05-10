"""Tests for the cookie-conversion helpers in BrowserClicker.

Only the conversion logic is exercised here — booting a real Chromium
in unit tests would be slow, brittle, and CI-unfriendly. The Playwright
import inside ``browser.py`` is lazy specifically so this test runs
without ``playwright install`` on the dev box.
"""

from point_sites.common.browser import _to_playwright_cookies


def test_to_playwright_cookies_preserves_required_fields() -> None:
    cookies = [
        {"name": "session", "value": "abc", "domain": ".example.jp", "path": "/", "secure": True},
    ]
    out = _to_playwright_cookies(cookies, default_domain=".fallback.jp")
    assert out == [
        {"name": "session", "value": "abc", "domain": ".example.jp", "path": "/", "secure": True},
    ]


def test_to_playwright_cookies_uses_default_domain_when_missing() -> None:
    out = _to_playwright_cookies(
        [{"name": "n", "value": "v"}],
        default_domain=".fallback.jp",
    )
    assert out[0]["domain"] == ".fallback.jp"
    assert out[0]["path"] == "/"  # default
    assert out[0]["secure"] is True  # default


def test_to_playwright_cookies_drops_entries_without_name() -> None:
    out = _to_playwright_cookies(
        [{"name": "", "value": "v"}, {"name": "ok", "value": "v"}],
        default_domain=".fallback.jp",
    )
    assert [c["name"] for c in out] == ["ok"]


def test_to_playwright_cookies_coerces_secure_falsy() -> None:
    out = _to_playwright_cookies(
        [{"name": "n", "value": "v", "secure": False}],
        default_domain=".fallback.jp",
    )
    assert out[0]["secure"] is False
