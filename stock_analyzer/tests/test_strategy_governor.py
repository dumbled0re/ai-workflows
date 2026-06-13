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
    governor_status_block,
    load_change_log,
    load_proposals,
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
