"""``GmailSource`` — Gmail-API-driven click-email source.

Wraps the ``GmailClient`` behind the ``ClickUrlSource`` protocol so the
orchestrator stays oblivious to email-specific details (subject
decoding, body MIME walking, dedup state).

After the 2026-05-17 IMAP retirement, the OAuth scope is ``gmail.readonly``,
so dedup of already-clicked messages is tracked in a local JSON file
(``processed_messages.json`` per site) rather than via server-side
``X-GM-LABELS``. The file is cached as part of ``point_sites/data/`` in
GitHub Actions so dedup state survives between cron runs.

A ``GmailSource`` is held by an ``Adapter`` and only carries static
config (the per-site ``parse_email`` callable). Live state — the API
client, the active ``Config``, the dedup set — is set in ``start()`` and
dropped in ``close()``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..gmail_client import GmailClient, GmailParseError
from .base import ClickBatch

if TYPE_CHECKING:
    import requests

    from ...config import Config
    from ..models import ClickCandidate


logger = logging.getLogger(__name__)

_ParseEmail = Callable[[str, bool], tuple[list["ClickCandidate"], list[str]]]

# Bound the dedup file to roughly a couple of months of click-mail
# volume across all adapters; old entries get FIFO-evicted.
_MAX_DEDUP_ENTRIES = 2000


class _DedupState:
    """File-backed FIFO set of already-processed Gmail message IDs."""

    def __init__(self, path: str, max_entries: int = _MAX_DEDUP_ENTRIES) -> None:
        self._path = Path(path)
        self._max = max_entries
        self._ids: list[str] = []
        self._set: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = self._path.read_text()
            data = json.loads(raw) if raw.strip() else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("processed_messages.json corrupt at %s, starting fresh: %s", self._path, exc)
            return
        ids = data.get("ids") if isinstance(data, dict) else None
        if not isinstance(ids, list):
            return
        self._ids = [str(x) for x in ids[-self._max :]]
        self._set = set(self._ids)

    def contains(self, msg_id: str) -> bool:
        return msg_id in self._set

    def add(self, msg_id: str) -> None:
        if msg_id in self._set:
            return
        self._ids.append(msg_id)
        self._set.add(msg_id)
        if len(self._ids) > self._max:
            drop_count = len(self._ids) - self._max
            for old in self._ids[:drop_count]:
                self._set.discard(old)
            self._ids = self._ids[drop_count:]
        self._save()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"ids": self._ids}, ensure_ascii=False, indent=2))


class GmailSource:
    """ClickUrlSource that reads click candidates from Gmail.

    The state_key for each batch is the Gmail message ID. ``mark_complete``
    and ``mark_no_credit`` both record the message ID in a local dedup
    file so subsequent cron runs skip it (server-side label/read writes
    are unavailable under the readonly OAuth scope).
    """

    def __init__(self, parse_email: _ParseEmail) -> None:
        self._parse_email = parse_email
        self._gmail: GmailClient | None = None
        self._cfg: Config | None = None
        self._dedup: _DedupState | None = None

    # --- lifecycle ----------------------------------------------------------------

    def start(self, cfg: Config, http_session: requests.Session | None = None) -> None:
        """Open the Gmail API client. ``http_session`` is unused for this source."""
        del http_session  # explicitly ignored — Gmail API uses its own transport
        self._cfg = cfg
        # GmailAuthError propagates to the orchestrator, which has the
        # context to send a Slack auth-error notification.
        self._gmail = GmailClient(
            cfg.gmail_client_id,
            cfg.gmail_client_secret,
            cfg.gmail_refresh_token,
        )
        self._dedup = _DedupState(cfg.processed_messages_path)

    def close(self) -> None:
        if self._gmail is not None:
            self._gmail.close()
        self._gmail = None
        self._cfg = None
        self._dedup = None

    # --- batch enumeration --------------------------------------------------------

    def list_state_keys(self) -> list[str]:
        gmail, cfg, dedup = self._require_started()
        # ``max_results`` is intentionally applied before dedup: if the
        # search window has more candidates than ``max_messages``, we
        # accept that some may get skipped this run rather than walking
        # an unbounded page set.
        ids = gmail.search_messages(cfg.gmail_query, max_results=cfg.max_messages)
        filtered = [i for i in ids if not dedup.contains(i)]
        if len(filtered) != len(ids):
            logger.info(
                "gmail dedup: skipping %d/%d messages already processed locally",
                len(ids) - len(filtered),
                len(ids),
            )
        return filtered

    def fetch_batch(self, state_key: str) -> ClickBatch:
        gmail, _, _ = self._require_started()
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
        return ClickBatch(
            state_key=state_key,
            label=parsed.subject,
            candidates=candidates,
            anomalies=anomalies,
        )

    # --- side effects -------------------------------------------------------------

    def mark_complete(self, batch: ClickBatch) -> None:
        gmail, cfg, dedup = self._require_started()
        # Server-side writes are no-ops under readonly scope; calls retained
        # so a future scope upgrade can re-activate them without refactoring.
        gmail.mark_as_read(batch.state_key)
        if cfg.clicked_label:
            gmail.add_label(batch.state_key, cfg.clicked_label)
        dedup.add(batch.state_key)

    def mark_no_credit(self, batch: ClickBatch) -> None:
        gmail, cfg, dedup = self._require_started()
        if cfg.no_coins_label:
            gmail.add_label(batch.state_key, cfg.no_coins_label)
        # No point re-fetching a known no-credit mail tomorrow.
        dedup.add(batch.state_key)

    # --- internal -----------------------------------------------------------------

    def _require_started(self) -> tuple[GmailClient, Config, _DedupState]:
        if self._gmail is None or self._cfg is None or self._dedup is None:
            raise RuntimeError("GmailSource used before start() — orchestrator bug")
        return self._gmail, self._cfg, self._dedup
