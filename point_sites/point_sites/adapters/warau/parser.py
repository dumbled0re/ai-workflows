"""Warau on-site Webメール inbox parser.

Two parsers used by ``OnsiteInboxSource``:

- ``parse_inbox`` enumerates message-detail links in the inbox listing
  at ``/mail/list``.
- ``parse_message`` extracts click-coin URLs from one message detail
  page.

⚠ All regexes are best-guess until the first authenticated inspect.
Refine after seeing a real inbox row + a real click-mail body.

**ad-fraud 隔離方針 (絶対):**
ワラウは独自運営のゲーム (``/games/auth/*``, ``/contents/jankenClover``,
``/contents/easygame/slotbox`` 等 8+) を持ち、ゲーム内の sponsored 広告は
第三者広告ネットワーク経由の可能性が recon で示唆された。click-mail
本文内に game URL が混ざっている可能性もあるため、複数防御を入れる:

1. ``allowed_hosts`` は warau.jp / www.warau.jp のみ (adapter 側)
2. ``EXCLUSION_URL_RE`` で game / gacha / slot / lottery / kuji /
   jankenClover / fuwapon / easygame / mero(fru) 等 path を弾く
3. callout (``クリックでXpt``) が無い URL は drop (= getmoney の survey
   URL 対応と同じ防御策、純粋な click-coin 以外を全部除外)

これで pure click-coin URL のみ click 対象になる。
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

_INBOX_BASE = "https://www.warau.jp/"

# Inbox listing rows. Pattern best-guess based on industry-standard
# on-site mailbox shapes (pointtown uses /mypage/mail/<id>; warau likely
# uses /mail/show/<id> or /mail/<id>). Refine after first inspect.
_MESSAGE_LINK_RE = re.compile(
    r"^/mail/(?:show/|view/|read/|detail/|)(\d+)/?(?:\?[^#]*)?$",
)

# Best-guess click-coin URL pattern. Apex + www subdomain; click /
# tracking endpoints typically live under one of these paths. Tighten
# to the actually observed shape after first message inspect.
_CLICK_COIN_URL_RE = re.compile(
    r"https://(?:www\.)?warau\.jp"
    r"/(?:click|cc|access|c|jump|track|mail/click|click/c)"
    r"[A-Za-z0-9+/=_\-?&%./]*",
)

# Standard callout shapes; warau is expected to use ``クリックでXpt`` /
# ``XPtゲット`` style. ``Pt`` and ``ポイント`` covered.
_CALLOUT_RE = re.compile(
    r"クリック(?:で|して)?\s*[「【\[]?\s*(\d{1,4})\s*(?:Pt|pt|P|ポイント)",
)
_CALLOUT_WINDOW_CHARS = 240

# Full-width → half-width normalisation for callout matching (some
# Japanese sites mix ``１Ｐｔ`` and ``1Pt`` in the same body).
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

# Hard exclusion for ad-fraud / game / non-click-coin endpoints. These
# are dropped even before the callout check — they must NEVER end up in
# the click pipeline regardless of body context. Updated as new game
# paths are surfaced via discover.
EXCLUSION_URL_RE = re.compile(
    r"https?://(?:www\.)?warau\.jp"
    r"/(?:games?|contents/(?:janken|fuwapon|easygame|slot|lottery|kuji|mero)"
    r"|gacha|slot|lottery|kuji|login|logout|entrance|faq|help|contact|opt"
    r"|unsubscribe|regist|withdraw|policy|terms)",
    re.IGNORECASE,
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

    Multi-defence against ad-fraud:
    1. ``EXCLUSION_URL_RE`` drops game / gacha / lottery URLs immediately.
    2. ``CALLOUT_RE`` is required — URLs without ``クリックでNPt`` callout
       are dropped (= getmoney survey-URL defence). This excludes
       campaign promo URLs, sponsored banners, and ad-network shells.
    3. Only the first valid candidate per message is kept (mirrors
       pointtown / getmoney FAQ pattern: 1 click credits per message).
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
            # Game / gacha / non-click-coin shell — never auto-click.
            continue
        # Callout required (= ad-fraud + survey defence).
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
    # Pure non-click-coin messages (callout check skipped everything)
    # are legitimate — let mark_no_credit handle them silently.
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
    """Convert HTML to plaintext while preserving anchor hrefs inline."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        a.insert_before(f" {a['href']} ")
    return soup.get_text("\n", strip=False)
