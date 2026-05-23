"""Warau / ワラウ (https://www.warau.jp) adapter.

Source: **Gmail** (NOT on-site inbox). Recon 2026-05-10 inferred an
on-site Webメール inbox but live probing 2026-05-15 disproved this:

- ``/mail/list`` → actual HTTP 404
- ``/service/mail_receive/`` → メール受信設定 (subscription on/off form)
- ``/service/player/p_mailModIndex.php`` → メアド変更 form (redirects to
  ssl.warau.jp on the modifier subdomain)
- ``/mypage/`` → does not link to any inbox section

Warau click-mails ship to the user's external Gmail only, like
moppy / fruitmail. Adapter accordingly uses ``GmailSource`` with the
standard label-skip pattern.

Background:
- 運営: 株式会社オープンスマイル
- earning paths in scope: Gmail click-mails (自動化対象 ✅)
- ad-fraud out-of-scope: ``/games/auth/*`` ``/contents/*`` 8+ ゲーム
- conversion: 1Pt = 1 円
- anti-bot: 比較的寛容、Playwright 不要

**ad-fraud 隔離 (絶対遵守):**
- ``allowed_hosts`` は warau.jp / www.warau.jp のみ。ゲーム subdomain は
  含めない
- parser の ``EXCLUSION_URL_RE`` で ``/games/`` ``/contents/`` ``/gacha/``
  ``/slot/`` ``/lottery/`` ``/kuji/`` ``/jankenClover`` ``/fuwapon``
  ``/easygame`` 等 game-related path を全部弾く
- 「クリックでXpt」callout 付きでない URL は問答無用で drop
  (= getmoney の survey URL 対応と同じ防御策)

**user 側のセットアップ (Gmail label 作成、moppy/fruitmail と同じ):**
1. Gmail で `warau-clicked` ラベルを作成 (click 成功メールに自動付与)
2. Gmail で `warau-no-coins` ラベルを作成 (click 不可と判定された
   メール本文に自動付与)
3. cron が自動で両ラベルを ``-label:warau-clicked -label:warau-no-coins``
   で除外して fresh メールのみ処理

Required Secrets:
  - ``WARAU_COOKIES`` — Cookie-Editor JSON export from a logged-in
    warau.jp browser session (login verification + balance scrape のみで使用)
  - ``SLACK_CHANNEL_WARAU`` — Slack channel ID or ``#name``
  - ``GMAIL_CLIENT_ID`` / ``GMAIL_CLIENT_SECRET`` / ``GMAIL_REFRESH_TOKEN``
    — Gmail API OAuth2 認証 (moppy 等と共有)
"""

from ...common.adapter import Adapter
from ...common.balance import DEFAULT_BALANCE_PATTERNS
from ...common.sources import GmailSource
from ...common.wizard import DailyWizard
from .parser import parse as parse_email

ADAPTER = Adapter(
    name="warau",
    site_label="ワラウ",
    mypage_url="https://www.warau.jp/mypage",
    # Apex + www only. Game subdomains and any third-party ad / tracking
    # host are intentionally excluded — the parser's exclusion regex
    # also drops game paths even on the main domain.
    allowed_hosts=frozenset({"warau.jp", "www.warau.jp"}),
    login_keyword="ログアウト",
    gmail_query="from:warau.jp -label:warau-clicked -label:warau-no-coins newer_than:3d",
    clicked_label="warau-clicked",
    no_coins_label="warau-no-coins",
    source=GmailSource(parse_email=parse_email),
    balance_patterns=DEFAULT_BALANCE_PATTERNS,
    discover_seeds=("https://www.warau.jp/mypage",),
    # 2026-05-23 ad-fraud policy 解禁後の追加 (issue #27)。
    #
    # warau は SPA で /play (= 毎日貯める hub) も /sp/play/ も JS-rendered。
    # 個別 game URL は静的 HTML に出てこない (run 26335852100 で全 230KB body
    # 解析するも /play/stampnote 以外発見できず、stampnote は desktop で 404)。
    # しかし wizard runner は domcontentloaded で goto + 4.5s 滞留する仕組み
    # なので、SPA hydration を完全に待たなくても hub への visit が起きれば
    # 広告 NW への impression は emit される想定 (=「毎日貯める」hub への daily
    # 巡回相当)。
    #
    # 1 週間 balance 観察で credit 無ければ、SPA 内 game の Playwright stealth
    # 突破 (memory `project_warau_fortune_blocked` で失敗確定済) に escalate
    # する判断材料。
    daily_wizards=(
        DailyWizard(
            name="warau_play_hub",
            url="https://www.warau.jp/play/",
            clicks=(),
        ),
    ),
)
