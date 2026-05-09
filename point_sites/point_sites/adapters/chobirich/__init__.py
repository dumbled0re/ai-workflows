"""ちょびリッチ (https://www.chobirich.com) adapter.

Status: **blocked at WAF level (2026-05-09)**. chobirich's CDN returns
HTTP 403 to every request from GitHub Actions IP ranges, even for the
public top page ``/``. Tested from local curl: 200. Tested from GHA
runner with valid cookies: 403. This is L7 IP-based blocklisting that
cannot be bypassed without (a) a self-hosted runner on a residential
IP, or (b) some kind of proxy. Both are gross overkill given the
small expected yield (5-15 円/month) post-2025-11 rate cuts.

Adapter is left in place so the framework remains symmetric, but it
will fail every run. Disable the schedule by NOT setting
``vars.CHOBIRICH_CRON_MODE=click`` (default extract-links also 403s
because it still calls verify_login). Realistic state: this site is
abandoned for our purposes.

Original site-specific risks (still relevant if anyone retries):
- Site rate is **2pt = 1円** (most others are 10pt = 1円).
- 2025-11-01 banner-click credit clamped to 1/account.

Required Secrets (kept for symmetry, won't help past the WAF):
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
    # ``/mypage`` returns 403 even with valid cookies (chobirich CDN
    # WAF appears to block direct access to mypage from datacenter IPs).
    # Top page ``/`` returns 200 and the header nav contains ログアウト
    # link when authenticated, which is sufficient for verify_login.
    mypage_url="https://www.chobirich.com/",
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
