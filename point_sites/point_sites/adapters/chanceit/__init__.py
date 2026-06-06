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

import re

from ...common.adapter import Adapter
from ...common.wizard import DailyWizard

# Chanceit-specific balance pattern. DEFAULT_BALANCE_PATTERNS picks up
# the first ``class="point_red">10pt`` it finds, which on tasklist.jsp
# is a *reward catalog* value (per-task reward shown next to a スタンプ
# 10個獲得 requirement), NOT the user's balance. That bug masked +0pt
# yield for 5 days (run logs reported "10pt" every day while the
# header pt counter was actually 11pt). Anchor on ``class="user_pt"``
# which only appears on the mypage header's balance link.
_CHANCEIT_BALANCE_PATTERNS = (re.compile(r'class="user_pt"[\s\S]*?>([0-9,]+)\s*pt', re.IGNORECASE),)


# /mypage/tasklist.jsp 「毎日コツコツ貯める」 daily missions の wizards.
#
# 2026-06-06 検証 (run 27052494587 等) で確定した事実:
#   - 5日 (06-01〜06-05) cron で 9 wizard × 5 = 45 visit 全 verify=True
#     なのに point_log の 2026-06 履歴は「該当の履歴はありません」 (0pt)
#   - 5月の click-log 全履歴も 5/31 1pt のみ (cron 開始 5/25 直後の偶発、
#     再現性なし)
#   - 旧実装は category index `/article/ranking/?g=6` を visit するだけ
#     で離脱。実 user は index から個別記事 detail link
#     (`?process=detail&id=NNNNN`) に進んで本文を読む
#
# 仮説: 「index visit」では server-side でスタンプ加算されない。
# detail page まで遷移して滞留すれば credit される可能性あり。
# 2026-06-13 まで観察、click-log に entry が出なければ adapter 撤退判断。
#
# 実装方針:
#   - `clicks=(("a[href*='process=detail']", 1),)` で index 内の最初の
#     detail link を click。use_navigation_click=True で href を follow
#   - `final_wait_ms=30000` で 30s detail page に滞留 (広告 impression +
#     view-tracking XHR 完了を待つ)
#   - `success_url_pattern` を `process=detail&id=\d+` に変更し、URL が
#     detail に遷移してることを真の verify 条件にする (= index page で
#     止まってたら未確定扱い)
#
# 除外 mission (旧コメントから保持):
#   - /game/ibgame/, /game/typing/, /game/mpgame/: 実プレイ必要
#   - /game/estlier/play.jsp?id=XX: anti-cheat 警告あり
#   - /pjump.srv?id=25677: アンケート → CLAUDE.md policy NG
#   - /#potitto-chance: home page fragment、別 mechanism
#   - /getfriend.srv: 友達紹介 referral、out of scope
def _article_visit(name: str, url: str) -> DailyWizard:
    return DailyWizard(
        name=name,
        url=url,
        # index page から最初の detail link を click して本文 page に遷移。
        # use_navigation_click=True で <a href> の href が follow される。
        clicks=(("a[href*='process=detail']", 1),),
        use_navigation_click=True,
        initial_wait_ms=3000,
        inter_click_ms=500,
        # detail page で 30s 滞留して広告 impression / view-tracking XHR
        # の完了を待つ。短すぎると credit trigger に届かない可能性あり。
        final_wait_ms=30000,
        title_selector="h1.header_title, h1",
        # 真の verify 条件: URL が `?process=detail&id=NNNNN` に遷移済か。
        # 旧 `chance\.com/article/` だと index page で止まっても match
        # するので false positive を生んでた (06-01〜06-05 5日連続 verify
        # pass で実 +0pt の根本原因)。
        success_url_pattern=r"process=detail&id=\d+",
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
    balance_patterns=_CHANCEIT_BALANCE_PATTERNS,
    discover_seeds=(
        "https://www.chance.com/present/list/easy-entry/",  # 応募が簡単
    ),
    # Static daily missions: /mypage/tasklist.jsp の article-view 系 9 件。
    # 各 wizard は visit-only (clicks=()) で server-side stamp 加算を期待。
    # dynamic_wizard とは別パスで並列実行される (main.py の wizards_to_run は
    # daily_wizards + 動的展開 wizards の concat)。
    daily_wizards=_TASKLIST_WIZARDS,
    # easy_apply (dynamic_wizard discovery from /present/list/easy-entry/)
    # は 2026-05-31 user 実証検証で disable 確定。
    #
    # 経緯:
    #   - 当初設計は「chanceit 会員 cookie で server 側が PII auto-fill、
    #     1 click で entry 完了する」 という公式仕様を信用した実装
    #   - bot の verify は「/present/detail/ から離れた = 応募成立」 という
    #     navigation-based チェックで、jump.srv の server-side 受理は確認
    #     してなかった
    #   - 2026-05-31 user が手動で「応募する」 button を 1 回 click → 別
    #     画面に遷移して、そこで **賞品ごとにバラバラなフォーム入力**
    #     必要と判明。chanceit 公式の「PII auto-fill」は実際には機能して
    #     おらず、bot の Slack 「応募確認済 14 件/日」 通知は **全部 false
    #     positive** だった
    #   - dreammail precam (commit 02904af) と同じパターンの誤検出を、
    #     chanceit でも同型の verify 緩さで踏んでた
    #
    # 教訓: 「navigation 完走 = 応募成立」 の verify は ad-funnel 系の
    # 仕様で必ず偽陽性を生む。framework 全体で再発防止すべき rule
    # (feedback_no_false_positive_notifications を補強)。
    #
    # 復活させる場合の前提: jump.srv hit 後の partner site 側 form 入力
    # まで含めた server-side 受理確認の仕組みが必要。賞品ごとに form が
    # 違うので「賞品ごと adapter」 レベルの実装が要求される = ROI 低い。
    # 抽選自動応募は撤退、user 手動応募に統一する判断。task_* wizards
    # (記事 visit でメダル獲得) は本物の獲得経路なので残す。
    # Lottery-style Slack: 「応募した賞品一覧」format。賞品名 + URL を
    # 列挙、user が当選時に内容を識別できるよう。
    lottery_mode=True,
    # 2026-06-06 検証で「抽選応募 / 応募確認済」表記は実態と乖離と確定
    # (chanceit cron は記事閲覧で stamp 加算を狙うだけで「応募」はしない)。
    # detail-click 仕様検証期間 (〜2026-06-13) は「(検証中) 記事閲覧 /
    # 着地確認」表示。仕様確定後に "記事閲覧" / "着地確認済" へ。
    lottery_run_header="(検証中) 記事閲覧",
    lottery_verified_label="着地確認済",
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
