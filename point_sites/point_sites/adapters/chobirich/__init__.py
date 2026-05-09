"""ちょびリッチ (https://www.chobirich.com) adapter.

Status: scaffolded. mypage_url + login_keyword + click URL regex are
best guesses pending discover. The public landing didn't expose enough
markup to nail down the exact paths, so first-run recon is the only
way to confirm.

Site-specific risks:
- Site rate is **2pt = 1円** (most others are 10pt = 1円), so
  estimated_points × pt-value computation must NOT assume 10:1.
- 2025-11-01 banner-click credit clamped to 1/account. This affects
  in-site banner clicks, not mail-driven URLs, but watch the
  degradation alert closely after enabling.
- Operator is actively tightening anti-fraud; treat as moderate
  detection risk.

Required Secrets:
  - ``CHOBIRICH_COOKIES``
  - ``SLACK_CHANNEL_CHOBIRICH``
"""

from ...common.adapter import Adapter
from ...common.balance import DEFAULT_BALANCE_PATTERNS
from ...common.sources import GmailSource
from .parser import parse as parse_email

ADAPTER = Adapter(
    name="chobirich",
    site_label="ちょびリッチ",
    # Best-guess mypage. If chobirich uses a different path (e.g.
    # ``/membersite/?action=mypage``), update after discover confirms.
    mypage_url="https://www.chobirich.com/mypage/",
    allowed_hosts=frozenset({"chobirich.com", "www.chobirich.com", "sp.chobirich.com"}),
    login_keyword="ログアウト",
    gmail_query=("from:chobirich.com -label:chobirich-clicked -label:chobirich-no-coins newer_than:3d"),
    clicked_label="chobirich-clicked",
    no_coins_label="chobirich-no-coins",
    source=GmailSource(parse_email=parse_email),
    balance_patterns=DEFAULT_BALANCE_PATTERNS,
    discover_seeds=(
        "https://www.chobirich.com/mypage/",
        "https://www.chobirich.com/today_chobirich",
    ),
)
