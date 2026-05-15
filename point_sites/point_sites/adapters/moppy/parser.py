"""Moppy email body → ClickCandidate extraction (plaintext-first).

Real moppy click-coin emails are plaintext only and follow a fixed shape::

    https://pc.moppy.jp/cc/c?t=<token>
    ▲(N日以内|明日まで)に上記URLアクセスで【Nコイン】GET！

Parser strategy:
  1. find every match of CLICK_COIN_URL_RE in the body (plaintext or HTML-stripped)
  2. confirm the match is a real click-coin URL by inspecting up to N chars after
     the URL — must contain CALLOUT_RE (the "上記URLアクセスで…GET" pattern)
  3. dedupe identical URLs
  4. extract estimated_points from the callout text

For HTML emails (future-proofing), strip tags first then run the same regex.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from ...common.models import ClickCandidate

logger = logging.getLogger(__name__)

CLICK_COIN_URL_RE = re.compile(r"https://pc\.moppy\.jp/cc/c\?t=[A-Za-z0-9+/=_\-]+")

CALLOUT_RE = re.compile(
    # Standard click-mails close with ``【Nコイン】GET！``; campaign
    # mails (e.g. friend-referral 5月キャンペーン) close with
    # ``【Nコイン】もらえるオマケ付き`` or ``【Nコイン】プレゼント``.
    # We accept any of the observed closers — the 【Nコイン】 block in
    # ``上記URLアクセスで`` context is what makes this a click-mail; the
    # trailing verb is fluff that moppy rotates per campaign.
    r"上記URLアクセスで\s*【\s*(\d{1,3})\s*コイン\s*】\s*(?:GET|ゲット|もらえる|プレゼント)",
)

CALLOUT_WINDOW_CHARS = 200

EXCLUSION_URL_RE = re.compile(
    r"https?://[^\s<>\"']*?(?:"
    r"unsubscribe|optout|policy|/terms|/faq|/help|/login|/contact|"
    r"edit_mail_flg|guide/|/info/rule|/friend/"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParseAnomaly:
    kind: str
    detail: str


def _strip_html(html: str) -> str:
    """Convert HTML to plaintext while preserving anchor href values inline.

    BeautifulSoup's ``get_text`` drops attributes; for click-coin extraction we
    need the href URL to land in the resulting text stream so the regex can
    find it. We inject each href before the anchor's visible text.
    """
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        a.insert_before(f" {href} ")
    return soup.get_text("\n", strip=False)


def _to_plaintext(body: str, *, is_html: bool) -> str:
    if not body:
        return ""
    return _strip_html(body) if is_html else body


def parse(body: str, is_html: bool = False) -> tuple[list[ClickCandidate], list[str]]:
    """Extract ClickCandidate list from a moppy email body.

    ``body`` may be plaintext or HTML; pass ``is_html=True`` for HTML emails
    (we strip tags then run the same regex). Returns
    ``(candidates, anomalies)`` where each anomaly is a flat string so the
    caller can just log/append without depending on this module's types.
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
                    detail=f"{len(unconfirmed_urls)} cc/c URL(s) without a matching callout",
                )
            )
        )
    return candidates, anomalies


def is_count_anomalous(count: int, baseline: int | None) -> bool:
    """Caller-supplied baseline lets us flag template-shift surges."""
    if baseline is None:
        return False
    return count > max(3, baseline * 3)
