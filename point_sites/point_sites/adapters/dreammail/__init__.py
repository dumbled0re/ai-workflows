"""dreammail.jp adapter — cookie-only lottery automation (Phase 1).

dreammail is a long-running (15+ 年) 懸賞 portal operated by 上場企業
group (1000万円 / 100万円 等の現金懸賞、即時抽選プレゼント、メダル
ベース通常懸賞). 2026-05-25 cost-low implementation: cookie-only
1-click paths — メダル消費型の応募と「0 メダル」 promo を組み合わせる
構成。Gmail OAuth setup は明示的に避けた (user 作業最小化)。

## 自動化 path 構成

| path | 種類 | クッキー消費 | 期待 yield |
|---|---|---|---|
| ``/game/gacha`` | medal-only payout | なし | 10-100 medals/日 (毎日ガチャ 1 回) |
| ``/mmillion`` | 現金 100 万円 entry | 50 medals/口 | 1 口/日 (毎日応募可能、抽選月 1) |
| ``/presents/precam/<id>`` | 0-medal promo | なし (動的 discovery) | promo 件数による |

## Phase 1 (本 commit) の限界 — 要 cookie 取得後の re-inspect

- ``/game/gacha`` / ``/mmillion`` の logged-in HTML は anonymous fetch 不可
  (login redirect)。下記 ``daily_wizards`` の selector は blind guess、
  user cookie が DREAMMAIL_COOKIES Secret に入った後に
  ``gh workflow run dreammail.yml -f inspect_url=...`` で実 HTML を確認、
  selector を refine する。click_force + use_navigation_click で fail-soft
  にしているので、selector miss でも例外は出ず silent no-op で次の wizard
  に進む。
- ``/presents/precam/<id>`` 動的 discovery は実装してあるが、precam page
  の応募 button selector も blind。最初の cron 後に Slack 出力を見て要 refine。

## User setup (1 回だけ)

1. https://www.dreammail.jp/touroku/ で会員登録 (PII 必要)
2. https://www.dreammail.jp/login でログイン
3. Cookie-Editor で dreammail.jp の cookies を JSON export
4. GitHub Secrets:
   - ``DREAMMAIL_COOKIES`` (JSON 全文)
   - ``SLACK_CHANNEL_DREAMMAIL`` (省略可、workflow で SLACK_CHANNEL_CHANCEIT に fallback)
5. ``.github/workflows/dreammail.yml`` cron が自動実行 (JST 8:45)

## TOS

dreammail 規約は「営利目的の使用禁止」のみで、自動応募 / プログラムによる
応募の明示禁止はなし。fruitmail と類似の posture。大量応募抑制のため
``dynamic_wizard_max_count=10`` で precam を 10 件 cap。
"""

from ...common.adapter import Adapter
from ...common.balance import DEFAULT_BALANCE_PATTERNS
from ...common.password_login import PasswordLoginConfig
from ...common.wizard import DailyWizard

# /game/gacha 毎日ガチャ wizard。1 日 1 回、結果は即時メール。
# blind selector — anonymous では login redirect で HTML 見れず。
# - 第 1 selector: 「ガチャを回す」 button (id / class 未確認)
# - 第 2 selector: 後続の confirm / 結果 button
# click_force でどの selector も silent no-op fail-soft。
# /game/gacha 構造 (2026-05-25 inspect run 26380200255 で確定):
#   <form action="/game/gacha/lottery" id="form-submit" method="post">
#     <input id="btn-submit" class="btn-gacha" type="submit" value="ガチャを回す">
#   </form>
# 1 click で POST → /game/gacha/lottery (結果 page)、結果は即時メール送付。
_GACHA_WIZARD = DailyWizard(
    name="dreammail_daily_gacha",
    url="https://www.dreammail.jp/game/gacha",
    clicks=(("#btn-submit", 1),),
    use_navigation_click=True,
    click_force=True,
    initial_wait_ms=3000,
    final_wait_ms=5000,
    title_selector="h1, h2",
    success_url_pattern=r"/game/gacha/lottery",
)


# /mmillion 現金 100 万円 (毎日応募、50 メダル/口)。
# blind selector で 「応募する」ボタンを推測。fruitmail と同じ
# ``#applyForm button[type="submit"]`` pattern なら通る可能性高い。
# /mmillion 構造 (2026-05-25 inspect run 26380225558 で確定):
#   <form action="/mmillion/apply" method="post" id="formMmillionDailyApply">
#     <button type="button" id="btnMmillionDailyApply" class="btn-black">
#       デイリー応募（50メダル）
#     </button>
#   </form>
#   ↓ click → confirm modal:
#   <button id="confirmYes" class="btn-black">はい</button>
#   <button id="confirmNo" class="btn-default">いいえ</button>
#   ↓ 「はい」 click → form JS submits → POST /mmillion/apply
# 注意: 50 メダル必要。残高 0 で発火しても server エラー → success_url_pattern
# 不一致で「未確定」になる (期待通り)。gacha 数日蓄積後に有効になる流れ。
_MMILLION_WIZARD = DailyWizard(
    name="dreammail_mmillion",
    url="https://www.dreammail.jp/mmillion",
    clicks=(
        # Step 1: デイリー応募 button → confirm modal を開く (JS handler)
        ("#btnMmillionDailyApply", 1),
        # Step 2: 「はい」 → form JS submit → POST /mmillion/apply
        ("#confirmYes", 1),
    ),
    use_navigation_click=True,
    click_force=True,
    initial_wait_ms=4000,
    inter_step_ms=3000,
    final_wait_ms=5000,
    title_selector="h1",
    success_url_pattern=r"/mmillion/apply",
)


