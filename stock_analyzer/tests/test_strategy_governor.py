"""Tests for strategy_governor — single-writer reconciliation of
Bayesian + weekly-review proposals into the active screening_weights.

The governor is the failure-mode fix for the 2026-06-13 race where
weekly-review and a manual Bayesian-derived edit each wrote
``screening_weights.json`` minutes apart and the rebase only caught
it because a human noticed. These tests pin:

- gating (red/yellow zone holds changes, pending verify holds the next
  batch),
- direction agreement and conflict tracking,
- per-batch delta cap,
- the change-log audit trail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stock_analyzer.strategy_governor import (
    _MAX_PER_CHANGE_DELTA,
    attempt_apply,
    auto_rollback_failed_changes,
    evaluate_change_metric,
    governor_status_block,
    load_change_log,
    load_proposals,
    rollback_change,
    submit_proposal,
)


@pytest.fixture(autouse=True)
def _isolate_verify_dirs(tmp_path: Path, monkeypatch):
    """Redirect _VERIFY_DIR / _SNAPSHOT_DIR / _REPO_ROOT into the test's
    tmp_path so attempt_apply() doesn't leak verify YAML and snapshot
    files into the real repo when tests run on a green-zone path."""
    fake_verify = tmp_path / "verify" / "stock_analyzer"
    fake_snapshot = fake_verify / "snapshots"
    monkeypatch.setattr("stock_analyzer.strategy_governor._VERIFY_DIR", fake_verify)
    monkeypatch.setattr("stock_analyzer.strategy_governor._SNAPSHOT_DIR", fake_snapshot)
    monkeypatch.setattr("stock_analyzer.strategy_governor._REPO_ROOT", tmp_path)
    yield


def _write_active(weights_path: Path, weights: dict) -> None:
    weights_path.write_text(json.dumps(weights, ensure_ascii=False), encoding="utf-8")


def _green_stats() -> dict:
    return {
        "performance_stats": {},  # not used here; the helper just shapes things
        "calibration_zone": {"zone": "green", "recovery_confirmed": True},
    }


def _perf_stats(zone: str = "green") -> dict:
    """Minimal perf_stats shape: only the calibration_zone field is read."""
    return {"calibration_zone": {"zone": zone, "recovery_confirmed": True}}


def test_submit_proposal_stores_per_source_idempotently(tmp_path: Path) -> None:
    """A second submission from the same source overwrites the first —
    a daily cron loop doesn't accumulate duplicates."""
    proposals_p = tmp_path / "proposals.json"
    submit_proposal("bayesian", {"per_value": 5}, "first", "2026-06-13", proposals_p)
    submit_proposal("bayesian", {"per_value": 7, "roe_profitable": 4}, "second", "2026-06-13", proposals_p)
    data = load_proposals(proposals_p)
    assert set(data.keys()) == {"bayesian"}
    assert data["bayesian"]["proposed"] == {"per_value": 7.0, "roe_profitable": 4.0}
    assert data["bayesian"]["reason"] == "second"


def test_attempt_apply_blocked_in_red_zone(tmp_path: Path) -> None:
    """Red zone means drift / calibration is broken — applying weight
    changes during this state would compound the problem. Proposals
    remain queued for later."""
    proposals_p = tmp_path / "proposals.json"
    log_p = tmp_path / "log.json"
    weights_p = tmp_path / "weights.json"
    _write_active(weights_p, {"per_value": 2, "dividend_yield": 4})

    submit_proposal("bayesian", {"per_value": 6, "dividend_yield": 8}, "bayes 推奨", "2026-06-13", proposals_p)
    result = attempt_apply(
        perf_stats=_perf_stats("red"),
        today="2026-06-13",
        proposals_path=proposals_p,
        log_path=log_p,
        weights_path=weights_p,
    )
    assert result is None
    # Proposal must remain queued so a later green-zone cron can apply.
    assert "bayesian" in load_proposals(proposals_p)
    # Active weights unchanged.
    assert json.loads(weights_p.read_text())["per_value"] == 2


