"""GetMoney! on-site Webメール inbox parser.

Two parsers used by ``OnsiteInboxSource``:

- ``parse_inbox`` enumerates message-detail links in the inbox listing
  at ``/pc/mypage/mail_notice/index``.
- ``parse_message`` extracts click-coin URLs from one message detail
  page (``/pc/mypage/mail_notice/index?recipientId=<N>``).

**Verified 2026-05-10** against real authenticated inbox + 1 sample
message (recipientId=945005814):

- Inbox listing: each row is ``<a href="index?recipientId=<N>">``
  (relative — joined against the inbox base URL).
- Message detail uses the same path with a query parameter (no
  iframe; body is inline in the HTML).
- Click-coin URL: ``https://dietnavi.com/click.php?cid=<N>&id=<N>&sec=<hex>``
  — note the host is bare ``dietnavi.com`` (no ``/pc/`` prefix), the
  same URL repeats multiple times in the body markup, and ``&`` is
  HTML-escaped (``&amp;``) so ``html.unescape`` runs first.
- Callout text mixes full-width and half-width Pt: ``クリックで「１Ｐｔ」ゲット！``
  and ``クリックで1Ptゲット（有効期限<date>まで）`` both appear in the
  same body. The regex normalises both forms before matching.

Per inbox notice (2026-05-10): ``クリックポイントは重複して獲得でき
ません。この一覧からクリックポイントを獲得した場合、メールソフト等で
受信したメールでのクリックポイントは加算されません。`` → on-site click
disqualifies the matching Gmail mail. We pick on-site so the Gmail
path is not used (the adapter switches source from GmailSource to
OnsiteInboxSource accordingly).
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

_INBOX_BASE = "https://dietnavi.com/pc/mypage/mail_notice/"

# Inbox listing rows. Verified against logged-in /pc/mypage/mail_notice/index
# 2026-05-10: ``<a href="index?recipientId=945005814">``. Same path,
# query-param differs per message — capture the numeric id so the
# state_key can be canonicalised to the absolute URL.
_MESSAGE_LINK_RE = re.compile(
    r"^index\?recipientId=(\d+)$",
)
# Heuristic empty-inbox marker — copy reads like
# "現在受信中のメールはありません" / "メールが届いていません" depending
# on the site copy. Without a confirmed real-empty fixture, fall back
# to "no entries + non-empty html" → anomaly behaviour for safety.

# Click-coin URL pattern. Verified 2026-05-10:
# ``https://dietnavi.com/click.php?cid=57448&id=3567347&sec=bcbc0f9b``.
# Host is the apex (``dietnavi.com``) with no subdomain or ``/pc/``
# prefix. ``cid`` and ``id`` are decimal; ``sec`` is hex (8 chars in
# the sample but not strictly bounded — match alphanumeric to be
# resilient).
_CLICK_COIN_URL_RE = re.compile(
    r"https://dietnavi\.com/click\.php\?cid=\d+&id=\d+&sec=[A-Za-z0-9]+",
)

# Callout near the click URL. Body uses both full-width 「１Ｐｔ」 and
# half-width ``1Pt`` for the same value, often within the same message.
# Normalise full-width digits + brackets + Ｐｔ/ｐｔ to half-width before
# matching so a single regex covers both.
_CALLOUT_RE = re.compile(
    r"クリック(?:で|して)?\s*[「【\[]?\s*(\d{1,4})\s*(?:Pt|pt|ポイント)",
)
_CALLOUT_WINDOW_CHARS = 240

# Full-width → half-width translation table. Built once at import.
# Covers digits 0-9, Pt/pt characters, and the full-width brackets that
# wrap the amount in some templates (「」). Keep it minimal — only the
# characters that actually appear in observed callout text.
_FULLWIDTH_TRANSLATE = str.maketrans(
    {
        "０": "0",
        "１": "1",
        "２": "2",
        "３": "3",
        "４": "4",
        "５": "5",
        "６": "6",
        "７": "7",
        "８": "8",
        "９": "9",
        "Ｐ": "P",
        "ｐ": "p",
        "ｔ": "t",
        "Ｔ": "T",
        "「": "[",
        "」": "]",
        "（": "(",
        "）": ")",
        "　": " ",
    }
)


def parse_inbox(html: str) -> tuple[list[InboxEntry], list[str]]:
    """Extract message-link rows from the inbox listing HTML.

    Returns (entries, anomalies). Empty entries with no anomalies =
    legitimate empty inbox; empty entries with non-empty HTML triggers
    an anomaly so a parser breakage surfaces loudly.
    """
    if not html.strip():
        return [], ["empty inbox HTML"]
    soup = BeautifulSoup(html, "html.parser")
    entries: list[InboxEntry] = []
    seen_keys: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        m = _MESSAGE_LINK_RE.match(href)
        if not m:
            continue
        msg_id = m.group(1)
        # Canonical state_key uses the absolute URL so dedup across runs
        # is stable even if the inbox base path changes.
        state_key = urljoin(_INBOX_BASE, href)
        if state_key in seen_keys:
            continue
        seen_keys.add(state_key)
        # Subject lives elsewhere in the inbox row; best-effort label
        # falls back to the recipientId so something useful shows in
        # logs even when the regex misses.
        label = a.get_text(strip=True) or f"recipientId={msg_id}"
        entries.append(
            InboxEntry(
                state_key=state_key,
                message_url=state_key,
                label=label[:120],
            )
        )

    anomalies: list[str] = []
    if not entries and len(html) > 1500:
        # Non-empty HTML with no message rows = either truly empty
        # inbox or stale regex. Without a confirmed "empty inbox"
        # fixture we surface this so the operator can verify.
        anomalies.append("no message links matched inbox-list regex (HTML may have changed or inbox is empty)")
    return entries, anomalies


def parse_message(body: str, is_html: bool = False) -> tuple[list[ClickCandidate], list[str]]:
    """Extract click-coin URL(s) from a single message detail page.

    Verified body has the same click URL repeated 5+ times across the
    HTML (anchor + plaintext + footer). Dedup by URL and keep one
    candidate per unique click URL. If multiple distinct URLs appear,
    keep the first only and flag an anomaly so the operator can verify
    crediting behaviour (no public FAQ rule confirms multi-URL handling
    on getmoney; the safe default mirrors pointtown's "one click per
    message" assumption).

    **Callout is required.** GetMoney! delivers two URL kinds into the
    same inbox: real click-coin URLs (carry a ``クリックで「N」Ptゲット``
    callout near the anchor) and survey-invitation URLs (no
    クリック-prefixed callout — credit is contingent on completing the
    survey, which we cannot and should not automate). Dropping
    no-callout URLs avoids spending click attempts on survey shells
    that won't credit and would otherwise drag down the credit-ratio
    detector with false-positive failures.
    """
    if not body.strip():
        return [], ["empty message body"]
    # ``&amp;`` → ``&`` so the literal ``&`` in the URL pattern matches.
    body = _html.unescape(body)
    text = _strip_html(body) if is_html else body
    # Normalise full-width Pt / digits / brackets so the single callout
    # regex hits both styles. Done after _strip_html so the URL itself
    # — which only uses ASCII — is unaffected.
    callout_text = text.translate(_FULLWIDTH_TRANSLATE)

    candidates: list[ClickCandidate] = []
    seen_urls: set[str] = set()
    skipped_no_callout = 0
    distinct_extra = 0
    for match in _CLICK_COIN_URL_RE.finditer(text):
        url = match.group(0)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        # Search both ahead of and behind the URL — getmoney click-coin
        # mails sometimes place ``クリックで1Ptゲット`` above the anchor.
        window_end = min(len(callout_text), match.end() + _CALLOUT_WINDOW_CHARS)
        callout = _CALLOUT_RE.search(callout_text[match.end() : window_end])
        if callout is None:
            window_start = max(0, match.start() - _CALLOUT_WINDOW_CHARS)
            callout = _CALLOUT_RE.search(callout_text[window_start : match.start()])
        if callout is None:
            # Survey-invitation URL or other non-click-coin shell —
            # clicking would not credit (survey requires completion)
            # and could spam the survey provider. Drop it so the click
            # pipeline only runs on real click-coin items.
            skipped_no_callout += 1
            continue
        if candidates:
            distinct_extra += 1
            continue
        try:
            estimated_points = int(callout.group(1))
        except ValueError:
            estimated_points = None
        try:
            candidate = ClickCandidate.model_validate(
                {
                    "url": url,
                    "anchor_text": callout.group(0),
                    "estimated_points": estimated_points,
                    "extraction_reason": "whitelist_url_pattern_and_anchor",
                }
            )
        except ValueError as exc:
            logger.debug("invalid candidate url %r: %s", url, exc)
            continue
        candidates.append(candidate)

    anomalies: list[str] = []
    # Pure survey-invitation messages (no callout near any URL) are a
    # legitimate "skip this batch" case — don't anomaly-flag them, just
    # let the orchestrator mark them no-credit so they don't get
    # re-processed. Only flag when neither a candidate nor a
    # skipped-survey URL was found in a non-trivially-sized body.
    if not candidates and not skipped_no_callout and len(text) > 800:
        anomalies.append("no click-coin URLs matched message regex (HTML may have changed)")
    if distinct_extra:
        anomalies.append(
            f"message had {distinct_extra + 1} distinct click-coin URL(s); only first kept "
            "(verify discover output if heuristic looks wrong)"
        )
    return candidates, anomalies


def _strip_html(html: str) -> str:
    """Convert HTML to plaintext while preserving anchor hrefs inline."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        a.insert_before(f" {a['href']} ")
    return soup.get_text("\n", strip=False)
