"""ChanceIt (https://www.chance.com) adapter — 抽選自動応募専用.

ChanceIt is a Japanese lottery (懸賞) portal aggregating ~500 active
prize campaigns. Unlike point_sites' other Gmail-driven adapters, the
yield mechanism here is **dynamic-list-discovery + button-click**:

  1. Visit /present/list/easy-entry/ (応募形式 = 応募が簡単 ~14 件/日)
  2. URL-dedup, cap to 20
  3. For each prize page (/present/detail/<id>/), click the 「応募する」
     anchor (href=/jump.srv?id=<id>) — fires the application
  4. Member cookie 由来で氏名 / 住所 / 電話 / メール は server 側で
     自動付与、bot 側に PII 不要 (cookie だけで動く)

2026-05-27: 賞品カテゴリ拡張 (cash-giftcard / instant-win 等) は X 投稿系
prize が混入する事が user 観察で判明し撤回 (feedback_lottery_entry_criteria)。
easy-entry のみは構造的に「応募形式 = 応募が簡単」 で安全。

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
  - **dynamic_wizard_max_count=20**: easy-entry の 14 件 + バッファ。selector
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


# /mypage/tasklist.jsp 「毎日コツコツ貯める」 daily missions の visit-only
# wizards. 2026-05-25 anonymous inspect (run 26402797581) で確定:
#
#   <div id="task_list"><div class="my_task">
#     <table><tbody>
#       <tr><td class="pt_contents"><div class="do">
#         <a href="https://www.chance.com/article/ranking/?g=6">芸能人...</a>
#       </div></td>
#       <td><p>スタンプ10個獲得</p></td>
#       <td><p class="get-pt"><span class="point_red">10pt</span></p></td>
#       </tr>...
#
# 9 件の article 系 mission は URL を訪問するだけで server-side でスタンプが
# 加算される (= 1-click visit-only)。会員 cookie 必須なので anonymous で
# inspect 出来ても credit はされない。各 article は別 page なので 1 wizard
# = 1 article visit。
#
# 除外 mission:
#   - /game/ibgame/, /game/typing/, /game/mpgame/: 実プレイ必要 (canvas /
#     score-gated)。anti-cheat 検出リスク
#   - /game/estlier/play.jsp?id=XX: getmoney と同じ NUMBERS DX 系で
#     anti-cheat (run 26357587465) 警告あり、skip
#   - /pjump.srv?id=25677: アンケート → CLAUDE.md policy NG (survey
#     data fraud)
#   - /#potitto-chance: home page fragment、別 mechanism
#   - /getfriend.srv: 友達紹介 referral、out of scope
#
# success_url_pattern: 訪問先がそのまま article page に着地するなら
# chance.com/article/ で match。login redirect / error 時は別 URL に
# なるので不一致 → 「未確定」表示。
# title_selector: h1.header_title が article のタイトル要素。
def _article_visit(name: str, url: str) -> DailyWizard:
    return DailyWizard(
        name=name,
        url=url,
        clicks=(),
        initial_wait_ms=2000,
        # 5s 滞留で広告 impression + view-tracking XHR が走る時間を確保。
        final_wait_ms=5000,
        title_selector="h1.header_title, h1",
        success_url_pattern=r"chance\.com/article/",
    )


_TASKLIST_WIZARDS: tuple[DailyWizard, ...] = (
    _article_visit("chanceit_task_ranking_geinou", "https://www.chance.com/article/ranking/?g=6"),
    _article_visit("chanceit_task_ranking_entame", "https://www.chance.com/article/ranking/?g=5"),
    _article_visit("chanceit_task_ranking_life", "https://www.chance.com/article/ranking/?g=7"),
    _article_visit("chanceit_task_prenew_entertainment", "https://www.chance.com/article/prenew-entertainment/"),
    _article_visit("chanceit_task_prenew_lifestyle", "https://www.chance.com/article/prenew-lifestyle/"),
    _article_visit("chanceit_task_dog", "https://www.chance.com/article/dog/"),
    _article_visit("chanceit_task_cat", "https://www.chance.com/article/cat/"),
    _article_visit("chanceit_task_ichioshi", "https://www.chance.com/article/ichioshi.srv"),
    _article_visit("chanceit_task_ai", "https://www.chance.com/article/ai/"),
)


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
        "https://www.chance.com/present/list/easy-entry/",  # 応募が簡単
    ),
    # Static daily missions: /mypage/tasklist.jsp の article-view 系 9 件。
    # 各 wizard は visit-only (clicks=()) で server-side stamp 加算を期待。
    # dynamic_wizard とは別パスで並列実行される (main.py の wizards_to_run は
    # daily_wizards + 動的展開 wizards の concat)。
    daily_wizards=_TASKLIST_WIZARDS,
    # Dynamic wizard discovery: ``/present/list/easy-entry/`` のみを scrape する。
    # 2026-05-27 巻き戻し (user 観察 + feedback_lottery_entry_criteria に従う):
    # 2026-05-25 に 4 list (easy-entry + daily-weekly-entry + instant-win +
    # cash-giftcard) に拡張したが、後者 3 つは「賞品カテゴリ」 (現金 / 即時 /
    # 毎日) であって「応募形式」 ではないため、X(Twitter)から応募 / Facebookから応募
    # 等の SNS 投稿必須 prize も内包する事が user 観察で判明 (49 件中の偽陽性源)。
    # ``easy-entry/`` だけは「応募形式 = 応募が簡単」 (= 1-click cookie auto-fill
    # のみ) で構造的に SNS 投稿系を含まないので、これだけに戻して安全運用。
    # 期待件数: 14 件/日 (2026-05-24 計測値)。
    #
    # 賞品カテゴリ拡張をやり直す場合は「応募形式 = 応募が簡単 ∩ 賞品カテゴリ」 の
    # 交差 list URL があれば使う、無ければ detail page で「応募形式」 td を確認して
    # post-filter する設計が必要。現状は素直に easy-entry のみで運用。
    dynamic_wizard_list_urls=("https://www.chance.com/present/list/easy-entry/",),
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
    # 2026-05-27 巻き戻し: easy-entry のみに戻ったので cap も 20 に戻す
    # (元実装値、~14 件/日 + バッファ)。selector misfire で 100 件超 click
    # とかを防ぐ safety cap。
    dynamic_wizard_max_count=20,
    # Lottery-style Slack: 「応募した賞品一覧」format。賞品名 + URL を
    # 列挙、user が当選時に内容を識別できるよう。
    lottery_mode=True,
    # 当選確率が低い & 1 日 14 件程度なので stagnation 判定難しい。
    # 1 月程度 yield 観察してから stagnation_window を設定する判断。
    stagnation_window=None,
    # 2026-05-25 password_login fallback 試行 → 失敗 (run 26400839026 /
    # 26400937339):
    #   - page.click() on <input type="image"> でも form.submit() bypass
    #     でも login.srv に form 再表示で server 拒否
    #   - user 確認: brower での手動 login は同じ credentials で成功 →
    #     credentials は正しい
    #   - 結論: chanceit は GitHub Actions runner IP (US data center) から
    #     の login を server-side で拒否する仕組みあり (IP geofence +
    #     device fingerprint な anti-bot)。pointincome の JP geofence
    #     問題と同種で framework 側からは突破不可
    # → password_login は無効化。cookie 主体運用に戻す。cookie 失効時は
    # user が Cookie-Editor で再 export → CHANCEIT_COOKIES Secret 更新が必要
    # (= 元の運用)。
    password_login=None,
)
