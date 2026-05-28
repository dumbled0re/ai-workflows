r"""Regression: fruitmail_lottery の skip_if_body_regex が間違って
verified-eligible な prize まで skip しないことを保証する。

2026-05-28 history:
- v1 regex `r"残り応募可能口数[\s\S]*?<dd[^>]*>\s*0\s*</dd>"` は non-greedy
  でも後続 dd を貪欲化し、everyday (残り 1 / 応募済み 0) でも 「応募済み
  口数 = 0」 の dd まで backtrack して match → 全 wizard 誤 skip。
- v2 `r"残り応募可能口数\s*</dt>\s*<dd[^>]*>\s*0\s*</dd>"` は `</dt>` で
  明示的に区切ることで「残り口数の dd」 のみを評価対象にする。
"""

from __future__ import annotations

import re

from point_sites.adapters.fruitmail_lottery import ADAPTER


def _regex() -> re.Pattern[str]:
    """Pull the live skip pattern off the first prize wizard.

    All 5 wizards (everyday / everyweek / everymonth / gorgeous / premium)
    share the same regex via the `_prize_wizard` factory, so any one of
    them works as the regression anchor.
    """
    pat = ADAPTER.daily_wizards[0].skip_if_body_regex
    assert pat is not None
    return re.compile(pat)


def _body(remaining: int, applied: int) -> str:
    """Build a minimal HTML snippet mirroring the live prize-page widget."""
    return (
        '<dt class="prizeComponent_prizeItems__applyNumberLabel">残り応募可能口数</dt>\n'
        f'<dd class="prizeComponent_prizeItems__applyNumberValue">{remaining}</dd>\n'
        '<dt class="prizeComponent_prizeItems__applyNumberLabel">応募済み口数</dt>\n'
        f'<dd class="prizeComponent_prizeItems__applyNumberValue">{applied}</dd>'
    )


def test_skip_when_remaining_is_zero() -> None:
    """応募上限到達: 残り 0 / 応募済 8 → skip 扱い"""
    assert _regex().search(_body(remaining=0, applied=8))


def test_no_skip_when_remaining_nonzero_even_if_applied_zero() -> None:
    """v1 regex の bug ケース: everyday に相当する 残り 1 / 応募済 0 は
    skip すべきでない (まだ応募可能)。"""
    assert _regex().search(_body(remaining=1, applied=0)) is None


def test_no_skip_when_remaining_high_even_if_applied_zero() -> None:
    assert _regex().search(_body(remaining=5, applied=0)) is None


def test_no_skip_when_only_applied_count_is_zero() -> None:
    """応募済み口数の dd が 0 でも、残り口数 dd が 0 でないなら skip しない。"""
    assert _regex().search(_body(remaining=3, applied=0)) is None
