"""ちょびリッチ (https://www.chobirich.com) click-mail body parser.

⚠ Click URL pattern + callout text are NOT YET verified against real
ちょびリッチ mails. The regex below is a best-guess; refine after
running ``discover`` and inspecting actual click-mail bodies.

Notes:
- ちょびリッチ mailmag has クリックURLs; the value per click is
  modest (sub-1 yen each, but with reasonable volume).
- Site rate is 2pt = 1円 (so 2pt unit), which is unusual.
- 2025-11-01 the operator clamped banner-click credit to 1/account
  (was 1/PC + 1/SP). This change applied to in-site banner clicks,
  not to mail-driven click URLs — but the trend says anti-fraud is
  tightening, so monitor degradation alerts carefully.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Final

from bs4 import BeautifulSoup

from ...common.models import ClickCandidate

logger = logging.getLogger(__name__)

# Best-guess click-coin URL pattern. Refine after discover.
CLICK_COIN_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"https://(?:www\.)?chobirich\.com/(?:click|redirect|c|access|cm)/[A-Za-z0-9+/=_\-?&%.]+"
)

CALLOUT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:クリックで|上記URLアクセスで|タップで)\s*[【\[]?\s*(\d{1,4})\s*(?:pt|P|ポイント)"
)
CALLOUT_WINDOW_CHARS: Final[int] = 200

EXCLUSION_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"https://(?:www\.)?chobirich\.com/(?:login|logout|faq|help|contact|optout|unsubscribe)",
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