# /presents/precam/<id> 0-medal promo の動的 discovery template。
# user cookie 取得後の inspect で確実な selector に refine する。
# anchor click で外部 ゆめキャン site にリダイレクト → impression credit。
# precam (0-medal promo) 構造 (inspect run 26380251527 で確定):
#   <a href="https://act.gro-fru.net/..." target="_blank"
#      class="gotoLink btn-black gtm-imp">このキャンペーンに参加する（無料）</a>
# 外部 ad-network (gro-fru.net) への redirect、参加 click 自体が credit。
# target="_blank" のため URL が現 page で変わらず success_url_pattern で
# verify 不可。click 自体は発火するが「成功確認」は別途別 path で必要。
# 暫定で NEVER_MATCH のまま — 真の verify path を別 commit で検討。
# Phase 2 案: framework に「target='' に書換えてから click」 option を
# 追加して same-tab navigation 化、URL で verify。
_PRECAM_TEMPLATE = DailyWizard(
    name="dreammail_precam",  # _<idx> suffix が runtime で付く
    url="<placeholder>",
    clicks=(
        # 「このキャンペーンに参加する」 anchor
        ("a.gotoLink.btn-black, a.gotoLink", 1),
    ),
    use_navigation_click=True,
    click_force=True,
    initial_wait_ms=3500,
    final_wait_ms=5000,
    title_selector="h1, h2, .prize_title, .campaign_title",
    # target="_blank" で同 page URL 変化なし → URL-based verify 不可
    success_url_pattern=r"__NEVER_MATCH__",
)


ADAPTER = Adapter(
    name="dreammail",
    site_label="ドリームメール",
    # mypage_url: トップページ (``/``)。anonymous でも 200 で返るが、nav の
    # logged-in/anonymous で差分が出る:
    #   - anonymous: ``<a href="/login?redirect_url=/" class="nav-login">ログイン</a>``
    #   - logged-in: ``<a href="/logout?...">ログアウト</a>`` (推定)
    # login_keyword=``/logout`` で logged-in 状態を検知 (anonymous body には
    # /logout URL が出ない、一方 logged-in body には header の logout link
    # として必ず登場する想定)。2026-05-25 inspect で確定。
    # 過去使った ``/my/modify`` + ``ログアウト`` の組合せは "stale cookies"
    # と判定されて inspect すらできなかった (verify_login 突破要)。
    mypage_url="https://www.dreammail.jp/",
    allowed_hosts=frozenset(
        {
            "dreammail.jp",
            "www.dreammail.jp",
            "yumecam.dreammail.jp",  # precam の外部リダイレクト先 subdomain
        }
    ),
    # 2026-05-25 確認: cookie 経由認証が server に reject されている
    # (login_keyword="" で verify_login を bypass して / inspect → body の
    # nav が anonymous render `class="nav-login"` のまま)。
    # 原因候補: session 切れ / UA bind / Cookie-Editor が HttpOnly session
    # cookie を export し損ね。
    # 復旧戦略: password_login=PasswordLoginConfig(...) で Playwright fresh
    # login fallback を導入 + DREAMMAIL_USER / DREAMMAIL_PASS Secret 登録。
    # 未着手のため cron は停止中 (.github/workflows/dreammail.yml)。
    login_keyword="/logout",
    # No click-mail pipeline (cookie-only Phase 1). future Phase 2 could
    # add GmailSource for メルマガクリック型 entries (1000万円 entry path)
    # but requires user-side Gmail OAuth setup.
    source=None,
    balance_patterns=DEFAULT_BALANCE_PATTERNS,
    discover_seeds=(
        "https://www.dreammail.jp/mypage",
        "https://www.dreammail.jp/presents",
        "https://www.dreammail.jp/game",
    ),
    # daily_wizards: 1) gacha (medal earning), 2) mmillion (100万 entry)
    daily_wizards=(
        _GACHA_WIZARD,
        _MMILLION_WIZARD,
    ),
    # dynamic discovery: /presents page を scrape して /presents/precam/<id>
    # の 0-medal promo URL を抽出 → template wizard で各 page を訪問。
    dynamic_wizard_list_url="https://www.dreammail.jp/presents",
    dynamic_wizard_link_selector='a[href*="/presents/precam/"]',
    dynamic_wizard_template=_PRECAM_TEMPLATE,
    dynamic_wizard_max_count=10,
    # Lottery output: 「応募した賞品一覧」 Slack format。daily gacha は
    # 厳密には抽選でないがメダル獲得を「応募成功」として表示 (本来の
    # 100万円 / precam が抽選 part)。
    lottery_mode=True,
    # 毎日 1 件以上の応募 + メダル獲得が想定通り。stagnation 検知は
    # 当選確率の低さで定義しにくい (chanceit と同じ判断)。
    stagnation_window=None,
    # 2026-05-25 追加: cookie 経由認証が server に reject される問題への
    # 復旧路。inspect (run 26379225015) で /login form 構造確定:
    #   - <form action="/login/index" id="login_form" method="post">
    #   - <input name="mailaddr" id="mailaddr" type="text">
    #   - <input name="password" id="password" type="password">
    #   - <input id="login_button" class="btn-orange" type="submit" value="ログイン">
    # success_marker は logged-in nav の logout link path。本 adapter の
    # login_keyword と同じ。
    # 必要 Secret: DREAMMAIL_USER (メールアドレス) / DREAMMAIL_PASS (パスワード)
    password_login=PasswordLoginConfig(
        login_url="https://www.dreammail.jp/login",
        username_selector="#mailaddr",
        password_selector="#password",
        submit_selector="#login_button",
        success_marker="/logout",
    ),
)
