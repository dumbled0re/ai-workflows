"""``GmailSource`` — IMAP-driven click-email source.

Wraps the existing ``GmailClient`` behind the ``ClickUrlSource`` protocol
so the orchestrator stays oblivious to email-specific details (subject
decoding, body MIME walking, ``X-GM-LABELS`` semantics).

A ``GmailSource`` is held by an ``Adapter`` and only carries static
config (the per-site ``parse_email`` callable). Live state — the IMAP
connection, the active ``Config`` — is set in ``start()`` and dropped
in ``close()``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ..gmail_client import GmailClient, GmailParseError
from .base import ClickBatch

if TYPE_CHECKING:
    import requests

    from ...config import Config
    from ..models import ClickCandidate


_ParseEmail = Callable[[str, bool], tuple[list["ClickCandidate"], list[str]]]


class GmailSource:
    """ClickUrlSource that reads click candidates from Gmail.

    The state_key for each batch is the Gmail ``msg_id``. ``mark_complete``
    adds the adapter's clicked-label and marks the message as read so
    subsequent runs skip it. ``mark_no_credit`` adds the no-coins label so
    purely-informational mails (newsletters etc) aren't repeatedly fetched.
    """

    def __init__(self, parse_email: _ParseEmail) -> None:
        self._parse_email = parse_email
        self._gmail: GmailClient | None = None
        self._cfg: Config | None = None

    # --- lifecycle ----------------------------------------------------------------

    def start(self, cfg: Config, http_session: requests.Session | None = None) -> None:
        """Open the IMAP connection. ``http_session`` is unused for this source."""
        del http_session  # explicitly ignored — Gmail is IMAP, not HTTP
        self._cfg = cfg
        # GmailAuthError propagates to the orchestrator, which has the
        # context to send a Slack auth-error notification.
        self._gmail = GmailClient(cfg.gmail_user, cfg.gmail_app_password)

    def close(self) -> None:
        if self._gmail is not None:
            self._gmail.close()
        self._gmail = None
        self._cfg = None

    # --- batch enumeration --------------------------------------------------------

    def list_state_keys(self) -> list[str]:
        gmail, cfg = self._require_started()
        return gmail.search_messages(cfg.gmail_query, max_results=cfg.max_messages)

    def fetch_batch(self, state_key: str) -> ClickBatch:
        gmail, _ = self._require_started()
        try:
            parsed = gmail.get_message(state_key)
        except GmailParseError:
            return ClickBatch(state_key=state_key, label="(get_message failed)", parse_failed=True)
        if not parsed.has_body:
            return ClickBatch(state_key=state_key, label=parsed.subject, parse_failed=True)
        if parsed.plaintext_body:
            body, is_html = parsed.plaintext_body, False
        else:
            assert parsed.html_body is not None  # has_body guarantees one or the other
            body, is_html = parsed.html_body, True
        candidates, anomalies = self._parse_email(body, is_html)
        if anomalies:
            import logging as _l

            _l.getLogger(__name__).warning(
                "DEBUG anomalous body uid=%s subj=%r is_html=%s head=%r",
                state_key,
                parsed.subject,
                is_html,
                body[:1500],
            )
        return ClickBatch(
            state_key=state_key,
            label=parsed.subject,
            candidates=candidates,
            anomalies=anomalies,
        )

    # --- side effects -------------------------------------------------------------

    def mark_complete(self, batch: ClickBatch) -> None:
        gmail, cfg = self._require_started()
        gmail.mark_as_read(batch.state_key)
        if cfg.clicked_label:
            gmail.add_label(batch.state_key, cfg.clicked_label)

    def mark_no_credit(self, batch: ClickBatch) -> None:
        gmail, cfg = self._require_started()
        if cfg.no_coins_label:
            gmail.add_label(batch.state_key, cfg.no_coins_label)

    # --- internal -----------------------------------------------------------------

    def _require_started(self) -> tuple[GmailClient, Config]:
        if self._gmail is None or self._cfg is None:
            raise RuntimeError("GmailSource used before start() — orchestrator bug")
        return self._gmail, self._cfg