def test_attempt_apply_reconciles_agreement_with_capped_delta(tmp_path: Path) -> None:
    """Both sources agree on direction → applied via mean delta, capped
    at _MAX_PER_CHANGE_DELTA per signal per batch."""
    proposals_p = tmp_path / "proposals.json"
    log_p = tmp_path / "log.json"
    weights_p = tmp_path / "weights.json"
    _write_active(weights_p, {"per_value": 5})

    # Bayesian wants +6 (5→11), weekly wants +10 (5→15). Mean is +8 →
    # capped at +_MAX_PER_CHANGE_DELTA = +4 → final 9.
    submit_proposal("bayesian", {"per_value": 11}, "bayes", "2026-06-13", proposals_p)
    submit_proposal("weekly_review", {"per_value": 15}, "weekly", "2026-06-13", proposals_p)

    summary = attempt_apply(
        perf_stats=_perf_stats("green"),
        today="2026-06-13",
        proposals_path=proposals_p,
        log_path=log_p,
        weights_path=weights_p,
    )
    assert summary is not None
    assert summary["applied"] == {"per_value": int(5 + _MAX_PER_CHANGE_DELTA)}
    assert summary["conflicts"] == []
    assert summary["sources"] == ["weekly_review", "bayesian"]
    assert summary["verify_status"] == "pending"
    # Active file updated and metadata stamp recorded.
    actual = json.loads(weights_p.read_text())
    assert actual["per_value"] == int(5 + _MAX_PER_CHANGE_DELTA)
    assert actual["_last_applied"] == summary["change_id"]
    # Proposals cleared after apply.
    assert load_proposals(proposals_p) == {}
    # Audit log entry persisted.
    log = load_change_log(log_p)
    assert len(log) == 1
    assert log[0]["change_id"] == summary["change_id"]


def test_attempt_apply_records_direction_conflict_without_moving(tmp_path: Path) -> None:
    """Bayesian and weekly disagree on direction → the signal is held
    at current weight and the conflict is logged."""
    proposals_p = tmp_path / "proposals.json"
    log_p = tmp_path / "log.json"
    weights_p = tmp_path / "weights.json"
    _write_active(weights_p, {"per_value": 5, "dividend_yield": 4})

    # per_value: bayesian wants up (5→9), weekly wants down (5→2) → conflict.
    # dividend_yield: only bayesian moves it → single-source pass-through.
    submit_proposal("bayesian", {"per_value": 9, "dividend_yield": 7}, "bayes", "2026-06-13", proposals_p)
    submit_proposal("weekly_review", {"per_value": 2}, "weekly", "2026-06-13", proposals_p)

    summary = attempt_apply(
        perf_stats=_perf_stats("green"),
        today="2026-06-13",
        proposals_path=proposals_p,
        log_path=log_p,
        weights_path=weights_p,
    )
    assert summary is not None
    # per_value held at 5, not in applied; conflict logged.
    assert "per_value" not in summary["applied"]
    assert "per_value" in summary["conflicts"]
    # dividend_yield: 4→7 is +3, within cap, applied directly.
    assert summary["applied"].get("dividend_yield") == 7
    assert json.loads(weights_p.read_text())["per_value"] == 5
    assert json.loads(weights_p.read_text())["dividend_yield"] == 7


def test_attempt_apply_blocked_when_prior_change_pending_verify(tmp_path: Path) -> None:
    """Until the previous batch's verify resolves, the next batch is
    held — one-change-at-a-time attribution."""
    proposals_p = tmp_path / "proposals.json"
    log_p = tmp_path / "log.json"
    weights_p = tmp_path / "weights.json"
    _write_active(weights_p, {"per_value": 5})

    # Pre-populate the change log with a pending verify entry.
    log_p.write_text(
        json.dumps(
            [
                {
                    "change_id": "wts-prior",
                    "activated_at": "2026-06-10",
                    "sources": ["bayesian"],
                    "applied": {"per_value": 4},
                    "conflicts": [],
                    "before": {"per_value": 5},
                    "after": {"per_value": 4},
                    "reason": "earlier batch",
                    "verify_status": "pending",
                    "verify_ticket": "wf-1",
                }
            ]
        ),
        encoding="utf-8",
    )
    submit_proposal("bayesian", {"per_value": 9}, "newer", "2026-06-13", proposals_p)

    summary = attempt_apply(
        perf_stats=_perf_stats("green"),
        today="2026-06-13",
        proposals_path=proposals_p,
        log_path=log_p,
        weights_path=weights_p,
    )
    assert summary is None
    # Proposal stays queued until prior verify resolves.
    assert "bayesian" in load_proposals(proposals_p)
    assert json.loads(weights_p.read_text())["per_value"] == 5


