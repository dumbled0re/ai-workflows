"""ポイントインカム (https://pointi.jp) adapter.

Status: cookies + auth verified 2026-05-09. Click-coin email pipeline
ready (waiting for first real mail to validate regex).

**Known limitation — balance scrape unavailable**: pointincome serves a
"コンテンツブロッカー" warning page (``/information.php?cn=2&sn=1``) to
non-browser HTTP clients on every authenticated mypage URL we tried
(``/my/my_page.php``, ``/exchange/pts_exchange_top.php``,
``sp.pointi.jp/`` and others, 2026-05-09). The session cookies are
valid (header shows logged-in nav with ログアウト link), but the body
is the warning page and never the real mypage. Without a way past the
JS-driven detection, ``fetch_balance`` returns None and the orchestrator's
degradation alert is inactive for pointincome. The actual click-coin
URLs in emails route through different server-side paths, so the email
→ click pipeline can still credit even though balance verification is
blind. Outcome tracking still records HTTP success/fail (a less
reliable proxy for credit landing).

Required Secrets to enable:
  - ``POINTINCOME_COOKIES`` — JSON array exported from a logged-in
    pointi.jp browser session.
  - ``SLACK_CHANNEL_POINTINCOME`` — Slack channel ID or ``#name``.
"""

from ...common.adapter import Adapter
from ...common.balance import DEFAULT_BALANCE_PATTERNS
from ...common.sources import GmailSource
from .parser import parse as parse_email

ADAPTER = Adapter(
    name="pointincome",
    site_label="ポイントインカム",
    mypage_url="https://pointi.jp/my/my_page.php",
    # Possibly needs www.pointi.jp / sp.pointi.jp too. Refine after the
    # first crawl shows which subdomains the site actually uses.
    allowed_hosts=frozenset({"pointi.jp", "www.pointi.jp", "sp.pointi.jp"}),
    # ポイントインカム mypage shows ログアウト link when authenticated;
    # if it differs, override here with a more robust marker.
    login_keyword="ログアウト",
    gmail_query=("from:pointi.jp -label:pointincome-clicked -label:pointincome-no-coins newer_than:3d"),
    clicked_label="pointincome-clicked",
    no_coins_label="pointincome-no-coins",
    source=GmailSource(parse_email=parse_email),
    # Same default mypage balance markers as Moppy until we know
    # otherwise. The DEFAULT patterns target 保有ポイント / 保有コイン
    # which are common across Japanese point sites.
    balance_patterns=DEFAULT_BALANCE_PATTERNS,
    discover_seeds=(
        "https://pointi.jp/my/my_page.php",
        "https://pointi.jp/daily.php",
    ),
)
