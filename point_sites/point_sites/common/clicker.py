"""HTTP GET click executor.

Failure mode policy: GET requests against site click endpoints have a side
effect (granting points), so failures are NOT auto-retried within a single
process run. The state_store handles cross-run retry up to ``max_attempts``.

The ``Clicker`` itself is site-agnostic; per-site values (default cookie
domain, mypage URL, login keyword, manual-click allowed hosts) come from
the active ``Adapter``.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import UTC, datetime
from urllib.parse import urlparse

import requests

from .models import ClickCandidate, ClickResult
from .redaction import host_only, redact_url

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Browser-like default headers in addition to User-Agent. Some site CDNs
# (chobirich-style WAFs in particular) reject requests that look like a
# bare scraper based on missing Accept / Accept-Language / Sec-Fetch-*
# headers. Sending them by default brings every request closer to a real
# Chrome navigation. Sites that are happy with minimal headers (moppy /
# amefuri / hapitas / pointincome) are unaffected — they don't reject
# extra headers, just don't require them.
_BROWSER_DEFAULT_HEADERS: dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


class Clicker:
    def __init__(
        self,
        interval_min: int = 5,
        interval_max: int = 15,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        max_redirects: int = 10,
        user_agent: str = DEFAULT_USER_AGENT,
        cookies: list[dict[str, object]] | None = None,
        default_cookie_domain: str = ".moppy.jp",
    ) -> None:
        self.interval_min = interval_min
        self.interval_max = interval_max
        self.timeout = (connect_timeout, read_timeout)
        self.session = requests.Session()
        self.session.max_redirects = max_redirects
        self.session.headers.update({"User-Agent": user_agent, **_BROWSER_DEFAULT_HEADERS})
        self.authenticated = False
        if cookies:
            for c in cookies:
                # ``secure`` defaults to True at the config layer so session
                # cookies never travel over plain HTTP — important for the
                # manual-click path which can be invoked with arbitrary URLs.
                self.session.cookies.set(
                    str(c["name"]),
                    str(c["value"]),
                    domain=str(c.get("domain", default_cookie_domain)),
                    path=str(c.get("path", "/")),
                    secure=bool(c.get("secure", True)),
                )
            self.authenticated = True

    def verify_login(self, mypage_url: str, login_keyword: str = "ログアウト") -> bool:
        """GET mypage and check whether the session is logged in.

        Heuristic: logged-in mypage contains the adapter's login_keyword
        (default 'ログアウト'); the public landing redirects/returns a page
        without it. We avoid asserting on specific HTML structure to stay
        resilient to template changes.
        """
        try:
            resp = self.session.get(mypage_url, timeout=self.timeout, allow_redirects=True)
        except requests.RequestException as exc:
            logger.warning("login verification request failed: %s", exc)
            return False
        if resp.status_code != 200:
            logger.warning("login verification returned HTTP %d", resp.status_code)
            return False
        # If we landed back on a login/entry page, we are NOT logged in.
        final_path = resp.url.lower() if resp.url else ""
        if "login" in final_path or "/entry/" in final_path:
            return False
        body = resp.text
        return login_keyword in body or "logout" in body.lower()

    def click(self, candidate: ClickCandidate) -> ClickResult:
        url_str = str(candidate.url)
        redacted = redact_url(url_str)
        started = time.monotonic()
        timestamp = datetime.now(UTC)

        status_label: str
        http_status: int | None = None
        final_host: str | None = None

        try:
            resp = self.session.get(url_str, timeout=self.timeout, allow_redirects=True)
            http_status = resp.status_code
            final_host = host_only(resp.url) if resp.url else None
            resp.close()
            if 200 <= resp.status_code < 400:
                status_label = "success"
            elif 400 <= resp.status_code < 500:
                status_label = "failed_4xx"
            else:
                status_label = "failed_5xx"
        except requests.exceptions.TooManyRedirects:
            status_label = "failed_redirect"
        except requests.exceptions.Timeout:
            status_label = "failed_timeout"
        except requests.exceptions.ConnectionError:
            status_label = "failed_connection"
        except requests.exceptions.RequestException as exc:
            logger.warning("unexpected request error for %s: %s", redacted, exc)
            status_label = "failed_connection"

        duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "click %s status=%s http=%s host=%s duration=%dms",
            redacted,
            status_label,
            http_status,
            final_host,
            duration_ms,
        )
        return ClickResult(
            candidate=candidate,
            final_status=status_label,  # type: ignore[arg-type]
            http_status=http_status,
            final_host=final_host,
            duration_ms=duration_ms,
            timestamp=timestamp,
        )

    def sleep_between(self) -> None:
        time.sleep(random.uniform(self.interval_min, self.interval_max))


def is_manual_url_allowed(url: str, allowed_hosts: frozenset[str] | set[str]) -> bool:
    """Restrict ``main click <URL>`` invocations to ``allowed_hosts``.

    Only ``https`` is accepted because authenticated clicks must never send
    session cookies over plain HTTP. The exact host set is per-adapter.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    if not parsed.hostname:
        return False
    if parsed.hostname in allowed_hosts:
        return True
    # Allow subdomains of any host in allowed_hosts (e.g. allowed
    # "moppy.jp" matches "pc.moppy.jp" too).
    return any(parsed.hostname.endswith("." + h) for h in allowed_hosts)
