"""げん玉 (https://www.gendama.jp) adapter.

Status: scaffolded.

Site-specific risks (worth documenting before activating):
- **180-day inactivity rule** → account is auto-purged. Click-point
  collection does NOT count as activity. The user has to make at
  least one real shopping/registration transaction per ~6 months to
  keep the account (and any accumulated points) alive. If the user
  isn't planning to do that, this adapter is not worth enabling.
- **10pt = 1円 conversion**, so estimated_points should not be
  treated as JPY.
- Auth lives on a different host (``ssl.gendama.jp``) than the
  content site (``www.gendama.jp``). The cookie domain ``.gendama.jp``
  should cover both.

Required Secrets:
  - ``GENDAMA_COOKIES``
  - ``SLACK_CHANNEL_GENDAMA``
"""

from ...common.adapter import Adapter
from ...common.balance import DEFAULT_BALANCE_PATTERNS
from ...common.sources import GmailSource
from .parser import parse as parse_email

ADAPTER = Adapter(
    name="gendama",
    site_label="げん玉",
    mypage_url="https://www.gendama.jp/mypage/",
    allowed_hosts=frozenset({"gendama.jp", "www.gendama.jp", "ssl.gendama.jp"}),
    login_keyword="ログアウト",
    gmail_query=("from:gendama.jp -label:gendama-clicked -label:gendama-no-coins newer_than:3d"),
    clicked_label="gendama-clicked",
    no_coins_label="gendama-no-coins",
    source=GmailSource(parse_email=parse_email),
    balance_patterns=DEFAULT_BALANCE_PATTERNS,
    discover_seeds=(
        "https://www.gendama.jp/mypage/",
        "https://www.gendama.jp/forest/",
    ),
)
