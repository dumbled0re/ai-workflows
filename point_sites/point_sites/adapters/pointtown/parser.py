"""ポイントタウン on-site mailbox HTML → click-coin extraction.

Two parsers used by ``OnsiteInboxSource``:

- ``parse_inbox`` finds individual click-coin message links in the
  ``/mypage/mail`` inbox listing.
- ``parse_message`` finds click-coin URLs inside one message detail
  page.

**Verified 2026-05-10** against real authenticated inbox + message:
- Inbox listing: ``<a href="/mypage/mail/<id>">`` rows.
- Message detail: the click-coin URL lives in an iframe whose ``src``
  is ``/mypage/mail/body/<id>``; the wrapper page itself only has
  the iframe markup, so ``parse_inbox`` produces ``message_url``
  pointed straight at the iframe so ``OnsiteInboxSource.fetch_batch``
  reads the body in one GET.
- Click-coin URL inside the iframe: ``https://www.pointtown.com/mail/click?t=<token>&u=<hex>``
  (token short alphanumeric, hex is the long campaign signature).

Per ポイントタウン FAQ (2026-05): "メール内に複数のURLがある場合、
コインを獲得できるのは1通につき1回のクリックのみ" — so even when a
message has multiple click URLs, only the first credits. ``parse_message``
returns all matches anyway and ``cmd_run``'s state-store dedupes; if
that proves too aggressive (clicks burning per-mail quota on a
non-credited URL), reduce to first-match-only here.
"""

from __future__ import annotations

import html as _html
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
# The capture group pulls the numeric id so ``parse_inbox`` can build
# the body iframe URL alongside the canonical state_key.
_MESSAGE_LINK_RE = re.compile(
    r"^/mypage/mail/(?:show\?id=|show/)?(\d+)/?$",
)
_EMPTY_INBOX_MARKER = "現在閲覧できるメールはございません"

# Verified 2026-05-10: click-coin URLs in /mypage/mail/body/<id> look
# like ``https://www.pointtown.com/mail/click?t=<short>&u=<long-hex>``.
# Body is GET-served HTML-escaped (``&amp;u=``); ``parse_message`` runs
# ``html.unescape`` first so the ``&`` literal here matches.
_CLICK_COIN_URL_RE = re.compile(
    r"https://www\.pointtown\.com/mail/click\?t=[A-Za-z0-9_-]+&u=[A-Za-z0-9]+",
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
    seen_keys: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        m = _MESSAGE_LINK_RE.match(href)
        if not m:
            continue
        msg_id = m.group(1)
        state_key = urljoin(_INBOX_BASE, href)
        if state_key in seen_keys:
            continue
        seen_keys.add(state_key)
        # ``message_url`` points at the iframe body so ``fetch_batch``
        # reads the actual click-coin markup; the canonical wrapper URL
        # stays as state_key so StateStore dedups across cron runs.
        body_url = f"{_INBOX_BASE}/mypage/mail/body/{msg_id}"
        # Subject / preview text — best effort. Falls back to the
        # state_key URL so a missing label never crashes the orchestrator.
        label = a.get_text(strip=True) or state_key
        entries.append(InboxEntry(state_key=state_key, message_url=body_url, label=label[:120]))

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
    So we keep just the first valid match and drop the rest.

    Note: pointtown click-mails routinely contain **duplicate CTAs**
    (the same ``▼…に登録する 【コイン付】 <url>`` block repeated, with
    different ``t=<token>`` tracking values but the same ``u=`` user
    hash — verified 2026-05-15 on msg 4512). All variants redirect to
    the same credit-grant endpoint, so keeping the first and dropping
    the rest is correct and silent. We do NOT raise an anomaly for
    this case — the parser previously did, which routed every such
    mail to ``anomaly_ids`` and the runner's ``continue`` then skipped
    clicking entirely, silently swallowing real credits.
    """
    if not body.strip():
        return [], ["empty message body"]
    # The iframe body GET returns HTML-escaped text where the click URL
    # carries ``&amp;u=`` rather than ``&u=``. Unescape before regex
    # match so the literal ``&`` in the pattern lines up.
    body = _html.unescape(body)
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
        # Don't escalate as anomaly — pointtown mails routinely have
        # 2+ duplicate CTAs pointing at the same credit endpoint, and
        # an anomaly here would block the click via the runner's
        # ``continue``. Log informationally for debugging only.
        logger.info(
            "pointtown message had %d click-coin URL(s); first kept (duplicate CTA pattern)",
            extra_match_count + 1,
        )
    return candidates, anomalies


def _strip_html(html: str) -> str:
    """Convert HTML to plaintext while preserving anchor hrefs inline."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        a.insert_before(f" {a['href']} ")
    return soup.get_text("\n", strip=False)