def test_attempt_apply_proceeds_when_prior_verify_resolved(tmp_path: Path) -> None:
    """Once the most-recent log entry is no longer pending, the next
    batch is free to apply."""
    proposals_p = tmp_path / "proposals.json"
    log_p = tmp_path / "log.json"
    weights_p = tmp_path / "weights.json"
    _write_active(weights_p, {"per_value": 5})
    log_p.write_text(
        json.dumps(
            [
                {
                    "change_id": "wts-prior",
                    "activated_at": "2026-06-10",
                    "applied": {"per_value": 4},
                    "verify_status": "passed",
                }
            ]
        ),
        encoding="utf-8",
    )
    submit_proposal("bayesian", {"per_value": 9}, "newer", "2026-06-13", proposals_p)

    summary = attempt_apply(
        perf_stats=_perf_stats("green"),
        today="2026-06-13",
        proposals_path=proposals_p,
        log_path=log_p,
        weights_path=weights_p,
    )
    assert summary is not None
    assert summary["applied"]["per_value"] == int(5 + _MAX_PER_CHANGE_DELTA)


def test_attempt_apply_writes_verify_yaml_and_snapshot(tmp_path: Path, monkeypatch) -> None:
    """When a change applies, the governor writes a snapshot of the
    pre-change weights and a verify YAML descriptor so the pending-
    verify cron can score the change later. Tests that the artifacts
    exist and reference the change_id."""
    proposals_p = tmp_path / "proposals.json"
    log_p = tmp_path / "log.json"
    weights_p = tmp_path / "weights.json"
    _write_active(weights_p, {"per_value": 5})

    # Redirect verify dir under tmp_path so we don't pollute the real
    # repo's verify/stock_analyzer directory during tests.
    fake_verify_dir = tmp_path / "verify" / "stock_analyzer"
    fake_snapshot_dir = fake_verify_dir / "snapshots"
    fake_repo_root = tmp_path
    monkeypatch.setattr("stock_analyzer.strategy_governor._VERIFY_DIR", fake_verify_dir)
    monkeypatch.setattr("stock_analyzer.strategy_governor._SNAPSHOT_DIR", fake_snapshot_dir)
    monkeypatch.setattr("stock_analyzer.strategy_governor._REPO_ROOT", fake_repo_root)

    submit_proposal("bayesian", {"per_value": 9}, "bayes", "2026-06-13", proposals_p)
    summary = attempt_apply(
        perf_stats=_perf_stats("green"),
        today="2026-06-13",
        proposals_path=proposals_p,
        log_path=log_p,
        weights_path=weights_p,
    )
    assert summary is not None
    change_id = summary["change_id"]

    yaml_file = fake_verify_dir / f"{change_id}.yml"
    snapshot_file = fake_snapshot_dir / f"{change_id}.json"
    assert yaml_file.exists()
    assert snapshot_file.exists()
    yaml_text = yaml_file.read_text()
    assert f"verify_id: stock_analyzer/{change_id}" in yaml_text
    assert "kind: strategy_change_metric_check" in yaml_text
    assert "primary_metric: net_expectancy.net_expectancy_pct" in yaml_text
    snap = json.loads(snapshot_file.read_text())
    assert snap["change_id"] == change_id
    assert snap["active_weights_before"] == {"per_value": 5}
    assert snap["applied"]["per_value"] == 9


def _resolved_trade(date_iso: str, ret_pct: float, prediction: str = "UP", status: str | None = None) -> dict:
    """Tiny helper for evaluator/auto-rollback tests."""
    return {
        "status": status or ("win" if ret_pct > 0 else "loss"),
        "prediction": prediction,
        "actual_return_pct": ret_pct,
        "reviewed_date": date_iso,
    }


def test_evaluate_change_metric_success_when_post_better(tmp_path: Path) -> None:
    """Post-activation expectancy meaningfully above pre → success."""
    log = [
        {
            "change_id": "wts-x",
            "activated_at": "2026-06-13",
            "verify_status": "pending",
            "applied": {"per_value": 9},
        }
    ]
    pre = [_resolved_trade(f"2026-05-{d:02d}", 1.0) for d in range(1, 16)]
    post = [_resolved_trade(f"2026-07-{d:02d}", 4.0) for d in range(1, 16)]
    perf = {"predictions": pre + post}
    verdict, detail = evaluate_change_metric("wts-x", perf, log=log)
    assert verdict == "success"
    assert "delta=+3.00" in detail


def test_evaluate_change_metric_failure_when_post_worse() -> None:
    log = [
        {
            "change_id": "wts-y",
            "activated_at": "2026-06-13",
            "verify_status": "pending",
        }
    ]
    pre = [_resolved_trade(f"2026-05-{d:02d}", 2.0) for d in range(1, 16)]
    post = [_resolved_trade(f"2026-07-{d:02d}", -3.0, status="loss") for d in range(1, 16)]
    perf = {"predictions": pre + post}
    verdict, _ = evaluate_change_metric("wts-y", perf, log=log)
    assert verdict == "failure"


