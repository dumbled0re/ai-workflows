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
    # Top-page 宝くじ交換券 banners (8/day, each grants 1 ticket). The
    # banner URLs rotate daily and only render after the JS hydration,
    # so cron has to discover them through a real browser before the
    # Clicker can hit each /item/redirect-to-client-if-zero-point-item/
    # tracking URL. ``clickget_banner`` is the wrapper class on the
    # rendered top page; the inner ``<a>`` carries the href that
    # credits the ticket on GET.
    daily_banner_url="https://hapitas.jp/",
    daily_banner_selector="div.clickget_banner > a[href]",
    # Spend 宝くじ交換券 on バラ (random individual numbers) — same
    # day's banner-click tickets get exchanged for entries into the
    # mini-takarakuji daily drawing. The バラ flow is two clicks: the
    # initial ``apart_ctrl`` opens a confirmation panel that already
    # has the full ticket count pre-filled, then ``btn_exchanges_10``
    # fires the actual exchange XHR. One round-trip drains the entire
    # daily ticket balance.
    takarakuji_exchange_url="https://hapitas.jp/minitakarakuji/",
    takarakuji_exchange_open_selector="#apart_ctrl",
    takarakuji_exchange_confirm_selector="#takarakuji_exchange_btn_exchanges_10",
)
