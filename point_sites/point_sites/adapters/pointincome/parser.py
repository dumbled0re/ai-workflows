"""ポイントインカム (https://pointi.jp) click-mail body parser.

⚠ The exact click-coin URL pattern + callout text format are not yet
verified against real ポイントインカム mails. The regex below is a best
guess based on:

  - Operator (株式会社セレス) is the same as Moppy as of 2025-09, so
    URL structure may resemble Moppy's ``/cc/c?t=...``.
  - Public site uses ``pointi.jp`` (no ``pc.`` prefix on PC).
  - Industry term is "クリポメール" with multiple URLs per mail crediting
    a few points each.

After registering ``POINTINCOME_COOKIES`` and running discover the
first time, refine the patterns to match what shows up in workflow
logs. Keep the regex narrow — wider patterns sweep in shopping/merchant
URLs which are out of scope.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Final

from bs4 import BeautifulSoup

from ...common.models import ClickCandidate

logger = logging.getLogger(__name__)

# Click-coin URL pattern. 2026-05-27 user fixture (sample mail body):
#   https://pointi.jp/al/click_mail_magazine.php?no=...&hash=...&html=1&a=...
# 既存の ``(?:click|cc|access|c)`` に ``al`` パスを追加。実 mail で確認した
# pointincome のメルマガクリック URL は ``/al/click_mail_magazine.php``。
CLICK_COIN_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"https://(?:www\.)?pointi\.jp/(?:al|click|cc|access|c)/[A-Za-z0-9+/=_\-?&%.]+"
)

# Callout pattern. 2026-05-27 user fixture:
#   "▼クリックで3ptゲット（※有効期限：05月29日まで）"
# 「クリックでXpt」 style で match 確認済。
CALLOUT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:クリックで|上記URLアクセスで|タップで)\s*[【\[]?\s*(\d{1,3})\s*(?:pt|P|ポイント|コイン)"
)
# 2026-05-27 user fixture で判明: pointincome のメルマガは callout が URL の
# **前** 行にある (「▼クリックで3ptゲット\nhttps://pointi.jp/al/...」)。
# 旧実装は URL の後 200 文字だけ検索していたため見逃していた。前後両方を
# 走査するため window は URL の前後それぞれ CALLOUT_WINDOW_CHARS で取る。
CALLOUT_WINDOW_CHARS: Final[int] = 200

# URLs to exclude even if they match (login, FAQ, etc).
EXCLUSION_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"https://(?:www\.)?pointi\.jp/(?:login|logout|entrance|faq|help|contact|opt|unsubscribe)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParseAnomaly:
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


def _strip_html(html: str) -> str:
    """Convert HTML to plaintext while preserving anchor href values inline.

    Same trick as the Moppy parser: BeautifulSoup ``get_text`` drops
    attributes, but we need href values to land in the text stream so
    the regex can find them. Inject each href before its visible text.
    """
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        a.insert_before(a["href"] + " ")
    return soup.get_text("\n", strip=False)


def _to_plaintext(body: str, *, is_html: bool) -> str:
    if not body:
        return ""
    return _strip_html(body) if is_html else body


def parse(body: str, is_html: bool = False) -> tuple[list[ClickCandidate], list[str]]:
    """Extract ClickCandidate list from a ポイントインカム email body.

    Same shape as the Moppy parser so the Adapter contract is uniform.
    Returns ``(candidates, anomalies)``. Anomalies are flat strings
    (``ParseAnomaly`` is just a local helper, never leaked).
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

        # 2026-05-27 fix: callout は URL の前後どちらにも置かれる
        # (pointincome 実 mail では「▼クリックでXptゲット\nhttps://...」 と
        # URL の **前** に出る)。URL の前後 ``CALLOUT_WINDOW_CHARS`` 文字を
        # 走査して match を探す。
        window_start = max(0, match.start() - CALLOUT_WINDOW_CHARS)
        window_end = min(len(text), match.end() + CALLOUT_WINDOW_CHARS)
        window = text[window_start:window_end]
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