def test_evaluate_change_metric_secondary_contradicts_downgrades_to_inconclusive() -> None:
    """Primary says success but a secondary metric says failure →
    downgrade to inconclusive. Prevents single-metric false positives
    from triggering automatic verdicts."""
    log = [
        {
            "change_id": "wts-mixed",
            "activated_at": "2026-06-13",
            "verify_status": "pending",
        }
    ]
    # Pre: 15 UP @ +1.0 (wins), 15 DOWN @ -2.0 (wins) — net_expectancy
    # = (15*1 + 15*2)/30 = 1.5, accuracy_pct = 100
    pre = []
    for d in range(1, 16):
        pre.append(_resolved_trade(f"2026-05-{d:02d}", 1.0, prediction="UP"))
    for d in range(1, 16):
        pre.append(
            {
                "status": "win",
                "prediction": "DOWN",
                "actual_return_pct": -2.0,
                "reviewed_date": f"2026-05-{15 + d:02d}",
            }
        )
    # Post: 30 UP @ +4.0 (huge wins) — primary net_expectancy = 4.0 (up)
    # but accuracy_pct still 100, no contradiction yet. To create
    # contradiction, set post UP all losses with positive raw return…
    # simpler: post net_expectancy IMPROVES but accuracy COLLAPSES.
    post = []
    # 20 UP wins @ +3% → net = +3, accuracy 100 within this sample → not contradictory
    # Instead: post net up, but accuracy down by mixing big wins + small-but-many losses
    # 5 UP wins @ +20% (net contribution huge), 10 UP losses @ -1.5%
    for d in range(1, 6):
        post.append(_resolved_trade(f"2026-07-{d:02d}", 20.0, prediction="UP"))
    for d in range(6, 16):
        post.append(_resolved_trade(f"2026-07-{d:02d}", -1.5, prediction="UP", status="loss"))
    # Pre had: net 1.5 (sum 30*1.5 over 30 trades), accuracy 100.
    # Post net: (5*20 + 10*-1.5)/15 = (100-15)/15 = 5.67 → SUCCESS
    # Post accuracy: 5/15 = 33.3 → DOWN by 66.7pp → FAILURE
    # → secondary contradicts → inconclusive.

    perf = {"predictions": pre + post}
    verdict, detail = evaluate_change_metric(
        "wts-mixed",
        perf,
        log=log,
        secondary_metrics=["accuracy_pct"],
    )
    assert verdict == "inconclusive"
    assert "contradicted" in detail
    assert "accuracy_pct" in detail


def test_evaluate_change_metric_secondary_concurs_keeps_primary() -> None:
    """When primary and all secondaries point the same way, the
    primary verdict is preserved."""
    log = [
        {
            "change_id": "wts-aligned",
            "activated_at": "2026-06-13",
            "verify_status": "pending",
        }
    ]
    pre = [_resolved_trade(f"2026-05-{d:02d}", 1.0, prediction="UP") for d in range(1, 16)]
    post = [_resolved_trade(f"2026-07-{d:02d}", 4.0, prediction="UP") for d in range(1, 16)]
    perf = {"predictions": pre + post}
    verdict, detail = evaluate_change_metric(
        "wts-aligned",
        perf,
        log=log,
        secondary_metrics=["accuracy_pct"],
    )
    assert verdict == "success"
    assert "primary" in detail
    assert "secondary" in detail


def test_evaluate_change_metric_inconclusive_below_min_samples() -> None:
    log = [
        {
            "change_id": "wts-z",
            "activated_at": "2026-06-13",
            "verify_status": "pending",
        }
    ]
    # Only 5 post-trades → below default 14
    pre = [_resolved_trade(f"2026-05-{d:02d}", 1.0) for d in range(1, 16)]
    post = [_resolved_trade(f"2026-07-{d:02d}", 4.0) for d in range(1, 6)]
    verdict, detail = evaluate_change_metric("wts-z", {"predictions": pre + post}, log=log)
    assert verdict == "inconclusive"
    assert "min" in detail


