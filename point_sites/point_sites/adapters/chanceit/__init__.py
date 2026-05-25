"""ChanceIt (https://www.chance.com) adapter — 抽選自動応募専用.

ChanceIt is a Japanese lottery (懸賞) portal aggregating ~500 active
prize campaigns. Unlike point_sites' other Gmail-driven adapters, the
yield mechanism here is **dynamic-list-discovery + button-click**:

  1. Visit /present/list.jsp?type=6 ("応募が簡単" 14 件 daily-rotating)
  2. For each prize page (/present/detail/<id>/), click the 「応募する」
     anchor (href=/jump.srv?id=<id>) — fires the application
  3. Member cookie 由来で氏名 / 住所 / 電話 / メール は server 側で
     自動付与、bot 側に PII 不要 (cookie だけで動く)

Setup (user side, 1 回だけ):
  1. New Gmail (lottery 専用) 作成 — memory feedback_gmail_shared_hub_risk
     (2026-05-17 incident) に従い既存 ポイ活 Gmail と分離
  2. ChanceIt に会員登録 (氏名/住所/電話 入力、メール認証)
  3. Cookie-Editor で chance.com の cookie を JSON export
  4. GitHub Secrets:
      - CHANCEIT_COOKIES (JSON 全文)
      - SLACK_CHANNEL_CHANCEIT
  5. ``.github/workflows/chanceit.yml`` の cron で自動実行

設計判断:
  - **source = None (wizards-only mode)**: 抽選専用なので click-mail
    pipeline は不要。Gmail API setup を user 作業から外す。
    将来 chanceit からのポイ活メルマガを処理したくなったら GmailSource
    に切替可能 (新 Gmail に Gmail API 設定が必要)
  - **dynamic_wizard_* fields**: chanceit の prize 一覧は毎日入れ替わる
    ので static daily_wizards では追従できない。framework 拡張で
    list page を毎 cron scrape して動的に wizards を生成
  - **dynamic_wizard_max_count=30**: 1 日 14 件 + バッファ。selector
    misfire で 100 件超 click とかを防ぐ safety cap

TOS 注意 (memory project_chanceit_tos 参照、本 adapter で明文化):
  - chanceit 利用規約 第 5 条: 「コンピュータウィルス等」「虚偽の
    情報送信」が明示禁止。**自動 click 自体は明示禁止されていない**が
    「不正行為」と判定されれば「会員資格停止」「損害賠償」可能性あり
  - 大量同時応募はリスク高、max_count + click_interval で抑制
  - 「自動入力機能」を公式提供している (会員登録時の PII を form
    auto-fill) ことから permissive と推定
"""

from __future__ import annotations

from ...common.adapter import Adapter
from ...common.balance import DEFAULT_BALANCE_PATTERNS
from ...common.wizard import DailyWizard

ADAPTER = Adapter(
    name="chanceit",
    site_label="チャンスイット",
    # 2026-05-24 確認: /member/mypage.jsp は 404、/mypage/tasklist.jsp
    # (毎日コツコツ貯める page) は logged-in 必須で「ログアウト」リンクを
    # 含むので login_verification に適する。
    mypage_url="https://www.chance.com/mypage/tasklist.jsp",
    allowed_hosts=frozenset({"chance.com", "www.chance.com", "prize.chance.com"}),
    login_keyword="ログアウト",
    # source=None: wizards-only adapter. click-mail pipeline 不要、
    # Gmail API setup 不要、cookie だけで daily 動作。
    source=None,
    balance_patterns=DEFAULT_BALANCE_PATTERNS,
    discover_seeds=(
        "https://www.chance.com/present/list.jsp?type=6",  # 応募が簡単
    ),
    # Dynamic wizard discovery: daily で /present/list.jsp?type=6 を
    # scrape → 個別 prize page link 全部を wizard に変換 →
    # 各 page で 応募 button を click。
    # selector: 個別 prize page anchor (`/present/detail/<id>/`)
    dynamic_wizard_list_url="https://www.chance.com/present/list.jsp?type=6",
    dynamic_wizard_link_selector='a[href*="/present/detail/"]',
    dynamic_wizard_template=DailyWizard(
        name="chanceit_easy_apply",  # name suffixed with _<index> at runtime
        url="<placeholder>",  # replaced with each discovered prize URL
        clicks=(
            # 「この懸賞に応募する」button (anchor to /jump.srv?id=XXX)。
            # chanceit 会員 cookie で server 側が PII auto-fill、
            # 1 click で entry 完了する想定 (公式「30秒足らずで応募」)。
            ('a[href*="/jump.srv?id="]', 1),
        ),
        use_navigation_click=True,
        click_force=True,
        initial_wait_ms=3000,
        final_wait_ms=8000,
        # 2026-05-25 確定 (run 26387678417 inspect):
        # apply anchor は ``target="_blank"`` で新 tab を開く構造のため、
        # 素朴な click では現 page URL が変化せず silent no-op になる。
        # dreammail precam と同じく pre_click_evaluate で target を剥離して
        # same-tab navigation 化、/jump.srv?id=<id> へ遷移させる。
        pre_click_evaluate=(
            "document.querySelectorAll('a[href*=\"/jump.srv?id=\"]')"
            ".forEach(function(a) { a.target = '_self'; a.removeAttribute('target'); });"
        ),
        # click 後の遷移先候補:
        #   - /jump.srv?id=<id> → 外部 partner サイト (= chance.com 以外)
        #   - /jump.srv?id=<id> → /thanks/ や /complete/ (chance.com 内)
        # /present/detail/<id>/ に残っていれば click 不発 = 未確定。
        # /jump.srv または chance.com 以外の host にいれば応募 click 成立。
        # ``error`` 含むのは 失敗 URL なので除外。
        success_url_pattern=r"^(?!.*/present/detail/)(?!.*error)",
    ),
    dynamic_wizard_max_count=20,
    # Lottery-style Slack: 「応募した賞品一覧」format。賞品名 + URL を
    # 列挙、user が当選時に内容を識別できるよう。
    lottery_mode=True,
    # 当選確率が低い & 1 日 14 件程度なので stagnation 判定難しい。
    # 1 月程度 yield 観察してから stagnation_window を設定する判断。
    stagnation_window=None,
)
