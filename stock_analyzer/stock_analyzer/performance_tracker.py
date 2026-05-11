from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"
_HISTORY_FILE = str(_DATA_DIR / "predictions_history.json")
_REVIEW_WINDOW_DAYS = 14  # Evaluate predictions after 2 weeks
_MIN_REVIEW_DAYS = 5  # Start checking after 5 trading days
# Minimum resolved trades before reporting a sub-bucket (HIGH/MEDIUM, by
# source, by confidence × direction). Below this, accuracy_pct is too
# noisy to drive Claude's self-improvement decisions.
_MIN_BUCKET_N = 5


def _directional_return(p: dict) -> float | None:
    """Return the realised return signed so that "predicting correctly" is positive.

    A DOWN prediction that resolves with -10% actual_return_pct means the
    AI was right and gained 10% (in a short / hedge sense), so we report
    +10. Without this flip, averaging raw signed returns mixes UP-wins
    (positive raw) with DOWN-wins (negative raw) and the mean ends up
    nonsensical (or worse, misleading — wins averaging negative is what
    the feedback prompt was showing the AI before this helper landed).
    """
    r = p.get("actual_return_pct")
    if r is None:
        return None
    direction = p.get("prediction")
    if direction == "UP":
        return float(r)
    if direction == "DOWN":
        return -float(r)
    return None


def load_history(path: str = _HISTORY_FILE) -> dict:
    """Load predictions history from JSON file."""
    p = Path(path)
    if not p.exists():
        return {"predictions": [], "performance_stats": {}}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("Failed to load history: %s", e)
        return {"predictions": [], "performance_stats": {}}


