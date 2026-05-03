from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"
_HISTORY_FILE = str(_DATA_DIR / "predictions_history.json")
_REVIEW_WINDOW_DAYS = 14  # Evaluate predictions after 2 weeks
_MIN_REVIEW_DAYS = 5  # Start checking after 5 trading days


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

        history.setdefault("predictions", []).append({
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
        })
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

        history.setdefault("predictions", []).append({
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
        })
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

        history.setdefault("predictions", []).append({
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
        })
        new_count += 1

    logger.info("Saved %d new predictions", new_count)

    # Recompute stats
    history["performance_stats"] = compute_performance_stats(history)
    return history


def compute_performance_stats(history: dict) -> dict:
    """Compute accuracy metrics from historical predictions."""
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

    # Average returns
    if wins:
        stats["avg_return_wins"] = round(
            sum(p.get("actual_return_pct", 0) for p in wins) / len(wins), 2
        )
    if losses:
        stats["avg_return_losses"] = round(
            sum(p.get("actual_return_pct", 0) for p in losses) / len(losses), 2
        )

    # Confidence breakdown
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

    # Source breakdown (holdings vs discovery)
    for source in ("holdings", "discovery"):
        src_preds = [p for p in resolved if p.get("source") == source]
        if src_preds:
            src_wins = [p for p in src_preds if p["status"] == "win"]
            stats[f"{source}_accuracy_pct"] = round(
                len(src_wins) / len(src_preds) * 100, 1
            )

    # Recent trend (last 10 resolved)
    recent = sorted(resolved, key=lambda p: p.get("reviewed_date", ""), reverse=True)[:10]
    if len(recent) >= 3:
        recent_wins = sum(1 for p in recent if p["status"] == "win")
        stats["recent_accuracy_pct"] = round(recent_wins / len(recent) * 100, 1)

    # Best and worst predictions
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


def format_performance_feedback(history: dict) -> str:
    """Format performance data into text for Claude's prompt.

    This is the key feedback loop: Claude sees its past accuracy
    and adjusts its analysis accordingly.
    """
    stats = history.get("performance_stats", {})
    predictions = history.get("predictions", [])

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

    # Confidence breakdown
    by_conf = stats.get("by_confidence", {})
    if by_conf:
        conf_parts = []
        for conf in ("HIGH", "MEDIUM", "LOW"):
            c = by_conf.get(conf)
            if c:
                conf_parts.append(f"{conf}={c['accuracy_pct']}%({c['total']}件)")
        if conf_parts:
            lines.append(f"信頼度別的中率: {' / '.join(conf_parts)}")

    # Average returns
    avg_w = stats.get("avg_return_wins")
    avg_l = stats.get("avg_return_losses")
    if avg_w is not None and avg_l is not None:
        lines.append(f"平均リターン: 的中時 {avg_w:+.1f}% / 外れ時 {avg_l:+.1f}%")

    # Recent trend
    recent_acc = stats.get("recent_accuracy_pct")
    overall_acc = stats.get("accuracy_pct")
    if recent_acc is not None and overall_acc is not None:
        if recent_acc > overall_acc + 5:
            lines.append(f"直近トレンド: 改善中（直近{recent_acc}% vs 通算{overall_acc}%）")
        elif recent_acc < overall_acc - 5:
            lines.append(f"直近トレンド: 悪化中（直近{recent_acc}% vs 通算{overall_acc}%）")

    # Recent losses (for learning)
    recent_losses = sorted(
        [p for p in predictions if p["status"] == "loss"],
        key=lambda p: p.get("reviewed_date", ""),
        reverse=True,
    )[:3]
    if recent_losses:
        lines.append("\n直近の失敗（反省材料）:")
        for p in recent_losses:
            lines.append(
                f"  - {p.get('name', '')} ({p['ticker']}): "
                f"{p['prediction']}予測→実際{p.get('actual_return_pct', 0):+.1f}% "
                f"[{p['date']}] 信頼度:{p.get('confidence', '?')}"
            )

    # Recent wins (what worked)
    recent_wins = sorted(
        [p for p in predictions if p["status"] == "win"],
        key=lambda p: p.get("actual_return_pct", 0),
        reverse=True,
    )[:3]
    if recent_wins:
        lines.append("\n成功パターン（継続すべき判断）:")
        for p in recent_wins:
            lines.append(
                f"  - {p.get('name', '')} ({p['ticker']}): "
                f"{p['prediction']}予測→実際{p.get('actual_return_pct', 0):+.1f}% "
                f"[{p['date']}] 信頼度:{p.get('confidence', '?')}"
            )

    lines.append(
        "\n上記の反省と成功パターンを踏まえ、同じ失敗を繰り返さず、"
        "成功パターンを強化する分析をしてください。"
    )

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
