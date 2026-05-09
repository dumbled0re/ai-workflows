"""``ClickUrlSource`` Protocol + ``ClickBatch`` shared by all source impls.

The orchestrator (``cmd_run``) only depends on this Protocol. Each adapter
points its ``source`` field at a concrete implementation under this
package — see ``GmailSource`` for the current production case and the
module docstring for ``OnsiteInboxSource`` / ``EndpointPollSource`` plans.

Lifecycle a source must support::

    source.start(cfg, http_session=...)
    keys = source.list_state_keys()
    for key in keys:
        batch = source.fetch_batch(key)
        # … click / record / notify …
        source.mark_complete(batch)   # or mark_no_credit
    source.close()

Sources MAY hold state across ``start`` / ``close`` (live IMAP handles,
per-run caches) but MUST drop everything in ``close`` so the next
``cmd_run`` invocation starts clean.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import requests

    from ...config import Config
    from ..models import ClickCandidate


@dataclass
class ClickBatch:
    """One unit the orchestrator processes — analogous to a Gmail message.

    The semantics are deliberately loose so the same shape covers Gmail
    messages, on-site inbox entries, and one-shot endpoint polls:

    - ``state_key``: stable identifier the orchestrator hands to
      ``StateStore`` for de-dup. Gmail = msg_id, inbox = inbox URL,
      endpoint poll = ``YYYY-MM-DD``.
    - ``label``: human-readable name for logs / Slack (subject for
      Gmail, page title for inbox, ``"daily bonus"`` for endpoint).
    - ``parse_failed``: source could not extract a body / structure.
      Counted as a parse_failure_id by the orchestrator.
    - ``anomalies``: structural-anomaly markers (template change?)
      that should NOT be treated as a hard failure but should be
      surfaced separately to the user.
    - ``candidates``: zero or more click candidates extracted from the
      batch. Empty list + no anomalies / failures → ``mark_no_credit``.
    """

    state_key: str
    label: str
    candidates: list[ClickCandidate] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    parse_failed: bool = False


@runtime_checkable
class ClickUrlSource(Protocol):
    """Per-site URL source (Gmail / inbox / endpoint poll).

    Stateful per-run: ``start()`` initialises live connections, ``close()``
    tears them down. The orchestrator owns the lifecycle.
    """

    def start(self, cfg: Config, http_session: requests.Session | None = None) -> None:
        """Open whatever resources the source needs (IMAP, HTTP)."""
        ...

    def list_state_keys(self) -> list[str]:
        """Cheap enumeration of batch identifiers for state.is_message_complete checks.

        Returning ``[]`` is a valid no-op (e.g. Gmail returned no matching
        messages, daily endpoint already crawled today).
        """
        ...

    def fetch_batch(self, state_key: str) -> ClickBatch:
        """Resolve one ``state_key`` to a ``ClickBatch``.

        May perform an expensive call (Gmail get_message, HTTP GET).
        Should NOT raise on per-batch parse failures — set
        ``parse_failed=True`` on the returned batch instead so the
        orchestrator can record and continue.
        """
        ...

    def mark_complete(self, batch: ClickBatch) -> None:
        """Called after every candidate in ``batch`` clicked successfully.

        Side effects vary: Gmail labels + marks read, inbox marks read,
        endpoint poll is a no-op.
        """
        ...

    def mark_no_credit(self, batch: ClickBatch) -> None:
        """Called when ``batch`` parsed cleanly but had no click candidates.

        Lets sources mark "this entry has nothing for us" so future runs
        skip it (e.g. Gmail no_coins_label). Default is no-op.
        """
        ...

    def close(self) -> None:
        """Tear down everything ``start()`` created. Must be idempotent."""
        ...
