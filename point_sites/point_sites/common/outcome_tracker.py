"""Per-run outcome telemetry and degradation detection.

Why this exists: a click returning HTTP 200 does NOT prove the point was
credited — already-clicked URLs and shadow-banned sessions also return
200. The summary "✅ 成功: 11件" can therefore look healthy while the
actual coin balance does not move. To run this workflow without a human
checking the dashboard daily, we need an automatic signal that flags the
gap between *clicks that succeeded* and *points that landed*.

Each run appends one JSONL line to ``data/outcomes.jsonl`` (artifact'd
across runs). ``detect_degradation`` looks at the last N runs that were
in click mode AND had enough expected points to compute a meaningful
ratio. If the credit ratio is persistently below threshold, it returns
an alert with a concrete next-action for the user to take.

A single low-credit run is often legitimate (URLs the user manually
clicked yesterday before the cron fired). Three in a row means the
pipeline is no longer crediting and silent failure becomes likely — that
is the moment to escalate.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Tuning knobs. Exposed as module constants so tests can override and
# future operational tuning has one obvious place to look.
DEGRADATION_WINDOW = 3
DEGRADATION_RATIO_THRESHOLD = 0.3
# Below this many expected points, ratio noise dominates (1pt rounding,
# in-flight crediting delays). Skip the run from the degradation window
# rather than tripping false positives on tiny days.
MIN_EXPECTED_FOR_RATIO = 2
# Fallback signal for sites where balance can't be scraped (pointincome
# is gated by an anti-bot interstitial). When ``window`` consecutive
# click-mode runs all had click attempts AND 100% of those clicks
# returned non-2xx, raise a "click pipeline broken" alert. Distinct
# from credit-ratio degradation because we can't tell whether the
# underlying credit landed — but we CAN tell whether the HTTP layer
# stopped working entirely.
CLICK_FAILURE_WINDOW = 3


@dataclass(frozen=True)
class Outcome:
    timestamp: datetime
    mode: str
    messages_found: int
    click_success: int
    click_fail: int
    expected_pt: int
    balance_before: int | None
    balance_after: int | None

    @property
    def actual_pt_delta(self) -> int | None:
        if self.balance_before is None or self.balance_after is None:
            return None
        return self.balance_after - self.balance_before

    @property
    def credit_ratio(self) -> float | None:
        delta = self.actual_pt_delta
        if delta is None or self.expected_pt <= 0:
            return None
        return delta / self.expected_pt

    def to_dict(self) -> dict[str, object]:
        return {
            "ts": self.timestamp.isoformat(),
            "mode": self.mode,
            "messages_found": self.messages_found,
            "click_success": self.click_success,
            "click_fail": self.click_fail,
            "expected_pt": self.expected_pt,
            "balance_before": self.balance_before,
            "balance_after": self.balance_after,
            "actual_pt_delta": self.actual_pt_delta,
            "credit_ratio": self.credit_ratio,
        }


@dataclass(frozen=True)
class DegradationAlert:
    runs_inspected: int
    median_ratio: float
    suggestion: str


class OutcomeTracker:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, outcome: Outcome) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(outcome.to_dict(), ensure_ascii=False) + "\n")

    def recent(self, n: int) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        recent_raw = lines[-n:] if lines else []
        out: list[dict[str, object]] = []
        for line in recent_raw:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def detect_degradation(
        self,
        window: int = DEGRADATION_WINDOW,
        ratio_threshold: float = DEGRADATION_RATIO_THRESHOLD,
    ) -> DegradationAlert | None:
        """Return an alert when crediting (or, fallback, the click
        pipeline itself) stops working over the recent window.

        Credit-ratio degradation runs first because it's the strongest
        signal — we can only compute it for sites where balance scraping
        works. When that's unavailable (pointincome's anti-bot
        interstitial), fall through to the HTTP-success fallback so the
        operator still gets paged on a fully broken click pipeline.
        """
        credit_alert = self._detect_credit_degradation(window, ratio_threshold)
        if credit_alert is not None:
            return credit_alert
        return self._detect_click_failure(window)

    def _detect_credit_degradation(
        self,
        window: int,
        ratio_threshold: float,
    ) -> DegradationAlert | None:
        """Strongest signal: crediting falls below threshold for ``window`` consecutive ratio-eligible runs.

        We only count runs where credit_ratio could be computed (had clicks
        AND captured both balances) AND where expected_pt >= MIN_EXPECTED.
        Otherwise we cannot distinguish "pipeline is broken" from "nothing
        to click today" or "balance scraping broke today".
        """
        # Pull a generous window so a row of skip-eligible runs (e.g.
        # nothing-to-click days) doesn't push the real signal out of view.
        runs = self.recent(window * 4)
        ratios: list[float] = []
        for r in runs:
            ratio = r.get("credit_ratio")
            expected = r.get("expected_pt")
            if not isinstance(ratio, int | float):
                continue
            if not isinstance(expected, int) or expected < MIN_EXPECTED_FOR_RATIO:
                continue
            ratios.append(float(ratio))
        # Use only the most recent ``window`` eligible runs.
        ratios = ratios[-window:]
        if len(ratios) < window:
            return None
        if not all(r < ratio_threshold for r in ratios):
            return None
        sorted_r = sorted(ratios)
        median = sorted_r[len(sorted_r) // 2]
        suggestion = (
            f"連続 {window} 回、加算比率が {ratio_threshold:.0%} 未満。"
            "Cookie 失効 or 既クリック URL の再クリックの可能性。"
            "(1) ブラウザでマイページを確認 (2) Cookie を再エクスポートして "
            "<SITE>_COOKIES Secret を更新 (3) 治らなければ GitHub Actions "
            "UI で workflow を一旦 disable して原因切り分けしてください。"
        )
        return DegradationAlert(
            runs_inspected=len(ratios),
            median_ratio=median,
            suggestion=suggestion,
        )

    def _detect_click_failure(
        self,
        window: int = CLICK_FAILURE_WINDOW,
    ) -> DegradationAlert | None:
        """Fallback signal for balance-blind sites: every click 4xx/5xx
        for ``window`` consecutive run-with-clicks.

        Distinguishes "site/login broken" from "nothing to click today"
        by only counting runs that actually attempted clicks. For
        pointincome (no balance scrape) this is the only signal we have,
        so the threshold is strict — 100% failures across the window —
        to avoid false positives from one flaky day.
        """
        runs = self.recent(window * 4)
        recent_with_clicks: list[tuple[int, int]] = []
        for r in runs:
            success = r.get("click_success", 0)
            fail = r.get("click_fail", 0)
            if not isinstance(success, int) or not isinstance(fail, int):
                continue
            if success + fail == 0:
                continue
            recent_with_clicks.append((success, fail))
        recent_with_clicks = recent_with_clicks[-window:]
        if len(recent_with_clicks) < window:
            return None
        if not all(s == 0 and f > 0 for s, f in recent_with_clicks):
            return None
        total_fails = sum(f for _, f in recent_with_clicks)
        suggestion = (
            f"連続 {window} 回、click 全部 HTTP 失敗 (累計 {total_fails} 件)。"
            "Cookie 切れ・サイト構造変更・anti-bot 強化の可能性。"
            "(1) ブラウザでログイン状態を確認 (2) Cookie を再エクスポートして "
            "<SITE>_COOKIES Secret を更新 (3) `force_fresh_cookies=true` で workflow_dispatch"
        )
        return DegradationAlert(
            runs_inspected=len(recent_with_clicks),
            # No credit ratio available; expose 0.0 so existing Slack
            # formatting still shows something meaningful.
            median_ratio=0.0,
            suggestion=suggestion,
        )


def make_outcome(
    *,
    mode: str,
    messages_found: int,
    click_success: int,
    click_fail: int,
    expected_pt: int,
    balance_before: int | None,
    balance_after: int | None,
) -> Outcome:
    return Outcome(
        timestamp=datetime.now(UTC),
        mode=mode,
        messages_found=messages_found,
        click_success=click_success,
        click_fail=click_fail,
        expected_pt=expected_pt,
        balance_before=balance_before,
        balance_after=balance_after,
    )
