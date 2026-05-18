"""Sugutama / すぐたま (https://www.sugutama.jp) adapter.

Source: **Gmail** (NOT on-site inbox). Recon 2026-05-10 assumed an
on-site Webメール inbox under ``/sugutama/mail/`` but live probing
2026-05-15 disproved it:

- ``/sugutama/mail/`` → 受信メール設定変更 (subscription on/off form),
  HTTP 200 but title ``受信メール設定変更`` — not an inbox
- Authenticated ``/sugutama/mypage`` lists no inbox link, only
  ``受信メール設定変更`` under ``menu_inbox`` block

ネットマイル系の click-mails ship to the user's external Gmail only,
like warau / moppy / fruitmail. Adapter accordingly uses ``GmailSource``
with the standard label-skip pattern.

Background:
- 運営: 株式会社ネットマイル (Netmile, Inc.) — 既存 6 サイトと**完全に
  独立した親会社** = 真の分散価値
- レート: ``mile`` 単位。1 mile = 0.5 円相当 (推定、要 user FAQ 確認)
- 休眠条件: 未確認 (user に最新 FAQ 経由で確認推奨)
- ネットマイルアカウントと共有 (1 アカウントで複数サイト跨ぎ可)
- earning paths in scope: Gmail click-mails (自動化対象 ✅)
- ad-fraud out-of-scope: ガチャ・スロット系 (存在するが第三者広告経由)
- auth cookies live on www.netmile.co.jp (Rails _mediafactory-user_
  session + X-Oc-LBS LBS sticky) — login + balance scrape は netmile.co.jp
  ドメインに対して実行

**ad-fraud 隔離 (絶対遵守):**
- ``allowed_hosts`` は sugutama.jp / netmile.co.jp 系のみ。ガチャ
  subdomain は含めない
- parser の ``EXCLUSION_URL_RE`` で ガチャ / スロット / 抽選 / kuji
  / garapon 等 path を全部弾く
- 「クリックでXmile」「クリックでXpt」callout 付きでない URL は
  問答無用で drop

**user 側のセットアップ (Gmail label 作成は不要):**
Gmail IMAP ``STORE +X-GM-LABELS`` は label 不在時に自動作成するため、
``sugutama-clicked`` / ``sugutama-no-coins`` ラベルは最初のクリック
メール処理時に自動で生成される。事前作成は省略可。

Required Secrets:
  - ``SUGUTAMA_COOKIES`` — Cookie-Editor JSON export from a logged-in
    netmile.co.jp browser session (login verification + balance scrape
    のみで使用)
  - ``SLACK_CHANNEL_SUGUTAMA`` — Slack channel ID or ``#name``
  - ``GMAIL_CLIENT_ID`` / ``GMAIL_CLIENT_SECRET`` / ``GMAIL_REFRESH_TOKEN``
    — Gmail API OAuth2 認証 (moppy/warau 等と共有)
"""

import re

from ...common.adapter import Adapter
from ...common.sources import GmailSource
from .parser import parse as parse_email

# sugutama (netmile) の mypage は server-side でマイル数を render せず、
# DOM 上は ``<div class="mile add_mile js-user_point">------</div>`` の
# ように placeholder のみ。実値は client-side JS が API 経由で後から
# 埋める JS-rendered balance。HTTP GET の生 HTML には数値が含まれない。
#
# 過去の挙動 (2026-05-16 まで): DEFAULT_BALANCE_PATTERNS の末尾
# ``class="...point..."[^>]*>\s*(\d+)`` が embedded ``<script>`` 内
# (ライブラリ JS の class 属性風 string や JSON literal 等) のノイズと
# マッチして、"2026" のような無関係な数字を残高として誤検出していた
# (user 報告 2026-05-16: 実残高ゼロなのに Slack に 2026pt 表示)。
#
# 対策: balance scraping を実質 disable。下記 pattern は将来 sugutama
# が server-side render に切り替わった場合のみ拾う forward-compat 用
# で、現状では ``------`` placeholder のため常に None を返す。Slack 通知
# は「残高: 取得失敗 / 推定なし」表記になり pointincome と同じ運用。
#
# TODO (別 issue): JS render 後の balance を取る正攻法を実装する
# (Playwright で page.content() か、netmile passbook API の直接呼出し)。
_SUGUTAMA_BALANCE_PATTERNS = (re.compile(r'class="[^"]*js-user_point[^"]*"[^>]*>\s*([0-9,]+)\s*<'),)

ADAPTER = Adapter(
    name="sugutama",
    site_label="すぐたま",
    mypage_url="https://www.netmile.co.jp/sugutama/mypage",
    allowed_hosts=frozenset(
        {
            "sugutama.jp",
            "www.sugutama.jp",
            "netmile.co.jp",
            "www.netmile.co.jp",
        }
    ),
    login_keyword="ログアウト",
    gmail_query=("from:(sugutama.jp OR netmile.co.jp) -label:sugutama-clicked -label:sugutama-no-coins newer_than:3d"),
    clicked_label="sugutama-clicked",
    no_coins_label="sugutama-no-coins",
    source=GmailSource(parse_email=parse_email),
    balance_patterns=_SUGUTAMA_BALANCE_PATTERNS,
    discover_seeds=("https://www.netmile.co.jp/sugutama/mypage",),
    # mypage は ``js-user_point`` を JS で後埋め (HTTP GET の生 HTML は
    # placeholder のみ)。Playwright で page.content() を取れば JS render
    # 後の数値が含まれる。``balance_uses_browser=True`` で BrowserClicker
    # 経由の balance 取得に切替、``_SUGUTAMA_BALANCE_PATTERNS`` が hydration
    # 完了後の数値を pick up する。
    balance_uses_browser=True,
)
