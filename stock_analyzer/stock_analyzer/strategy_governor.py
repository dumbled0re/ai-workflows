"""Single-writer governor for ``screening_weights.json``.

Background — before this module landed, two independent sources could
each rewrite the active weights file:

- ``phase_prepare`` (every cron) auto-applied a Bayesian proposal when
  ``calibration_zone == green`` + ``recovery_confirmed``.
- ``phase_apply_review`` (weekly cron) applied AI weekly-review
  ``screening_weight_adjustments`` directly.

Two writers with no coordination layer means one can stomp the other.
Concretely, on 2026-06-13 the weekly review committed
``rsi_oversold_recovery: 20`` minutes before a manual edit (Phase B-small
in the accuracy pivot) pushed ``rsi_oversold_recovery: 12``; the rebase
caught it but only because a human noticed. The same race would silently
clobber Bayesian-applied changes too.

This module is the **only** writer to ``screening_weights.json`` after
this change. Both sources submit *proposals* via :func:`submit_proposal`;
the governor reconciles them under explicit rules and applies an
``active`` batch via :func:`attempt_apply`. Every applied batch is
appended to ``strategy_change_log.json`` with the audit trail needed for
post-mortem (and for the P0-2 pending-verify wire-up that hooks into the
``change_id`` emitted here).

Reconciliation rules (intentionally simple, listed in order):

1. **Gating** — when ``calibration_zone.zone`` is ``red`` or ``yellow``,
   no weight change is applied (止血優先). Drift / red is exactly the
   state where retuning would compound the problem.
2. **One-batch-at-a-time** — if any prior change in the log is still
   ``pending_verify`` (= verify ticket not resolved), no new batch is
   applied. Avoids attribution noise; a failing change must be observed
   and judged before the next one piles on.
3. **Direction agreement** — when Bayesian and weekly review both
   propose moving the same signal, the directions must match. Opposing
   directions → the signal is held at its current value and recorded as
   ``conflict``.
4. **Capped delta** — for signals with agreement, the applied weight is
   ``mid + clip(proposed_mid - current, ±_MAX_PER_CHANGE_DELTA)`` where
   ``proposed_mid`` is the mean of both proposals when both present,
   else the single source's value. Caps the per-batch move so a wild
   Bayesian or weekly suggestion can't yank the active config.
5. **Single-source pass-through** — when only one source proposed
   (e.g. mid-week run between weekly reviews), apply that source's
   weights subject to the same delta cap.

Failure modes:

- Proposal file corrupted → governor logs and treats as "no pending
  proposal". The active weights stay where they are.
- Log file corrupted → same; cron continues without governor activity
  rather than crashing prediction generation.
- Submitting two proposals from the same source on the same date —
  the second overrides the first (idempotent for daily cron loops).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"
_PROPOSAL_FILE = _DATA_DIR / "strategy_change_proposals.json"
_CHANGE_LOG_FILE = _DATA_DIR / "strategy_change_log.json"
_ACTIVE_WEIGHTS_FILE = _DATA_DIR / "screening_weights.json"
_REPO_ROOT = Path(__file__).parent.parent.parent
_VERIFY_DIR = _REPO_ROOT / "verify" / "stock_analyzer"
_SNAPSHOT_DIR = _VERIFY_DIR / "snapshots"

_DEFAULT_MIN_RESOLVED = 14
"""Minimum number of resolved trades that must accumulate after the
change's activation date before a verify verdict is rendered. Matches
drift_indicator's recent window so the metric comparison has enough
power."""

_DEFAULT_VERIFY_WAIT_DAYS = 21
"""Calendar floor on when verify is even attempted. Even if 14 trades
resolve in a single week, give the change some time to settle before
declaring it good or bad — too-quick verdicts overweight a noisy
sample."""

_VALID_SOURCES = ("bayesian", "weekly_review", "manual")
"""Sources allowed to submit proposals. ``manual`` is reserved for
operator overrides documented in the same audit trail."""

_MAX_PER_CHANGE_DELTA = 4.0
"""Hard cap on how much a single applied batch may move any single
weight. 4 points roughly matches one ``+50%`` move on a typical
8-weight signal — large enough to be meaningful, small enough that no
single batch can re-shape the screener overnight."""

_WEIGHT_MIN = 1
_WEIGHT_MAX = 50
"""Hard bounds on resulting active weights, matching
``apply_review_results`` in strategy_learner."""


# --- proposal storage -----------------------------------------------------


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.warning("Failed to read %s — treating as empty", path, exc_info=True)
        return default


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_proposals(path: Path | None = None) -> dict:
    """Return the proposal store as ``{source: {proposed: {sig: weight}, ...}}``.

    Missing / corrupt file → empty dict. Callers can mutate the result
    and pass it back to :func:`save_proposals`.
    """
    p = path or _PROPOSAL_FILE
    data = _load_json(p, {})
    return data if isinstance(data, dict) else {}


def save_proposals(proposals: dict, path: Path | None = None) -> None:
    _save_json(path or _PROPOSAL_FILE, proposals)


def load_change_log(path: Path | None = None) -> list[dict]:
    """Append-only audit log of applied weight changes. Newest at the tail."""
    p = path or _CHANGE_LOG_FILE
    data = _load_json(p, [])
    return data if isinstance(data, list) else []


def save_change_log(log: list[dict], path: Path | None = None) -> None:
    _save_json(path or _CHANGE_LOG_FILE, log)


def submit_proposal(
    source: str,
    proposed_weights: dict[str, int | float],
    reason: str,
    today: str | None = None,
    proposals_path: Path | None = None,
) -> None:
    """Record a weight-change proposal from ``source`` without touching
    the active config.

    Idempotent per (source, today): re-submitting the same source on
    the same date overwrites that source's entry rather than appending,
    so a cron loop won't pile up duplicates.

    Args:
      source: One of ``_VALID_SOURCES``. Used for reconciliation.
      proposed_weights: ``{signal_name: new_weight}``. Only signals the
        source actually wants to move should appear — omitted signals
        are treated as "no opinion" by the governor.
      reason: Free-text rationale. Persisted into the change_log when
        the proposal is later applied, so post-mortem can read it.
      today: ISO date stamp. Defaults to ``datetime.now()``.
    """
    if source not in _VALID_SOURCES:
        raise ValueError(f"Unknown proposal source: {source!r}; expected one of {_VALID_SOURCES}")
    if not isinstance(proposed_weights, dict):
        raise TypeError("proposed_weights must be a dict of signal -> weight")
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")

    cleaned: dict[str, float] = {}
    for sig, w in proposed_weights.items():
        if isinstance(w, (int, float)) and not isinstance(w, bool):
            cleaned[str(sig)] = float(w)

    proposals = load_proposals(proposals_path)
    proposals[source] = {
        "proposed": cleaned,
        "reason": reason,
        "submitted_at": today,
    }
    save_proposals(proposals, proposals_path)
    logger.info("strategy_governor: proposal submitted (source=%s, signals=%d)", source, len(cleaned))


# --- gating ----------------------------------------------------------------


def _zone_blocks_apply(perf_stats: dict | None) -> tuple[bool, str]:
    """Returns ``(blocked, reason)``. Red / Yellow zone blocks all
    weight changes; missing stats also blocks (we'd rather hold than
    apply blind)."""
    if not perf_stats:
        return True, "no performance_stats available — holding active weights"
    zone_info = perf_stats.get("calibration_zone")
    if not isinstance(zone_info, dict):
        return False, ""  # No zone signal == legacy mode, allow.
    zone = zone_info.get("zone")
    if zone in ("red", "yellow"):
        return True, f"calibration_zone={zone} — weight changes held"
    return False, ""


def _verify_blocks_apply(log: list[dict]) -> tuple[bool, str]:
    """When an earlier batch is still ``pending_verify``, hold the next
    batch. One-change-at-a-time is the only way to attribute outcomes
    to the change that caused them."""
    for entry in reversed(log):
        if not isinstance(entry, dict):
            continue
        if entry.get("verify_status") == "pending":
            return True, f"prior batch {entry.get('change_id', '?')} still pending verify"
        # Any non-pending status (passed / failed / inconclusive) lets
        # us proceed. We only need the *most recent* batch to be
        # resolved — older history doesn't block.
        break
    return False, ""


# --- reconciliation --------------------------------------------------------


def _reconcile(
    weekly: dict[str, float] | None,
    bayesian: dict[str, float] | None,
    current: dict[str, float],
) -> tuple[dict[str, float], list[str]]:
    """Merge per-signal proposals into a single applied delta map.

    Returns ``(applied_weights, conflicts)`` where ``applied_weights``
    contains only signals that actually moved (delta-capped + bound-
    clamped) and ``conflicts`` lists signal names where the two
    sources disagreed on direction (and therefore neither was applied).
    """
    applied: dict[str, float] = {}
    conflicts: list[str] = []

    all_sigs = set()
    if weekly:
        all_sigs.update(weekly.keys())
    if bayesian:
        all_sigs.update(bayesian.keys())

    for sig in all_sigs:
        cur = float(current.get(sig, 0))
        w_proposal = weekly.get(sig) if weekly else None
        b_proposal = bayesian.get(sig) if bayesian else None
        # If neither source has an opinion on this sig, skip.
        if w_proposal is None and b_proposal is None:
            continue

        w_delta = (w_proposal - cur) if w_proposal is not None else None
        b_delta = (b_proposal - cur) if b_proposal is not None else None

        # Direction check when both present.
        if w_delta is not None and b_delta is not None:
            # Treat near-zero deltas (< 0.5) as "no opinion" so we don't
            # flag conflicts on numerical rounding.
            w_dir = 0 if abs(w_delta) < 0.5 else (1 if w_delta > 0 else -1)
            b_dir = 0 if abs(b_delta) < 0.5 else (1 if b_delta > 0 else -1)
            if w_dir != 0 and b_dir != 0 and w_dir != b_dir:
                conflicts.append(sig)
                continue
            # Direction agrees (or one is zero) — use the average.
            target_delta = (w_delta + b_delta) / 2.0
        else:
            target_delta = w_delta if w_delta is not None else b_delta

        # Cap the per-batch delta.
        capped = max(-_MAX_PER_CHANGE_DELTA, min(_MAX_PER_CHANGE_DELTA, target_delta or 0.0))
        new_w = cur + capped
        new_w = max(_WEIGHT_MIN, min(_WEIGHT_MAX, new_w))
        new_w_int = round(new_w)
        # Only record an actual change.
        if new_w_int != round(cur):
            applied[sig] = new_w_int

    return applied, conflicts


# --- public apply ----------------------------------------------------------


def attempt_apply(
    perf_stats: dict | None,
    today: str | None = None,
    proposals_path: Path | None = None,
    log_path: Path | None = None,
    weights_path: Path | None = None,
) -> dict | None:
    """Attempt to reconcile pending proposals into a single applied
    change. Returns a summary dict when a change was applied, ``None``
    when blocked or nothing to apply.

    The summary shape (also persisted to the change log):

    ``{
        "change_id": "wts-2026-06-13-abcd1234",
        "activated_at": "2026-06-13",
        "sources": ["bayesian", "weekly_review"],
        "applied": {"signal": new_weight, ...},
        "conflicts": ["signal_x", ...],
        "before": {signal: prior_weight, ...},
        "after": {signal: new_weight, ...},
        "reason": "Bayesian: ...; weekly_review: ...",
        "verify_status": "pending",
        "verify_ticket": null
    }``

    Even when the apply is gated (red/yellow zone, prior verify
    pending), the proposal store is *not* cleared — proposals remain
    queued for the next attempt. They get overwritten by a fresh
    submission from the same source on the next cron.
    """
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")

    proposals = load_proposals(proposals_path)
    if not proposals:
        return None

    log = load_change_log(log_path)
    blocked, reason = _zone_blocks_apply(perf_stats)
    if blocked:
        logger.info("strategy_governor: apply blocked (%s)", reason)
        return None
    blocked, reason = _verify_blocks_apply(log)
    if blocked:
        logger.info("strategy_governor: apply blocked (%s)", reason)
        return None

    # Load current active weights (excluding metadata keys).
    weights_p = weights_path or _ACTIVE_WEIGHTS_FILE
    active_raw = _load_json(weights_p, {})
    active = {k: float(v) for k, v in active_raw.items() if not k.startswith("_") and isinstance(v, (int, float))}

    weekly = (proposals.get("weekly_review") or {}).get("proposed")
    bayesian = (proposals.get("bayesian") or {}).get("proposed")
    if not weekly and not bayesian:
        return None

    applied, conflicts = _reconcile(weekly, bayesian, active)
    if not applied and not conflicts:
        return None

    new_active_raw = dict(active_raw)
    before: dict[str, float] = {}
    after: dict[str, float] = {}
    for sig, new_w in applied.items():
        before[sig] = float(active_raw.get(sig, 0))
        after[sig] = float(new_w)
        new_active_raw[sig] = new_w

    change_id = f"wts-{today}-{uuid.uuid4().hex[:8]}"
    sources = [s for s in ("weekly_review", "bayesian") if proposals.get(s)]
    reasons: list[str] = []
    for s in sources:
        r = (proposals.get(s) or {}).get("reason")
        if r:
            reasons.append(f"{s}: {r}")

    summary = {
        "change_id": change_id,
        "activated_at": today,
        "sources": sources,
        "applied": applied,
        "conflicts": conflicts,
        "before": before,
        "after": after,
        "reason": "; ".join(reasons) if reasons else "",
        "verify_status": "pending",
        "verify_ticket": None,
    }

    if applied:
        new_active_raw["_last_applied"] = change_id
        _save_json(weights_p, new_active_raw)
        # Persist verify artifacts (rollback snapshot + verify YAML)
        # so the pending-verify cron can score this change after the
        # observation window elapses. Failure here is logged but never
        # blocks the apply — the change_log entry is still authoritative
        # and a follow-up cron can regenerate the YAML if needed.
        try:
            verify_path = _persist_verify_artifacts(summary, active_raw, today)
            if verify_path is not None:
                summary["verify_yaml"] = str(verify_path.relative_to(_REPO_ROOT))
        except Exception:
            logger.exception("strategy_governor: failed to persist verify artifacts for %s", change_id)
    log.append(summary)
    save_change_log(log, log_path)

    # Clear consumed proposals so they aren't re-applied.
    # Conflicts stay observable through the change log entry instead.
    for s in sources:
        proposals.pop(s, None)
    save_proposals(proposals, proposals_path)

    logger.info(
        "strategy_governor: applied %d weight changes (id=%s, conflicts=%d)",
        len(applied),
        change_id,
        len(conflicts),
    )
    return summary


# --- issue creation (best-effort, no-op without gh) -----------------------


def _maybe_create_verify_issue(change_id: str, yaml_relpath: str, summary: dict) -> str | None:
    """Best-effort GitHub issue creation for the pending-verify cron.

    Skips silently when:
    - ``gh`` is not on PATH (local dev, no CLI installed)
    - ``GH_TOKEN`` / ``GITHUB_TOKEN`` env var is unset (running outside CI
      where the user expects no GitHub side-effects)
    - The subprocess fails for any reason

    The intent is that GitHub-Actions runtime has both gh and a token,
    so the cron flow auto-creates the issue end-to-end. Local pytest /
    manual python runs skip the network call entirely.
    """
    if shutil.which("gh") is None:
        logger.debug("gh CLI not available — verify issue creation skipped for %s", change_id)
        return None
    if not (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        logger.debug("no GH_TOKEN / GITHUB_TOKEN — verify issue creation skipped for %s", change_id)
        return None

    applied = summary.get("applied") or {}
    body_lines = [
        "---",
        f"verify_id: stock_analyzer/{change_id}",
        "---",
        "",
        f"strategy_governor が {summary.get('activated_at', '?')} に適用した weight 変更の効果検証。",
        f"YAML schema: `{yaml_relpath}`",
        f"sources: {', '.join(summary.get('sources') or []) or 'unknown'}",
        "",
        "## 変更内容",
        "",
    ]
    if applied:
        before = summary.get("before") or {}
        for sig, new_w in applied.items():
            body_lines.append(f"- `{sig}`: {before.get(sig, '?')} → {new_w}")
    else:
        body_lines.append("- (active 変更なし、conflict のみ)")
    conflicts = summary.get("conflicts") or []
    if conflicts:
        body_lines.extend(["", "## 競合保留", "", *(f"- `{s}`" for s in conflicts)])
    body_lines.extend(
        [
            "",
            "## 検証",
            "",
            "21 日 + 14 確定 trades 後に `pending_verify` cron が ",
            "`kind_strategy_change_metric_check` を実行し、`net_expectancy_pct` の ",
            "pre-change vs post-change を比較して passed / failed / inconclusive を判定。",
        ]
    )
    body = "\n".join(body_lines)
    title = f"[stock_analyzer] verify weight change {change_id}"
    try:
        proc = subprocess.run(
            [
                "gh",
                "issue",
                "create",
                "--title",
                title,
                "--body",
                body,
                "--label",
                "pending-verify",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        url = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
        logger.info("strategy_governor: verify issue created for %s → %s", change_id, url)
        return url or None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("strategy_governor: gh issue create failed for %s: %s", change_id, exc)
        return None


# --- verify artifact persistence ------------------------------------------


def _persist_verify_artifacts(
    summary: dict,
    active_before: dict,
    today: str,
) -> Path | None:
    """Write a snapshot of the pre-change active weights and a verify
    YAML descriptor that the pending-verify cron consumes.

    The YAML uses the new ``strategy_change_metric_check`` kind. It
    declares which performance_stat to read after the observation
    window and which direction = improvement. Concrete fields:

    - ``primary_metric``: dot-path into ``performance_stats``
      (e.g. ``net_expectancy.net_expectancy_pct``)
    - ``improvement_direction``: ``increase`` / ``decrease`` /
      ``zone_to_green``
    - ``min_resolved_trades``: how many post-activation resolutions
      must be present before a verdict
    - ``earliest_date``: calendar floor on verdict time
    - ``rollback_snapshot``: path to the snapshot for manual rollback
    """
    from datetime import date, timedelta

    try:
        activation = date.fromisoformat(today)
    except ValueError:
        activation = datetime.now().date()
    earliest = (activation + timedelta(days=_DEFAULT_VERIFY_WAIT_DAYS)).isoformat()

    change_id = summary["change_id"]

    _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = _SNAPSHOT_DIR / f"{change_id}.json"
    snapshot_payload = {
        "change_id": change_id,
        "activated_at": today,
        "active_weights_before": active_before,
        "applied": summary.get("applied") or {},
        "conflicts": summary.get("conflicts") or [],
        "reason": summary.get("reason", ""),
        "sources": summary.get("sources") or [],
    }
    _save_json(snapshot_path, snapshot_payload)

    yaml_path = _VERIFY_DIR / f"{change_id}.yml"
    description_parts = [
        f"strategy_governor が {today} に適用した weight 変更 batch {change_id}",
        f"sources: {', '.join(summary.get('sources') or []) or 'unknown'}",
    ]
    applied = summary.get("applied") or {}
    if applied:
        diff_lines = []
        before_map = summary.get("before") or {}
        for sig, new_w in applied.items():
            diff_lines.append(f"{sig}: {before_map.get(sig, '?')} → {new_w}")
        description_parts.append("変更内容: " + "; ".join(diff_lines))
    conflicts = summary.get("conflicts") or []
    if conflicts:
        description_parts.append("競合保留 signals: " + ", ".join(conflicts))
    description_parts.append(
        "効果検証: net_expectancy_pct が pre-change baseline と比較して改善方向に "
        "ある場合 success、悪化方向なら failure、有意差なしなら inconclusive。"
    )

    yaml_body = (
        f"verify_id: stock_analyzer/{change_id}\n"
        f"verify_after: {earliest}T00:00:00+09:00\n"
        f"project: stock_analyzer\n"
        f"description: |\n  " + "\n  ".join(description_parts) + "\n"
        f"kind: strategy_change_metric_check\n"
        f"args:\n"
        f"  change_id: {change_id}\n"
        f"  primary_metric: net_expectancy.net_expectancy_pct\n"
        f"  improvement_direction: increase\n"
        f"  min_resolved_trades: {_DEFAULT_MIN_RESOLVED}\n"
        f"  secondary_metrics:\n"
        f"    - accuracy_pct\n"
        f"    - up_winrate_pct\n"
        f"  rollback_snapshot: {snapshot_path.relative_to(_REPO_ROOT)}\n"
        f"max_attempts: 6\n"
        f"retry_after_hours: 72\n"
        f"relates_to: {change_id}\n"
    )
    _VERIFY_DIR.mkdir(parents=True, exist_ok=True)
    yaml_path.write_text(yaml_body, encoding="utf-8")
    logger.info(
        "strategy_governor: verify yaml + snapshot written for %s (yaml=%s, snapshot=%s)",
        change_id,
        yaml_path,
        snapshot_path,
    )

    # Best-effort GitHub issue creation. The pending-verify cron needs
    # the issue + yaml pair to run; in CI we create both, locally we
    # leave just the yaml so a follow-up commit gets it into the repo.
    yaml_relpath_str = str(yaml_path.relative_to(_REPO_ROOT))
    issue_url = _maybe_create_verify_issue(change_id, yaml_relpath_str, summary)
    if issue_url:
        summary["verify_ticket"] = issue_url
    return yaml_path


# --- auto-rollback ---------------------------------------------------------


def _slice_metric(metric: str, sample: list[dict]) -> float | None:
    """Compute one of the supported scalar metrics over a sample of
    resolved trades. ``None`` when the metric can't be computed (empty
    sample, unsupported metric name).
    """
    if metric in ("net_expectancy.net_expectancy_pct", "net_expectancy_pct"):
        returns: list[float] = []
        for p in sample:
            r = p.get("actual_return_pct")
            if r is None:
                continue
            d = p.get("prediction")
            if d == "UP":
                returns.append(float(r))
            elif d == "DOWN":
                returns.append(-float(r))
        return sum(returns) / len(returns) if returns else None
    if metric == "accuracy_pct":
        wins = sum(1 for p in sample if p.get("status") == "win")
        return wins / len(sample) * 100 if sample else None
    if metric in ("up_winrate_pct", "recent_direction_winrate.UP.winrate_pct"):
        up = [p for p in sample if p.get("prediction") == "UP"]
        if not up:
            return None
        return sum(1 for p in up if p.get("status") == "win") / len(up) * 100
    if metric in ("down_winrate_pct", "recent_direction_winrate.DOWN.winrate_pct"):
        down = [p for p in sample if p.get("prediction") == "DOWN"]
        if not down:
            return None
        return sum(1 for p in down if p.get("status") == "win") / len(down) * 100
    return None


def _verdict_for_metric(
    metric: str,
    pre: list[dict],
    post: list[dict],
    direction: str,
    noise_band_pp: float = 0.5,
) -> tuple[str, str]:
    """Single-metric pre-vs-post comparison → ``(verdict, detail)``.
    Returns ``inconclusive`` when either slice can't compute or the
    delta is in the noise band.
    """
    pre_val = _slice_metric(metric, pre)
    post_val = _slice_metric(metric, post)
    if pre_val is None or post_val is None:
        return ("inconclusive", f"{metric}: could not compute on both slices")
    delta = post_val - pre_val
    detail = f"{metric}: pre={pre_val:.2f}, post={post_val:.2f}, delta={delta:+.2f}"
    if abs(delta) < noise_band_pp:
        return ("inconclusive", f"{detail} (within noise band)")
    if (direction == "increase" and delta > 0) or (direction == "decrease" and delta < 0):
        return ("success", detail)
    return ("failure", detail)


def evaluate_change_metric(
    change_id: str,
    perf_history: dict,
    primary_metric: str = "net_expectancy.net_expectancy_pct",
    improvement_direction: str = "increase",
    min_resolved_trades: int = 14,
    noise_band_pp: float = 0.5,
    log: list[dict] | None = None,
    secondary_metrics: list[str] | None = None,
) -> tuple[str, str]:
    """Re-run the metric check inline. Returns ``(verdict, detail)``
    where verdict ∈ ``{"success","failure","inconclusive"}``.

    Mirrors ``scripts/pending_verify.py:kind_strategy_change_metric_check``
    so the governor can act on the same verdict without depending on
    the verify cron's issue plumbing. Both paths read the same data
    files and apply the same threshold so they agree by construction.

    Secondary metrics (optional list of metric names) implement
    "evidence stacking" — when the primary verdict is success or
    failure but a secondary contradicts it (resolves with the opposite
    direction signal), the verdict is downgraded to ``inconclusive``.
    This catches false-positives where one metric moved due to mix
    shift rather than genuine signal change.
    """
    entries = log if log is not None else load_change_log()
    entry = next((e for e in entries if isinstance(e, dict) and e.get("change_id") == change_id), None)
    if entry is None:
        return ("inconclusive", f"change_id {change_id} not in change_log")
    activation_iso = entry.get("activated_at")
    if not activation_iso:
        return ("inconclusive", "change_log entry missing activated_at")

    resolved = [
        p for p in perf_history.get("predictions", []) if p.get("status") in ("win", "loss") and p.get("reviewed_date")
    ]
    # pre/post の割り当ては **予測が作られた日 (date)** 基準。weight 変更が
    # 影響するのは「変更後にどの予測が生成されるか」であって、変更前に出た
    # 予測が変更後に解決されてもそれは旧 weight の成果 (codex 2026-07-04
    # 指摘: reviewed_date 基準だと変更前の予測が post に混入して pass/fail
    # と auto-rollback を誤判定する)。reviewed_date は「結果確定済み」の
    # フィルタ (上の resolved) にだけ使う。
    post = [p for p in resolved if (p.get("date") or "") >= activation_iso]
    if len(post) < min_resolved_trades:
        return ("inconclusive", f"post-activation resolved trades = {len(post)} (< min {min_resolved_trades})")
    pre = [p for p in resolved if (p.get("date") or "") < activation_iso]
    if len(pre) < min_resolved_trades:
        return ("inconclusive", f"pre-activation sample = {len(pre)} (< min {min_resolved_trades})")

    primary_verdict, primary_detail = _verdict_for_metric(
        primary_metric, pre, post, improvement_direction, noise_band_pp=noise_band_pp
    )
    # Without secondary metrics the primary verdict stands as-is.
    if not secondary_metrics:
        return (primary_verdict, f"n_post={len(post)}, {primary_detail}")

    contradictory: list[str] = []
    secondary_details: list[str] = []
    for m in secondary_metrics:
        v, d = _verdict_for_metric(m, pre, post, improvement_direction, noise_band_pp=noise_band_pp)
        secondary_details.append(f"{v}: {d}")
        if (primary_verdict == "success" and v == "failure") or (primary_verdict == "failure" and v == "success"):
            contradictory.append(m)
    detail = f"n_post={len(post)}, primary={primary_detail}; secondary=[{'; '.join(secondary_details)}]"
    if contradictory:
        return (
            "inconclusive",
            f"primary={primary_verdict} but secondary contradicted: {contradictory}. {detail}",
        )
    return (primary_verdict, detail)


def rollback_change(
    change_id: str,
    reason: str,
    today: str | None = None,
    log_path: Path | None = None,
    weights_path: Path | None = None,
    snapshot_dir: Path | None = None,
) -> dict | None:
    """Restore the pre-change weights snapshot for ``change_id`` and
    record a new change_log entry with ``source="auto-rollback"``.

    Idempotent: re-calling on an already-rolled-back entry is a no-op
    (returns None, logs a debug line). Marks the original entry's
    ``verify_status`` to ``failed_rolled_back`` so future passes
    don't reprocess it.

    Returns the new audit-log entry on success, ``None`` when no
    rollback was performed (snapshot missing, original entry not
    found, already rolled back, etc.).
    """
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")

    log = load_change_log(log_path)
    target_idx: int | None = None
    for i, entry in enumerate(log):
        if isinstance(entry, dict) and entry.get("change_id") == change_id:
            target_idx = i
            break
    if target_idx is None:
        logger.warning("rollback_change: %s not in change_log; no-op", change_id)
        return None
    original = log[target_idx]
    if original.get("verify_status") in ("failed_rolled_back", "passed"):
        logger.debug("rollback_change: %s already finalised (%s); no-op", change_id, original.get("verify_status"))
        return None

    snapshot_d = snapshot_dir or _SNAPSHOT_DIR
    snapshot_path = snapshot_d / f"{change_id}.json"
    if not snapshot_path.exists():
        logger.warning("rollback_change: snapshot missing for %s at %s", change_id, snapshot_path)
        return None
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("rollback_change: failed to read snapshot for %s: %s", change_id, exc)
        return None
    before_weights = snapshot.get("active_weights_before")
    if not isinstance(before_weights, dict):
        logger.warning("rollback_change: snapshot for %s missing active_weights_before", change_id)
        return None

    weights_p = weights_path or _ACTIVE_WEIGHTS_FILE
    current_raw = _load_json(weights_p, {})
    restored_raw = dict(current_raw)
    rolled: dict[str, float] = {}
    for sig, prior_w in before_weights.items():
        if isinstance(prior_w, int | float) and current_raw.get(sig) != prior_w:
            restored_raw[sig] = prior_w
            rolled[sig] = float(prior_w)
    if not rolled:
        # Snapshot matches current state — nothing to restore. Still
        # mark the entry as rolled back so it doesn't loop.
        log[target_idx]["verify_status"] = "failed_rolled_back"
        log[target_idx]["rollback_at"] = today
        save_change_log(log, log_path)
        logger.info("rollback_change: snapshot for %s already equals current; marked rolled back", change_id)
        return None

    new_change_id = f"rb-{today}-{uuid.uuid4().hex[:8]}"
    audit = {
        "change_id": new_change_id,
        "activated_at": today,
        "sources": ["auto-rollback"],
        "applied": {k: round(v) for k, v in rolled.items()},
        "conflicts": [],
        "before": {k: float(current_raw.get(k, 0)) for k in rolled},
        "after": {k: float(v) for k, v in rolled.items()},
        "reason": f"auto-rollback of {change_id}: {reason}",
        "verify_status": "rolled_back",  # rollback itself is not subject to verify
        "verify_ticket": None,
        "rolled_back_change_id": change_id,
    }
    restored_raw["_last_applied"] = new_change_id
    _save_json(weights_p, restored_raw)
    log[target_idx]["verify_status"] = "failed_rolled_back"
    log[target_idx]["rollback_at"] = today
    log.append(audit)
    save_change_log(log, log_path)
    logger.warning(
        "strategy_governor: auto-rolled back %s — restored %d weights to snapshot (new audit %s)",
        change_id,
        len(rolled),
        new_change_id,
    )
    return audit


def auto_rollback_failed_changes(
    perf_history: dict,
    today: str | None = None,
    log_path: Path | None = None,
    weights_path: Path | None = None,
    snapshot_dir: Path | None = None,
) -> list[str]:
    """Inspect every ``pending`` entry in the change_log and rollback
    the ones whose metric check now returns ``failure``.

    Returns the list of change_ids that were rolled back. Intended to
    be called once at the start of each cron's ``phase_prepare`` so
    the rest of the run uses post-rollback weights when applicable.

    Conservatism: ``inconclusive`` does **not** trigger rollback —
    only an explicit ``failure`` verdict does. Inconclusive verdicts
    return the change to the queue for next cron, matching the
    pending_verify cron's retry semantics.
    """
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")
    entries = load_change_log(log_path)
    rolled_back: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("verify_status") != "pending":
            continue
        # The audit entry from a rollback itself has verify_status =
        # "rolled_back", so it's correctly skipped by the filter above.
        change_id = entry.get("change_id")
        if not change_id:
            continue
        verdict, detail = evaluate_change_metric(change_id, perf_history, log=entries)
        if verdict != "failure":
            continue
        rolled = rollback_change(
            change_id,
            reason=detail,
            today=today,
            log_path=log_path,
            weights_path=weights_path,
            snapshot_dir=snapshot_dir,
        )
        if rolled is not None:
            rolled_back.append(change_id)
    return rolled_back


# --- prompt helpers --------------------------------------------------------


def governor_status_block(log: list[dict] | None = None) -> str:
    """Render a short prompt-side status: the most recent applied batch
    and whether any verify is still pending. Empty string when there is
    no history (= legacy / first run)."""
    entries = log if log is not None else load_change_log()
    if not entries:
        return ""
    latest = entries[-1]
    if not isinstance(latest, dict):
        return ""
    parts = [
        f"📋 直近の screening_weights 変更: {latest.get('change_id', '?')} "
        f"({latest.get('activated_at', '?')}, sources={','.join(latest.get('sources', []) or [])})",
    ]
    applied = latest.get("applied") or {}
    if applied:
        sig_lines = [f"{k}: {latest.get('before', {}).get(k, '?')} → {v}" for k, v in applied.items()]
        parts.append("  反映済 weight 変更: " + ", ".join(sig_lines))
    conflicts = latest.get("conflicts") or []
    if conflicts:
        parts.append("  ⚠ 競合保留 signals: " + ", ".join(conflicts))
    if latest.get("verify_status") == "pending":
        parts.append("  ⏳ 効果検証 pending (この batch が解決するまで次の weight 変更は保留)")
    return "\n".join(parts)
