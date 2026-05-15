"""Sugutama Gmail click-mail parser.

Single ``parse(body, is_html)`` entry point used by ``GmailSource``:
extracts click-coin URLs from one mail body (plaintext or HTML).
The on-site inbox approach was abandoned 2026-05-15 after live probing
showed sugutama ships click-mails to external email only — see
``__init__.py`` for the rationale.

⚠ URL regex + callout patterns are best-guess until the first real
click-mail is observed via ``-f extract_links=true`` and the actual
shape is confirmed against Slack output.

**ad-fraud 隔離方針 (絶対):**
ネットマイル系は無料ガチャ・スロットを持つことが recon で確認済み。
ガチャ系 path がメール本文に混ざるリスクに備えて 3 層防御:

1. ``allowed_hosts`` は sugutama.jp / netmile.co.jp 系のみ (adapter 側)
2. ``EXCLUSION_URL_RE`` で game / gacha / slot / lottery / kuji
   / garapon / regist / login 等を弾く
3. callout (``クリックでXmile`` or ``クリックでXpt``) が無い URL は drop
   (= getmoney/warau の防御パターン)
"""

from __future__ import annotations

import html as _html
import logging
import re

from bs4 import BeautifulSoup

from ...common.models import ClickCandidate

logger = logging.getLogger(__name__)

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


def parse(body: str, is_html: bool = False) -> tuple[list[ClickCandidate], list[str]]:
    """Extract click-coin URL(s) from a single Gmail click-mail body.

    Multi-defence against ad-fraud:
    1. ``EXCLUSION_URL_RE`` drops game / gacha / lottery URLs immediately.
    2. ``CALLOUT_RE`` is required — URLs without ``クリックでN(mile|Pt)``
       callout are dropped (= getmoney/warau survey-URL defence).
    3. Only the first valid candidate per message is kept.
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
    # Only escalate "0 URL matches" when the body looks like a click-mail
    # (carries a ``クリックでN(mile|Pt)`` callout). Welcome / registration
    # / newsletter mails legitimately have zero click URLs and would
    # otherwise raise false positives every run — they silently go to
    # no_coins instead.
    if not candidates and not skipped_no_callout and len(text) > 800 and _CALLOUT_RE.search(callout_text):
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
