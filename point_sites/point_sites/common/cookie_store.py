"""Persist Moppy session cookies across workflow runs.

Why this exists: Moppy rotates session cookies via Set-Cookie. Within a
single process, ``requests.Session`` tracks those updates automatically,
so all 11 clicks in one workflow run see the rotated values. But the
NEXT workflow run starts fresh from the (now stale) ``MOPPY_COOKIES``
Secret. Submitting a stale token to Moppy looks like a replay and the
session is killed — which is exactly what we observed: a freshly
exported cookie working once, then 401-equivalent on the next run a few
minutes later.

The fix is to persist the live jar to ``data/cookies.json`` (artifact'd
alongside ``state.json``) so each run begins from the latest rotation.
On the very first run, on artifact expiry, or on file corruption, we
fall back to the env ``MOPPY_COOKIES`` Secret.

Security: the file contains real session tokens. Stays within the
private repo's artifact storage (30-day retention), never in git, and
must not be logged or sent to Slack.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from pathlib import Path

from requests.cookies import RequestsCookieJar

logger = logging.getLogger(__name__)


# Known third-party tracker cookie name patterns. These commonly slip
# past ``domain_matches_hosts`` because trackers (Google Analytics,
# Microsoft Clarity, Hotjar, etc.) set them with ``Domain=.<site>.com``
# umbrella scope — so the cookie appears first-party from the filter's
# perspective even though it carries no auth state.
#
# Observed 2026-05-30 on pointtown: 8 GMO wizards visiting ad-laden
# subdomains ballooned the persisted jar from 16 → 49 cookies, all of
# which passed the domain filter (Domain=.pointtown.com) but were
# tracker-set. The resulting bloated Cookie header tripped GMO's anti-bot
# detection and invalidated the session every day. This name-based
# blacklist complements the domain filter to keep auth-relevant cookies
# only.
#
# The list intentionally covers the most prolific umbrella-scope
# offenders. Add a pattern here when a new failure run shows a tracker
# slipping through.
TRACKER_COOKIE_NAME_RE = re.compile(
    r"^("
    r"_ga(_|$)|_gid|_gat|__utm[a-z]?|_dc_gtm_|"  # Google Analytics / GTM
    r"_clck|_clsk|"  # Microsoft Clarity
    r"_hj[A-Za-z]+|"  # Hotjar
    r"_uet(sid|vid)|"  # Microsoft UET (Bing Ads)
    r"gcl_au|"  # Google Conversion Linker
    r"_fbp|_fbc|"  # Meta Pixel
    r"AMCVS?_|s_(cc|sq|ppv|vi)|"  # Adobe Analytics
    r"_ttp|ttwid|"  # TikTok Pixel
    r"_pin_unauth|"  # Pinterest
    r"OptanonConsent|OptanonAlertBoxClosed|"  # OneTrust consent
    r"yjad_|_yjuid|YJSC_"  # Yahoo! Japan ads (omit "B" legacy bcookie — too broad)
    r")"
)


def is_tracker_cookie(name: str) -> bool:
    """True iff ``name`` matches a known third-party tracker pattern."""
    return TRACKER_COOKIE_NAME_RE.match(name) is not None


def domain_matches_hosts(cookie_domain: str, allowed_hosts: Iterable[str]) -> bool:
    """Standard cookie domain matching — does any ``allowed_hosts`` host
    receive a cookie with this domain?

    A cookie with domain ``D`` (with or without leading dot) is sent to
    host ``H`` if either ``D`` (stripped of any leading dot) equals
    ``H``, or ``H`` is a subdomain of ``D``. Returns ``False`` for an
    empty domain (which can happen for first-party non-domain cookies)
    so that anti-bot tracker cookies don't slip through the filter.

    Used by ``save_jar`` and by main.py's browser-cookie merge path to
    keep third-party tracking cookies (analytics, ads) from
    accumulating in the persisted jar — observed 2026-05-15 with
    pointtown, where a Playwright login-bonus wizard ballooned the jar
    from 16 to 338 cookies and induced 1-hour session expiry.
    """
    d = cookie_domain.lstrip(".")
    if not d:
        return False
    return any(host == d or host.endswith("." + d) for host in allowed_hosts)


def save_jar(
    jar: RequestsCookieJar,
    path: str | Path,
    allowed_hosts: Iterable[str] | None = None,
) -> int:
    """Write the jar to ``path`` atomically. Returns the number of cookies saved.

    Output shape matches the user's exported MOPPY_COOKIES so the persisted
    file and the bootstrap Secret round-trip identically through
    ``Clicker.__init__``.

    When ``allowed_hosts`` is provided, cookies whose domain does not
    cover any of those hosts are dropped before writing. This keeps
    third-party tracking cookies — picked up by Playwright wizards on
    pages that include analytics / ad scripts — from polluting the
    persisted jar across runs. Pass ``None`` for the (legacy) "save
    everything" behavior.
    """
    cookies: list[dict[str, object]] = []
    dropped_trackers = 0
    for c in jar:
        domain = c.domain or ".moppy.jp"
        if allowed_hosts is not None and not domain_matches_hosts(domain, allowed_hosts):
            continue
        if is_tracker_cookie(c.name):
            dropped_trackers += 1
            continue
        cookies.append(
            {
                "name": c.name,
                "value": c.value,
                "domain": domain,
                "path": c.path or "/",
                "secure": bool(c.secure),
            }
        )
    if dropped_trackers:
        logger.info("dropped %d tracker cookie(s) before persist", dropped_trackers)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: a crash mid-write must not poison the next run with
    # a half-flushed file. Same pattern as state_store.save.
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    return len(cookies)


def load(path: str | Path) -> list[dict[str, object]] | None:
    """Return the persisted cookie list, or ``None`` to signal the bootstrap path.

    ``None`` is returned for any of: file missing, JSON corrupt, wrong
    shape, empty list. The caller falls back to ``MOPPY_COOKIES``.
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("cookie store corrupt; falling back to env. err=%s", exc)
        return None
    if not isinstance(data, list) or not data:
        return None
    for c in data:
        if not isinstance(c, dict) or "name" not in c or "value" not in c:
            logger.warning("cookie store invalid shape; falling back to env")
            return None
    return data
