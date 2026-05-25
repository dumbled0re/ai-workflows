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

# Shared prize-form click sequence. All 5 categories use the same
# ``#applyForm`` POST to /prize/step1/ then a final submit to /prize/step2/.
# Each step is fail-soft (selector miss = silent no-op under click_force).
_PRIZE_CLICKS: tuple[tuple[str, int], ...] = (
    # Step 0: submit /prize/<category>/ form → navigates to /prize/step1/
    ('#applyForm button[type="submit"]', 1),
    # Step 1: on /prize/step1/, submit「登録情報を確認する」→ /prize/step2/
    # The step1 page is the only one with a 2nd form whose submit button
    # carries class ``prizeComponent_common__button``; pick that as the
    # most-specific safe selector.
    ('button.prizeComponent_common__button[type="submit"]', 1),
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
        # Real-success marker: only count as success if the POST chain
        # actually reached /prize/step2/. ``/prize/<category>/`` 残留は
        # submit blocked (select required validator fail / PII missing
        # server-side / silent selector miss) を意味する。2026-05-25 false-
        # positive 事故 (登録情報未入力 account で「応募成功」誤通知) の
        # 直接の対策。
        # NOTE: step2 到達 = 受理という確証はまだ取れていない。step2 が
        # success / error 両方の終点になる可能性があり、ユーザが mypage
        # 応募履歴で実 entry を確認した後に必要なら success_text_marker
        # も追加する (例「応募完了」「ご応募ありがとう」)。
        success_url_pattern=r"/prize/step2/?$",
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
