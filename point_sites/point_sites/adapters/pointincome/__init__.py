"""ポイントインカム (https://pointi.jp) adapter.

Status: scaffolded. The mypage URL, login keyword, allowed hosts, and
discover seeds below are best guesses based on public site structure.
The click-coin URL regex in ``parser.py`` likewise needs to be
verified against real ポイントインカム emails on first run.

Required Secrets to enable:
  - ``POINTINCOME_COOKIES`` — JSON array exported from Cookie-Editor
    after logging into pointi.jp on the user's browser.
  - ``SLACK_CHANNEL_POINTINCOME`` — Slack channel ID or ``#name``.

Bring-up flow:
  1. Register the two secrets above on the GitHub repo.
  2. ``gh workflow run pointincome.yml -f discover=true`` for read-only
     recon. Inspect the workflow log for actual click-mail URL pattern
     and refine ``parser.py``.
  3. ``gh workflow run pointincome.yml -f inspect_url=...`` to dump
     specific page HTML if needed.
  4. Once the regex is confirmed, ``gh workflow run pointincome.yml``
     for live click-mode. Cookie persistence + degradation detection
     come for free via the shared pipeline.
"""

from ...common.adapter import Adapter
from ...common.balance import DEFAULT_BALANCE_PATTERNS
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
    parse_email=parse_email,
    # Same default mypage balance markers as Moppy until we know
    # otherwise. The DEFAULT patterns target 保有ポイント / 保有コイン
    # which are common across Japanese point sites.
    balance_patterns=DEFAULT_BALANCE_PATTERNS,
    discover_seeds=(
        "https://pointi.jp/my/my_page.php",
        "https://pointi.jp/daily.php",
    ),
)
