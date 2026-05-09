"""``EndpointPollSource`` — daily single-URL poll source.

For point sites that credit the user by hitting a fixed endpoint once
per day (e.g. アメフリ ログインボーナス). No Gmail, no inbox scrape —
the source emits one ``ClickCandidate`` per JST day pointing at the
configured URL, and ``cmd_run``'s normal click + balance-delta pipeline
verifies whether the credit landed.

Re-running the same day after a successful click is a no-op:
``state_key`` is the JST date, so ``StateStore.is_message_complete``
filters the second invocation. Manual ``--site amefuri`` re-runs after
a hiccup are safe (they retry the same endpoint up to ``max_attempts``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ..models import ClickCandidate
from .base import ClickBatch

if TYPE_CHECKING:
    import requests

    from ...config import Config


# Japanese point sites reset their daily bonuses at midnight JST. Computing
# state_key in JST keeps the source's idea of "today" aligned with the
# site's idea of "today" regardless of where the GHA runner is.
_JST = timezone(timedelta(hours=9))


class EndpointPollSource:
    """ClickUrlSource that emits one click candidate per JST day.

    ``endpoint_url`` is hit via the standard ``Clicker`` so the live
    cookie jar (incl. session rotation) is reused.

    Credit verification is **balance-delta based** (per codex consult
    2026-05-09): an HTTP 200 from the endpoint proves nothing about
    whether the bonus actually credited. The orchestrator's pre/post
    balance scrape is the source of truth.
    """

    def __init__(
        self,
        endpoint_url: str,
        label_prefix: str = "ログインボーナス",
    ) -> None:
        self._endpoint_url = endpoint_url
        self._label_prefix = label_prefix
        self._cfg: Config | None = None

    # --- lifecycle ----------------------------------------------------------------

    def start(self, cfg: Config, http_session: requests.Session | None = None) -> None:
        # The actual GET happens inside ``Clicker.click()``, which already
        # holds the authenticated session. We just need cfg for any future
        # env-driven knobs and to satisfy the protocol.
        del http_session
        self._cfg = cfg

    def close(self) -> None:
        self._cfg = None

    # --- batch enumeration --------------------------------------------------------

    def list_state_keys(self) -> list[str]:
        return [datetime.now(_JST).strftime("%Y-%m-%d")]

    def fetch_batch(self, state_key: str) -> ClickBatch:
        # ``estimated_points`` stays None because the per-day reward
        # depends on member rank + streak length and isn't knowable
        # ahead of time. The credited amount shows up in the post-click
        # balance scrape.
        candidate = ClickCandidate.model_validate(
            {
                "url": self._endpoint_url,
                "anchor_text": f"{self._label_prefix} {state_key}",
                "extraction_reason": "whitelist_url_pattern",
            }
        )
        return ClickBatch(
            state_key=state_key,
            label=f"{self._label_prefix} {state_key}",
            candidates=[candidate],
        )

    # --- side effects -------------------------------------------------------------

    def mark_complete(self, batch: ClickBatch) -> None:
        # Endpoint polls have no external "mark as read" — the only
        # idempotency layer is StateStore's per-day key.
        del batch

    def mark_no_credit(self, batch: ClickBatch) -> None:
        # fetch_batch always returns one candidate, so this is unreachable
        # in practice. Kept as a no-op for protocol compliance.
        del batch
