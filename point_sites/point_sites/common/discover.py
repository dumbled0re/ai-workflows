"""One-shot recon of Moppy's 毎日貯める (daily-earn) section.

Read-only scraper that visits a fixed list of candidate index pages and
follows in-section links, classifying each page by what kind of
interaction would be needed to claim points. The result tells us which
items are simple enough to add to the auto-click pipeline (a single GET
to a known URL) versus which would require form submission, JS
execution, or human input.

Intentional non-goals:
- Click anything. Discovery never grants or consumes a point.
- Crawl outside Moppy. Items that redirect to merchant sites or external
  surveys are out of scope — they require account-binding actions with
  real consequences and aren't suitable for cron automation.
- Bypass detection. We use the same Session as the click flow (cookies,
  stable UA) and stay within a small page budget so the recon doesn't
  look like a wide crawl.

The output is consumed by ``main.cmd_discover`` which renders a compact
human-readable summary and a JSON dump. Both go to stdout so the GitHub
Actions log captures them — the report is not posted to Slack to avoid
leaking exploitable URLs into chat history.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_BASE = "https://pc.moppy.jp"

# Mypage is the only page we know returns 200 with a logged-in session,
# so it's our reliable starting point. The other seeds are guesses at
# possible 毎日貯める indexes; if any are 404 the crawler just records
# that and moves on to whatever links it can find from mypage.
INDEX_CANDIDATES: tuple[str, ...] = (
    f"{_BASE}/mypage/",
    f"{_BASE}/everyday/",
    f"{_BASE}/coin/",
    f"{_BASE}/cap/",
    f"{_BASE}/category/coin/",
)

# Within-Moppy paths that smell like point-granting destinations. Kept
# narrow on purpose — wider patterns drag in shopping/merchant pages,
# which are out of scope and just waste request budget. Both absolute
# URLs and relative paths are matched; ``_resolve_link`` joins them to
# the page's own URL before queueing.
_HREF_PATH_KEYWORDS = "coin|gacha|bingo|slot|game|quiz|fortune|click|stamp|lottery|everyday|tracking|cap|daily"
_HREF_RE = re.compile(
    rf'<a[^>]+href="((?:https?://(?:pc\.)?moppy\.jp)?/[^"]*?(?:{_HREF_PATH_KEYWORDS})[^"]*?)"',
    re.IGNORECASE,
)
# Anchors whose visible text has a daily-earn keyword — catches links
# whose path doesn't include one of the keywords above (e.g. obscure
# campaign URLs renamed by Moppy).
_DAILY_TEXT_RE = re.compile(
    r'<a[^>]+href="((?:https?://(?:pc\.)?moppy\.jp)?/[^"]+)"[^>]*>[^<]{0,40}'
    r"(?:毎日|コイン|ガチャ|ビンゴ|スロット|占い|くじ|スタンプ|貯める|クリック)",
    re.IGNORECASE,
)
_POINT_TEXT_RE = re.compile(r"(\d{1,4})\s*(?:P|Ｐ|ポイント|コイン)")
_TITLE_RE = re.compile(r"<title[^>]*>([^<]+)</title>", re.IGNORECASE)
# Anchors whose visible text suggests "click here to earn" — used to
# distinguish a get_click page from a passive listing.
_ACTION_BUTTON_RE = re.compile(
    r'<a[^>]+href="([^"]+)"[^>]*>[^<]*(?:クリック|GET|獲得|チャレンジ|スタート|参加|応募|プレイ|コイン)',
    re.IGNORECASE,
)
_FORM_RE = re.compile(r"<form\b", re.IGNORECASE)
_JS_KEYWORDS: tuple[str, ...] = ("addEventListener", "fetch(", "XMLHttpRequest", "csrf", "onclick=")

MAX_PAGES = 30


@dataclass
class FormField:
    """Single ``<input>`` / ``<select>`` / ``<button>`` inside a form.

    ``value`` may be a CSRF token; we keep it because the gacha
    automation needs to pass it back verbatim. Discovery output goes
    only to workflow logs (private repo), not to Slack.
    """

    name: str
    field_type: str
    value: str | None


@dataclass
class FormInfo:
    action: str | None
    method: str
    fields: list[FormField] = field(default_factory=list)


@dataclass
class PageReport:
    url: str
    http_status: int | None
    title: str | None = None
    point_hints: list[str] = field(default_factory=list)
    action_buttons: list[str] = field(default_factory=list)
    forms_count: int = 0
    forms: list[FormInfo] = field(default_factory=list)
    js_keywords: list[str] = field(default_factory=list)
    interaction_guess: str = "unknown"
    err: str | None = None


def classify_interaction(buttons: int, forms: int, js: int) -> str:
    """Best guess at what the page wants from a user.

    Order matters: a page with a form_post path is treated as form_post
    even if it also contains action-button anchors (the form is the
    crediting endpoint; the buttons are usually navigation).
    """
    if forms > 0:
        return "form_post"
    if js > 0 and buttons == 0:
        return "js_required"
    if buttons > 0:
        return "get_click"
    return "unknown"


def extract_forms(html: str, base_url: str) -> list[FormInfo]:
    """Parse ``<form>`` elements with BeautifulSoup for accurate structure.

    Regex-on-HTML is fragile for nested elements and attribute order,
    so we use bs4 here even though the rest of discover is regex-based.
    The cost is one extra parse per page; the recon flow runs at most
    30 pages so this is fine.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[FormInfo] = []
    for form in soup.find_all("form"):
        action_attr = form.get("action")
        action_resolved = _resolve_link(base_url, action_attr) if action_attr else base_url
        method = (form.get("method") or "get").lower()
        fields: list[FormField] = []
        for tag in form.find_all(["input", "select", "textarea", "button"]):
            name = tag.get("name")
            if not name:
                continue
            field_type = tag.get("type") or tag.name
            # Hidden / CSRF tokens carry the full value (caller needs it
            # to round-trip the form). Visible text inputs are also kept
            # so we know what the form expects from a user.
            value = tag.get("value")
            fields.append(FormField(name=str(name), field_type=str(field_type), value=value))
        out.append(FormInfo(action=action_resolved, method=method, fields=fields))
    return out


