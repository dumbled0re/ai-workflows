"""Sugutama / すぐたま (https://www.sugutama.jp) adapter.

Status: scaffolded 2026-05-10. Awaiting ``SUGUTAMA_COOKIES`` +
``SLACK_CHANNEL_SUGUTAMA`` registration. URL roots verified anonymously
(`/sugutama/mypage`, `/sugutama/mail/`, `/sugutama/login` all 200).

Background:
- 運営: 株式会社ネットマイル (Netmile, Inc.) — 既存 6 サイトと**完全に
  独立した親会社** = 真の分散価値 (gendama/getmoney = インフォニア系、
  moppy/pointincome = セレス系、hapitas = オズビジョン系 とも独立)
- レート: ``mile`` 単位。1 mile = 0.5 円相当 (推定、要 user FAQ 確認)
- 休眠条件: 未確認 (user に最新 FAQ 経由で確認推奨)
- ネットマイルアカウントと共有 (1 アカウントで複数サイト跨ぎ可)
- earning paths (推定):
  - ``/sugutama/mail/`` or ``/sugutama/mail_box/``: on-site Webメール 受信箱
    (click-coin、自動化対象 ✅)
  - ``/sugutama/everyday/`` `/sugutama/daily_click/`: daily click section
    (要 inspect で実態確認)
  - ガチャ・スロット系: 存在するが ad-fraud risk のため自動化対象外

Why on-site inbox (not Gmail):
  ネットマイル系は getmoney と同様 Gmail 配信 + on-site inbox 両方が
  ある可能性が高く、その場合は重複加算不可 → 認証コスト低い on-site
  inbox を採用 (Gmail App Password 不要)。

Discover-time TODOs (after Secret 登録):
  1. ``gh workflow run sugutama.yml -f discover=true`` で `/sugutama/mail/`
     の構造確認、必要なら `inbox_url` を `mail_box/` 等に refine。
  2. ``-f inspect_url=https://www.sugutama.jp/sugutama/mypage`` で実 mypage
     を見て `DEFAULT_BALANCE_PATTERNS` がマッチするか確認。``mile`` 専用
     widget が独自 class を使ってたら adapter 専用 ``balance_patterns`` を
     追加。
  3. 1 通目を inspect して click-coin URL pattern を確定 → parser refine。
  4. ``-f extract_links=true`` で実 candidate を Slack 目視、ad-fraud path
     混入なし確認後 click 本番化。

Required Secrets:
  - ``SUGUTAMA_COOKIES`` — Cookie-Editor JSON export (logged-in
    sugutama.jp browser session、ネットマイル SSO 通った状態)
  - ``SLACK_CHANNEL_SUGUTAMA`` — Slack channel ID or ``#name``.
"""

from ...common.adapter import Adapter
from ...common.balance import DEFAULT_BALANCE_PATTERNS
from ...common.sources import OnsiteInboxSource
from .parser import parse_inbox, parse_message

ADAPTER = Adapter(
    name="sugutama",
    site_label="すぐたま",
    # Auth cookies live on www.netmile.co.jp (Rails _mediafactory-user_
    # session + X-Oc-LBS LBS sticky). Anonymous probe 2026-05-15:
    # - www.sugutama.jp/sugutama/mypage → 404 (actual HTTP 404)
    # - www.netmile.co.jp/sugutama/mypage → 200 (login form anonymously,
    #   logged-in mypage with auth cookies)
    # So all auth-required endpoints must target netmile.co.jp where
    # cookies are scoped. sugutama.jp is now public-marketing only.
    mypage_url="https://www.netmile.co.jp/sugutama/mypage",
    # Apex + www on both domains (legacy sugutama.jp redirects survive
    # for the marketing landing). Auth requests target netmile.co.jp.
    allowed_hosts=frozenset(
        {
            "sugutama.jp",
            "www.sugutama.jp",
            "netmile.co.jp",
            "www.netmile.co.jp",
        }
    ),
    login_keyword="ログアウト",
    source=OnsiteInboxSource(
        # Inbox under netmile.co.jp/sugutama/. Probe-anonymous shows the
        # same login form template as mypage (= same auth-gated app),
        # so with the netmile session cookie this should render the
        # actual inbox.
        inbox_url="https://www.netmile.co.jp/sugutama/mail/",
        parse_inbox=parse_inbox,
        parse_message=parse_message,
    ),
    # DEFAULT covers 保有ポイント / 現在のポイント patterns. ``mile`` is
    # the unit on this site; if mypage uses a custom marker like
    # ``保有マイル``/``現在のマイル``, add a site-specific pattern after
    # first inspect.
    balance_patterns=DEFAULT_BALANCE_PATTERNS,
    discover_seeds=(
        "https://www.netmile.co.jp/sugutama/mypage",
        "https://www.netmile.co.jp/sugutama/mail/",
        "https://www.netmile.co.jp/sugutama/everyday/",
    ),
)
