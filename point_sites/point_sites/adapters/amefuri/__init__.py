"""アメフリ (https://www.amefri.net) adapter — endpoint-poll source.

アメフリ has no click-mail flow; the daily reward is claimed by tapping
the "ログインボーナスを受け取る" button on the top page. We emulate
that with a single GET to the underlying URL via ``EndpointPollSource``,
gated to once per JST day by ``state_key = YYYY-MM-DD``.

Per-rank yield: 0.1〜2.5 円/day plus 1〜100 円 milestones every 30 logins.
At 10pt = 1円 (アメフリの conversion) the auto-click budget is small but
high purity (no ad-fraud risk).

**Risk-of-validity**: ``daily_bonus_url`` below is a *best-guess
placeholder*. Site auth-guards the actual click handler so the real URL
isn't visible from public scraping. Once ``AMEFURI_COOKIES`` is
registered, run::

    gh workflow run amefuri.yml -f discover=true

then read the discover log and update ``daily_bonus_url`` here to the
real claim URL (likely under ``/login_bonus`` or ``/daily``). The
``cmd_run`` pipeline will use balance-delta to verify whether the
endpoint actually credits points — codex's recommendation 2026-05-09
since HTTP 200 alone isn't proof of credit on these sites.

Required Secrets to enable:
  - ``AMEFURI_COOKIES`` — JSON array exported from Cookie-Editor on a
    logged-in browser session.
  - ``SLACK_CHANNEL_AMEFURI`` — Slack channel ID or ``#name``.
"""

from ...common.adapter import Adapter
from ...common.balance import DEFAULT_BALANCE_PATTERNS
from ...common.sources import EndpointPollSource

# Best-guess daily-bonus URL. Refine after first authenticated discover.
# The "ログインボーナスを受け取る" button on the top page hits an
# auth-guarded path — this is a documented placeholder, not a confirmed
# endpoint.
_DAILY_BONUS_URL = "https://www.amefri.net/login_bonus"

ADAPTER = Adapter(
    name="amefuri",
    site_label="アメフリ",
    mypage_url="https://www.amefri.net/mypage",
    allowed_hosts=frozenset({"amefri.net", "www.amefri.net"}),
    login_keyword="ログアウト",
    # Gmail fields stay blank — endpoint-poll source ignores them.
    source=EndpointPollSource(endpoint_url=_DAILY_BONUS_URL),
    balance_patterns=DEFAULT_BALANCE_PATTERNS,
    discover_seeds=(
        "https://www.amefri.net/",
        "https://www.amefri.net/mypage",
    ),
)
