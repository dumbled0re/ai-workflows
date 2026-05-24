"""ハピタス (https://hapitas.jp) adapter.

Status: scaffolded. login_keyword + mypage_url are best guesses;
discover will tell us the real ones.

Site-specific risks documented at scaffold time:
- 2FA (TOTP) became opt-in 2024-10. If the user has 2FA on, this
  cookie-only flow won't survive: TOTP would be required on each
  re-auth. Currently scaffolded assuming 2FA OFF.
- Click-coin URLs valid for 7 days from mail receipt — the existing
  ``newer_than:3d`` window in the gmail query gives plenty of
  margin.
- Public site uses bare ``hapitas.jp`` (no ``www.``).

Required Secrets:
  - ``HAPITAS_COOKIES`` — JSON array exported from Cookie-Editor on
    a logged-in browser session.
  - ``SLACK_CHANNEL_HAPITAS`` — Slack channel ID or ``#name``.

Bring-up flow: identical to other adapters. See HANDOFF.md "新しい
サイトを追加する手順" section 5 for the verify → refine → ship steps.
"""

from ...common.adapter import Adapter
from ...common.balance import DEFAULT_BALANCE_PATTERNS
from ...common.sources import GmailSource
from ...common.wizard import DailyWizard
from .parser import parse as parse_email

ADAPTER = Adapter(
    name="hapitas",
    site_label="ハピタス",
    # ``/exchange/`` is one of the authenticated-only landings. The agent
    # research also surfaced ``/monitor/mypage/`` but the exchange page
    # is a more reliable verify_login target (always exists for any
    # logged-in account).
    mypage_url="https://hapitas.jp/exchange/",
    allowed_hosts=frozenset({"hapitas.jp", "www.hapitas.jp"}),
    login_keyword="ログアウト",
    gmail_query=("from:hapitas.jp -label:hapitas-clicked -label:hapitas-no-coins newer_than:7d"),
    clicked_label="hapitas-clicked",
    no_coins_label="hapitas-no-coins",
    source=GmailSource(parse_email=parse_email),
    balance_patterns=DEFAULT_BALANCE_PATTERNS,
    discover_seeds=(
        "https://hapitas.jp/exchange/",
        "https://hapitas.jp/takarakuji/",
        "https://hapitas.jp/minitakarakuji/",
    ),
    # Top-page 宝くじ交換券 banners (8/day, each grants 1 ticket). The
    # banner URLs rotate daily and only render after the JS hydration,
    # so cron has to discover them through a real browser before the
    # Clicker can hit each /item/redirect-to-client-if-zero-point-item/
    # tracking URL. ``clickget_banner`` is the wrapper class on the
    # rendered top page; the inner ``<a>`` carries the href that
    # credits the ticket on GET.
    daily_banner_url="https://hapitas.jp/",
    daily_banner_selector="div.clickget_banner > a[href]",
    # Spend 宝くじ交換券 on バラ (random individual numbers). The flow
    # is a 4-step wizard: pick mode → set count → next → confirm.
    # ``apart_ctrl`` opens the count-selector panel ``_08`` (default
    # count 10); ``up_takarakuji_exchange_08`` increments the count
    # to drain more tickets per run; 次へ advances to the ``_10``
    # confirmation; 交換する fires the AJAX. 30 Up clicks caps at the
    # user's actual ticket balance — extras hit a disabled max.
    daily_wizards=(
        DailyWizard(
            name="hapitas_takarakuji_exchange",
            url="https://hapitas.jp/minitakarakuji/",
            clicks=(
                ("#apart_ctrl", 1),
                ("#up_takarakuji_exchange_08", 30),
                ("#takarakuji_exchange_btn_next_08", 1),
                ("#takarakuji_exchange_btn_exchanges_10", 1),
            ),
        ),
        # 2026-05-23 ad-fraud policy 解禁後の追加 (issue #25)。
        #
        # hapitas top page (run 26328580122) で発見:
        #   - m.hapitas.jp/gmo/game/easygame   → hapitas.kantangame.com にリダイレクト
        #   - m.hapitas.jp/gmo/game/gesoten    → ゲソてん (GMO 系 mini-game hub)
        #   - m.hapitas.jp/gmo/game/quiz       → クイズ
        #   - m.hapitas.jp/gmo/game/spotdiff   → 間違い探し
        #
        # 各 entry URL は GMO ad-game platform への redirect 入口、訪問だけで
        # impression が出る。easygame inspect (run 26328615517) で確認: redirect 先で
        # /easygame/event (ミッション) + /easygame/lottery (抽選券) が存在。
        # interactive game でスコア出さないと credit しないかもしれないが、visit-only
        # で 1 週間 balance 観察してから escalate 判断。
        #
        # m.hapitas.jp は ``hapitas.jp`` の subdomain として is_manual_url_allowed
        # の subdomain-match で許可される (clicker.py 参照)。
        # GMO platform game 4 種 全部 interactive 化。hapitas quiz と同じ
        # ``c-n-btn-gameplay--start`` class は GMO 共通の「挑戦する/
        # プレイする」start button。click_force JS evaluate で ad-iframe
        # 隠れていても発火。
        DailyWizard(
            name="hapitas_gmo_easygame",
            url="https://m.hapitas.jp/gmo/game/easygame",
            clicks=(("a.c-n-btn-gameplay--start", 1),),
            use_navigation_click=True,
            click_force=True,
            final_wait_ms=20000,
        ),
        DailyWizard(
            name="hapitas_gmo_gesoten",
            url="https://m.hapitas.jp/gmo/game/gesoten",
            clicks=(("a.c-n-btn-gameplay--start", 1),),
            use_navigation_click=True,
            click_force=True,
            final_wait_ms=20000,
        ),
        DailyWizard(
            name="hapitas_gmo_quiz",
            url="https://m.hapitas.jp/gmo/game/quiz",
            clicks=(
                # quiz hub の「挑戦する」link click でクイズ開始。
                # hapitas.kantangame.com に redirect 後、navigation click 必要。
                ("a.c-n-btn-gameplay--start", 1),
            ),
            use_navigation_click=True,
            final_wait_ms=20000,
        ),
        DailyWizard(
            name="hapitas_gmo_spotdiff",
            url="https://m.hapitas.jp/gmo/game/spotdiff",
            clicks=(("a.c-n-btn-gameplay--start", 1),),
            use_navigation_click=True,
            click_force=True,
            final_wait_ms=20000,
        ),
    ),
    # password_login は 2026-05-21 に試したが reCAPTCHA v2 (``g-recaptcha`` class)
    # に弾かれて submit 後 login form のまま (run 26259164716)。Playwright stealth
    # では突破不可と確定したため設定を削除。Cookie 失効時は従来の Slack auth_error
    # path で user に Cookie 再エクスポートを要求する。pointtown と同類のパターン。
)
