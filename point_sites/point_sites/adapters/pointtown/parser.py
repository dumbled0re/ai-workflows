"""ポイントタウン on-site mailbox HTML → click-coin extraction.

Two parsers used by ``OnsiteInboxSource``:

- ``parse_inbox`` finds individual click-coin message links in the
  ``/mypage/mail`` inbox listing.
- ``parse_message`` finds click-coin URLs inside one message detail
  page.

**Risk-of-validity**: Both regexes are *best-guess* — the real inbox
HTML isn't visible without a session cookie. After
``POINTTOWN_COOKIES`` is registered, run ``gh workflow run pointtown.yml
-f discover=true`` and ``-f inspect_url=https://www.pointtown.com/mypage/mail``
to see the actual markup, then refine the regexes.

Per ポイントタウン FAQ (2026-05): "メール内に複数のURLがある場合、
コインを獲得できるのは1通につき1回のクリックのみ" — so even when a
message has multiple click URLs, only the first credits. ``parse_message``
returns all matches anyway and ``cmd_run``'s state-store dedupes; if
that proves too aggressive (clicks burning per-mail quota on a
non-credited URL), reduce to first-match-only here.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ...common.models import ClickCandidate
from ...common.sources import InboxEntry

logger = logging.getLogger(__name__)

_INBOX_BASE = "https://www.pointtown.com"

# Verified pattern from real /mypage/mail HTML (2026-05-09): each row in
# the mail listing links to ``/mypage/mail/<id>``. The empty-inbox case
# shows "現在閲覧できるメールはございません" instead of any message
# rows, so we detect that explicitly to avoid false-positive anomalies.
_MESSAGE_LINK_RE = re.compile(
    r"^/mypage/mail/(?:show\?id=\d+|(?:show/)?\d+)/?$",
)
_EMPTY_INBOX_MARKER = "現在閲覧できるメールはございません"

# Best-guess click-coin URL on a message detail page. Common GMO/Pointown
# patterns: ``/coin/c/<token>``, ``/click/<token>``, ``/cc/<token>``.
# The regex stays loose so first-run discover surfaces matches even if
# the exact path differs slightly.
_CLICK_COIN_URL_RE = re.compile(
    r"https://www\.pointtown\.com/(?:coin|click|cc|cm|m)/[A-Za-z0-9_\-/?=&]+",
)

# Coin-amount callout near the click URL (``XXコイン獲得``,
# ``5pt``, etc). The window after the URL is searched for this pattern
# to estimate yield. Optional — absence does NOT skip the URL because
# pointtown messages may render the amount elsewhere on the page.
_CALLOUT_RE = re.compile(r"(\d{1,4})\s*(?:コイン|pt|ポイント)")
_CALLOUT_WINDOW_CHARS = 240


def parse_inbox(html: str) -> tuple[list[InboxEntry], list[str]]:
    """Extract message-link rows from the inbox listing HTML.

    Returns (entries, anomalies). An empty entries list with no
    anomalies usually means "no unread/uncredited mail" (legitimate);
    an empty list with anomalies means the parser broke.
    """
    if not html.strip():
        return [], ["empty inbox HTML"]
    # Legitimate empty-inbox case (new account or fully consumed inbox)
    # — return no entries and no anomaly so the daily cron stops noisy
    # Slack ``send_parse_failure`` posts.
    if _EMPTY_INBOX_MARKER in html:
        return [], []
    soup = BeautifulSoup(html, "html.parser")
    entries: list[InboxEntry] = []
    seen_urls: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        if not _MESSAGE_LINK_RE.match(href):
            continue
        absolute = urljoin(_INBOX_BASE, href)
        if absolute in seen_urls:
            continue
        seen_urls.add(absolute)
        # Subject / preview text — best effort. Falls back to the URL
        # itself so a missing label never crashes the orchestrator.
        label = a.get_text(strip=True) or absolute
        entries.append(InboxEntry(state_key=absolute, message_url=absolute, label=label[:120]))

    anomalies: list[str] = []
    # If the inbox HTML is non-empty but yielded zero entries, the regex
    # is likely stale. Surface it loudly so the operator refines.
    if not entries and len(html) > 1000:
        anomalies.append("no message links matched inbox-list regex (HTML may have changed)")
    return entries, anomalies


def parse_message(body: str, is_html: bool = False) -> tuple[list[ClickCandidate], list[str]]:
    """Extract click-coin URL(s) from a single message detail page.

    Per ポイントタウン FAQ: only one click per message credits — clicking
    additional URLs in the same message is wasted (and looks aggressive).
    So we keep just the first valid match and drop the rest. If extra
    matches existed, an anomaly is emitted so the operator can verify
    that the first match is actually the credited one (heuristic =
    document order; refine if discover shows otherwise).
    """
    if not body.strip():
        return [], ["empty message body"]
    text = _strip_html(body) if is_html else body
    candidates: list[ClickCandidate] = []
    seen_urls: set[str] = set()
    extra_match_count = 0
    for match in _CLICK_COIN_URL_RE.finditer(text):
        url = match.group(0).rstrip("&?")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        # FAQ guarantees only one click credits — once we have one valid
        # candidate, just count any further matches so we can flag the
        # message as multi-URL (anomaly) and stop processing.
        if candidates:
            extra_match_count += 1
            continue
        # Look for a coin amount near the URL. None is fine — credit
        # verification is balance-delta based per codex 2026-05-09.
        window_end = min(len(text), match.end() + _CALLOUT_WINDOW_CHARS)
        callout = _CALLOUT_RE.search(text[match.end() : window_end])
        estimated_points: int | None = None
        if callout is not None:
            try:
                estimated_points = int(callout.group(1))
            except ValueError:
                estimated_points = None
        try:
            candidate = ClickCandidate.model_validate(
                {
                    "url": url,
                    "anchor_text": (callout.group(0) if callout else "<onsite_inbox_message>"),
                    "estimated_points": estimated_points,
                    "extraction_reason": ("whitelist_url_pattern_and_anchor" if callout else "whitelist_url_pattern"),
                }
            )
        except ValueError as exc:
            logger.debug("invalid candidate url %r: %s", url, exc)
            continue
        candidates.append(candidate)

    anomalies: list[str] = []
    if not candidates and len(text) > 800:
        anomalies.append("no click-coin URLs matched message regex (HTML may have changed)")
    if extra_match_count:
        anomalies.append(
            f"message had {extra_match_count + 1} click-coin URL(s); only first kept "
            "(per FAQ only one credits — verify discover output if heuristic looks wrong)"
        )
    return candidates, anomalies


def _strip_html(html: str) -> str:
    """Convert HTML to plaintext while preserving anchor hrefs inline."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        a.insert_before(f" {a['href']} ")
    return soup.get_text("\n", strip=False)
