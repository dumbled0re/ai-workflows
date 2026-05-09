"""ClickUrlSource implementations.

A *source* abstracts where click candidates come from. Per ``cmd_run``'s
contract, the orchestrator iterates over batches the source returns and
applies the same click/state/notify pipeline regardless of origin.

Three implementations are envisaged (Gmail is the only one used today):

- ``GmailSource``      — IMAP-fetched click emails (Moppy, ポイントインカム etc).
- ``OnsiteInboxSource``— scraped on-site message inbox (e.g. ポイントタウン).
- ``EndpointPollSource``— a fixed daily-bonus URL (e.g. アメフリ).

Adapters carry a ``source`` field that points to one of these. The source
holds per-run live state (Gmail handle, http session) created in
``start()`` and torn down in ``close()``.
"""

from .base import ClickBatch, ClickUrlSource
from .gmail import GmailSource

__all__ = ["ClickBatch", "ClickUrlSource", "GmailSource"]
