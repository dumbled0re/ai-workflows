"""ポイントタウン (https://www.pointtown.com) adapter — onsite-inbox source.

ポイントタウン delivers click-coin "メール" inside the site itself, not
via Gmail. The mailbox lives at ``/mypage/mail`` and each row links to
a message detail page that contains 1+ click-coin URLs (per FAQ:
"メール内に複数のURLがある場合、コインを獲得できるのは1通につき
1回のクリックのみ" — only the first click per message credits).

We use ``OnsiteInboxSource`` instead of ``GmailSource``: it GETs the
inbox, runs ``parse_inbox`` to enumerate per-message URLs, then GETs
each message and runs ``parse_message`` to extract click candidates.

**Risk-of-validity**: regexes in ``parser.py`` are best-guess. GMO's
anti-fraud is the strictest in the industry — the very first
authenticated GET to the inbox should be done in extract-links mode
(no clicks) so bot-detection does NOT see anything more aggressive
than a one-shot read until URL patterns are confirmed via discover.

Required Secrets to enable:
  - ``POINTTOWN_COOKIES`` — JSON array exported from Cookie-Editor on a
    logged-in pointtown.com browser session. **Use a dedicated
    ポイ活専用 account** — TOS violations under your main GMO account
    can ripple to other GMO services.
  - ``SLACK_CHANNEL_POINTTOWN`` — Slack channel ID or ``#name``.
"""

import re

from ...common.adapter import Adapter
from ...common.balance import DEFAULT_BALANCE_PATTERNS
from ...common.password_login import PasswordLoginConfig
from ...common.sources import OnsiteInboxSource
from ...common.wizard import DailyWizard
from .parser import parse_inbox, parse_message

ADAPTER = Adapter(
    name="pointtown",
    site_label="ポイントタウン",
    mypage_url="https://www.pointtown.com/mypage",
    # id.gmo.jp は GMO 共通の SSO。pointtown の /login は anonymous GET で
    # ``https://id.gmo.jp/gui/auth/login/sso?ckey=...`` に redirect され、
    # そこで login form が出る。Playwright 経由の password_login が SSO
    # form に navigate するため allowed_hosts に追加。
    allowed_hosts=frozenset({"pointtown.com", "www.pointtown.com", "sp.pointtown.com", "id.gmo.jp"}),
    login_keyword="ログアウト",
    source=OnsiteInboxSource(
        inbox_url="https://www.pointtown.com/mypage/mail",
        parse_inbox=parse_inbox,
        parse_message=parse_message,
    ),
    # Click-coin URLs credit コイン (not ポイント — 10 coins auto-convert
    # to 1 pt). The mypage header shows both labels, but the default
    # 保有ポイント patterns hit the ポイント count first (always 0 for
    # short-term click activity) and miss the actual signal we need to
    # detect crediting. ``c-coin-large-label`` is the coin counter
    # widget; this site-specific regex takes precedence over the
    # defaults so degradation alerts fire when click-coin clicks stop
    # crediting.
    balance_patterns=(
        re.compile(r'class="c-coin-large-label"[^>]*>\s*([0-9,]+)'),
        *DEFAULT_BALANCE_PATTERNS,
    ),
    discover_seeds=(
        "https://www.pointtown.com/mypage",
        "https://www.pointtown.com/mypage/mail",
    ),
    # Daily login bonus modal (mypage widget; 20-day cycle averaging
    # ~5.5 coins/day with 5/10/30/50-coin bonus days at days 5/10/15/20).
    # The 確認する button (button[onclick*="MikasaLoginBonus"]) opens
    # a dialog whose ``#js-get-reward-btn`` is hidden by default and
    # only renders when today's reward hasn't been claimed yet — so a
    # second daily run finds the button missing and the wizard times
    # out gracefully without crediting twice.
    #
    # The modal also offers a "宝箱を選んで追加ボーナス" treasure pick
    # AFTER the base reward, but FAQ implies it requires watching a
    # video ad — that veers into ad-fraud territory and is intentionally
    # skipped here. Only the base 1〜50 coin reward path is automated.
    daily_wizards=(
        DailyWizard(
            name="pointtown_login_bonus",
            url="https://www.pointtown.com/mypage",
            clicks=(
                ('button[onclick*="MikasaLoginBonus"]', 1),
                ("#js-get-reward-btn", 1),
            ),
        ),
    ),
    # 2026-05-16 inspect (--anonymous, /login → GMO SSO redirect 追従) で
    # 確定。pointtown 独自 login form は存在せず、GMO 共通 SSO (id.gmo.jp)
    # に redirect される。Playwright で /login に goto すると自動で SSO
    # form 画面まで進むので、login_url は /login に設定すれば足りる。
    #
    # SSO form fields:
    #   email   input[type="email"][name="LoginForm[email]"]
    #   pass    input[name="LoginForm[pass]"]
    #   submit  button.Btn__primary[type="submit"]
    #
    # ckey は SSO URL の query で session 都度生成されるが、/login 経由
    # で自動 redirect するため login_url 側で静的に固定可能。
    password_login=PasswordLoginConfig(
        login_url="https://www.pointtown.com/login",
        username_selector='input[name="LoginForm[email]"]',
        password_selector='input[name="LoginForm[pass]"]',
        submit_selector="button.Btn__primary",
        success_marker="ログアウト",
    ),
)
