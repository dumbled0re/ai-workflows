"""Tests for outcome telemetry and degradation detection.

The detector's job is to distinguish "pipeline is broken" from "nothing
to click today" — if a single low-credit run trips the alert, the user
gets paged on every quiet morning. These tests pin down the time-series
logic so the threshold math doesn't silently drift.
"""

from datetime import UTC, datetime

from point_sites.common.outcome_tracker import (
    CLICK_FAILURE_WINDOW,
    DEGRADATION_RATIO_THRESHOLD,
    DEGRADATION_WINDOW,
    Outcome,
    OutcomeTracker,
)


def _outcome(
    *,
    expected: int = 10,
    before: int | None = 100,
    after: int | None = 110,
    success: int = 5,
) -> Outcome:
    return Outcome(
        timestamp=datetime.now(UTC),
        mode="click",
        messages_found=success,
        click_success=success,
        click_fail=0,
        expected_pt=expected,
        balance_before=before,
        balance_after=after,
    )


def test_credit_ratio_computed(tmp_path) -> None:
    o = _outcome(expected=10, before=100, after=105)
    assert o.actual_pt_delta == 5
    assert o.credit_ratio == 0.5


def test_credit_ratio_none_when_balance_missing() -> None:
    o = _outcome(expected=10, before=None, after=110)
    assert o.actual_pt_delta is None
    assert o.credit_ratio is None


def test_credit_ratio_none_when_no_expected() -> None:
    o = _outcome(expected=0, before=100, after=100)
    assert o.credit_ratio is None


def test_append_and_recent(tmp_path) -> None:
    tracker = OutcomeTracker(tmp_path / "outcomes.jsonl")
    tracker.append(_outcome())
    tracker.append(_outcome(after=120))
    runs = tracker.recent(5)
    assert len(runs) == 2
    assert runs[0]["actual_pt_delta"] == 10
    assert runs[1]["actual_pt_delta"] == 20


def test_degradation_not_triggered_with_one_low_run(tmp_path) -> None:
    tracker = OutcomeTracker(tmp_path / "outcomes.jsonl")
    tracker.append(_outcome(expected=10, before=100, after=100))
    assert tracker.detect_degradation() is None


def test_degradation_triggered_after_window_low_runs(tmp_path) -> None:
    tracker = OutcomeTracker(tmp_path / "outcomes.jsonl")
    for _ in range(DEGRADATION_WINDOW):
        # ratio = 0/10 = 0 < threshold
        tracker.append(_outcome(expected=10, before=100, after=100))
    alert = tracker.detect_degradation()
    assert alert is not None
    assert alert.runs_inspected == DEGRADATION_WINDOW
    assert alert.median_ratio == 0.0
    assert "Cookie" in alert.suggestion


def test_degradation_resets_after_one_good_run(tmp_path) -> None:
    """A good run in the middle clears the alert window."""
    tracker = OutcomeTracker(tmp_path / "outcomes.jsonl")
    tracker.append(_outcome(expected=10, before=100, after=100))
    tracker.append(_outcome(expected=10, before=100, after=100))
    tracker.append(_outcome(expected=10, before=100, after=110))  # ratio=1.0, good
    assert tracker.detect_degradation() is None


def test_low_expected_runs_skipped(tmp_path) -> None:
    """Runs with tiny expected_pt are noisy; they shouldn't anchor an alert."""
    tracker = OutcomeTracker(tmp_path / "outcomes.jsonl")
    # Three runs with expected=1 (below MIN_EXPECTED_FOR_RATIO=2). Even
    # though ratio is 0%, the detector should ignore them.
    for _ in range(DEGRADATION_WINDOW):
        tracker.append(_outcome(expected=1, before=100, after=100))
    assert tracker.detect_degradation() is None


def test_runs_without_balances_ignored(tmp_path) -> None:
    tracker = OutcomeTracker(tmp_path / "outcomes.jsonl")
    for _ in range(DEGRADATION_WINDOW):
        tracker.append(_outcome(expected=10, before=None, after=None))
    assert tracker.detect_degradation() is None


def test_threshold_boundary(tmp_path) -> None:
    """A ratio at exactly the threshold is NOT a degradation (strictly less)."""
    tracker = OutcomeTracker(tmp_path / "outcomes.jsonl")
    # ratio == threshold (0.3) with expected=10 → delta=3 → ratio=0.3
    delta_at_threshold = int(10 * DEGRADATION_RATIO_THRESHOLD)
    for _ in range(DEGRADATION_WINDOW):
        tracker.append(_outcome(expected=10, before=100, after=100 + delta_at_threshold))
    assert tracker.detect_degradation() is None


def test_click_failure_alert_triggers_for_balance_blind_sites(tmp_path) -> None:
    """When balance can't be scraped (pointincome), the click-failure
    fallback should still fire if every recent click 4xx/5xx'd."""
    tracker = OutcomeTracker(tmp_path / "outcomes.jsonl")
    # All HTTP failures, no balance available — credit detector skips,
    # click-failure detector fires.
    for _ in range(CLICK_FAILURE_WINDOW):
        tracker.append(
            Outcome(
                timestamp=datetime.now(UTC),
                mode="click",
                messages_found=2,
                click_success=0,
                click_fail=2,
                expected_pt=0,
                balance_before=None,
                balance_after=None,
            ),
        )
    alert = tracker.detect_degradation()
    assert alert is not None
    assert "HTTP 失敗" in alert.suggestion
    assert alert.runs_inspected == CLICK_FAILURE_WINDOW


def test_click_failure_alert_skipped_when_no_clicks(tmp_path) -> None:
    """Runs with zero click attempts don't count toward the click-failure
    window — otherwise quiet days would accumulate false signal."""
    tracker = OutcomeTracker(tmp_path / "outcomes.jsonl")
    for _ in range(CLICK_FAILURE_WINDOW):
        tracker.append(
            Outcome(
                timestamp=datetime.now(UTC),
                mode="click",
                messages_found=0,
                click_success=0,
                click_fail=0,
                expected_pt=0,
                balance_before=None,
                balance_after=None,
            ),
        )
    assert tracker.detect_degradation() is None


def test_click_failure_alert_skipped_when_any_click_succeeded(tmp_path) -> None:
    """One successful click in the window means the pipeline is alive."""
    tracker = OutcomeTracker(tmp_path / "outcomes.jsonl")
    # Two failed runs, one with a single success — should not fire.
    for success, fail in [(0, 2), (1, 1), (0, 2)]:
        tracker.append(
            Outcome(
                timestamp=datetime.now(UTC),
                mode="click",
                messages_found=success + fail,
                click_success=success,
                click_fail=fail,
                expected_pt=0,
                balance_before=None,
                balance_after=None,
            ),
        )
    assert tracker.detect_degradation() is None


def test_ignores_corrupt_lines(tmp_path) -> None:
    path = tmp_path / "outcomes.jsonl"
    path.write_text(
        '{"valid": true, "credit_ratio": 0.0, "expected_pt": 10}\n'
        "this is not json\n"
        '{"valid": true, "credit_ratio": 0.0, "expected_pt": 10}\n',
        encoding="utf-8",
    )
    tracker = OutcomeTracker(path)
    runs = tracker.recent(10)
    assert len(runs) == 2  # corrupt line skipped
