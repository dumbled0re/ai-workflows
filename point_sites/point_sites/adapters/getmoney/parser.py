"""GetMoney! click-mail body parser.

⚠ The URL pattern + callout text format are not yet verified against
real GetMoney! mails. Initial regex below is a best-guess; refine
after the first mail lands and shows up in workflow logs.

Reference points for the guess:
  - 旧ドメイン ``getmoney.jp`` は ``dietnavi.com`` に統合済。クリックURL
    も dietnavi 側に寄っている可能性が高いが、メール内 link が
    ``getmoney.jp`` 経由で発行されているケースも残っているはず。
  - 同系の moppy / pointincome は ``/cc/c?t=...`` 形式の tracking URL
    を使っているので、dietnavi 系も同類のパスがあると推測。
  - 業界標準の callout は ``クリックでXpt`` ``上記URLアクセスでXpt``
    ``タップでXpt``。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Final

from bs4 import BeautifulSoup

from ...common.models import ClickCandidate

logger = logging.getLogger(__name__)

# TEMPORARY: ultra-wide pattern for first-mail discovery. Once we see
# what dietnavi click-coin URLs actually look like in the wild, narrow
# this to the real path (e.g. ``/pc/click/c\?...``) so shopping/login
# URLs aren't accidentally picked up.
CLICK_COIN_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"https://(?:[a-z0-9-]+\.)?(?:dietnavi\.com|getmoney\.jp)/[A-Za-z0-9+/=_\-?&%.#]+"
)

# Standard callout shapes across major Japanese point sites. Refine
# once a real fixture lands.
CALLOUT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:クリックで|上記URLアクセスで|タップで)\s*[【\[]?\s*(\d{1,3})\s*(?:pt|P|ポイント|コイン)"
)
CALLOUT_WINDOW_CHARS: Final[int] = 200

# URLs to exclude even if they match the click pattern.
EXCLUSION_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"https://(?:[a-z0-9-]+\.)?(?:dietnavi\.com|getmoney\.jp)"
    r"/(?:login|logout|entrance|faq|help|contact|opt|unsubscribe)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParseAnomaly:
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


def _strip_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        a.insert_before(a["href"] + " ")
    return soup.get_text("\n", strip=False)


def _to_plaintext(body: str, *, is_html: bool) -> str:
    if not body:
        return ""
    return _strip_html(body) if is_html else body


def parse(body: str, is_html: bool = False) -> tuple[list[ClickCandidate], list[str]]:
    """Extract ClickCandidate list from a GetMoney! email body.

    Same shape as the moppy/pointincome parsers so the Adapter contract
    stays uniform. Returns ``(candidates, anomalies)``.
    """
    text = _to_plaintext(body, is_html=is_html)
    if not text.strip():
        return [], [str(ParseAnomaly(kind="empty_body", detail="no text content"))]

    candidates: list[ClickCandidate] = []
    seen_urls: set[str] = set()
    unconfirmed_urls: list[str] = []

    for match in CLICK_COIN_URL_RE.finditer(text):
        url = match.group(0)
        if url in seen_urls:
            continue
        if EXCLUSION_URL_RE.match(url):
            continue

        window_end = min(len(text), match.end() + CALLOUT_WINDOW_CHARS)
        window = text[match.end() : window_end]
        callout = CALLOUT_RE.search(window)
        if callout is None:
            unconfirmed_urls.append(url)
            continue

        try:
            estimated_points = int(callout.group(1))
        except ValueError:
            estimated_points = None

        try:
            candidate = ClickCandidate(
                url=url,  # type: ignore[arg-type]
                anchor_text=callout.group(0),
                estimated_points=estimated_points,
                extraction_reason="whitelist_url_pattern_and_anchor",
            )
        except ValueError as exc:
            logger.debug("invalid candidate url %r: %s", url, exc)
            continue

        seen_urls.add(url)
        candidates.append(candidate)

    anomalies: list[str] = []
    if unconfirmed_urls:
        anomalies.append(
            str(
                ParseAnomaly(
                    kind="url_without_callout",
                    detail=f"{len(unconfirmed_urls)} click URL(s) without a matching callout",
                )
            )
        )
    return candidates, anomalies
