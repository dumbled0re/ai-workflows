"""Gmail API wrapper.

OAuth flow:
  - Local first run: ``credentials.json`` (OAuth client) → InstalledAppFlow → ``token.json``
  - GitHub Actions: ``GMAIL_TOKEN_JSON`` env var → loaded each run, refreshed in-place

The refresh-token-only secret is stored in GitHub Secrets. Token files written
to disk get mode 0600 via ``umask 077`` in the workflow.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, cast

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .models import ParsedMessage

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


class GmailAuthError(RuntimeError):
    pass


class GmailParseError(RuntimeError):
    pass


def _load_credentials(
    token_path: str,
    credentials_path: str,
    token_json_env: str | None,
) -> Credentials:
    creds: Credentials | None = None

    if token_json_env:
        try:
            creds = Credentials.from_authorized_user_info(  # type: ignore[no-untyped-call]
                json.loads(token_json_env), SCOPES
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise GmailAuthError(f"GMAIL_TOKEN_JSON malformed: {exc}") from exc
    elif Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)  # type: ignore[no-untyped-call]

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())  # type: ignore[no-untyped-call]
            return creds
        except RefreshError as exc:
            raise GmailAuthError(f"refresh failed (re-auth required): {exc}") from exc

    if not Path(credentials_path).exists():
        raise GmailAuthError(
            f"no usable credentials: token_json={'set' if token_json_env else 'unset'}, "
            f"token_path={token_path} (exists={Path(token_path).exists()}), "
            f"credentials_path={credentials_path} (exists=False)"
        )

    flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
    creds = flow.run_local_server(port=0)

    Path(token_path).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(creds.to_json())
    except Exception:
        os.close(fd)
        raise
    return creds


def _decode_part(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8", errors="replace")


def _extract_part(payload: dict[str, Any], mime_type: str) -> str | None:
    if payload.get("mimeType") == mime_type:
        body = payload.get("body", {})
        data = body.get("data")
        if data:
            return _decode_part(data)
    for part in payload.get("parts", []) or []:
        found = _extract_part(part, mime_type)
        if found:
            return found
    return None


def _extract_html(payload: dict[str, Any]) -> str | None:
    return _extract_part(payload, "text/html")


def _extract_plaintext(payload: dict[str, Any]) -> str | None:
    return _extract_part(payload, "text/plain")


def _header(payload: dict[str, Any], name: str) -> str:
    for h in payload.get("headers", []) or []:
        if h.get("name", "").lower() == name.lower():
            return cast(str, h.get("value", ""))
    return ""


class GmailClient:
    def __init__(
        self,
        token_path: str,
        credentials_path: str,
        token_json_env: str | None = None,
    ) -> None:
        creds = _load_credentials(token_path, credentials_path, token_json_env)
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._label_cache: dict[str, str] = {}

    def search_messages(self, query: str, max_results: int = 50) -> list[str]:
        ids: list[str] = []
        page_token: str | None = None
        while len(ids) < max_results:
            req = (
                self._service.users()
                .messages()
                .list(
                    userId="me",
                    q=query,
                    pageToken=page_token,
                    maxResults=min(100, max_results - len(ids)),
                )
            )
            try:
                resp = req.execute()
            except HttpError as exc:
                raise GmailAuthError(f"messages.list failed: {exc}") from exc
            for m in resp.get("messages", []) or []:
                ids.append(m["id"])
                if len(ids) >= max_results:
                    break
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return ids

    def get_message(self, msg_id: str) -> ParsedMessage:
        try:
            msg = (
                self._service.users()
                .messages()
                .get(userId="me", id=msg_id, format="full")
                .execute()
            )
        except HttpError as exc:
            raise GmailParseError(f"messages.get failed for {msg_id}: {exc}") from exc

        payload = msg.get("payload", {})
        subject = _header(payload, "Subject")
        sender = _header(payload, "From")
        date_str = _header(payload, "Date")
        received_at: datetime | None = None
        if date_str:
            try:
                received_at = parsedate_to_datetime(date_str)
            except (TypeError, ValueError):
                received_at = None

        try:
            html = _extract_html(payload)
            plaintext = _extract_plaintext(payload)
        except (UnicodeDecodeError, ValueError) as exc:
            raise GmailParseError(f"body decode failed for {msg_id}: {exc}") from exc

        return ParsedMessage(
            message_id=msg_id,
            subject=subject,
            sender=sender,
            html_body=html,
            plaintext_body=plaintext,
            received_at=received_at or datetime.now(UTC),
        )

    def mark_as_read(self, msg_id: str) -> None:
        try:
            self._service.users().messages().modify(
                userId="me",
                id=msg_id,
                body={"removeLabelIds": ["UNREAD"]},
            ).execute()
        except HttpError as exc:
            logger.warning("mark_as_read failed for %s: %s", msg_id, exc)

    def add_label(self, msg_id: str, label_name: str) -> None:
        label_id = self._ensure_label(label_name)
        if label_id is None:
            return
        try:
            self._service.users().messages().modify(
                userId="me",
                id=msg_id,
                body={"addLabelIds": [label_id]},
            ).execute()
        except HttpError as exc:
            logger.warning("add_label %s failed for %s: %s", label_name, msg_id, exc)

    def _ensure_label(self, name: str) -> str | None:
        if name in self._label_cache:
            return self._label_cache[name]
        try:
            resp = self._service.users().labels().list(userId="me").execute()
            for lbl in resp.get("labels", []) or []:
                if lbl.get("name") == name:
                    label_id = cast(str, lbl["id"])
                    self._label_cache[name] = label_id
                    return label_id
            created = (
                self._service.users()
                .labels()
                .create(
                    userId="me",
                    body={
                        "name": name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
                .execute()
            )
            label_id = cast(str, created["id"])
            self._label_cache[name] = label_id
            return label_id
        except HttpError as exc:
            logger.warning("ensure_label %s failed: %s", name, exc)
            return None