def save_history(history: dict, path: str = _HISTORY_FILE) -> None:
    """Save updated history to JSON file."""
    p = Path(path)
    p.parent.mkdir(exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    logger.info("Saved %d predictions to %s", len(history.get("predictions", [])), p)


def review_predictions(
    history: dict,
    current_prices: dict[str, float],
    today: str | None = None,
) -> dict:
    """Check past predictions against current prices, update statuses.

    Args:
        history: The predictions history dict
        current_prices: Dict mapping ticker to current price
        today: Today's date string (YYYY-MM-DD), defaults to now

    Returns:
        Updated history dict
    """
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")
    today_dt = datetime.strptime(today, "%Y-%m-%d")

    reviewed_count = 0
    for pred in history.get("predictions", []):
        if pred["status"] != "pending":
            continue

        pred_date = datetime.strptime(pred["date"], "%Y-%m-%d")
        days_elapsed = (today_dt - pred_date).days

        if days_elapsed < _MIN_REVIEW_DAYS:
            continue

        ticker = pred["ticker"]
        current_price = current_prices.get(ticker)
        if current_price is None:
            continue

        entry_price = pred.get("entry_price")
        if entry_price is None or entry_price == 0:
            continue

        return_pct = ((current_price - entry_price) / entry_price) * 100
        pred["actual_price"] = round(current_price, 1)
        pred["actual_return_pct"] = round(return_pct, 2)
        pred["reviewed_date"] = today
        pred["days_held"] = days_elapsed

        # Determine outcome
        prediction_direction = pred.get("prediction", "UP")
        if prediction_direction == "UP":
            if return_pct >= 3.0:
                pred["status"] = "win"
            elif return_pct <= -3.0:
                pred["status"] = "loss"
            elif days_elapsed >= _REVIEW_WINDOW_DAYS:
                # Expired: marginal result
                pred["status"] = "win" if return_pct > 0 else "loss"
            # else: still pending, wait longer
        elif prediction_direction == "DOWN":
            if return_pct <= -3.0:
                pred["status"] = "win"
            elif return_pct >= 3.0:
                pred["status"] = "loss"
            elif days_elapsed >= _REVIEW_WINDOW_DAYS:
                pred["status"] = "win" if return_pct < 0 else "loss"

        if pred["status"] != "pending":
            reviewed_count += 1

    if reviewed_count > 0:
        logger.info("Reviewed %d predictions", reviewed_count)

    # Recompute stats
    history["performance_stats"] = compute_performance_stats(history)
    return history


def save_new_predictions(
    history: dict,
    holdings_result: dict,
    discovery_result: dict,
    current_prices: dict[str, float],
    today: str | None = None,
    signal_components: dict[str, dict[str, bool]] | None = None,
) -> dict:
    """Extract new predictions from Claude's analysis results and add to history.

    Args:
        history: The predictions history dict
        holdings_result: Claude's holdings analysis result
        discovery_result: Claude's discovery result
        current_prices: Dict mapping ticker to current price
        today: Today's date string

    Returns:
        Updated history dict
    """
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")
    sig_lookup = signal_components or {}

    new_count = 0

    # Extract from holdings analysis
    for h in holdings_result.get("holdings_analysis", []):
        ticker = h.get("ticker", "")
        if not ticker:
            continue
        # Only track actionable predictions
        prediction = h.get("prediction")
        if prediction not in ("UP", "DOWN"):
            continue

        entry_price = current_prices.get(ticker)
        if entry_price is None:
            continue

        pred_id = f"{today}_{ticker}_holdings"
        # Skip if already recorded today
        if any(p["id"] == pred_id for p in history.get("predictions", [])):
            continue

        history.setdefault("predictions", []).append(
            {
                "id": pred_id,
                "date": today,
                "ticker": ticker,
                "name": h.get("name", ""),
                "prediction": prediction,
                "confidence": h.get("confidence", "MEDIUM"),
                "entry_price": round(entry_price, 1),
                "stop_loss": h.get("stop_loss", ""),
                "action": h.get("action", ""),
                "source": "holdings",
                "status": "pending",
                "actual_price": None,
                "actual_return_pct": None,
                "reviewed_date": None,
                "days_held": None,
                "signal_components": sig_lookup.get(ticker, {}),
            }
        )
        new_count += 1

    # Extract from short-term picks
    short_term = discovery_result.get("short_term_picks", [])
    if not short_term:
        short_term = discovery_result.get("recommended_stocks", [])
    for r in short_term:
        ticker = r.get("ticker", "")
        if not ticker:
            continue
        prediction = r.get("prediction")
        if prediction not in ("UP", "DOWN"):
            continue

        entry_price = current_prices.get(ticker)
        if entry_price is None:
            continue

        pred_id = f"{today}_{ticker}_short_term"
        if any(p["id"] == pred_id for p in history.get("predictions", [])):
            continue

        history.setdefault("predictions", []).append(
            {
                "id": pred_id,
                "date": today,
                "ticker": ticker,
                "name": r.get("name", ""),
                "prediction": prediction,
                "confidence": r.get("confidence", "MEDIUM"),
                "entry_price": round(entry_price, 1),
                "expected_move": r.get("expected_move", ""),
                "stop_loss": r.get("stop_loss", ""),
                "target_price": r.get("target_price", ""),
                "entry_strategy": r.get("entry_strategy", ""),
                "source": "short_term",
                "status": "pending",
                "actual_price": None,
                "actual_return_pct": None,
                "reviewed_date": None,
                "days_held": None,
                "signal_components": sig_lookup.get(ticker, {}),
            }
        )
        new_count += 1

    # Extract from long-term picks
    for r in discovery_result.get("long_term_picks", []):
        ticker = r.get("ticker", "")
        if not ticker:
            continue
        prediction = r.get("prediction")
        if prediction not in ("UP", "DOWN"):
            continue

        entry_price = current_prices.get(ticker)
        if entry_price is None:
            continue

        pred_id = f"{today}_{ticker}_long_term"
        if any(p["id"] == pred_id for p in history.get("predictions", [])):
            continue

        history.setdefault("predictions", []).append(
            {
                "id": pred_id,
                "date": today,
                "ticker": ticker,
                "name": r.get("name", ""),
                "prediction": prediction,
                "confidence": r.get("confidence", "MEDIUM"),
                "entry_price": round(entry_price, 1),
                "investment_thesis": r.get("investment_thesis", ""),
                "expected_return": r.get("expected_return", ""),
                "ideal_entry_zone": r.get("ideal_entry_zone", ""),
                "source": "long_term",
                "status": "pending",
                "actual_price": None,
                "actual_return_pct": None,
                "reviewed_date": None,
                "days_held": None,
                "signal_components": sig_lookup.get(ticker, {}),
            }
        )
        new_count += 1

    logger.info("Saved %d new predictions", new_count)

    # Recompute stats
    history["performance_stats"] = compute_performance_stats(history)
    return history


def compute_performance_stats(history: dict) -> dict:
    """Compute accuracy + risk-adjusted P&L metrics from historical predictions.

    All return-based metrics use ``_directional_return`` (= return signed so
    "predicting correctly" is positive), so DOWN-wins don't cancel out
    UP-wins in the averages. The previous version summed raw signed
    returns and reported ``avg_return_wins`` as negative when the
    population was DOWN-heavy — actively misleading the feedback loop.
    """
    predictions = history.get("predictions", [])
    if not predictions:
        return {}

    total = len(predictions)
    wins = [p for p in predictions if p["status"] == "win"]
    losses = [p for p in predictions if p["status"] == "loss"]
    pending = [p for p in predictions if p["status"] == "pending"]
    resolved = wins + losses

    stats: dict = {
        "total_predictions": total,
        "wins": len(wins),
        "losses": len(losses),
        "pending": len(pending),
        "accuracy_pct": round(len(wins) / len(resolved) * 100, 1) if resolved else None,
    }

    # Direction-aware average returns (a DOWN-win with raw -10% → +10
    # directional return, so DOWN-wins are correctly aggregated alongside
    # UP-wins instead of dragging the average toward zero or negative).
    win_dir_returns = [r for r in (_directional_return(p) for p in wins) if r is not None]
    loss_dir_returns = [r for r in (_directional_return(p) for p in losses) if r is not None]
    if win_dir_returns:
        stats["avg_return_wins"] = round(sum(win_dir_returns) / len(win_dir_returns), 2)
    if loss_dir_returns:
        stats["avg_return_losses"] = round(sum(loss_dir_returns) / len(loss_dir_returns), 2)

    # Risk-adjusted P&L: expectancy, profit factor, Sharpe-like, max DD.
    # These answer "are we actually making money?" rather than just "are
    # we right >50% of the time?". A 55% win rate with -2% avg-win and
    # +5% avg-loss is still losing money.
    all_dir_returns = win_dir_returns + loss_dir_returns
    if all_dir_returns:
        mean_r = sum(all_dir_returns) / len(all_dir_returns)
        stats["mean_return_per_trade_pct"] = round(mean_r, 2)
        if len(all_dir_returns) >= 2:
            variance = sum((r - mean_r) ** 2 for r in all_dir_returns) / (len(all_dir_returns) - 1)
            stdev = math.sqrt(variance)
            stats["return_stdev_pct"] = round(stdev, 2)
            # Sharpe-like ratio (per-trade, not annualised). Above 0
            # means positive risk-adjusted return; above ~0.3 is a
            # genuinely good per-trade edge.
            if stdev > 0:
                stats["sharpe_like_per_trade"] = round(mean_r / stdev, 2)
    if win_dir_returns and loss_dir_returns:
        win_rate = len(wins) / len(resolved)
        avg_w = sum(win_dir_returns) / len(win_dir_returns)
        avg_l_abs = abs(sum(loss_dir_returns) / len(loss_dir_returns))
        # Expectancy = average % gained per trade including losers.
        # >0 means each trade is positive-EV on average; <0 means
        # bleeding even before slippage / commissions.
        stats["expectancy_per_trade_pct"] = round(win_rate * avg_w - (1 - win_rate) * avg_l_abs, 2)
        # Profit factor = gross wins / gross losses. >1 means total
        # winning $$ exceeds total losing $$.
        gross_loss = abs(sum(loss_dir_returns))
        if gross_loss > 0:
            stats["profit_factor"] = round(sum(win_dir_returns) / gross_loss, 2)

    # Equity-curve max drawdown — treat each resolved trade as a 1%
    # position size and walk through chronologically. Peak/trough
    # measured against the running cumulative sum, so a series of
    # losing trades shows up as a single drawdown number. Sensitive
    # only to ordering and magnitudes, not annualised.
    if all_dir_returns:
        chrono_resolved = sorted(resolved, key=lambda p: p.get("reviewed_date") or p.get("date", ""))
        chrono_returns = [r for r in (_directional_return(p) for p in chrono_resolved) if r is not None]
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for r in chrono_returns:
            cumulative += r
            peak = max(peak, cumulative)
            drawdown = peak - cumulative
            max_dd = max(max_dd, drawdown)
        stats["max_drawdown_pct"] = round(max_dd, 2)

    # Confidence breakdown (the single most important diagnostic — if
    # HIGH < MEDIUM, the AI is over-using HIGH and needs to tighten its
    # bar). ``format_performance_feedback`` reads this back and emits an
    # explicit warning when inverted.
    confidence_stats: dict[str, dict] = {}
    for conf in ("HIGH", "MEDIUM", "LOW"):
        conf_preds = [p for p in resolved if p.get("confidence") == conf]
        if conf_preds:
            conf_wins = [p for p in conf_preds if p["status"] == "win"]
            confidence_stats[conf] = {
                "total": len(conf_preds),
                "wins": len(conf_wins),
                "accuracy_pct": round(len(conf_wins) / len(conf_preds) * 100, 1),
            }
    if confidence_stats:
        stats["by_confidence"] = confidence_stats

    # Confidence × direction cross-tab. Reveals asymmetric bias — e.g.
    # HIGH-UP could be reliable while HIGH-DOWN is the failure mode (or
    # vice versa). Only emit buckets with ``_MIN_BUCKET_N`` resolved
    # trades so noise doesn't dominate.
    conf_dir_stats: dict[str, dict] = {}
    for conf in ("HIGH", "MEDIUM", "LOW"):
        for direction in ("UP", "DOWN"):
            subset = [p for p in resolved if p.get("confidence") == conf and p.get("prediction") == direction]
            if len(subset) >= _MIN_BUCKET_N:
                sub_wins = [p for p in subset if p["status"] == "win"]
                conf_dir_stats[f"{conf}_{direction}"] = {
                    "total": len(subset),
                    "wins": len(sub_wins),
                    "accuracy_pct": round(len(sub_wins) / len(subset) * 100, 1),
                }
    if conf_dir_stats:
        stats["by_confidence_direction"] = conf_dir_stats

    # Source breakdown — separate holdings / short_term / long_term /
    # discovery so the review prompt can tell which path is dragging.
    for source in ("holdings", "short_term", "long_term", "discovery"):
        src_preds = [p for p in resolved if p.get("source") == source]
        if len(src_preds) >= _MIN_BUCKET_N:
            src_wins = [p for p in src_preds if p["status"] == "win"]
            stats[f"{source}_accuracy_pct"] = round(len(src_wins) / len(src_preds) * 100, 1)

    # Recent trend (last 10 resolved)
    recent = sorted(resolved, key=lambda p: p.get("reviewed_date", ""), reverse=True)[:10]
    if len(recent) >= 3:
        recent_wins = sum(1 for p in recent if p["status"] == "win")
        stats["recent_accuracy_pct"] = round(recent_wins / len(recent) * 100, 1)

    # Strategy drift: compare expectancy on the last 14 resolved trades
    # against the older baseline. A negative delta means today's
    # selection logic / prompt has decayed relative to whatever it
    # was doing earlier — the most actionable early-warning signal we
    # can compute from in-band data. Requires enough samples on both
    # sides so a stray run doesn't trigger the alarm.
    drift = _compute_drift_indicator(resolved, recent_n=14, min_recent=5, min_baseline=10)
    if drift is not None:
        stats["drift_indicator"] = drift

    # Best and worst predictions — keep using raw return for the
    # ticker-level highlight so the date+name pair is interpretable
    # without explaining direction adjustment.
    if resolved:
        best = max(resolved, key=lambda p: p.get("actual_return_pct", 0))
        worst = min(resolved, key=lambda p: p.get("actual_return_pct", 0))
        stats["best_prediction"] = {
            "ticker": best["ticker"],
            "name": best.get("name", ""),
            "return_pct": best.get("actual_return_pct"),
            "date": best["date"],
        }
        stats["worst_prediction"] = {
            "ticker": worst["ticker"],
            "name": worst.get("name", ""),
            "return_pct": worst.get("actual_return_pct"),
            "date": worst["date"],
        }

    return stats


def compute_signal_efficacy(history: dict, min_samples: int = 5) -> dict[str, dict]:
    """For each screening signal, compute the realised win rate of predictions
    whose entry was scored with that signal firing.

    Only resolved predictions (status ∈ {"win", "loss"}) that carry a
    ``signal_components`` payload are counted. Below ``min_samples``
    resolved trades, a signal is suppressed from the result to avoid
    noise-driven conclusions. Output shape::

        {
            "volume_spike": {
                "with_signal": {"total": 12, "wins": 9, "accuracy_pct": 75.0},
                "without_signal": {"total": 40, "wins": 18, "accuracy_pct": 45.0},
                "lift_pct": 30.0,   # wins% with - wins% without
            },
            ...
        }

    The ``lift_pct`` is the most actionable column for tuning weights:
    positive means "predictions WITH this signal won more often than
    those without". Strong positive lift → weight up; negative → weight
    down or drop.
    """
    resolved = [
        p
        for p in history.get("predictions", [])
        if p.get("status") in ("win", "loss") and isinstance(p.get("signal_components"), dict)
    ]
    if not resolved:
        return {}

    # Collect every signal we've ever recorded so absence vs presence
    # can be reported per signal.
    all_signals: set[str] = set()
    for p in resolved:
        for name, fired in p["signal_components"].items():
            if fired:
                all_signals.add(name)

    result: dict[str, dict] = {}
    for signal in sorted(all_signals):
        with_signal = [p for p in resolved if p["signal_components"].get(signal)]
        without_signal = [p for p in resolved if not p["signal_components"].get(signal)]
        if len(with_signal) < min_samples or len(without_signal) < min_samples:
            continue
        with_wins = sum(1 for p in with_signal if p["status"] == "win")
        without_wins = sum(1 for p in without_signal if p["status"] == "win")
        with_acc = with_wins / len(with_signal) * 100
        without_acc = without_wins / len(without_signal) * 100
        result[signal] = {
            "with_signal": {
                "total": len(with_signal),
                "wins": with_wins,
                "accuracy_pct": round(with_acc, 1),
            },
            "without_signal": {
                "total": len(without_signal),
                "wins": without_wins,
                "accuracy_pct": round(without_acc, 1),
            },
            "lift_pct": round(with_acc - without_acc, 1),
        }
    return result


def format_signal_efficacy(efficacy: dict[str, dict]) -> str:
    """Render signal efficacy as a prompt-injection block.

    Lift_pct > 0 → 「この signal は実勝率を押し上げている」、
    lift_pct < 0 → 「この signal は実勝率を下げているので外す候補」.
    The weekly review prompt consumes this to tune screening_weights
    using data instead of intuition.
    """
    if not efficacy:
        return ""
    lines = ["=== シグナル別 実勝率 (signal efficacy) ==="]
    lines.append("各 screening signal について、その signal が fire した予測の勝率と、")
    lines.append("fire しなかった予測の勝率を比較。lift = with - without (正なら有効、負なら有害)")
    sorted_signals = sorted(efficacy.items(), key=lambda kv: kv[1]["lift_pct"], reverse=True)
    for signal, stats in sorted_signals:
        ws = stats["with_signal"]
        wo = stats["without_signal"]
        lift = stats["lift_pct"]
        icon = "📈" if lift > 5 else ("📉" if lift < -5 else "➖")
        lines.append(
            f"{icon} {signal}: with {ws['accuracy_pct']}%({ws['total']}件) "
            f"vs without {wo['accuracy_pct']}%({wo['total']}件) → lift {lift:+.1f}%"
        )
    lines.append("→ lift が大きい signal の重みを screening_weights で増やし、負の signal は重みを下げてください")
    return "\n".join(lines)


def _compute_drift_indicator(
    resolved: list[dict],
    recent_n: int = 14,
    min_recent: int = 5,
    min_baseline: int = 10,
    drift_threshold_pp: float = 2.0,
) -> dict | None:
    """Compare per-trade expectancy on the latest ``recent_n`` resolved
    trades against the older baseline.

    Returns ``None`` when either side has fewer than its minimum sample
    count — drift conclusions on tiny windows are noise. Otherwise
    returns ``{"recent_expectancy_pct", "baseline_expectancy_pct",
    "delta_pp", "is_drift", "recent_n", "baseline_n"}`` and the
    ``format_performance_feedback`` prompt block emits a ⚠ when
    ``is_drift`` is True (= recent expectancy is more than
    ``drift_threshold_pp`` percentage points below baseline).

    The motivation: any prompt / scoring change that *seems* fine on
    the unit tests but quietly biases picks toward marginal setups
    will show up here within a couple of weeks of cron runs. Without
    this signal the cron stays green and the operator only notices
    weeks later when accuracy reports look bad in aggregate.
    """
    chrono = sorted(
        (p for p in resolved if p.get("reviewed_date") and _directional_return(p) is not None),
        key=lambda p: p["reviewed_date"],
    )
    if len(chrono) < min_recent + min_baseline:
        return None
    recent = chrono[-recent_n:]
    baseline = chrono[: max(0, len(chrono) - recent_n)]
    if len(recent) < min_recent or len(baseline) < min_baseline:
        return None
    recent_returns = [_directional_return(p) for p in recent]
    baseline_returns = [_directional_return(p) for p in baseline]
    # Mypy-friendly: every entry passed the None filter above.
    recent_exp = sum(r for r in recent_returns if r is not None) / len(recent)
    baseline_exp = sum(r for r in baseline_returns if r is not None) / len(baseline)
    delta_pp = recent_exp - baseline_exp
    return {
        "recent_expectancy_pct": round(recent_exp, 2),
        "baseline_expectancy_pct": round(baseline_exp, 2),
        "delta_pp": round(delta_pp, 2),
        "is_drift": delta_pp < -drift_threshold_pp,
        "recent_n": len(recent),
        "baseline_n": len(baseline),
    }


def extract_few_shot_examples(
    history: dict,
    n_wins: int = 3,
    n_losses: int = 3,
    min_directional_return: float = 5.0,
) -> dict[str, list[dict]]:
    """Pick the most informative resolved trades for prompt embedding.

    Returns ``{"wins": [...], "losses": [...]}`` ranked by the magnitude
    of directional return so DOWN-wins (raw negative return, positive
    directional return) are included alongside UP-wins. The previous
    in-line "成功パターン" block sorted on raw ``actual_return_pct``
    which silently dropped every DOWN-win — direction-aware ranking
    fixes that.

    ``min_directional_return`` filters out marginal outcomes (±3% to
    ±5%) that resolved as wins/losses on the technicality of the
    review window. Examples with weak magnitude don't teach the AI
    much — we want to surface trades where the win/loss reason is
    likely to be in the data the AI saw at entry.

    Each example carries the signal fingerprint that fired at entry
    (``signal_components`` keys whose value was truthy), the
    directional return for compactness, and identifying metadata so
    the AI can pattern-match a current candidate against it.
    """
    resolved = [p for p in history.get("predictions", []) if p.get("status") in ("win", "loss")]
    if not resolved:
        return {"wins": [], "losses": []}

    def example_payload(p: dict) -> dict:
        sig = p.get("signal_components") or {}
        fired = sorted(name for name, on in sig.items() if on)
        return {
            "ticker": p.get("ticker", ""),
            "name": p.get("name", ""),
            "direction": p.get("prediction", ""),
            "confidence": p.get("confidence", ""),
            "date": p.get("date", ""),
            "reviewed_date": p.get("reviewed_date", ""),
            "entry_price": p.get("entry_price"),
            "actual_return_pct": p.get("actual_return_pct"),
            "directional_return_pct": _directional_return(p),
            "days_held": p.get("days_held"),
            "source": p.get("source", ""),
            "fired_signals": fired,
        }

    wins_pool = []
    losses_pool = []
    for p in resolved:
        dr = _directional_return(p)
        if dr is None:
            continue
        if abs(dr) < min_directional_return:
            continue
        payload = example_payload(p)
        if p["status"] == "win":
            wins_pool.append(payload)
        else:
            losses_pool.append(payload)

    # Wins: directional return descending (best first). Losses: ascending
    # (worst, i.e. most-negative directional, first — these resolve to
    # large adverse moves against the predicted direction).
    wins = sorted(wins_pool, key=lambda x: x["directional_return_pct"] or 0, reverse=True)[:n_wins]
    losses = sorted(losses_pool, key=lambda x: x["directional_return_pct"] or 0)[:n_losses]
    return {"wins": wins, "losses": losses}


def format_few_shot_for_prompt(examples: dict[str, list[dict]]) -> str:
    """Render extracted examples as a prompt-injection block.

    The structure is deliberately parallel for wins and losses so the
    AI can do a side-by-side comparison. Each line includes the firing
    signal fingerprint, which is the most actionable feature: a
    current candidate firing the same signals as a past loss should
    raise confidence-downgrade caution.
    """
    wins = examples.get("wins") or []
    losses = examples.get("losses") or []
    if not wins and not losses:
        return ""

    lines: list[str] = []
    if wins:
        lines.append("\n成功パターン（同じ条件の picks は信頼度を上げて良い）:")
        for ex in wins:
            lines.append(_format_one_example(ex, is_win=True))
    if losses:
        lines.append("\n失敗パターン（同じ条件の picks は entry 回避 or 信頼度を下げる）:")
        for ex in losses:
            lines.append(_format_one_example(ex, is_win=False))
    lines.append(
        "\n本日の各 pick について、上記のどの成功/失敗パターンに最も似ているか暗黙に判定し、"
        "似ているパターンの結果に応じて信頼度を調整してください。signal の組合せが同じなら結果も近づく傾向があります。"
    )
    return "\n".join(lines)


def _format_one_example(ex: dict, is_win: bool) -> str:
    """Single line in the few-shot block. Kept here so wins/losses stay parallel."""
    dr = ex.get("directional_return_pct")
    raw = ex.get("actual_return_pct")
    days = ex.get("days_held")
    direction = ex.get("direction", "")
    confidence = ex.get("confidence", "?")
    fired = ex.get("fired_signals") or []
    sig_text = ", ".join(fired) if fired else "(シグナル記録なし)"
    days_text = f"{days}日保有" if days else ""
    # Show raw return too — when direction is DOWN, "+10% directional"
    # corresponds to "-10% actual price move", and the AI needs to see
    # both numbers to map the lesson back onto a UP/DOWN candidate.
    raw_text = f"raw {raw:+.1f}%" if isinstance(raw, (int, float)) else ""
    dr_text = f"方向調整 {dr:+.1f}%" if isinstance(dr, (int, float)) else ""
    arrow = "✅" if is_win else "❌"
    return (
        f"  {arrow} {ex.get('name', '')} ({ex.get('ticker', '')}) "
        f"{direction}/{confidence} {dr_text} ({raw_text} {days_text}) "
        f"[{ex.get('date', '')}] signals=[{sig_text}]"
    )


def format_performance_feedback(history: dict) -> str:
    """Format performance data into text for Claude's prompt.

    This is the key feedback loop: Claude sees its past accuracy
    and adjusts its analysis accordingly.
    """
    stats = history.get("performance_stats", {})

    if not stats or stats.get("total_predictions", 0) == 0:
        return ""

    resolved_count = stats.get("wins", 0) + stats.get("losses", 0)
    if resolved_count == 0:
        pending = stats.get("pending", 0)
        if pending > 0:
            return f"\n=== 予測トラッキング ===\n現在{pending}件の予測を追跡中（結果待ち）"
        return ""

    lines = ["=== あなたの過去の予測パフォーマンス ==="]
    lines.append(
        f"通算成績: {stats['wins']}勝 {stats['losses']}敗 "
        f"(勝率{stats.get('accuracy_pct', 0)}%) "
        f"保留{stats.get('pending', 0)}件"
    )

    # Risk-adjusted P&L — answers "are these picks actually profitable?".
    # Expectancy <=0 means even the winning trades aren't covering the
    # losing trades on average; profit_factor < 1.0 means total gain $$
    # is less than total loss $$. Surface these explicitly so the AI
    # doesn't lean on accuracy_pct alone.
    exp = stats.get("expectancy_per_trade_pct")
    pf = stats.get("profit_factor")
    sharpe = stats.get("sharpe_like_per_trade")
    max_dd = stats.get("max_drawdown_pct")
    risk_parts: list[str] = []
    if exp is not None:
        risk_parts.append(f"期待値 {exp:+.2f}%/件")
    if pf is not None:
        risk_parts.append(f"PF {pf:.2f}")
    if sharpe is not None:
        risk_parts.append(f"Sharpe {sharpe:+.2f}")
    if max_dd is not None:
        risk_parts.append(f"最大DD {max_dd:.1f}%")
    if risk_parts:
        lines.append("リスク調整後: " + " / ".join(risk_parts))
        if exp is not None and exp <= 0:
            lines.append("  ⚠ 期待値が 0 以下 = 平均で損失方向。勝率より「勝ち幅 > 負け幅」を優先してください")
        if pf is not None and pf < 1.0:
            lines.append("  ⚠ プロフィットファクター < 1 = 累積 P&L マイナス。損切り徹底 + 利益拡大を意識")

    # Confidence breakdown — same data as before, but now we surface
    # calibration inversion explicitly so the AI can self-correct.
    by_conf = stats.get("by_confidence", {})
    if by_conf:
        conf_parts = []
        for conf in ("HIGH", "MEDIUM", "LOW"):
            c = by_conf.get(conf)
            if c:
                conf_parts.append(f"{conf}={c['accuracy_pct']}%({c['total']}件)")
        if conf_parts:
            lines.append(f"信頼度別的中率: {' / '.join(conf_parts)}")
        # Calibration inversion warning. HIGH being less accurate than
        # MEDIUM is the strongest signal that the AI is over-using HIGH
        # for noisy / strong-looking setups. The fix needs to come from
        # the AI itself; this warning forces it into view every prompt.
        high = by_conf.get("HIGH", {})
        medium = by_conf.get("MEDIUM", {})
        if (
            high.get("accuracy_pct") is not None
            and medium.get("accuracy_pct") is not None
            and high.get("total", 0) >= 5
            and medium.get("total", 0) >= 5
            and high["accuracy_pct"] < medium["accuracy_pct"]
        ):
            lines.append(
                f"  ⚠ キャリブレーション逆転: HIGH({high['accuracy_pct']}%) < MEDIUM({medium['accuracy_pct']}%)。"
                "HIGHの判定基準が緩い可能性 — テクニカル+ファンダ+需給の全一致を確認してから HIGH を付与してください。"
                "迷ったら MEDIUM 以下に下げる方が結果的に当たります。"
            )

    # Confidence × direction breakdown (only when meaningful). Reveals
    # whether the AI's bias is direction-specific (e.g. HIGH-UP solid
    # but HIGH-DOWN failing) so the targeted fix can be smaller.
    by_cd = stats.get("by_confidence_direction", {})
    if by_cd:
        cd_parts = [f"{key}={val['accuracy_pct']}%({val['total']}件)" for key, val in sorted(by_cd.items())]
        lines.append(f"信頼度×方向: {' / '.join(cd_parts)}")

    # Source breakdown — which earning path (holdings / short_term /
    # long_term / discovery) is the weak link?
    src_parts: list[str] = []
    for src in ("holdings", "short_term", "long_term", "discovery"):
        v = stats.get(f"{src}_accuracy_pct")
        if v is not None:
            src_parts.append(f"{src}={v}%")
    if src_parts:
        lines.append(f"ソース別的中率: {' / '.join(src_parts)}")

    # Average returns (now direction-aware: wins should be positive,
    # losses negative — if not, the fix in compute_performance_stats
    # didn't take).
    avg_w = stats.get("avg_return_wins")
    avg_l = stats.get("avg_return_losses")
    if avg_w is not None and avg_l is not None:
        lines.append(f"方向調整リターン: 的中時 {avg_w:+.1f}% / 外れ時 {avg_l:+.1f}%")
        if avg_w + avg_l < 0:
            # Even on equal win/loss rates this means each round-trip
            # loses money — risk-reward asymmetry needs widening.
            lines.append("  ⚠ 勝ち幅 < 負け幅 (リスクリワード逆転)。損切りを早めるか、利確を遅らせてください")

    # Recent trend
    recent_acc = stats.get("recent_accuracy_pct")
    overall_acc = stats.get("accuracy_pct")
    if recent_acc is not None and overall_acc is not None:
        if recent_acc > overall_acc + 5:
            lines.append(f"直近トレンド: 改善中（直近{recent_acc}% vs 通算{overall_acc}%）")
        elif recent_acc < overall_acc - 5:
            lines.append(f"直近トレンド: 悪化中（直近{recent_acc}% vs 通算{overall_acc}%）")

    # Strategy-drift early warning. Expectancy (not accuracy) is the
    # right metric here: a strategy can stay 55% accurate but bleed
    # money if win-size shrinks while loss-size grows. ⚠ fires on a
    # 2 percentage-point drop vs older baseline — small enough to
    # catch quietly but large enough to be statistically meaningful
    # on ~14 vs ~10+ samples.
    drift = stats.get("drift_indicator")
    if drift is not None:
        lines.append(
            f"戦略ドリフト: 直近{drift['recent_n']}件期待値 {drift['recent_expectancy_pct']:+.2f}%/件 "
            f"vs ベースライン{drift['baseline_n']}件 {drift['baseline_expectancy_pct']:+.2f}%/件 "
            f"({drift['delta_pp']:+.2f}pp)"
        )
        if drift.get("is_drift"):
            lines.append(
                "  ⚠ 直近の期待値がベースラインから 2pp 以上低下。最近の判断バイアスが効いている可能性。"
                "今回は新規 HIGH の付与をさらに厳格にし、迷ったら MEDIUM に下げてください"
            )

    # Direction-aware few-shot examples with signal fingerprints. The
    # previous in-line "成功パターン" sorted on raw return descending,
    # which silently dropped every DOWN-win (raw negative); ranking
    # by ``directional_return_pct`` magnitude fixes that. Including
    # the firing-signal list per example lets the AI pattern-match
    # current candidates ("this pick fires the same signals as a
    # past loser → downgrade confidence") instead of just seeing a
    # ticker / return / date triple.
    few_shot_block = format_few_shot_for_prompt(extract_few_shot_examples(history))
    if few_shot_block:
        lines.append(few_shot_block)

    return "\n".join(lines)


def get_current_prices_from_data(
    holdings_data: dict[str, object],
    screening_data: dict[str, object],
) -> dict[str, float]:
    """Extract current prices from already-fetched DataFrames.

    Avoids re-fetching data just for price checks.
    """
    prices: dict[str, float] = {}
    for ticker, df in {**holdings_data, **screening_data}.items():
        try:
            close = df["Close"]  # type: ignore[index]
            if len(close) > 0:
                prices[ticker] = float(close.iloc[-1])
        except Exception:
            pass
    return prices