def test_rollback_change_restores_snapshot_and_marks_entry(tmp_path: Path, monkeypatch) -> None:
    """rollback_change copies snapshot's active_weights_before back to
    the active file, appends an auto-rollback audit, and marks the
    original entry as failed_rolled_back."""
    weights_p = tmp_path / "weights.json"
    log_p = tmp_path / "log.json"
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir(parents=True)
    # Active config reflects the (allegedly bad) post-change state.
    _write_active(weights_p, {"per_value": 9, "dividend_yield": 4})
    # Snapshot says before-change was per_value=5.
    (snap_dir / "wts-bad.json").write_text(
        json.dumps(
            {
                "change_id": "wts-bad",
                "active_weights_before": {"per_value": 5, "dividend_yield": 4},
            }
        ),
        encoding="utf-8",
    )
    log_p.write_text(
        json.dumps(
            [
                {
                    "change_id": "wts-bad",
                    "activated_at": "2026-06-13",
                    "verify_status": "pending",
                    "applied": {"per_value": 9},
                    "before": {"per_value": 5},
                    "after": {"per_value": 9},
                }
            ]
        ),
        encoding="utf-8",
    )

    audit = rollback_change(
        "wts-bad",
        reason="metric regression",
        today="2026-07-10",
        log_path=log_p,
        weights_path=weights_p,
        snapshot_dir=snap_dir,
    )
    assert audit is not None
    assert audit["sources"] == ["auto-rollback"]
    assert audit["rolled_back_change_id"] == "wts-bad"
    # Active weights restored.
    assert json.loads(weights_p.read_text())["per_value"] == 5
    # Original entry marked failed_rolled_back.
    log_after = json.loads(log_p.read_text())
    original = next(e for e in log_after if e["change_id"] == "wts-bad")
    assert original["verify_status"] == "failed_rolled_back"
    # New audit entry appended.
    assert any(e["change_id"] == audit["change_id"] for e in log_after)


def test_rollback_change_idempotent_on_already_finalised(tmp_path: Path) -> None:
    weights_p = tmp_path / "weights.json"
    log_p = tmp_path / "log.json"
    _write_active(weights_p, {"per_value": 5})
    log_p.write_text(
        json.dumps(
            [
                {
                    "change_id": "wts-already",
                    "activated_at": "2026-06-13",
                    "verify_status": "failed_rolled_back",
                }
            ]
        ),
        encoding="utf-8",
    )
    result = rollback_change("wts-already", reason="x", log_path=log_p, weights_path=weights_p)
    assert result is None
    # Active config untouched.
    assert json.loads(weights_p.read_text())["per_value"] == 5


def test_auto_rollback_failed_changes_skips_inconclusive(tmp_path: Path) -> None:
    """``inconclusive`` does NOT trigger rollback — the entry stays
    pending for next cron's re-evaluation."""
    weights_p = tmp_path / "weights.json"
    log_p = tmp_path / "log.json"
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir(parents=True)
    _write_active(weights_p, {"per_value": 9})
    log_p.write_text(
        json.dumps(
            [
                {
                    "change_id": "wts-recent",
                    "activated_at": "2026-07-01",  # very recent
                    "verify_status": "pending",
                    "applied": {"per_value": 9},
                }
            ]
        ),
        encoding="utf-8",
    )
    # Only 3 post-trades → inconclusive due to min_resolved_trades=14
    perf = {
        "predictions": [
            *(_resolved_trade(f"2026-06-{d:02d}", 1.0) for d in range(1, 16)),
            *(_resolved_trade(f"2026-07-{d:02d}", -1.0, status="loss") for d in range(2, 5)),
        ]
    }
    rolled = auto_rollback_failed_changes(perf, log_path=log_p, weights_path=weights_p, snapshot_dir=snap_dir)
    assert rolled == []
    # Status still pending.
    assert json.loads(log_p.read_text())[0]["verify_status"] == "pending"


def test_governor_status_block_summarises_latest_change() -> None:
    """The prompt-side status string shows the most recent applied
    change, including conflicts and the pending-verify flag."""
    log = [
        {
            "change_id": "wts-2026-06-13-abcd",
            "activated_at": "2026-06-13",
            "sources": ["bayesian", "weekly_review"],
            "applied": {"per_value": 9},
            "conflicts": ["dividend_yield"],
            "before": {"per_value": 5},
            "verify_status": "pending",
        }
    ]
    block = governor_status_block(log)
    assert "wts-2026-06-13-abcd" in block
    assert "per_value: 5 → 9" in block
    assert "dividend_yield" in block
    assert "効果検証 pending" in block


def test_governor_status_block_empty_log_returns_empty() -> None:
    assert governor_status_block([]) == ""
