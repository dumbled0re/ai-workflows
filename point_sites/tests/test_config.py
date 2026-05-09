from __future__ import annotations

import pytest

from point_sites.config import ConfigError, _parse_cookies


def test_parse_cookies_none_when_absent():
    assert _parse_cookies(None) is None
    assert _parse_cookies("") is None


def test_parse_cookies_minimal():
    raw = '[{"name": "PHPSESSID", "value": "abc123"}]'
    cookies = _parse_cookies(raw)
    assert cookies == [
        {
            "name": "PHPSESSID",
            "value": "abc123",
            "domain": ".moppy.jp",
            "path": "/",
            "secure": True,
        }
    ]


def test_parse_cookies_preserves_explicit_fields():
    raw = '[{"name": "x", "value": "y", "domain": "pc.moppy.jp", "path": "/api", "secure": false}]'
    cookies = _parse_cookies(raw)
    assert cookies is not None
    assert cookies[0]["domain"] == "pc.moppy.jp"
    assert cookies[0]["path"] == "/api"
    assert cookies[0]["secure"] is False


def test_parse_cookies_secure_defaults_to_true():
    cookies = _parse_cookies('[{"name": "a", "value": "b"}]')
    assert cookies is not None
    assert cookies[0]["secure"] is True


def test_parse_cookies_secure_must_be_bool():
    with pytest.raises(ConfigError, match="secure"):
        _parse_cookies('[{"name": "a", "value": "b", "secure": "yes"}]')


def test_parse_cookies_invalid_json():
    with pytest.raises(ConfigError, match="valid JSON"):
        _parse_cookies("not json")


def test_parse_cookies_not_array():
    with pytest.raises(ConfigError, match="JSON array"):
        _parse_cookies('{"name": "x", "value": "y"}')


def test_parse_cookies_missing_name():
    with pytest.raises(ConfigError, match=r"\[0\]\.name"):
        _parse_cookies('[{"value": "y"}]')


def test_parse_cookies_empty_name():
    with pytest.raises(ConfigError, match="non-empty"):
        _parse_cookies('[{"name": "", "value": "y"}]')


def test_parse_cookies_value_not_string():
    with pytest.raises(ConfigError, match=r"\[0\]\.value"):
        _parse_cookies('[{"name": "x", "value": 123}]')
