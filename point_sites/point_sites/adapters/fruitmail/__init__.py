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
from ...common.password_login import PasswordLoginConfig
from ...common.sources import GmailSource
from ...common.wizard import DailyWizard
from .parser import parse as parse_email

ADAPTER = Adapter(
    name="fruitmail",
    site_label="フルーツメール",
    # Verified anonymously 2026-05-10: ``/mypage/`` returns 200,
    # ``/my/`` is 404. Login keyword check (ログアウト) runs on this URL.
    mypage_url="https://www.fruitmail.net/mypage/",
    # ``apricot.fruitmail.net`` / ``almond.fruitmail.net`` host the CM 視聴
    # / estlier ad-wall game hubs respectively — those redirect through
    # 第三者ゲーム業者 hosts (content-lump.net 等) but the entry URLs
    # themselves stay on the apricot/almond subdomain. 2026-05-23 ad-fraud
    # policy 解禁で visit-only wizard 化対象に追加 (apex ``fruitmail.net``
    # に subdomain-match で既に許可されていたが、明示化する)。
    # ``slot.fruitmail.net`` hosts the in-house プレゼントスロット (賞品:
    # Amazon ギフト券・ポイント、当選は fruitmail 自社抽選).
    allowed_hosts=frozenset(
        {
            "fruitmail.net",
            "www.fruitmail.net",
            "slot.fruitmail.net",
            "almond.fruitmail.net",
            "apricot.fruitmail.net",
        }
    ),
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
        # daily ログインボーナス。HTML inspect 2026-05-16 (point_de page) で
        # ``<button class="global_loginBonus__confirmButton">`` がヘッダー
        # widget として全 page に出現することを確認。click で stamp + reward
        # modal を開き、modal の background ヘッダーに ``data-is-today-
        # login-bonus-granted`` 属性が立ったら受け取り済 = fail-soft で skip。
        # mypage で実行 (top でも動くが pointtown 例に倣う)。
        DailyWizard(
            name="fruitmail_login_bonus",
            url="https://www.fruitmail.net/mypage/",
            clicks=((".global_loginBonus__confirmButton", 1),),
        ),
        # 2026-05-23 ad-fraud policy 解禁後の追加 (issue #26)。
        #
        # apricot.fruitmail.net/mch/michannel.php = CM 視聴 (CM をみてためる)。
        # inspect (run 26328480422) は networkidle で 30s timeout したが、これは
        # CM 動画 + 広告 SSP の polling が常時走るため。wizard runner は
        # ``wait_until="domcontentloaded"`` で goto するので timeout 回避できる
        # 想定 (main.py 682 行)。動画の自動再生 + 4.5s 滞留で視聴 impression が
        # 計上される最簡素実装。
        #
        # almond.fruitmail.net/estlier/ = ad-wall hub「コインアイランド」。
        # inspect (run 26328481364) で ターザンゲーム / パンボンスロット /
        # ファッションチェック 等の多数 mini-game への入口と判明。各 mini-game は
        # multi-step で実装難なので、hub の landing page だけ開いて広告 impression
        # を稼ぐ POC とする。
        #
        # 両 wizard は clicks=() で visit-only。1 週間 balance 観察で credit
        # 確認、無 yield なら multi-step / CM 視聴待機実装に escalate。
        # apricot michannel = fruitmail.cmnw.jp/cm にリダイレクトする「CM
        # 視聴」hub。inspect (run 26354XXX) で <a href="/cm/cmplay/<id>">
        # の CM コイン link が複数あることを確認 (1 つ 10 CMコイン)。
        # 最初の cmplay link を click_force nav で開き、CM 視聴 simulation
        # 35s 滞留。各 CM ID は時間で rotate するので href* で動的 match。
        DailyWizard(
            name="fruitmail_apricot_michannel",
            url="https://apricot.fruitmail.net/mch/michannel.php",
            clicks=(('a[href^="/cm/cmplay/"]', 1),),
            use_navigation_click=True,
            click_force=True,
            final_wait_ms=35000,
        ),
        DailyWizard(
            name="fruitmail_almond_estlier",
            url="https://almond.fruitmail.net/estlier/",
            clicks=(),
            # ad-wall hub への visit-only、sub-game 個別 wizard 化は下記。
        ),
        # almond コインアイランド (ad-wall hub) の sub-game 5 種を click_force
        # JS evaluate navigation で展開。inspect (run 26328481364) で発見した
        # 5 つの popular mini-game の path に href が match した瞬間 .click()
        # 発火。各 sub-game の rule.php / index.php に navigate して 15s 滞留。
        # 共通 URL は hub の almond.fruitmail.net/estlier/ で uid 等の query は
        # 自然に redirect 経由で content-lump.net に follow される想定。
        DailyWizard(
            name="fruitmail_almond_tarzan",
            url="https://almond.fruitmail.net/estlier/",
            clicks=(('a[href*="/tarzan2/fruitmail/"]', 1),),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            final_wait_ms=15000,
        ),
        # 2026-05-24: amefri estlier_panbon_slot (`e8acc62`) で発見した
        # i2ipoint platform の rule.php → game_start.php nav pattern を
        # /pc/<game>/ 系の sub-game にも適用。inter_step_ms=12000 で
        # 1st click 後の i2ipoint redirect chain settle を待つ。
        DailyWizard(
            name="fruitmail_almond_panbon_slot",
            url="https://almond.fruitmail.net/estlier/",
            clicks=(
                ('a[href*="/pc/panbon-slot/"]', 1),
                ('a[href="game_start.php"]', 1),
            ),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            inter_step_ms=12000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="fruitmail_almond_panbon_roulette",
            url="https://almond.fruitmail.net/estlier/",
            clicks=(
                ('a[href*="/pc/panbon-roulette/"]', 1),
                ('a[href="game_start.php"]', 1),
            ),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            inter_step_ms=12000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="fruitmail_almond_kokuhaku",
            url="https://almond.fruitmail.net/estlier/",
            clicks=(
                ('a[href*="/pc/kokuhaku/"]', 1),
                ('a[href="game_start.php"]', 1),
            ),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            inter_step_ms=12000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="fruitmail_almond_highlow",
            url="https://almond.fruitmail.net/estlier/",
            clicks=(
                ('a[href*="/pc/highlow/"]', 1),
                ('a[href="game_start.php"]', 1),
            ),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            inter_step_ms=12000,
            final_wait_ms=15000,
        ),
        # 追加 sub-game 4 種 (sarasara / fashion / cook / dog)。同 pattern で動作 想定。
        DailyWizard(
            name="fruitmail_almond_sarasara",
            url="https://almond.fruitmail.net/estlier/",
            clicks=(('a[href*="/sarasara/"]', 1),),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="fruitmail_almond_fashion",
            url="https://almond.fruitmail.net/estlier/",
            clicks=(
                ('a[href*="/pc/fashion/"]', 1),
                ('a[href="game_start.php"]', 1),
            ),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            inter_step_ms=12000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="fruitmail_almond_cook",
            url="https://almond.fruitmail.net/estlier/",
            clicks=(
                ('a[href*="/pc/cook/"]', 1),
                ('a[href="game_start.php"]', 1),
            ),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            inter_step_ms=12000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="fruitmail_almond_dog",
            url="https://almond.fruitmail.net/estlier/",
            clicks=(
                ('a[href*="/pc/dog/"]', 1),
                ('a[href="game_start.php"]', 1),
            ),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            inter_step_ms=12000,
            final_wait_ms=15000,
        ),
    ),
    # 2026-05-16 inspect (--anonymous) で確定。form id="login" action は
    # 同 URL POST。identifier は email or 会員 ID 両対応 (name="identifier")、
    # password は name="password"。submit は専用 class の button。
    # cookie 失効時に FRUITMAIL_USER / FRUITMAIL_PASS Secret から fresh login。
    password_login=PasswordLoginConfig(
        login_url="https://www.fruitmail.net/login?go_html=https://www.fruitmail.net/",
        username_selector="#user_identifier",
        password_selector="#password",
        submit_selector="button.login_index__loginButtonControl",
        success_marker="ログアウト",
    ),
)
