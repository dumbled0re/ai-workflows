"""Sugutama on-site Webメール inbox parser.

Two parsers used by ``OnsiteInboxSource``:

- ``parse_inbox`` enumerates message-detail links in the inbox listing
  at ``/sugutama/mail/`` (or ``/sugutama/mail_box/`` if the first inspect
  shows that's the canonical path).
- ``parse_message`` extracts click-coin URLs from one message detail
  page.

⚠ All regexes are best-guess until the first authenticated inspect.
The recon agent (2026-05-10) suggested ``/sugutama/ads/{id}?lo=...`` as
a candidate message URL shape, but that was based on indirect signals
and may not match. Refine after seeing real inbox + real click-mail.

**ad-fraud 隔離 (絶対):**
ネットマイル系は無料ガチャ・スロットを持つことが recon で確認済み。
ガチャ系 path がメール本文に混ざるリスクに備えて 3 層防御:

1. ``allowed_hosts`` は sugutama.jp / netmile.co.jp 系のみ (adapter 側)
2. ``EXCLUSION_URL_RE`` で game / gacha / slot / lottery / kuji
   / regist / login 等を弾く
3. callout (``クリックでXmile`` or ``クリックでXpt``) が無い URL は drop
   (= getmoney/warau の防御パターン)
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

_INBOX_BASE = "https://www.netmile.co.jp/sugutama/"

# Inbox listing rows. Best-guess pattern based on recon hint
# (`/sugutama/ads/{id}?lo=...` was suggested) and standard on-site
# mailbox shapes. Cover both `/ads/<id>` and `/mail/<id>` shapes;
# narrow after inspect.
_MESSAGE_LINK_RE = re.compile(
    r"^(?:/sugutama)?/(?:ads|mail|message)/(?:show/|view/|read/)?(\d+)(?:\?[^#]*)?$",
)

# Best-guess click-coin URL pattern. Apex sugutama.jp + netmile click
# tracker. Tighten to actual shape after inspect.
_CLICK_COIN_URL_RE = re.compile(
    r"https://(?:www\.)?(?:sugutama\.jp|netmile\.co\.jp)"
    r"/(?:click|cc|access|c|jump|track|sugutama/click|sugutama/c|click_through)"
    r"[A-Za-z0-9+/=_\-?&%./]*",
)

# Callout shapes. すぐたま rewards in ``mile`` so include that unit on
# top of standard Pt / ポイント. Full-width ``Ｐｔ`` / ``１`` 等 normalised
# below.
_CALLOUT_RE = re.compile(
    r"クリック(?:で|して)?\s*[「【\[]?\s*(\d{1,4})\s*(?:mile|マイル|Pt|pt|P|ポイント)",
)
_CALLOUT_WINDOW_CHARS = 240

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

# Hard exclusion for ad-fraud / non-click-coin endpoints.
EXCLUSION_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:sugutama\.jp|netmile\.co\.jp)"
    r"/(?:sugutama/)?(?:games?|gacha|slot|lottery|kuji|garapon"
    r"|login|logout|entrance|faq|help|contact|opt|unsubscribe"
    r"|regist|withdraw|policy|terms)",
    re.IGNORECASE,
)


def parse_inbox(html: str) -> tuple[list[InboxEntry], list[str]]:
    """Extract message-link rows from the inbox listing HTML."""
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
        state_key = urljoin(_INBOX_BASE, href)
        if state_key in seen_keys:
            continue
        seen_keys.add(state_key)
        label = a.get_text(strip=True) or f"mail-{msg_id}"
        entries.append(
            InboxEntry(
                state_key=state_key,
                message_url=state_key,
                label=label[:120],
            )
        )

    anomalies: list[str] = []
    if not entries and len(html) > 1500:
        anomalies.append(
            "no message links matched inbox-list regex (HTML may have changed — refine _MESSAGE_LINK_RE after inspect)"
        )
    return entries, anomalies


def parse_message(body: str, is_html: bool = False) -> tuple[list[ClickCandidate], list[str]]:
    """Extract click-coin URL(s) from a single message detail page.

    Same multi-defence model as warau/getmoney: exclusion regex first,
    callout required, only first valid candidate kept.
    """
    if not body.strip():
        return [], ["empty message body"]
    body = _html.unescape(body)
    text = _strip_html(body) if is_html else body
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
        if EXCLUSION_URL_RE.match(url):
            continue
        window_end = min(len(callout_text), match.end() + _CALLOUT_WINDOW_CHARS)
        callout = _CALLOUT_RE.search(callout_text[match.end() : window_end])
        if callout is None:
            window_start = max(0, match.start() - _CALLOUT_WINDOW_CHARS)
            callout = _CALLOUT_RE.search(callout_text[window_start : match.start()])
        if callout is None:
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
    if not candidates and not skipped_no_callout and len(text) > 800:
        anomalies.append(
            "no click-coin URLs matched message regex (HTML may have changed — refine _CLICK_COIN_URL_RE after inspect)"
        )
    if distinct_extra:
        anomalies.append(
            f"message had {distinct_extra + 1} distinct click-coin URL(s); only first kept "
            "(verify discover output if heuristic looks wrong)"
        )
    return candidates, anomalies


def _strip_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        a.insert_before(f" {a['href']} ")
    return soup.get_text("\n", strip=False)
