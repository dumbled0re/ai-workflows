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
)
