"""アメフリ (https://www.amefri.net) adapter — endpoint-poll source.

アメフリ has no click-mail flow; the daily reward is claimed by tapping
the "ログインボーナスを受け取る" button on the top page. We emulate
that with a single GET to the underlying URL via ``EndpointPollSource``,
gated to once per JST day by ``state_key = YYYY-MM-DD``.

Per-rank yield: 0.1〜2.5 円/day plus 1〜100 円 milestones every 30 logins.
At 10pt = 1円 (アメフリの conversion) the auto-click budget is small but
high purity (no ad-fraud risk).

**Risk-of-validity**: ``daily_bonus_url`` below is a *best-guess
placeholder*. Site auth-guards the actual click handler so the real URL
isn't visible from public scraping. Once ``AMEFURI_COOKIES`` is
registered, run::

    gh workflow run amefuri.yml -f discover=true

then read the discover log and update ``daily_bonus_url`` here to the
real claim URL (likely under ``/login_bonus`` or ``/daily``). The
``cmd_run`` pipeline will use balance-delta to verify whether the
endpoint actually credits points — codex's recommendation 2026-05-09
since HTTP 200 alone isn't proof of credit on these sites.

Required Secrets to enable:
  - ``AMEFURI_COOKIES`` — JSON array exported from Cookie-Editor on a
    logged-in browser session.
  - ``SLACK_CHANNEL_AMEFURI`` — Slack channel ID or ``#name``.
"""

import re

from ...common.adapter import Adapter
from ...common.balance import DEFAULT_BALANCE_PATTERNS
from ...common.browser_action import BrowserAction
from ...common.sources import EndpointPollSource
from ...common.wizard import DailyWizard

# Daily-login GET target. アメフリ has no discrete "claim bonus" button
# (verified via inspect_url 2026-05-09: /account shows the bonus as a
# status block, no onclick/AJAX trigger in the HTML). Per the in-page
# copy ("毎日のログイン 1pt") the trigger appears to be a daily session
# GET to an authenticated page; /account is the canonical logged-in
# landing and the most likely trigger.
#
# If balance doesn't grow after ~2 days of cron, the trigger may be the
# SSO login flow itself (not session reuse), in which case this source
# can't credit the bonus and amefuri should be disabled.
_DAILY_BONUS_URL = "https://www.amefri.net/account"

