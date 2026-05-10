"""Tests for the BrowserAction executor.

Real Playwright is not booted — a fake BrowserClicker + fake Page
exercise the orchestration. Goals:
- one action's failure must not skip later actions
- success_marker check rejects pages that lack the marker
- click_selector failures are reported with the selector text
"""

from __future__ import annotations

from typing import Any

from point_sites.common.browser_action import (
    BrowserAction,
    BrowserActionResult,
    run_browser_actions,
)


class _FakePage:
    def __init__(self, content: str = "ok content", click_raises: Exception | None = None) -> None:
        self._content = content
        self._click_raises = click_raises
        self.clicked: list[str] = []
        self.waited_ms: list[int] = []
        self.closed = False

    def click(self, selector: str) -> None:
        if self._click_raises is not None:
            raise self._click_raises
        self.clicked.append(selector)

    def wait_for_timeout(self, ms: int) -> None:
        self.waited_ms.append(ms)

    def content(self) -> str:
        return self._content

    def close(self) -> None:
        self.closed = True


class _FakeBrowserClicker:
    def __init__(self, pages: dict[str, _FakePage] | None = None) -> None:
        self._pages = pages or {}
        self.gotos: list[str] = []

    def goto(self, url: str, *, wait_until: str = "networkidle") -> _FakePage:
        self.gotos.append(url)
        if url in self._pages:
            return self._pages[url]
        # Default: a page that's "good" for any URL we didn't pre-stage.
        return _FakePage(content="ok content")


def test_runs_actions_in_order_and_reports_ok() -> None:
    actions = (
        BrowserAction(name="visit_home", url="https://example.jp/"),
        BrowserAction(name="visit_acct", url="https://example.jp/account"),
    )
    bc: Any = _FakeBrowserClicker()
    out = run_browser_actions(bc, actions)
    assert out == [
        BrowserActionResult("visit_home", True, "ok"),
        BrowserActionResult("visit_acct", True, "ok"),
    ]
    assert bc.gotos == ["https://example.jp/", "https://example.jp/account"]


def test_navigation_failure_does_not_skip_later_actions() -> None:
    class _BadGoto(_FakeBrowserClicker):
        def goto(self, url: str, *, wait_until: str = "networkidle") -> _FakePage:  # type: ignore[override]
            self.gotos.append(url)
            if "fail" in url:
                raise RuntimeError("connection refused")
            return _FakePage()

    actions = (
        BrowserAction(name="bad", url="https://fail.example.jp/"),
        BrowserAction(name="good", url="https://example.jp/"),
    )
    bc: Any = _BadGoto()
    out = run_browser_actions(bc, actions)
    assert out[0].name == "bad" and not out[0].ok
    assert "connection refused" in out[0].message
    assert out[1] == BrowserActionResult("good", True, "ok")


def test_click_selector_failure_recorded_with_selector_text() -> None:
    actions = (BrowserAction(name="spin", url="https://example.jp/gacha", click_selector="button.spin"),)
    bc: Any = _FakeBrowserClicker(
        pages={"https://example.jp/gacha": _FakePage(click_raises=RuntimeError("not found"))},
    )
    out = run_browser_actions(bc, actions)
    assert not out[0].ok
    assert "button.spin" in out[0].message


def test_success_marker_missing_marks_failure() -> None:
    actions = (
        BrowserAction(
            name="bonus",
            url="https://example.jp/",
            success_marker="ボーナス獲得",
        ),
    )
    bc: Any = _FakeBrowserClicker(
        pages={"https://example.jp/": _FakePage(content="<html>nothing here</html>")},
    )
    out = run_browser_actions(bc, actions)
    assert not out[0].ok
    assert "ボーナス獲得" in out[0].message


def test_success_marker_present_marks_ok() -> None:
    actions = (
        BrowserAction(
            name="bonus",
            url="https://example.jp/",
            success_marker="ボーナス獲得",
        ),
    )
    bc: Any = _FakeBrowserClicker(
        pages={"https://example.jp/": _FakePage(content="<html>ボーナス獲得しました</html>")},
    )
    out = run_browser_actions(bc, actions)
    assert out[0] == BrowserActionResult("bonus", True, "ok")
