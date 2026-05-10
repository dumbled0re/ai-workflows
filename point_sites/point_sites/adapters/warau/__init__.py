"""Warau / ワラウ (https://www.warau.jp) adapter.

Status: scaffolded 2026-05-10. Awaiting ``WARAU_COOKIES`` +
``SLACK_CHANNEL_WARAU`` registration. mypage / inbox / login URLs were
confirmed anonymously to be 200 — both ``/mypage`` and ``/member/mypage``
work; ``/mail/list`` is the on-site inbox, ``/daily`` is the login-bonus
landing.

Background:
- 運営: 株式会社オープンスマイル
- earning paths (recon 2026-05-10):
  - ``/mail/list``: on-site Webメール inbox (click-coin, 自動化対象 ✅)
  - ``/daily``: 毎日ログインボーナス (自動化候補、後で `EndpointPollSource` 追加)
  - ``/games/auth/*`` `/contents/*`: 8+ ゲーム (= ad-fraud risk、**自動化対象外**)
- conversion: 1Pt = 1 円
- 休眠: 規約「一定期間」(具体日数未記載)
- anti-bot: 比較的寛容、Playwright 不要

**ad-fraud 隔離 (絶対遵守):**
- ``allowed_hosts`` は warau.jp / www.warau.jp のみ。ゲーム subdomain は
  含めない (もし将来 ``games.warau.jp`` などが現れたら、その時 audit して
  からの判断)
- parser の ``EXCLUSION_URL_RE`` で ``/games/`` ``/contents/`` ``/gacha/``
  ``/slot/`` ``/lottery/`` ``/kuji/`` ``/jankenClover`` ``/fuwapon``
  ``/easygame`` 等 game-related path を全部弾く
- 初メールで「クリックでXpt」callout 付きでない URL は問答無用で drop
  (= getmoney の survey URL 対応と同じ防御策)

**初メール後の TODO (after Secret 登録):**
1. ``gh workflow run warau.yml -f discover=true`` で `/mail/list` の構造確認
2. ``-f inspect_url=https://www.warau.jp/mail/list`` で inbox HTML を見て
   ``parse_inbox`` の link regex を実 HTML に合わせる
3. 1 通目の inspect (``-f inspect_url=<message URL>``) で click-coin URL
   pattern を確定 → ``parser.py`` の regex を narrow
4. ``-f extract_links=true`` で実 candidate を Slack で目視確認、ad-fraud
   path が混ざっていないか EXCLUSION_URL_RE 強化

Required Secrets:
  - ``WARAU_COOKIES`` — Cookie-Editor JSON export from a logged-in
    warau.jp browser session.
  - ``SLACK_CHANNEL_WARAU`` — Slack channel ID or ``#name``.
"""

from ...common.adapter import Adapter
from ...common.balance import DEFAULT_BALANCE_PATTERNS
from ...common.sources import OnsiteInboxSource
from .parser import parse_inbox, parse_message

ADAPTER = Adapter(
    name="warau",
    site_label="ワラウ",
    # Verified anonymously 2026-05-10: ``/mypage`` returns 200. The
    # alias ``/member/mypage`` also works but ``/mypage`` is shorter
    # and matches the pointtown convention.
    mypage_url="https://www.warau.jp/mypage",
    # Apex + www only. Game subdomains and any third-party ad / tracking
    # host are intentionally excluded — the parser's exclusion regex
    # also drops game paths even on the main domain.
    allowed_hosts=frozenset({"warau.jp", "www.warau.jp"}),
    login_keyword="ログアウト",
    source=OnsiteInboxSource(
        inbox_url="https://www.warau.jp/mail/list",
        parse_inbox=parse_inbox,
        parse_message=parse_message,
    ),
    # Default mypage markers (保有ポイント / 現在のポイント) cover most
    # Japanese point sites. Verify on first inspect; if the balance is
    # rendered with a site-specific class, add a narrow pattern here.
    balance_patterns=DEFAULT_BALANCE_PATTERNS,
    discover_seeds=(
        "https://www.warau.jp/mypage",
        "https://www.warau.jp/mail/list",
    ),
)
