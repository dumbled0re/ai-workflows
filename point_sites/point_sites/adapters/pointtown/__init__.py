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

from ...common.adapter import Adapter
from ...common.balance import DEFAULT_BALANCE_PATTERNS
from ...common.sources import OnsiteInboxSource
from .parser import parse_inbox, parse_message

ADAPTER = Adapter(
    name="pointtown",
    site_label="ポイントタウン",
    mypage_url="https://www.pointtown.com/mypage",
    allowed_hosts=frozenset({"pointtown.com", "www.pointtown.com", "sp.pointtown.com"}),
    login_keyword="ログアウト",
    source=OnsiteInboxSource(
        inbox_url="https://www.pointtown.com/mypage/mail",
        parse_inbox=parse_inbox,
        parse_message=parse_message,
    ),
    balance_patterns=DEFAULT_BALANCE_PATTERNS,
    discover_seeds=(
        "https://www.pointtown.com/mypage",
        "https://www.pointtown.com/mypage/mail",
    ),
)
