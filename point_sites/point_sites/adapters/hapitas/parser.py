"""ハピタス (https://hapitas.jp) click-mail body parser.

⚠ Click URL pattern + callout text are NOT YET verified against real
ハピタス mails. The regex below is best-guess; refine after running
``discover`` and inspecting the actual click-mail body.

Notes from research:
- ハピタス mails include both click-coin URLs AND 宝くじ交換券 URLs
  (treat both as candidates).
- Crediting URL is valid 7 days from mail receipt.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Final

from bs4 import BeautifulSoup

from ...common.models import ClickCandidate

logger = logging.getLogger(__name__)

# Best-guess click-coin URL: hapitas tracking endpoints typically live
# under /click/ or /redirect/ subpaths. Refine after discover.
CLICK_COIN_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"https://(?:www\.)?hapitas\.jp/(?:click|redirect|c|access)/[A-Za-z0-9+/=_\-?&%.]+"
)

CALLOUT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:クリックで|上記URLアクセスで|タップで|宝くじ交換券)\s*[【\[]?\s*(\d{1,4})\s*(?:pt|P|ポイント|枚)"
)
CALLOUT_WINDOW_CHARS: Final[int] = 200

EXCLUSION_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"https://(?:www\.)?hapitas\.jp/(?:auth/signin|logout|faq|help|contact|optout|unsubscribe)",
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