def analyze_html(url: str, http_status: int, html: str) -> PageReport:
    title_m = _TITLE_RE.search(html)
    title = title_m.group(1).strip() if title_m else None
    point_hints = sorted({m.group(0) for m in _POINT_TEXT_RE.finditer(html)})[:10]
    action_buttons = sorted({m.group(1) for m in _ACTION_BUTTON_RE.finditer(html)})[:20]
    forms = extract_forms(html, url)
    forms_count = len(forms)
    js_kw = [kw for kw in _JS_KEYWORDS if kw in html]
    return PageReport(
        url=url,
        http_status=http_status,
        title=title,
        point_hints=point_hints,
        action_buttons=action_buttons,
        forms_count=forms_count,
        forms=forms,
        js_keywords=js_kw,
        interaction_guess=classify_interaction(len(action_buttons), forms_count, len(js_kw)),
    )


def fetch_page(
    session: requests.Session,
    url: str,
    *,
    timeout: tuple[float, float] = (10.0, 30.0),
) -> tuple[PageReport, str]:
    """Fetch a page and return (analyzed report, raw html).

    On HTTP error or network failure we still return a (skeletal) report
    so the caller's pages-seen ledger doesn't lose track. Empty html
    means "no body to scan for follow-up links".
    """
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        return PageReport(url=url, http_status=None, err=str(exc)), ""
    if resp.status_code != 200:
        return PageReport(url=url, http_status=resp.status_code), ""
    html = resp.text
    return analyze_html(url, 200, html), html


def _resolve_link(base_url: str, href: str) -> str | None:
    """Join a (possibly relative) href to ``base_url`` and gate on host.

    Returns ``None`` if the result would leave Moppy or has a scheme we
    don't follow (mailto:, javascript:, etc).
    """
    href = href.split("#", 1)[0].strip()
    if not href:
        return None
    if href.startswith(("javascript:", "mailto:", "tel:", "#")):
        return None
    abs_url = urljoin(base_url, href)
    parsed = urlparse(abs_url)
    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.hostname or not parsed.hostname.endswith("moppy.jp"):
        return None
    return abs_url


def _extract_links(base_url: str, html: str) -> list[str]:
    """Combine path-keyword and visible-text-keyword link extraction."""
    found: list[str] = []
    for pat in (_HREF_RE, _DAILY_TEXT_RE):
        for m in pat.finditer(html):
            link = _resolve_link(base_url, m.group(1))
            if link is not None:
                found.append(link)
    # Preserve discovery order while deduping.
    seen_local: set[str] = set()
    deduped: list[str] = []
    for link in found:
        if link in seen_local:
            continue
        seen_local.add(link)
        deduped.append(link)
    return deduped


def discover(
    session: requests.Session,
    *,
    seeds: tuple[str, ...] = INDEX_CANDIDATES,
    max_pages: int = MAX_PAGES,
) -> list[PageReport]:
    """BFS within the daily-earn section starting from ``seeds``.

    Capped at ``max_pages`` to keep the request volume from looking like
    a wide-area crawl to Moppy's anti-fraud system.
    """
    seen: dict[str, PageReport] = {}
    queue: list[str] = list(seeds)
    while queue and len(seen) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        report, html = fetch_page(session, url)
        seen[url] = report
        if not html:
            continue
        for link in _extract_links(url, html):
            if link in seen or link in queue:
                continue
            if len(seen) + len(queue) >= max_pages:
                break
            queue.append(link)
    return list(seen.values())


def render_report(reports: list[PageReport]) -> str:
    """Compact human-readable summary for workflow logs."""
    lines = [f"Discovered {len(reports)} pages:"]
    for r in sorted(reports, key=lambda x: (x.interaction_guess, x.url)):
        status = r.http_status if r.http_status is not None else "ERR"
        lines.append(f"  [{r.interaction_guess:11s}] HTTP {status} {r.url}")
        if r.title:
            lines.append(f"      title: {r.title}")
        if r.point_hints:
            lines.append(f"      points: {', '.join(r.point_hints[:5])}")
        if r.action_buttons:
            lines.append(f"      action_buttons ({len(r.action_buttons)}):")
            for b in r.action_buttons[:5]:
                lines.append(f"        - {b}")
        for i, fi in enumerate(r.forms):
            field_names = [f.name for f in fi.fields]
            lines.append(f"      form[{i}]: {fi.method.upper()} {fi.action or '<self>'} fields={field_names}")
        if r.err:
            lines.append(f"      err: {r.err}")
    return "\n".join(lines)
