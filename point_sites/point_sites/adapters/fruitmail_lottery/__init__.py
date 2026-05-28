"""Fruitmail prize (懸賞) lottery adapter — sister to ``fruitmail`` adapter.

Background:
  Fruitmail has been a 懸賞 (lottery) portal since 2000 with 10 prize
  categories. The existing ``fruitmail`` adapter only covers the
  point-earning paths (slot / bingo / login_bonus / CM 視聴 /
  almond mini-games — 22 wizards). The 5 free-entry prize categories
  (毎日 / 毎週 / 毎月 / 豪華 / プレミアム) — i.e. the site's original
  raison d'être — were untapped until 2026-05-25.

  This adapter runs the prize-entry flow daily:
    1. Visit /prize/<category>/ (e.g. /prize/everyday/)
    2. Set selected_apply_number=1 on the form (default is "選択"
       which trips the ``required`` validator)
    3. Click the submit button — POSTs to /prize/step1/
    4. On step1, click the second submit button — POSTs to /prize/step2/
       (the「登録情報を確認する」step that finalizes the entry)
  Each category exposes the same ``#applyForm`` with hidden
  ``item_name`` carrying the day's prize title. That value is surfaced
  in the Slack「応募した賞品一覧」output via ``title_selector``.

Architecture decision: separate adapter, shared credentials.
  - ``lottery_mode=True`` replaces the entire Slack output flow with
    ``send_lottery_summary``, which would clobber the existing
    fruitmail point-credit summary. Cleaner to split into two adapters
    that share the same Fruitmail account + cookie.
  - ``source=None``: this adapter doesn't process click-mail. The
    parent ``fruitmail`` adapter handles all Gmail click-coin URLs.
  - Cookies + login credentials are mapped from the existing fruitmail
    Secrets at the workflow level (no new user setup required beyond
    SLACK_CHANNEL).
  - Slack channel: defaults to SLACK_CHANNEL_CHANCEIT (= #lottery), so
    fruitmail prize and chanceit entries land in the same channel.

Setup (user side, 1 time):
  1. Already done if existing fruitmail adapter works (FRUITMAIL_COOKIES,
     FRUITMAIL_USER, FRUITMAIL_PASS).
  2. ``.github/workflows/fruitmail_lottery.yml`` maps those Secrets to
     this adapter's env-var names.

TOS note:
  Fruitmail has been operated by アイブリッジ since 2000 and is part of
  NTT Card Solution group. 利用規約 makes no explicit prohibition on
  programmatic entries; the site's own UX (server-side PII auto-fill
  from cookie) treats one-click entry as the normal flow. Each prize is
  rate-limited by ``応募可能口数`` (default 1/day, bumped via
  questionnaire — we keep to the free default since CLAUDE.md disallows
  survey automation).

Premium (/prize/premium/) requires Diamond/Platinum/Black rank — for
lower-rank accounts the form may not render. The wizard is fail-soft
so the run continues regardless.
"""

from ...common.adapter import Adapter
from ...common.balance import DEFAULT_BALANCE_PATTERNS
from ...common.password_login import PasswordLoginConfig
from ...common.wizard import DailyWizard

