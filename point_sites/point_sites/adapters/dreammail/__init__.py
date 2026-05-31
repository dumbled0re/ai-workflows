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

## Phase 2 で他ゲーム未追加の理由 (2026-05-25 inspect 結果)

``/game/`` 配下の 18+ ゲームを inspect (run 26402959549 等)した結果、
**1-click submit + URL-verifiable な game は ``/game/gacha`` のみ**。他
ゲームの追加は false positive リスクが高いので見送り:

- ``/game/flip`` / ``/game/farm`` / ``/game/farm/play`` / ``/game/sugoroku`` /
  ``/game/highlow`` / ``/game/maison`` / ``/game/wkbox`` / ``/game/garden`` /
  ``/game/balloon`` / ``/game/piyo`` / ``/game/japan`` / ``/game/math`` /
  ``/game/sudoku`` / ``/game/training``: いずれも ``/game/<name>`` は
  landing で ``<a href="/game/<name>/play">プレイする</a>`` の anchor のみ。
  ``/play`` page は canvas / multi-step / iterative なゲーム本体で 1-click
  submit form は無い (anti-cheat 検出リスクも高い)
- ``/omkj`` (おみくじ365): ``<button type="button" id="omkjBtn">`` を click
  すると XHR ``POST /api/omkj/check/`` + ``POST /api/omkj/challenge/`` が
  fire するが URL は ``/omkj`` のまま不変。``success_url_pattern=r"/omkj"``
  は click 不発でも match するので false positive trap。``success_text_marker``
  を pin する path は将来検討 (post-click DOM の 「大吉/中吉」等 fortune
  result を marker にする)
- ``/game/uquiz`` (ウルトラAIクイズ): Q&A → CLAUDE.md policy NG
- ``/game/seven`` (アマゾン 7DX slot): 3-reel 揃え judgement、anti-bot 検出
  リスク (warau 前例)

判定基準: ``success_url_pattern`` で server-side 受理を確実に verify
できる game のみ実装。XHR-only は ``success_text_marker`` の post-click
state を pin する別作業が要るので、yield 対 effort で見送り。

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
    # 完了 URL: ``/game/gacha/lotteried`` (lottery の過去形、抽選済結果 page)。
    # POST /game/gacha/lottery が 302 → /lotteried に redirect される実装。
    success_url_pattern=r"/game/gacha/lotteried",
)


# /omkj おみくじ365 wizard。1 日 1 回、メダル必ず獲得 (1 枚〜)。
# inspect (2026-05-31 run 26700895849) で確定:
#   <button type="button" id="omkjBtn" data-date="...">おみくじをひく</button>
# click 後 XHR `POST /api/omkj/check/` + `POST /api/omkj/challenge/` 発火、
# URL は ``/omkj`` のまま不変。result DOM:
#   <div id="resultOmkj" style="display: block;">
#     <div id="kikkyoOmkj"><img src="/img/omkj/kikkyo_N.png"></div>  ← 大吉/中吉/...
#     <div id="pointOmkj">メダル1枚進呈</div>                          ← 必ず 1 枚以上
#   </div>
# URL 不変 → success_url_pattern 使えず。success_text_marker で
# 「枚進呈」を check (landing には現れない、click 成功後のみ DOM に挿入)。
# メダル消費なし、副作用なし。
_OMKJ_WIZARD = DailyWizard(
    name="dreammail_omkj",
    url="https://www.dreammail.jp/omkj",
    clicks=(("#omkjBtn", 1),),
    # XHR-only (no navigation) なので False。click_force で element.click()
    # を evaluate-based 発火 (Playwright の actionability check を bypass)。
    use_navigation_click=False,
    click_force=True,
    initial_wait_ms=3000,
    final_wait_ms=5000,
    title_selector="#dateOmkj, h1",
    success_text_marker="枚進呈",
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
    # daily_wizards: /game/gacha (1 日 1 回ガチャ) + /omkj (おみくじ365)。
    # 両方ともメダル獲得専用 (消費なし)。mmillion (現金100万) は 50 メダル
    # 消費のため、メダル蓄積優先方針 ([[2026-05-31 user 方針]]) で外した。
    # gacha が安定してメダル稼げるようになったら mmillion 復活検討。
    daily_wizards=(_GACHA_WIZARD, _OMKJ_WIZARD),
    # precam (/presents/precam/<id>) 動的 discovery は 2026-05-31 に削除。
    # 「参加」 anchor の click 後に外部 ad-network に飛ぶだけで verify pass
    # 扱いにしていたが、実際の応募成立には外部 site でメアド + 複数項目の
    # form 入力が必要で「応募確認済」通知は誤検出だった (user 報告 +
    # CLAUDE.md の「false positive 禁止」「抽選応募は少項目で完結のみ」
    # rule に違反)。1-click form-less な precam を pre-filter する手段が
    # 無いので、precam 系を全面 disable。本物の lottery は gacha + mmillion
    # で十分。再導入するなら参加先 page を inspect して form 有無を判定する
    # 仕組みが必要。
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
