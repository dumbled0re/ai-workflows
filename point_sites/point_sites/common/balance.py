"""Scrape the logged-in user's point balance from a site's mypage.

This is the authoritative way to verify that a click actually credited
points: HTTP 200 from the click endpoint does NOT prove crediting (the
endpoint returns 200 even for already-clicked URLs or shadow-banned
sessions). Comparing balance before/after the click batch is what
catches "click succeeded but no points" silently.

The mypage HTML is undocumented and may change per site, so we try
several regex patterns and log a redacted body excerpt on parse
failure. Returning None instead of raising lets the run still post a
summary even when balance verification breaks.

Adapters inject their own (mypage_url, balance_patterns) tuple. The
``DEFAULT_BALANCE_PATTERNS`` here cover Moppy and many similar Japanese
point sites whose mypage uses ``保有ポイント``/``保有コイン`` labels.
"""

from __future__ import annotations

import logging
import re
from typing import Final

import requests

logger = logging.getLogger(__name__)

# Default patterns. Ordered most-specific → most-permissive. Each must
# capture the numeric balance (digits with optional thousand separators)
# in group 1. Adapters can override via Adapter.balance_patterns.
DEFAULT_BALANCE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r'data-(?:point|balance|coin)s?\s*=\s*["\']?([0-9,]+)', re.IGNORECASE),
    # ``[\s\S]{0,40}?`` between digits and unit allows ``5,678</span> P``
    # (digits and unit separated by an inline close-tag, common in mypage
    # markup). Lazy quantifier keeps us anchored to the closest unit.
    re.compile(r"保有(?:ポイント|コイン)[\s\S]{0,80}?([0-9,]+)[\s\S]{0,40}?(?:P|Ｐ|ポイント|コイン)"),
    re.compile(r"現在の(?:ポイント|コイン)[\s\S]{0,80}?([0-9,]+)"),
    re.compile(r"所持(?:ポイント|コイン)[\s\S]{0,80}?([0-9,]+)"),
    re.compile(r'class="[^"]*(?:point|coin|balance)[^"]*"[^>]*>\s*([0-9,]+)'),
    re.compile(r'(?:title|aria-label)="(?:保有|現在|所持)[^"]*"[^>]*>\s*([0-9,]+)'),
)


def parse_balance(
    html: str,
    patterns: tuple[re.Pattern[str], ...] = DEFAULT_BALANCE_PATTERNS,
) -> int | None:
    """Return the parsed balance, or None when no pattern matches."""
    for pat in patterns:
        match = pat.search(html)
        if not match:
            continue
        try:
            return int(match.group(1).replace(",", ""))
        except ValueError:
            continue
    return None


def fetch_balance(
    session: requests.Session,
    mypage_url: str,
    *,
    patterns: tuple[re.Pattern[str], ...] = DEFAULT_BALANCE_PATTERNS,
    secondary_patterns: tuple[re.Pattern[str], ...] | None = None,
    timeout: tuple[float, float] = (10.0, 30.0),
) -> tuple[int | None, int | None]:
    """GET mypage and parse the current balance(s).

    Returns ``(primary, secondary)`` where each is ``int`` or ``None``.
    ``None`` on either side means that side's parse failed (or the GET
    failed — both will be ``None`` in that case). The caller treats
    ``None`` as "unknown" and skips verification rather than mistaking
    it for a zero balance.

    ``secondary_patterns`` lets an adapter expose a second currency
    (e.g. pointtown's コイン / pt pair) on the same mypage GET — saves
    a duplicate request.
    """
    try:
        resp = session.get(mypage_url, timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        logger.warning("balance fetch request failed: %s", exc)
        return None, None
    if resp.status_code != 200:
        logger.warning("balance fetch returned HTTP %d", resp.status_code)
        return None, None
    body = resp.text
    balance = parse_balance(body, patterns)
    secondary = parse_balance(body, secondary_patterns) if secondary_patterns else None
    if balance is None:
        # Log a small redacted snippet to aid parser updates without
        # leaking tracking IDs or session-bound markup.
        snippet = re.sub(r"[A-Za-z0-9+/=_-]{20,}", "<redacted>", body)
        logger.warning(
            "balance parse failed; no pattern matched. snippet head: %s",
            snippet[:200].replace("\n", " "),
        )
    return balance, secondary
