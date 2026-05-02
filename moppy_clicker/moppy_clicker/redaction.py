"""URL/subject/log redaction utilities.

All user-visible output (logs, Slack notifications, state file) MUST pass through
these functions to avoid leaking personal identifiers, tracking tokens, or query
parameters embedded in moppy URLs.
"""

from urllib.parse import urlparse, urlunparse


def redact_url(url: str) -> str:
    """Strip query string and fragment from a URL.

    Example: ``https://pc.moppy.jp/click?uid=123&t=xyz`` →
    ``https://pc.moppy.jp/click``.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return "<unparseable-url>"
    return urlunparse(parsed._replace(query="", fragment=""))


def host_only(url: str) -> str:
    try:
        return urlparse(url).hostname or "<no-host>"
    except ValueError:
        return "<unparseable-url>"


def redact_subject(subject: str, prefix_len: int = 5) -> str:
    if not subject:
        return ""
    stripped = subject.strip()
    if len(stripped) <= prefix_len:
        return stripped
    return stripped[:prefix_len] + "…"
