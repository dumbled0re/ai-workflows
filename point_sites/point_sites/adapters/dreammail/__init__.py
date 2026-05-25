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
from ...common.wizard import DailyWizard

# /game/gacha 毎日ガチャ wizard。1 日 1 回、結果は即時メール。
# blind selector — anonymous では login redirect で HTML 見れず。
# - 第 1 selector: 「ガチャを回す」 button (id / class 未確認)
# - 第 2 selector: 後続の confirm / 結果 button
# click_force でどの selector も silent no-op fail-soft。
_GACHA_WIZARD = DailyWizard(
    name="dreammail_daily_gacha",
    url="https://www.dreammail.jp/game/gacha",
    clicks=(
        # 候補 1: id="play_button" or "gacha_start" pattern
        ('button[id*="play"], button[id*="gacha"], a[id*="play"]', 1),
        # 候補 2: class に "gacha" or "play" or "spin" を含む button/anchor
        ('button[class*="gacha"], button[class*="play"], a[class*="gacha"]', 1),
        # 候補 3: 結果モーダルの「閉じる」/「OK」 button
        ('button[id*="close"], button[class*="close"], button[type="submit"]', 1),
    ),
    use_navigation_click=True,
    click_force=True,
    initial_wait_ms=4000,
    inter_step_ms=3000,
    final_wait_ms=5000,
    title_selector="h1, h2",  # gacha タイトルを 「応募成功」表示用に流用
)


# /mmillion 現金 100 万円 (毎日応募、50 メダル/口)。
# blind selector で 「応募する」ボタンを推測。fruitmail と同じ
# ``#applyForm button[type="submit"]`` pattern なら通る可能性高い。
_MMILLION_WIZARD = DailyWizard(
    name="dreammail_mmillion",
    url="https://www.dreammail.jp/mmillion",
    clicks=(
        # 候補 1: 標準的な form submit
        ('form button[type="submit"], form input[type="submit"]', 1),
        # 候補 2: anchor with "entry" or "応募"
        ('a[href*="/entry/"], a[href*="/presents/entry"]', 1),
        # 候補 3: 確認 page の最終 submit
        ('button[type="submit"], form input[type="submit"]', 1),
    ),
    use_navigation_click=True,
    click_force=True,
    initial_wait_ms=4000,
    inter_step_ms=4000,
    final_wait_ms=5000,
    title_selector="h1",  # ページタイトルを流用
)


# /presents/precam/<id> 0-medal promo の動的 discovery template。
# user cookie 取得後の inspect で確実な selector に refine する。
# anchor click で外部 ゆめキャン site にリダイレクト → impression credit。
_PRECAM_TEMPLATE = DailyWizard(
    name="dreammail_precam",  # _<idx> suffix が runtime で付く
    url="<placeholder>",
    clicks=(
        # precam page 内の「応募する」/「ゆめキャンで応募」button
        ('a[href*="yumecam"], a[href*="precam"][href*="entry"], a.entry, button.entry', 1),
        # 標準的な form submit fallback
        ('form button[type="submit"], button[type="submit"]', 1),
    ),
    use_navigation_click=True,
    click_force=True,
    initial_wait_ms=3500,
    inter_step_ms=3000,
    final_wait_ms=5000,
    title_selector="h1, h2, .prize_title, .campaign_title",
)


ADAPTER = Adapter(
    name="dreammail",
    site_label="ドリームメール",
    # mypage_url: ``/my/modify`` (登録情報の確認・変更) is the canonical
    # authenticated page. Anonymous access redirects to /login; with a
    # valid cookie the header carries "ログアウト" (login_keyword).
    # ``/mypage`` is a 404; the path prefix for member pages is ``/my/``.
    mypage_url="https://www.dreammail.jp/my/modify",
    allowed_hosts=frozenset(
        {
            "dreammail.jp",
            "www.dreammail.jp",
            "yumecam.dreammail.jp",  # precam の外部リダイレクト先 subdomain
        }
    ),
    login_keyword="ログアウト",
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
)
