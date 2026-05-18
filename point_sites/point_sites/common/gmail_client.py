"""Gmail API client (OAuth2 + readonly scope).

Replaces the prior IMAP-based client after the IMAP-authenticated Gmail
account was suspended by Google bot detection (2026-05-17). Uses the
Gmail HTTP API with a stored ``refresh_token`` so GitHub Actions can
obtain a short-lived ``access_token`` at run time without user
interaction.

Auth requires three GitHub Secrets, obtained via
``scripts/get_refresh_token.py``:

  - GMAIL_CLIENT_ID      — OAuth2 Desktop client ID
  - GMAIL_CLIENT_SECRET  — OAuth2 client secret
  - GMAIL_REFRESH_TOKEN  — long-lived refresh token

Scope is ``gmail.readonly`` — server-side flag/label writes
(``mark_as_read`` / ``add_label``) are no-ops to keep the OAuth
permission surface minimal. Dedup of already-clicked messages is handled
locally in ``GmailSource`` via ``processed_messages.json``.
"""

from __future__ import annotations

import base64
import email.header
import email.message
import logging
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .models import ParsedMessage

logger = logging.getLogger(__name__)

GMAIL_TOKEN_URI = "https://oauth2.googleapis.com/token"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailAuthError(RuntimeError):
    pass


class GmailParseError(RuntimeError):
    pass


def _decode_header(value: str) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    out: list[str] = []
    for fragment, charset in parts:
        if isinstance(fragment, bytes):
            out.append(fragment.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(fragment)
    return "".join(out)


def _decode_payload(part: email.message.Message) -> str:
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    if not isinstance(payload, bytes):
        return str(payload)
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _extract_bodies(msg: email.message.Message) -> tuple[str | None, str | None]:
    plain: str | None = None
    html: str | None = None
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain" and plain is None:
                plain = _decode_payload(part)
            elif ctype == "text/html" and html is None:
                html = _decode_payload(part)
    else:
        ctype = msg.get_content_type()
        payload = _decode_payload(msg)
        if ctype == "text/plain":
            plain = payload
        elif ctype == "text/html":
            html = payload
    return plain, html


class GmailClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> None:
        if not client_id or not client_secret or not refresh_token:
            raise GmailAuthError("GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN are all required")
        creds = Credentials(  # type: ignore[no-untyped-call]
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri=GMAIL_TOKEN_URI,
            scopes=GMAIL_SCOPES,
        )
        try:
            creds.refresh(Request())  # type: ignore[no-untyped-call]
        except Exception as exc:
            raise GmailAuthError(
                "refresh_token rejected — re-run scripts/get_refresh_token.py if "
                f"the OAuth client was revoked or the token expired: {exc}"
            ) from exc
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    def close(self) -> None:
        # google-api-python-client uses short-lived HTTP transports per call;
        # nothing to close. Kept for backward compatibility with the prior
        # IMAP-based client's context-manager interface.
        pass

    def __enter__(self) -> GmailClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def search_messages(self, query: str, max_results: int = 50) -> list[str]:
        """Return Gmail message IDs matching ``query``.

        The ``q`` parameter accepts Gmail-web-UI search syntax
        (``from:foo newer_than:5d``), so adapter-level queries remain
        unchanged from the prior IMAP X-GM-RAW path.
        """
        try:
            resp = self._service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        except HttpError as exc:
            raise GmailAuthError(f"Gmail API messages.list failed: {exc}") from exc
        msgs = resp.get("messages") or []
        ids: list[str] = [m["id"] for m in msgs]
        return ids[:max_results]

    def get_message(self, msg_id: str) -> ParsedMessage:
        """Fetch the full RFC822 message for ``msg_id`` and parse it."""
        try:
            resp = self._service.users().messages().get(userId="me", id=msg_id, format="raw").execute()
        except HttpError as exc:
            raise GmailParseError(f"Gmail API messages.get failed for id={msg_id}: {exc}") from exc
        raw_b64 = resp.get("raw")
        if not raw_b64:
            raise GmailParseError(f"empty raw body for id={msg_id}")
        raw = base64.urlsafe_b64decode(raw_b64)
        msg = email.message_from_bytes(raw)
        subject = _decode_header(msg.get("Subject", ""))
        sender = _decode_header(msg.get("From", ""))
        date_str = msg.get("Date", "")
        received_at: datetime | None = None
        if date_str:
            try:
                received_at = parsedate_to_datetime(date_str)
            except (TypeError, ValueError):
                received_at = None
        plain, html = _extract_bodies(msg)
        return ParsedMessage(
            message_id=msg_id,
            subject=subject,
            sender=sender,
            html_body=html,
            plaintext_body=plain,
            received_at=received_at or datetime.now(UTC),
        )

    def mark_as_read(self, msg_id: str) -> None:
        """No-op under the readonly OAuth scope.

        Dedup of already-processed messages is handled locally in
        ``GmailSource`` rather than via server-side ``\\Seen`` flags.
        """
        del msg_id

    def add_label(self, msg_id: str, label_name: str) -> None:
        """No-op under the readonly OAuth scope. See ``mark_as_read``."""
        del msg_id, label_name
