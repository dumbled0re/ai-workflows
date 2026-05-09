"""``OnsiteInboxSource`` — scrape an on-site mailbox for click-coin URLs.

For point sites that deliver click-coin "emails" inside the site itself
(no Gmail involvement) — e.g. ポイントタウン's
``/mypage/mail`` mailbox. The flow is:

1. GET the inbox page; ``parse_inbox(html)`` extracts links to each
   unread/uncredited message. The link URL serves as ``state_key`` so
   ``StateStore`` skips messages already credited.
2. For each message, GET the message detail page; ``parse_message``
   extracts the click-coin URLs (the same callable shape as ``GmailSource``
   uses for email bodies).
3. ``cmd_run`` clicks each URL via the authenticated session and verifies
   credit via balance delta — codex consult 2026-05-09.

Per-site: write ``parse_inbox`` and ``parse_message`` in the adapter's
``parser.py`` and inject. Site HTML can change at any time; the parsers
are designed to fail loudly (return empty / anomaly) rather than silently
miss messages, so ``cmd_run`` records a parse_failure and surfaces it
to Slack.

Listing-level anomalies (e.g. inbox HTML shape changed) are surfaced via
synthetic sentinel ``state_key``s whose ``fetch_batch`` returns a
``parse_failed=True`` batch. That routes them through ``cmd_run``'s
existing parse-failure pipeline (logged, counted, posted to Slack)
instead of dying silently.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..models import ClickCandidate
from .base import ClickBatch

if TYPE_CHECKING:
    import requests

    from ...config import Config


logger = logging.getLogger(__name__)


# Prefix used to mark synthetic state_keys representing inbox-listing
# anomalies (HTML shape change, network failure, 4xx). The prefix is
# unique enough that no real message URL collides with it.
_ANOMALY_KEY_PREFIX = "__inbox_anomaly_"


@dataclass(frozen=True)
class InboxEntry:
    """One unread/uncredited message surfaced from the inbox listing.

    ``state_key`` is the value ``StateStore`` keys on (URL or message ID).
    ``message_url`` is what the source GETs to read the body.
    ``label`` is shown in logs / Slack (e.g. subject / preview snippet).
    """

    state_key: str
    message_url: str
    label: str


_ParseInbox = Callable[[str], tuple[list[InboxEntry], list[str]]]
_ParseMessage = Callable[[str, bool], tuple[list[ClickCandidate], list[str]]]


class OnsiteInboxSource:
    """ClickUrlSource that scrapes an on-site message mailbox.

    Two stages mirror Gmail's search + get_message split:
      - ``list_state_keys`` GETs ``inbox_url`` and runs ``parse_inbox``.
      - ``fetch_batch`` GETs ``message_url`` and runs ``parse_message``.

    The two stages share the authenticated ``http_session`` passed in
    ``start`` so cookie rotation is preserved end-to-end.
    """

    def __init__(
        self,
        inbox_url: str,
        parse_inbox: _ParseInbox,
        parse_message: _ParseMessage,
    ) -> None:
        self._inbox_url = inbox_url
        self._parse_inbox = parse_inbox
        self._parse_message = parse_message
        self._cfg: Config | None = None
        self._http: requests.Session | None = None
        # state_key → InboxEntry for fetch_batch lookup. Populated in
        # list_state_keys; cleared in close.
        self._entries: dict[str, InboxEntry] = {}
        # Sentinel state_key → human-readable anomaly text. fetch_batch
        # returns these as parse-failed batches so the orchestrator's
        # parse_failure pipeline (Slack notify, count) picks them up.
        self._anomaly_keys: dict[str, str] = {}

    # --- lifecycle ----------------------------------------------------------------

    def start(self, cfg: Config, http_session: requests.Session | None = None) -> None:
        # http_session may be None when the orchestrator hasn't built
        # a Clicker (no cookies registered yet). We accept that here and
        # fail at GET-time with a clearer message — that way ``--site
        # pointtown`` with COOKIES unset still proceeds far enough to
        # show a useful error in Slack rather than crashing inside
        # start().
        self._cfg = cfg
        self._http = http_session
        self._entries = {}
        self._anomaly_keys = {}

    def close(self) -> None:
        self._cfg = None
        self._http = None
        self._entries = {}
        self._anomaly_keys = {}

    # --- batch enumeration --------------------------------------------------------

    def list_state_keys(self) -> list[str]:
        cfg = self._require_cfg()
        if self._http is None:
            # No authenticated session — surface as a single parse-failed
            # batch so the operator gets a clear "register cookies" notice
            # instead of a silent zero-message run.
            return [
                self._record_anomaly(
                    f"OnsiteInboxSource needs authenticated cookies. "
                    f"Set {cfg.adapter.cookies_env} (Cookie-Editor JSON export from logged-in browser)."
                )
            ]
        try:
            resp = self._http.get(self._inbox_url, timeout=(10.0, 30.0), allow_redirects=True)
        except Exception as exc:
            logger.warning("inbox GET failed: %s", exc)
            return [self._record_anomaly(f"inbox GET failed: {type(exc).__name__}: {exc}")]
        if resp.status_code >= 400:
            return [self._record_anomaly(f"inbox GET status={resp.status_code}")]
        entries, anomalies = self._parse_inbox(resp.text)
        # De-dup by state_key in case the listing has duplicate rows; keep
        # first occurrence (oldest-first convention).
        deduped: dict[str, InboxEntry] = {}
        for entry in entries:
            deduped.setdefault(entry.state_key, entry)
        self._entries = deduped
        # Apply the per-run message cap before exposing keys to the
        # orchestrator. Without this, an inbox with thousands of entries
        # would all be fetched/clicked in one run — both slow and
        # bot-like.
        keys = list(deduped)[: cfg.max_messages]
        # Append anomaly sentinels AFTER the real keys so legitimate work
        # gets done first; the cap above only applies to real keys, so
        # listing anomalies are always surfaced regardless of cap.
        for anomaly_text in anomalies:
            keys.append(self._record_anomaly(anomaly_text))
        return keys

    def fetch_batch(self, state_key: str) -> ClickBatch:
        if state_key in self._anomaly_keys:
            return ClickBatch(
                state_key=state_key,
                label=self._anomaly_keys[state_key],
                parse_failed=True,
            )
        if self._http is None:
            # Defensive — list_state_keys should have produced an anomaly
            # sentinel before we ever got here, but fail safely if not.
            return ClickBatch(state_key=state_key, label="(no http session)", parse_failed=True)
        entry = self._entries.get(state_key)
        if entry is None:
            return ClickBatch(state_key=state_key, label="(unknown inbox entry)", parse_failed=True)
        try:
            resp = self._http.get(entry.message_url, timeout=(10.0, 30.0), allow_redirects=True)
        except Exception as exc:
            logger.warning("message GET failed for %s: %s", state_key, exc)
            return ClickBatch(state_key=state_key, label=entry.label, parse_failed=True)
        if resp.status_code >= 400:
            return ClickBatch(state_key=state_key, label=entry.label, parse_failed=True)
        body = resp.text
        # Onsite messages are HTML pages, not multipart MIME — pass
        # is_html=True to mirror GmailSource's HTML branch contract.
        candidates, anomalies = self._parse_message(body, True)
        return ClickBatch(
            state_key=state_key,
            label=entry.label,
            candidates=candidates,
            anomalies=anomalies,
        )

    # --- side effects -------------------------------------------------------------

    def mark_complete(self, batch: ClickBatch) -> None:
        # No mark-as-read API exposed publicly. Once the click URL is
        # hit and StateStore records the message as complete, the next
        # ``list_state_keys`` will still surface it (because the inbox
        # itself doesn't know we credited), but the orchestrator's
        # state.is_message_complete check filters it out before we
        # bother fetching the message body again.
        del batch

    def mark_no_credit(self, batch: ClickBatch) -> None:
        # Same logic as mark_complete: nothing to update server-side.
        del batch

    # --- internal -----------------------------------------------------------------

    def _require_cfg(self) -> Config:
        if self._cfg is None:
            raise RuntimeError("OnsiteInboxSource used before start() — orchestrator bug")
        return self._cfg

    def _record_anomaly(self, text: str) -> str:
        """Reserve a sentinel state_key carrying the anomaly text.

        ``fetch_batch`` recognises the prefix and returns a parse-failed
        batch. Indexing by length keeps each anomaly distinct so multiple
        listing-level anomalies in one run all surface separately.
        """
        sentinel = f"{_ANOMALY_KEY_PREFIX}{len(self._anomaly_keys)}__"
        self._anomaly_keys[sentinel] = text
        logger.warning("inbox listing anomaly: %s", text)
        return sentinel
