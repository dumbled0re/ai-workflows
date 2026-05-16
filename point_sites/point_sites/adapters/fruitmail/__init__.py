"""Fruitmail / フルーツメール (https://www.fruitmail.net) adapter.

Status: scaffolded 2026-05-10. Awaiting ``FRUITMAIL_COOKIES`` +
``SLACK_CHANNEL_FRUITMAIL`` registration. mypage URL was confirmed
anonymously to be `/mypage/` (200), and daily-click landing
`/clickpoint/daily` is also 200 — both safe defaults for the first
authenticated probe.

Background:
- 運営: 株式会社 NTT カードソリューション / アイブリッジ系 (20 年老舗、ISMS 認証)
- earning paths (recon 2026-05-10): Gmail click-mail + on-site Webメール 受信箱 +
  daily click sections (`/clickpoint/daily`、`/point/click` 等). The first
  pass uses ``GmailSource`` only — same pattern as moppy/hapitas — and
  daily endpoints can be added later as ``EndpointPollSource`` once the
  Gmail flow is verified.
- conversion rate / 休眠条件 / click URL pattern は未確定。最初の cron 後に
  ``inspect_url=...`` で実 mypage と実 click-mail を見て regex を refine する。

Discover-time TODOs (after Secret 登録):
  1. ``gh workflow run fruitmail.yml -f discover=true`` で earning section の
     URL 構造を確認、必要なら ``discover_seeds`` を refine。
  2. ``gh workflow run fruitmail.yml -f inspect_url=https://www.fruitmail.net/mypage/``
     で mypage HTML を取得し、``DEFAULT_BALANCE_PATTERNS`` がマッチするか確認。
     合わなければ adapter 専用 ``balance_patterns`` を追加。
  3. 初の click-coin メールが届いたら ``gh workflow run fruitmail.yml -f extract_links=true``
     で URL 抽出を Slack で確認 → parser regex を実 URL に narrow。

Required Secrets:
  - ``FRUITMAIL_COOKIES`` — Cookie-Editor JSON export from a logged-in
    fruitmail.net browser session.
  - ``SLACK_CHANNEL_FRUITMAIL`` — Slack channel ID or ``#name``.
"""

from ...common.adapter import Adapter
from ...common.balance import DEFAULT_BALANCE_PATTERNS
from ...common.sources import GmailSource
from ...common.wizard import DailyWizard
from .parser import parse as parse_email

ADAPTER = Adapter(
    name="fruitmail",
    site_label="フルーツメール",
    # Verified anonymously 2026-05-10: ``/mypage/`` returns 200,
    # ``/my/`` is 404. Login keyword check (ログアウト) runs on this URL.
    mypage_url="https://www.fruitmail.net/mypage/",
    # ``apricot.fruitmail.net`` is the on-site game-server subdomain
    # for michannel / estlier — those embed 第三者ゲーム業者 jumps
    # (kantangame, dropgame, pochitto2 等) so they stay out of the
    # allowlist. ``slot.fruitmail.net`` hosts the in-house プレゼント
    # スロット (賞品: Amazon ギフト券・ポイント、当選は fruitmail 自
    # 社抽選) — that's added so the slot DailyWizard's navigation passes
    # ``is_manual_url_allowed`` and the wizard's Playwright session can
    # GET the page.
    allowed_hosts=frozenset({"fruitmail.net", "www.fruitmail.net", "slot.fruitmail.net"}),
    login_keyword="ログアウト",
    # Sender domain best-guess. If real mails come from a different
    # subdomain (e.g. ``info@fruitmail.net`` vs ``mail@``), broaden
    # after the first message arrives.
    gmail_query="from:fruitmail.net -label:fruitmail-clicked -label:fruitmail-no-coins newer_than:3d",
    clicked_label="fruitmail-clicked",
    no_coins_label="fruitmail-no-coins",
    source=GmailSource(parse_email=parse_email),
    # Default mypage markers (保有ポイント / 現在のポイント) cover most
    # Japanese point sites. Verify on first inspect; the recon could
    # not see balance widgets (anonymous view).
    balance_patterns=DEFAULT_BALANCE_PATTERNS,
    discover_seeds=(
        "https://www.fruitmail.net/mypage/",
        "https://www.fruitmail.net/clickpoint/daily",
    ),
    # プレゼントスロット (https://slot.fruitmail.net/present_slot/) は
    # 自社抽選 (賞品: Amazon ギフト券・ポイント) で第三者広告経由ではな
    # い。HTML inspect 2026-05-16 で確認できた action anchors:
    #   #start  ← スロット回転開始
    #   #stop   ← 停止 (停止位置でハズレ/当たり抽選)
    #   #retry  ← 次の回 (本日のプレイ回数 N まで)
    #   #end    ← 終了 (point_de に遷移)
    # 初回実装は 1 プレイ分のみ。selector 間 800ms wait が start→stop
    # の reel 回転時間として機能する想定。動作確認後に retry を追加する。
    # 「本日のプレイ回数」は JS で render されるため static HTML では
    # 上限不明 (typical 日本のポイントサイトスロットは 1-3 回/日)。
    #
    # ビンゴ (https://www.fruitmail.net/bingo/index.php) は自社運営。
    # form ``<input type="submit" id="bingo_start" name="bingo_card">``
    # で daily 1 回カード生成。完成 (= 縦横斜め揃う) でポイント獲得。
    # 1 日 1 回の submit で番号が 1 つずつ埋まる累積式が一般的なので、
    # cron で毎日 submit するだけで累積。観察期間が必要 (issue で追跡)。
    daily_wizards=(
        DailyWizard(
            name="fruitmail_present_slot",
            url="https://slot.fruitmail.net/present_slot/",
            clicks=(
                ("#start", 1),
                ("#stop", 1),
            ),
        ),
        DailyWizard(
            name="fruitmail_bingo",
            url="https://www.fruitmail.net/bingo/index.php",
            clicks=(("#bingo_start", 1),),
        ),
    ),
)
