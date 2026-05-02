"""Gmail IMAP client.

Authenticates via Gmail App Password (requires 2FA on the account):
  - GMAIL_USER: full Gmail address
  - GMAIL_APP_PASSWORD: 16-char password from
    https://myaccount.google.com/apppasswords (spaces are stripped)

Uses the X-GM-RAW IMAP search extension so the same Gmail web-UI search
syntax (e.g. ``from:moppy.jp -label:moppy-clicked newer_than:5d``) works
unchanged. Labels are added via the X-GM-LABELS extension.
"""

from __future__ import annotations

import contextlib
import email.header
import email.message
import imaplib
import logging
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType

from .models import ParsedMessage

logger = logging.getLogger(__name__)

GMAIL_IMAP_HOST = "imap.gmail.com"
GMAIL_IMAP_PORT = 993
ALL_MAIL_FOLDER = '"[Gmail]/All Mail"'  # quoted because of space


class GmailAuthError(RuntimeError):
    pass


class GmailParseError(RuntimeError):
    pass


def _quote_imap(s: str) -> str:
    """Return ``s`` quoted for IMAP commands."""
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


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
        user: str,
        app_password: str,
        *,
        host: str = GMAIL_IMAP_HOST,
        port: int = GMAIL_IMAP_PORT,
    ) -> None:
        if not user or "@" not in user:
            raise GmailAuthError(f"GMAIL_USER invalid: {user!r}")
        password = app_password.replace(" ", "")
        if len(password) != 16:
            raise GmailAuthError(
                f"GMAIL_APP_PASSWORD has unexpected length {len(password)}; "
                "Google app passwords are exactly 16 characters"
            )
        try:
            self._conn = imaplib.IMAP4_SSL(host, port)
        except OSError as exc:
            raise GmailAuthError(f"IMAP connect to {host}:{port} failed: {exc}") from exc
        try:
            self._conn.login(user, password)
        except imaplib.IMAP4.error as exc:
            raise GmailAuthError(
                f"IMAP login rejected (regenerate app password if 2FA recently changed): {exc}"
            ) from exc
        typ, _ = self._conn.select(ALL_MAIL_FOLDER, readonly=False)
        if typ != "OK":
            raise GmailAuthError(f"could not select {ALL_MAIL_FOLDER}")

    def close(self) -> None:
        with contextlib.suppress(imaplib.IMAP4.error):
            self._conn.close()
        with contextlib.suppress(imaplib.IMAP4.error):
            self._conn.logout()

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
        """Return UIDs matching the Gmail-style search query (X-GM-RAW)."""
        try:
            typ, data = self._conn.uid("SEARCH", "X-GM-RAW", _quote_imap(query))
        except imaplib.IMAP4.error as exc:
            raise GmailAuthError(f"IMAP search failed: {exc}") from exc
        if typ != "OK":
            raise GmailAuthError(f"IMAP search non-OK: typ={typ} data={data!r}")
        if not data or not data[0]:
            return []
        first = data[0]
        if not isinstance(first, bytes):
            return []
        uids: list[str] = first.decode("ascii").split()
        return uids[:max_results]

    def get_message(self, uid: str) -> ParsedMessage:
        try:
            typ, data = self._conn.uid("FETCH", uid, "(BODY.PEEK[])")
        except imaplib.IMAP4.error as exc:
            raise GmailParseError(f"FETCH failed for uid={uid}: {exc}") from exc
        if typ != "OK" or not data:
            raise GmailParseError(f"FETCH non-OK for uid={uid}: typ={typ} data={data!r}")
        raw: bytes | None = None
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                raw = item[1]
                break
        if not raw:
            raise GmailParseError(f"empty body for uid={uid}")
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
            message_id=uid,
            subject=subject,
            sender=sender,
            html_body=html,
            plaintext_body=plain,
            received_at=received_at or datetime.now(UTC),
        )

    def mark_as_read(self, uid: str) -> None:
        try:
            self._conn.uid("STORE", uid, "+FLAGS", r"(\Seen)")
        except imaplib.IMAP4.error as exc:
            logger.warning("mark_as_read failed for %s: %s", uid, exc)

    def add_label(self, uid: str, label_name: str) -> None:
        quoted = _quote_imap(label_name)
        try:
            self._conn.uid("STORE", uid, "+X-GM-LABELS", f"({quoted})")
        except imaplib.IMAP4.error as exc:
            logger.warning("add_label %s failed for %s: %s", label_name, uid, exc)
