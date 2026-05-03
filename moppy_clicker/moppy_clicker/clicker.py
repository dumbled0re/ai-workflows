"""HTTP GET click executor.

Failure mode policy: GET requests against moppy click endpoints have a side
effect (granting points), so failures are NOT auto-retried within a single
process run. The state_store handles cross-run retry up to ``max_attempts``.
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


class Clicker:
    def __init__(
        self,
        interval_min: int = 5,
        interval_max: int = 15,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        max_redirects: int = 10,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.interval_min = interval_min
        self.interval_max = interval_max
        self.timeout = (connect_timeout, read_timeout)
        self.session = requests.Session()
        self.session.max_redirects = max_redirects
        self.session.headers.update({"User-Agent": user_agent})

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


ALLOWED_MANUAL_HOSTS = {
    "pc.moppy.jp",
    "mail.moppy.jp",
    "track.moppy.jp",
    "moppy.jp",
}


def is_manual_url_allowed(url: str) -> bool:
    """Restrict ``main click <URL>`` invocations to moppy hosts."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.hostname:
        return False
    return parsed.hostname in ALLOWED_MANUAL_HOSTS or parsed.hostname.endswith(".moppy.jp")