# Shared prize-form click sequence. fruitmail の応募 flow は **3 step**:
#   /prize/<category>/  → #applyForm submit → /prize/step1/
#   /prize/step1/       → 「登録情報を確認する」 → /prize/step2/ (送付先の確認)
#   /prize/step2/       → 「応募する」 / 「送付先を確定して応募」 → /prize/complete/ など
#
# 2026-05-25 false-positive 事故: 当初 step2 到達を「応募完了」と判定した
# が、実際の step2 page title は 「送付先の確認」 で 1 step 足りない事が
# /prize/everymonth/ の post-wizard inspect で発覚 (form が消えず残っていた)。
# 3 つ目の click を追加して **真の応募完了** まで到達させる。
#
# 各 step の submit button は class ``prizeComponent_common__button`` で
# 統一されているので、同 selector を 3 回 click する click_force 設計で
# 各 step を順次進めていく。selector miss は silent no-op。
# step2 (送付先の確認) page には 2 つの submit button があり、両方 class
# ``prizeComponent_common__button`` を持つ:
#   - "戻る" button: 追加で ``--secondary`` modifier class
#   - "確認して次へ" button: modifier class なし、これが forward submit
# step1 → step2 までは forward button が DOM 順で先なので素朴な
# ``button.prizeComponent_common__button[type="submit"]`` で意図通り。
# step2 → 完了は back button が先 → 同 selector が back を click してしまう。
# ``:not(.prizeComponent_common__button--secondary)`` で back を除外して
# 全 step 共通の forward-only selector にする (2026-05-25 buttons_dump で確定)。
_FORWARD_SUBMIT = 'button.prizeComponent_common__button[type="submit"]:not(.prizeComponent_common__button--secondary)'
# 2026-05-25 第三発見: fruitmail の応募 flow は **4 step**:
#   /prize/<cat>/  → 応募する         → /prize/step1/ (登録情報の確認)
#   /prize/step1/  → 確認して次へ     → /prize/step2/ (送付先の確認)
#   /prize/step2/  → 確認して次へ     → /prize/step3/ (最終確認)
#   /prize/step3/  → 応募する         → /prize/complete/? (完了)
# step3 にも「戻る」 button が混じるので、forward-only selector を 4 step
# 全部に適用 (step0 だけ #applyForm 内の submit で済む)。
_PRIZE_CLICKS: tuple[tuple[str, int], ...] = (
    # Step 0 → Step 1: /prize/<category>/ の #applyForm submit
    ('#applyForm button[type="submit"]', 1),
    # Step 1 → Step 2: /prize/step1/ 「登録情報を確認する」
    (_FORWARD_SUBMIT, 1),
    # Step 2 → Step 3: /prize/step2/ 「確認して次へ」 (back 除外)
    (_FORWARD_SUBMIT, 1),
    # Step 3 → 完了: /prize/step3/ 「応募する」 (final submit、back 除外)
    (_FORWARD_SUBMIT, 1),
)

# JS executed after navigation but before clicks. Sets
# selected_apply_number=1 on the form's <select>. ``required`` on the
# select otherwise blocks submit (default value is the "please choose"
# placeholder). ``dispatchEvent`` on 'change' is the polite version —
# fruitmail's form JS listens for it to recompute totals.
_SET_APPLY_NUMBER_JS = (
    "const sel = document.querySelector('select[name=\"selected_apply_number\"]');"
    "if (sel) { sel.value = '1'; sel.dispatchEvent(new Event('change', { bubbles: true })); }"
)

# 2026-05-28 user 観察で判明: everyweek / everymonth / premium が連日 「未確定」
# 通知を生成していた。原因は仕様内の benign no-op だった:
#  - everyweek: 「応募済み口数: 8 / 残り応募可能口数: 0」 で週上限到達
#    (memory project_fruitmail_lottery_2026_05_25 でも「複数口可能」と記録)
#  - everymonth: 同 仕組み、月上限到達
#  - premium: Diamond/Platinum/Black ランク限定で form 非表示 (memory に既知)
#
# どれも「click loop に入っても submit が無効」 で URL 不変 → verified=False
# 経由「未確定」 と通知される。framework に skip_if_body_regex を追加して、
# 「残り応募可能口数: 0」 を pre-click で検出 → skip 扱いに振り分ける。
#
# HTML 構造 (run 26549663735 inspect で確認):
#   <dt class="...applyNumberLabel">残り応募可能口数</dt>
#   <dd class="...applyNumberValue">0</dd>
#   <dt class="...applyNumberLabel">応募済み口数</dt>
#   <dd class="...applyNumberValue">N</dd>
#
# 2026-05-28 v1 bug (run 26554232054 で発覚): non-greedy `[\s\S]*?` で
# 後続 dd を貪欲化 → everyday (残り 1 / 応募済み 0) でも「応募済み = 0」
# の dd まで backtrack して match → 全 wizard が誤 skip。
# 「残り応募可能口数</dt>」 を明示的に区切って、その直後の dd だけを対象に。
_REMAINING_ZERO_REGEX = r"残り応募可能口数\s*</dt>\s*<dd[^>]*>\s*0\s*</dd>"