ADAPTER = Adapter(
    name="amefuri",
    site_label="アメフリ",
    # ``/account`` is the auth-gate landing — unauth visits are 302'd to
    # the i2i SSO login URL, so a 200 there with the login_keyword
    # present is a strong logged-in signal. ``/mypage`` simply doesn't
    # exist on amefri.net (404).
    mypage_url="https://www.amefri.net/account",
    # i2i (id.i2i.jp) is the SSO domain — login cookies live there too,
    # so the cookie jar must span both hosts for re-auth on session
    # rotation. Plain ``amefri.net`` covers any subdomain via Clicker's
    # suffix-match logic.
    allowed_hosts=frozenset({"amefri.net", "www.amefri.net", "id.i2i.jp"}),
    login_keyword="ログアウト",
    # Gmail fields stay blank — endpoint-poll source ignores them.
    source=EndpointPollSource(endpoint_url=_DAILY_BONUS_URL),
    # The default 保有ポイント patterns expect uppercase ``P``/``Ｐ`` near
    # the digits. アメフリ renders ``<span class="point">N</span>pt``
    # (lowercase ``pt``) with the digits buried 60+ chars after the
    # ``保有ポイント`` label, so we add a site-specific pattern that
    # anchors on the ``ownedPoint__point`` container instead.
    balance_patterns=(
        re.compile(r'class="ownedPoint__point"[\s\S]{0,120}?<span[^>]*class="point"[^>]*>([0-9,]+)</span>'),
        *DEFAULT_BALANCE_PATTERNS,
    ),
    discover_seeds=(
        "https://www.amefri.net/",
        "https://www.amefri.net/account",
    ),
    # アメフリ is an Angular SPA — the daily login bonus state machine
    # only fires from client JS. The two visits below let Playwright
    # run that JS so the 30-day login milestone counter ticks even when
    # we're a "たぬき" rank account whose per-day 1pt bonus rounds to 0
    # in the displayed balance.
    #
    # Gacha was investigated 2026-05-10: ``/game/gacha`` server-side
    # 302s to ``/`` for our authenticated session even via Playwright,
    # so spin automation is structurally blocked behind a feature gate
    # we can't clear without manual user action. Login milestone is
    # the only auto-clickable yield path on amefri.
    browser_actions=(
        BrowserAction(name="login_visit_home", url="https://www.amefri.net/"),
        BrowserAction(name="login_visit_account", url="https://www.amefri.net/account"),
    ),
    # 2026-05-23 ad-fraud policy 解禁 + user 提供 screenshot で
    # ``/special/freepoint`` (毎日貯める hub) に多数の wizard 候補が
    # あることが判明 (issue #28)。inspect runs:
    #   - 26335036974: hub の href 一覧で /video/* 7 entry + /game/gacha
    #   - 26335110777: /video/estlier/index/83 = PANBONスロット (host:
    #                  i2ipoint.nail-monster.work) を確定
    #   - 26335073242: /video/estlier/index/1 = コラムとアンケート list
    #
    # mapping (user screenshot tile vs URL):
    #   - 無料ガチャ            → /game/gacha (server 302 で blocked、別 issue)
    #   - アメフリゲームボックス → /video/gmomedia/easygame
    #   - 脳トレクイズ           → /video/gmomedia/quiz
    #   - 間違い探しボックス     → /video/gmomedia/spotdiff
    #   - PANBONスロット         → /video/estlier/index/83
    #   - コラムとアンケート     → /video/estlier/index/1 (list page 訪問のみ
    #                             → アンケート 自動回答はしない、policy 遵守)
    #   - アメフリ頭の体操広場   → /video/ibridge/index/stamp
    #   - みんなのフルーツ農場   → /video/ibridge/index/farm
    #
    # 全 wizard は visit-only (clicks=())、entry URL を開いて 4.5s 留まる
    # だけ。gmomedia easygame の inspect は 30s networkidle で timeout した
    # が wizard runner は domcontentloaded で goto するので問題なし想定。
    #
    # 1 週間 balance 観察で credit 確認、無 yield なら個別 multi-step に
    # escalate (別 follow-up issue)。期待値は user screenshot の最大値で
    # 合計 ~5000-50000 pt 範囲 (10pt=1円換算なので月 ~1500-15000 円相当)。
    daily_wizards=(
        # GMO platform 共通の「挑戦する」class ``c-n-btn-gameplay--start`` を
        # click_force JS evaluate で確実発火 → game プレイ画面へ navigate
        # して 20s 滞留 simulation。
        DailyWizard(
            name="amefuri_gmomedia_easygame",
            url="https://www.amefri.net/video/gmomedia/easygame",
            clicks=(("a.c-n-btn-gameplay--start", 1),),
            use_navigation_click=True,
            click_force=True,
            final_wait_ms=20000,
        ),
        DailyWizard(
            name="amefuri_gmomedia_quiz",
            url="https://www.amefri.net/video/gmomedia/quiz",
            clicks=(("a.c-n-btn-gameplay--start", 1),),
            use_navigation_click=True,
            click_force=True,
            final_wait_ms=20000,
        ),
        DailyWizard(
            name="amefuri_gmomedia_spotdiff",
            url="https://www.amefri.net/video/gmomedia/spotdiff",
            clicks=(("a.c-n-btn-gameplay--start", 1),),
            use_navigation_click=True,
            click_force=True,
            final_wait_ms=20000,
        ),
        # PANBONスロット (i2ipoint.nail-monster.work redirect)。inspect
        # で rule.php に <a href="game_start.php" class="link-button">
        # の「次へ進む」button を発見 (network capture で確認)。
        # wait_until="networkidle" にして redirect chain が settle するの
        # を待つ。click_force=True で JS evaluate 経由 click 発火。
        DailyWizard(
            name="amefuri_estlier_panbon_slot",
            url="https://www.amefri.net/video/estlier/index/83",
            clicks=(('a[href="game_start.php"]', 1),),
            wait_until="networkidle",
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            final_wait_ms=15000,
        ),
        # コラムとアンケート list page。visit-only だけど final_wait_ms を
        # 長めにして「list を眺めて立ち去る」simulation。アンケート自動回答は
        # しない (CLAUDE.md policy)。
        DailyWizard(
            name="amefuri_estlier_column",
            url="https://www.amefri.net/video/estlier/index/1",
            clicks=(),
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="amefuri_ibridge_stamp",
            url="https://www.amefri.net/video/ibridge/index/stamp",
            clicks=(),
        ),
        # stamp sub-games re-activated with click_force=True (framework
        # 改修 後述 commit)。Playwright actionability check を bypass し、
        # ad-iframe / sidebar collapse で hidden な link でも click 発火。
        # selector は href-based で安定 (data-layout-nav-id は server-render
        # 由来で確実、第三者 host も同形式)。各 sub-game の top.php
        # navigation → 15s 滞留で「entry 計上」impression yield 狙い。
        DailyWizard(
            name="amefuri_stamp_nanpre",
            url="https://www.amefri.net/video/ibridge/index/stamp",
            clicks=(('a[href*="/nanpre/top.php"]', 1),),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="amefuri_stamp_keisan",
            url="https://www.amefri.net/video/ibridge/index/stamp",
            clicks=(('a[href*="/keisan/top.php"]', 1),),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="amefuri_stamp_eitango",
            url="https://www.amefri.net/video/ibridge/index/stamp",
            clicks=(('a[href*="/eitango/top.php"]', 1),),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="amefuri_stamp_shape_memory",
            url="https://www.amefri.net/video/ibridge/index/stamp",
            clicks=(('a[href*="/shape_memory/top.php"]', 1),),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="amefuri_stamp_sakana",
            url="https://www.amefri.net/video/ibridge/index/stamp",
            clicks=(('a[href*="/sakana/top.php"]', 1),),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            final_wait_ms=15000,
        ),
        # 追加 sub-game 5 種。脳トレ系で popular な crossword / jhistory /
        # prefectures / shisokuenzan / dkanji を選定。残り (sanji / kokki /
        # tsume_shogi / proverb / library / elavator / tenshoot / balance /
        # movie) は次回 batch。adenq は広告アンケート系で out of scope。
        DailyWizard(
            name="amefuri_stamp_crossword",
            url="https://www.amefri.net/video/ibridge/index/stamp",
            clicks=(('a[href*="/crossword/top.php"]', 1),),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="amefuri_stamp_jhistory",
            url="https://www.amefri.net/video/ibridge/index/stamp",
            clicks=(('a[href*="/jhistory/top.php"]', 1),),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="amefuri_stamp_prefectures",
            url="https://www.amefri.net/video/ibridge/index/stamp",
            clicks=(('a[href*="/prefectures/top.php"]', 1),),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="amefuri_stamp_shisokuenzan",
            url="https://www.amefri.net/video/ibridge/index/stamp",
            clicks=(('a[href*="/shisokuenzan/top.php"]', 1),),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="amefuri_stamp_dkanji",
            url="https://www.amefri.net/video/ibridge/index/stamp",
            clicks=(('a[href*="/dkanji/top.php"]', 1),),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            final_wait_ms=15000,
        ),
        # 残り 9 sub-game (sanji/kokki/tsume_shogi/proverb/library/elavator/
        # tenshoot/balance/movie)。adenq は広告アンケート系で out of scope。
        DailyWizard(
            name="amefuri_stamp_sanji",
            url="https://www.amefri.net/video/ibridge/index/stamp",
            clicks=(('a[href*="/sanji/top.php"]', 1),),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="amefuri_stamp_kokki",
            url="https://www.amefri.net/video/ibridge/index/stamp",
            clicks=(('a[href*="/kokki/top.php"]', 1),),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="amefuri_stamp_tsume_shogi",
            url="https://www.amefri.net/video/ibridge/index/stamp",
            clicks=(('a[href*="/tsume_shogi/top.php"]', 1),),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="amefuri_stamp_proverb",
            url="https://www.amefri.net/video/ibridge/index/stamp",
            clicks=(('a[href*="/proverb/top.php"]', 1),),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="amefuri_stamp_library",
            url="https://www.amefri.net/video/ibridge/index/stamp",
            clicks=(('a[href*="/library/top.php"]', 1),),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="amefuri_stamp_elavator",
            url="https://www.amefri.net/video/ibridge/index/stamp",
            clicks=(('a[href*="/elavator/top.php"]', 1),),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="amefuri_stamp_tenshoot",
            url="https://www.amefri.net/video/ibridge/index/stamp",
            clicks=(('a[href*="/tenshoot/top.php"]', 1),),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="amefuri_stamp_balance",
            url="https://www.amefri.net/video/ibridge/index/stamp",
            clicks=(('a[href*="/balance/top.php"]', 1),),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="amefuri_stamp_movie",
            url="https://www.amefri.net/video/ibridge/index/stamp",
            clicks=(('a[href*="/movie/top.php"]', 1),),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=6000,
            final_wait_ms=15000,
        ),
        DailyWizard(
            name="amefuri_ibridge_farm",
            url="https://www.amefri.net/video/ibridge/index/farm",
            clicks=(
                # farm landing で「ゲームスタート」<a href> click。
                # navigation_click で /game/landing.php?_method=confirm に遷移。
                # その後 30s 滞留で農場本編 (種まき/水やり/収穫) を simulate。
                # 「1日8回プレイ可能」なので 1 wizard 1 回相当。
                (".btn_set.cont_m a.btn_positive", 1),
            ),
            use_navigation_click=True,
            final_wait_ms=30000,
        ),
        # /game/gacha は server-side 302 で / にリダイレクトされる (確認:
        # run 26334911695、26335071376)。referer 不足 or feature gate と推定。
        # framework 拡張 (commit 8e9ae18) で referer 指定が可能になったので、
        # /special/freepoint からの navigation を装う。これで 302 が解除
        # されたら gacha ページに着地して entry tracking が記録される想定。
        DailyWizard(
            name="amefuri_gacha",
            url="https://www.amefri.net/game/gacha",
            clicks=(),
            referer="https://www.amefri.net/special/freepoint",
        ),
    ),
    # 1pt/day yield rounds out credit-ratio's MIN_EXPECTED_FOR_RATIO
    # threshold (=2), so the strong detector skips amefri entirely.
    # 30 runs ≈ one full milestone cycle (10〜100pt jump every 30
    # logins) — if balance stays flat through that, the login bonus
    # endpoint is genuinely silent, not just yielding below display
    # precision.
    stagnation_window=30,
    # password_login は 2026-05-22 に試したが i2i SSO chain で躓いて撤回。
    # amefri.net の "ログイン" link は ``goto`` だけでなく動的 ``rtoken``
    # (CSRF、amefri 側でセッション per-request 発行) を含む URL で i2i に
    # 飛ぶ。これが無いと SSO chain (i2i ログイン → amefri.net/service/login
    # の trampoline → amefri 側 session 復元) が完成しない。run 26260139555
    # で確認: goto あっても rtoken なしだと submit 後 amefri.net 根 page に
    # 着地するが session 未復元、ログアウト リンクが見つからず fail。
    #
    # 修正には password_login.py framework に「初回 navigation で home page
    # の login link href を抽出 → そこへ navigate」という 2-step flow を入れ
    # る必要あり (別 issue で対応予定)。それまで Cookie-only で運用、Cookie
    # 失効時は Slack auth_error で user に Cookie 再エクスポートを依頼。
)
