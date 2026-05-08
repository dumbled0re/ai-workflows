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
from pathlib import Path

from requests.cookies import RequestsCookieJar

logger = logging.getLogger(__name__)


def save_jar(jar: RequestsCookieJar, path: str | Path) -> int:
    """Write the jar to ``path`` atomically. Returns the number of cookies saved.

    Output shape matches the user's exported MOPPY_COOKIES so the persisted
    file and the bootstrap Secret round-trip identically through
    ``Clicker.__init__``.
    """
    cookies: list[dict[str, object]] = []
    for c in jar:
        cookies.append(
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain or ".moppy.jp",
                "path": c.path or "/",
                "secure": bool(c.secure),
            }
        )
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