def _prize_wizard(name: str, slug: str) -> DailyWizard:
    """Build a prize-category wizard. ``slug`` matches the URL path segment."""
    return DailyWizard(
        name=name,
        url=f"https://www.fruitmail.net/prize/{slug}/",
        clicks=_PRIZE_CLICKS,
        # Step 0 navigates to /prize/step1/, so navigation-click semantics
        # are required. click_force evaluates ``el.click()`` directly,
        # which bypasses Playwright's visibility checks under the ad iframes.
        use_navigation_click=True,
        click_force=True,
        # Form select hydration sometimes lags by a beat; 3000ms covers it.
        initial_wait_ms=3000,
        # /prize/step1/ form needs a moment to render after POST navigation.
        inter_step_ms=5000,
        # Let the success page settle (analytics + redirect to /prize/step2/).
        final_wait_ms=5000,
        pre_click_evaluate=_SET_APPLY_NUMBER_JS,
        title_selector='input[name="item_name"]',
        # Real-success marker: 2026-05-25 false-positive 第二弾で判明 ——
        # /prize/step2/ は 「送付先の確認」 page (中間 step) であり完了で
        # はない。真の完了は step2 の次の URL (本 commit 後の run で確定)。
        # 暫定的に step2 以降を許可 (``step[2-9]`` or ``/complete``)。
        # Next-run の title log を見て pin する。
        # 完了 URL は ``/prize/end/?page=<category>``。step3「応募する」
        # click 後にここに redirect される (run 26378313632 で確認、page 内
        # は header しか無く forward submit ボタン無 = 確実な終端)。
        success_url_pattern=r"/prize/end/",
        # 応募上限到達 (everyweek 8 口 / everymonth N 口) の場合、page 上の
        # 「残り応募可能口数: 0」 widget を検出して click を skip。premium も
        # ランク条件を満たしてないアカウントは form 非表示で 「残り口数」 dd
        # 自体が無いケースがある — その場合は match せず通常の verify path
        # に流れて「未確定」 と通知される (これは別途 user 判断)。
        skip_if_body_regex=_REMAINING_ZERO_REGEX,
    )


ADAPTER = Adapter(
    name="fruitmail_lottery",
    site_label="フルーツメール 懸賞",
    mypage_url="https://www.fruitmail.net/mypage/",
    allowed_hosts=frozenset(
        {
            "fruitmail.net",
            "www.fruitmail.net",
        }
    ),
    login_keyword="ログアウト",
    # No click-mail pipeline — sister fruitmail adapter handles Gmail.
    source=None,
    balance_patterns=DEFAULT_BALANCE_PATTERNS,
    discover_seeds=(
        "https://www.fruitmail.net/prize/",
        "https://www.fruitmail.net/mypage/",
    ),
    daily_wizards=(
        _prize_wizard("fruitmail_prize_everyday", "everyday"),
        _prize_wizard("fruitmail_prize_everyweek", "everyweek"),
        _prize_wizard("fruitmail_prize_everymonth", "everymonth"),
        _prize_wizard("fruitmail_prize_gorgeous", "gorgeous"),
        # Premium requires Diamond/Platinum/Black rank — wizard is
        # fail-soft so a non-eligible account just no-ops the click step.
        _prize_wizard("fruitmail_prize_premium", "premium"),
    ),
    # Lottery output: 「応募した賞品一覧」Slack format. Prize titles
    # come from each wizard's ``title_selector`` (input[name=item_name]).
    lottery_mode=True,
    # Daily entries land in #lottery (same as chanceit). Re-use
    # SLACK_CHANNEL_CHANCEIT at the workflow level — no new Secret needed.
    # Per-wizard yield is one prize entry per day; stagnation detection
    # doesn't fit lottery semantics (drawings are infrequent).
    stagnation_window=None,
    # Same login form as the sister adapter — reuse FRUITMAIL_USER /
    # FRUITMAIL_PASS via workflow mapping for cookie-rotation fallback.
    password_login=PasswordLoginConfig(
        login_url="https://www.fruitmail.net/login?go_html=https://www.fruitmail.net/",
        username_selector="#user_identifier",
        password_selector="#password",
        submit_selector="button.login_index__loginButtonControl",
        success_marker="ログアウト",
    ),
)
