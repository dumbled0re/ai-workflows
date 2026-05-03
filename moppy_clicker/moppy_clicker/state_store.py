"""URL-level state tracking to prevent duplicate clicks across runs.

State is keyed by ``message_id`` → ``url_hash``. Only redacted URLs are stored
on disk. Atomic writes (tmp + rename) ensure crashes never produce a partial
state file.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .models import ClickResult, MessageState, StateFile, UrlState
from .redaction import redact_url

_PRUNE_DAYS_DEFAULT = 30


def _hash_url(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._state = self._load()

    def _load(self) -> StateFile:
        if not self.path.exists():
            return StateFile()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return StateFile.model_validate(data)
        except (json.JSONDecodeError, ValueError):
            backup = self.path.with_suffix(self.path.suffix + ".corrupt")
            self.path.rename(backup)
            return StateFile()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix=self.path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp.write(self._state.model_dump_json(indent=2))
            tmp_path = tmp.name
        os.replace(tmp_path, self.path)

    def is_url_done(self, message_id: str, url: str, max_attempts: int) -> bool:
        msg = self._state.messages.get(message_id)
        if msg is None:
            return False
        url_state = msg.urls.get(_hash_url(url))
        if url_state is None:
            return False
        if url_state.status == "success":
            return True
        return msg.attempt_count >= max_attempts

    def is_message_complete(self, message_id: str, max_attempts: int) -> bool:
        msg = self._state.messages.get(message_id)
        if msg is None or not msg.urls:
            return False
        if msg.attempt_count >= max_attempts:
            return True
        return all(u.status == "success" for u in msg.urls.values())

    def record_attempt(self, message_id: str, result: ClickResult) -> None:
        url_str = str(result.candidate.url)
        url_hash = _hash_url(url_str)
        now = datetime.now(UTC)
        msg = self._state.messages.get(message_id)
        if msg is None:
            msg = MessageState(first_seen=now, last_attempt=now, attempt_count=0)
            self._state.messages[message_id] = msg
        msg.last_attempt = now
        msg.urls[url_hash] = UrlState(
            redacted_url=redact_url(url_str),
            status=result.final_status,
            last_status_at=result.timestamp,
            http_status=result.http_status,
        )

    def increment_attempt(self, message_id: str) -> None:
        msg = self._state.messages.get(message_id)
        if msg is None:
            now = datetime.now(UTC)
            self._state.messages[message_id] = MessageState(first_seen=now, last_attempt=now, attempt_count=1)
            return
        msg.attempt_count += 1
        msg.last_attempt = datetime.now(UTC)

    def attempt_count(self, message_id: str) -> int:
        msg = self._state.messages.get(message_id)
        return msg.attempt_count if msg else 0

    def prune_old(self, days: int = _PRUNE_DAYS_DEFAULT) -> int:
        cutoff = datetime.now(UTC).timestamp() - days * 86400
        to_remove = [mid for mid, m in self._state.messages.items() if m.last_attempt.timestamp() < cutoff]
        for mid in to_remove:
            del self._state.messages[mid]
        return len(to_remove)
