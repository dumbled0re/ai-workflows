from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"
_HISTORY_FILE = str(_DATA_DIR / "predictions_history.json")
_MIN_REVIEW_DAYS = 5  # Start checking after 5 trading days

# Source-aware review windows. The original 14-day blanket window
# resolved long_term picks (3-12 month thesis) as wins/losses far too
# early — a 6-month value play tagged as "loss" at day 14 because it
# hadn't moved +/-3% yet pollutes every downstream metric (expectancy,
# calibration, signal_efficacy, drift). Holding short-term horizons
# at 14d preserves the original behaviour for swings; long_term gets
# the 90 days it actually needs before a verdict is meaningful.
_REVIEW_WINDOW_DAYS_BY_SOURCE = {
    "holdings": 14,
    "short_term": 14,
    "discovery": 14,  # legacy schema before holdings/short/long split
    "long_term": 90,
}
_DEFAULT_REVIEW_WINDOW_DAYS = 14

# |return_pct| がこの閾値以上のとき、株式分割 / 併合を疑って split factor を
# 照会する。entry_price は予測時の生値で記録されるため、途中で分割が入ると
# 「住友商事が 5 日で -75%」のようなゴミ return が win/loss・期待値・DD・
# drift 判定すべてに混入する (2026-07-04 監査で 8053.T / 4452.T の実害を確認)。
# 大型 move のみ照会するのは API 呼び出しを稀にするため — 1.25:1 以上の
# 分割はこの閾値で必ず引っかかる。
_SPLIT_CHECK_THRESHOLD_PCT = 20.0

# universe から外れて価格が取れなくなった pending は、この猶予日数を
# review window に加えた日数で強制 expire する。放置すると永久 pending
# (= metrics から静かに消える生存者バイアス) になる。
_PENDING_EXPIRY_GRACE_DAYS = 30

# Minimum resolved trades before reporting a sub-bucket (HIGH/MEDIUM, by
# source, by confidence × direction). Below this, accuracy_pct is too
# noisy to drive Claude's self-improvement decisions.
_MIN_BUCKET_N = 5

# HIGH-ban drawdown discipline の判定窓。通算累積和の drawdown
# (current_drawdown_pct) は全期間 peak 基準のため一度深く沈むと数百 trade
# 分の利益がないと回復せず、「HIGH 恒久禁止」の吸収状態を作っていた
# (2026-07-04 監査: current DD 199.7pp で probation 再試験が構造的に不可能)。
# 直近 N trade の窓内 drawdown なら losing streak を検知しつつ自然回復する。
_RECENT_DD_WINDOW = 30

# Predictions whose ``prediction`` field equals one of these are
# considered "no directional bet" — the AI explicitly declined to call
# a direction. They never enter the history (no future price comparison
# can resolve a non-directional pick win/loss) and they never count
# against the AI's hit rate. Codex review (2026-05-15) flagged that
# forcing UP/DOWN on every holding was the structural driver of the
# 46.8% UP-prediction hit rate.
_NO_DIRECTION_PREDICTIONS = {"NO_TRADE", "NEUTRAL"}

# UP-gate configuration. When the AI's recent UP predictions are
# winning less than this fraction, phase_prepare prepends a hard
# directive blocking new short_term UP picks. Threshold is 50% because
# UP/DOWN is a binary choice — below random implies real bias rather
# than noise. ``_UP_GATE_MIN_SAMPLES`` is the minimum recent UP count
# we require before trusting the rate (12 samples × 50% = 6 wins;
# below this a single coin-flip swings the gate).
_UP_GATE_THRESHOLD_PCT = 50.0
_UP_GATE_RECENT_N = 20
_UP_GATE_MIN_SAMPLES = 12

# confidence label → 確率 mapping (codex canonical, issue #46 Phase 2)。
# Brier score 計算と reliability diagram で共有。
_CONF_PROB_MAP = {"HIGH": 0.75, "MEDIUM": 0.65, "LOW": 0.55}

# 市場相対採点のベンチマーク (2026-07-04 監査 follow-up)。TOPIX 連動 ETF。
# 「予測が当たった」だけでは利益にならない — 同じ期間に指数を買うだけで
# 得られた return を超えた分 (dir excess) だけが銘柄選択の付加価値。
# 絶対 return 採点だと下落相場の DOWN 的中 (= β) をスキルと誤認する。
_BENCHMARK_TICKER = "1306.T"


def _compute_confidence_buckets(preds: list[dict]) -> dict[str, dict]:
    """Accuracy + Brier per confidence tier over the given *resolved* preds.

    Factored out so the calibration zone can judge the **recent** window
    with the same math the lifetime ``by_confidence`` stat uses. The
    lifetime bucket is frozen the moment a tier stops being emitted (e.g.
    HIGH suppressed by a Red zone), so the zone must look at a rolling
    window to ever detect recovery — see ``_compute_calibration_zone``.
    """
    out: dict[str, dict] = {}
    for conf in ("HIGH", "MEDIUM", "LOW"):
        conf_preds = [p for p in preds if p.get("confidence") == conf]
        if not conf_preds:
            continue
        wins = sum(1 for p in conf_preds if p.get("status") == "win")
        prob = _CONF_PROB_MAP[conf]
        brier = sum((prob - (1.0 if p.get("status") == "win" else 0.0)) ** 2 for p in conf_preds) / len(conf_preds)
        out[conf] = {
            "total": len(conf_preds),
            "wins": wins,
            "accuracy_pct": round(wins / len(conf_preds) * 100, 1),
            "brier_score": round(brier, 3),
            "predicted_prob": prob,
        }
    return out


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


def _lookup_close(prices: dict[str, float], date_str: str, max_lookback_days: int = 7) -> float | None:
    """``date_str`` 以前で最も近い営業日の終値を返す (休日 / 祝日 fallback)。"""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    for i in range(max_lookback_days + 1):
        key = (d - timedelta(days=i)).strftime("%Y-%m-%d")
        close = prices.get(key)
        if close:
            return close
    return None


def _benchmark_window_return(prices: dict[str, float], start_date: str, end_date: str) -> float | None:
    """ベンチマークの start→end 期間 return (%)。どちらか欠損なら None。"""
    start = _lookup_close(prices, start_date)
    end = _lookup_close(prices, end_date)
    if start and end and start > 0:
        return (end - start) / start * 100
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
    split_factor_fn: Callable[[str, str], float] | None = None,
    benchmark_prices: dict[str, float] | None = None,
) -> dict:
    """Check past predictions against current prices, update statuses.

    Args:
        history: The predictions history dict
        current_prices: Dict mapping ticker to current price
        today: Today's date string (YYYY-MM-DD), defaults to now
        split_factor_fn: Optional ``(ticker, since_date) -> factor`` で、
            since_date 以降の累積分割比を返す (4:1 分割 → 4.0、無し → 1.0)。
            |return| が ``_SPLIT_CHECK_THRESHOLD_PCT`` 以上のときだけ照会し、
            entry_price を分割後スケールに補正する。None なら従来挙動。
        benchmark_prices: Optional ``{YYYY-MM-DD: close}`` のベンチマーク
            (TOPIX ETF) 日次終値。解決時に同一期間の市場 return を
            ``benchmark_return_pct`` として記録し、市場相対採点
            (スキル vs β の分離) を可能にする。

    Returns:
        Updated history dict
    """
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")
    today_dt = datetime.strptime(today, "%Y-%m-%d")

    reviewed_count = 0
    expired_count = 0
    for pred in history.get("predictions", []):
        if pred["status"] != "pending":
            continue

        pred_date = datetime.strptime(pred["date"], "%Y-%m-%d")
        days_elapsed = (today_dt - pred_date).days

        if days_elapsed < _MIN_REVIEW_DAYS:
            continue

        review_window = _REVIEW_WINDOW_DAYS_BY_SOURCE.get(pred.get("source", ""), _DEFAULT_REVIEW_WINDOW_DAYS)

        ticker = pred["ticker"]
        current_price = current_prices.get(ticker)
        if current_price is None:
            # 価格が取れない (universe から外れた / 上場廃止 等)。window +
            # 猶予を過ぎたら expired として確定させ、永久 pending による
            # 生存者バイアスを防ぐ。expired は win/loss どちらにも数えない。
            if days_elapsed >= review_window + _PENDING_EXPIRY_GRACE_DAYS:
                pred["status"] = "expired"
                pred["reviewed_date"] = today
                pred["days_held"] = days_elapsed
                pred["expire_reason"] = "price_unavailable"
                expired_count += 1
            continue

        entry_price = pred.get("entry_price")
        if entry_price is None or entry_price == 0:
            continue

        return_pct = ((current_price - entry_price) / entry_price) * 100

        # 大型 move は分割 / 併合を疑う。事後調整された entry と現在価格の
        # スケール不一致を補正してから win/loss を判定する。
        if split_factor_fn is not None and abs(return_pct) >= _SPLIT_CHECK_THRESHOLD_PCT:
            try:
                factor = float(split_factor_fn(ticker, pred["date"]))
            except Exception:
                logger.warning("split factor lookup failed for %s — using raw prices", ticker, exc_info=True)
                factor = 1.0
            if factor > 0 and abs(factor - 1.0) > 1e-9:
                entry_price = entry_price / factor
                return_pct = ((current_price - entry_price) / entry_price) * 100
                pred["split_factor"] = round(factor, 4)
                pred["entry_price_split_adjusted"] = round(entry_price, 2)
                logger.info(
                    "Split-adjusted %s: factor=%.4f, adjusted return %.2f%%",
                    ticker,
                    factor,
                    return_pct,
                )

        pred["actual_price"] = round(current_price, 1)
        pred["actual_return_pct"] = round(return_pct, 2)
        pred["reviewed_date"] = today
        pred["days_held"] = days_elapsed

        # 同一期間の市場 return を記録 (市場相対採点用)。win/loss 判定には
        # 使わない — 判定基準を変えると過去との比較可能性が壊れるため、
        # excess は stats 側で別軸として計算する。
        if benchmark_prices:
            bench_ret = _benchmark_window_return(benchmark_prices, pred["date"], today)
            if bench_ret is not None:
                pred["benchmark_return_pct"] = round(bench_ret, 2)

        # Determine outcome
        prediction_direction = pred.get("prediction", "UP")
        if prediction_direction == "UP":
            if return_pct >= 3.0:
                pred["status"] = "win"
            elif return_pct <= -3.0:
                pred["status"] = "loss"
            elif days_elapsed >= review_window:
                # Expired: marginal result
                pred["status"] = "win" if return_pct > 0 else "loss"
            # else: still pending, wait longer
        elif prediction_direction == "DOWN":
            if return_pct <= -3.0:
                pred["status"] = "win"
            elif return_pct >= 3.0:
                pred["status"] = "loss"
            elif days_elapsed >= review_window:
                pred["status"] = "win" if return_pct < 0 else "loss"

        if pred["status"] != "pending":
            reviewed_count += 1

    if reviewed_count > 0:
        logger.info("Reviewed %d predictions", reviewed_count)
    if expired_count > 0:
        logger.info("Expired %d stale pending predictions (price unavailable)", expired_count)

    # Recompute stats
    history["performance_stats"] = compute_performance_stats(history)
    return history


def _has_open_thesis(history: dict, ticker: str, source: str, prediction: str) -> bool:
    """同一 (ticker, source, direction) の pending が既に存在するか。

    存在する間は新しい予測を記録しない = **thesis 単位の記録** (2026-07-04
    監査 follow-up)。旧実装は同じ建玉に毎日同方向の予測を貼り直しており、
    解決も同じ価格で同時に起きるため、学習ループに強相関サンプルが流れて
    名目 n を水増ししていた (6 月の holdings 300 件の実体は 25 銘柄)。

    方向が変わった場合は「新しい thesis」として記録される (旧 pending は
    自然に解決を待つ)。解決済みになった後の再予測も新 thesis として扱う。
    """
    return any(
        p.get("status") == "pending"
        and p.get("ticker") == ticker
        and p.get("source") == source
        and p.get("prediction") == prediction
        for p in history.get("predictions", [])
    )


def save_new_predictions(
    history: dict,
    holdings_result: dict,
    discovery_result: dict,
    current_prices: dict[str, float],
    today: str | None = None,
    signal_components: dict[str, dict[str, bool]] | None = None,
    pre_entry_metrics: dict[str, dict[str, float]] | None = None,
    regime: str | None = None,
    critic_decisions: dict[str, str] | None = None,
) -> dict:
    """Extract new predictions from Claude's analysis results and add to history.

    Args:
        history: The predictions history dict
        holdings_result: Claude's holdings analysis result
        discovery_result: Claude's discovery result
        current_prices: Dict mapping ticker to current price
        today: Today's date string
        signal_components: ticker → {signal_name: True/False} dict
        pre_entry_metrics: ticker → {"price_change_5d", "price_change_1m",
            "price_change_3m", "rsi_14", "trailing_pe", ...} 等の事前指標。
            Phase 5 #46 で追加: HIGH bucket の overpriced bias 検証用 (D 仮説 4)。
            None で従来通り (省略可)。

    Returns:
        Updated history dict
    """
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")
    sig_lookup = signal_components or {}
    pem_lookup = pre_entry_metrics or {}

    new_count = 0
    skipped_open_thesis = 0

    # Extract from holdings analysis
    for h in holdings_result.get("holdings_analysis", []):
        ticker = h.get("ticker", "")
        if not ticker:
            continue
        # Only track directional bets. ``NO_TRADE`` / ``NEUTRAL`` are
        # the AI's explicit "no directional view this run" output —
        # there's nothing to verify against a future price, and
        # counting them dilutes the calibration sample.
        prediction = h.get("prediction")
        if prediction in _NO_DIRECTION_PREDICTIONS:
            continue
        if prediction not in ("UP", "DOWN"):
            continue

        entry_price = current_prices.get(ticker)
        if entry_price is None:
            continue

        pred_id = f"{today}_{ticker}_holdings"
        # Skip if already recorded today
        if any(p["id"] == pred_id for p in history.get("predictions", [])):
            continue
        # 同方向の pending が生きている間は再記録しない (thesis 単位)。
        if _has_open_thesis(history, ticker, "holdings", prediction):
            skipped_open_thesis += 1
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
                "pre_entry_metrics": pem_lookup.get(ticker, {}),
                "regime": regime,
                "critic_verdict": (critic_decisions or {}).get(ticker),
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
        if prediction in _NO_DIRECTION_PREDICTIONS:
            continue
        if prediction not in ("UP", "DOWN"):
            continue

        entry_price = current_prices.get(ticker)
        if entry_price is None:
            continue

        pred_id = f"{today}_{ticker}_short_term"
        if any(p["id"] == pred_id for p in history.get("predictions", [])):
            continue
        if _has_open_thesis(history, ticker, "short_term", prediction):
            skipped_open_thesis += 1
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
                "pre_entry_metrics": pem_lookup.get(ticker, {}),
                "regime": regime,
                "critic_verdict": (critic_decisions or {}).get(ticker),
            }
        )
        new_count += 1

    # Extract from long-term picks
    for r in discovery_result.get("long_term_picks", []):
        ticker = r.get("ticker", "")
        if not ticker:
            continue
        prediction = r.get("prediction")
        if prediction in _NO_DIRECTION_PREDICTIONS:
            continue
        if prediction not in ("UP", "DOWN"):
            continue

        entry_price = current_prices.get(ticker)
        if entry_price is None:
            continue

        pred_id = f"{today}_{ticker}_long_term"
        if any(p["id"] == pred_id for p in history.get("predictions", [])):
            continue
        if _has_open_thesis(history, ticker, "long_term", prediction):
            skipped_open_thesis += 1
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
                "pre_entry_metrics": pem_lookup.get(ticker, {}),
                "regime": regime,
                "critic_verdict": (critic_decisions or {}).get(ticker),
            }
        )
        new_count += 1

    logger.info(
        "Saved %d new predictions (%d skipped: open thesis already pending)",
        new_count,
        skipped_open_thesis,
    )

    # Recompute stats
    history["performance_stats"] = compute_performance_stats(history)
    return history


def _compute_episode_stats(resolved: list[dict]) -> dict | None:
    """同一ポジションの連日再予測を 1 episode に集約した成績。

    dedup key = (ticker, source, prediction, actual_price)。同じ銘柄の
    同じ方向の予測が同じ価格で解決された = 同一の値動きを複数回数えて
    いるとみなし、date 最古の 1 件を代表にする。actual_price が None の
    ものは key が潰れないよう素通しする。
    """
    if not resolved:
        return None
    seen: set[tuple] = set()
    episodes: list[dict] = []
    for p in sorted(resolved, key=lambda x: x.get("date", "")):
        actual = p.get("actual_price")
        if actual is None:
            episodes.append(p)
            continue
        key = (p.get("ticker"), p.get("source"), p.get("prediction"), actual)
        if key in seen:
            continue
        seen.add(key)
        episodes.append(p)
    n = len(episodes)
    if n == 0:
        return None
    wins = sum(1 for p in episodes if p.get("status") == "win")
    dir_returns = [r for r in (_directional_return(p) for p in episodes) if r is not None]
    result: dict = {
        "n_episodes": n,
        "n_raw": len(resolved),
        "accuracy_pct": round(wins / n * 100, 1),
    }
    if dir_returns:
        result["mean_dir_return_pct"] = round(sum(dir_returns) / len(dir_returns), 2)
    return result


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
    expired = [p for p in predictions if p["status"] == "expired"]
    # NOTE: ``wins + losses`` の連結にすると、後段の reviewed_date 安定
    # ソートで同日 tie 内が必ず「勝ち→負け」順になり、同日大量レビュー日
    # の DD / rolling Sharpe / 直近窓系 metric が systematically 歪む
    # (2026-07-04 監査: recent DD 217pp vs 実際 97pp)。ファイル順 (= 保存
    # 順) を保持する filter で構築する。
    resolved = [p for p in predictions if p["status"] in ("win", "loss")]

    stats: dict = {
        "total_predictions": total,
        "wins": len(wins),
        "losses": len(losses),
        "pending": len(pending),
        "accuracy_pct": round(len(wins) / len(resolved) * 100, 1) if resolved else None,
    }
    if expired:
        # 価格が取れず強制クローズした件数。多い場合は universe 変動で
        # 予測が観測不能になっている = accuracy に生存者バイアスの疑い。
        stats["expired"] = len(expired)

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
    #
    # Track current drawdown (peak-to-latest) separately from max DD.
    # NOTE: これらは通算累積和ベースで、一度深く沈むと全期間 peak を
    # 更新するまで回復しない (2026-07-04 監査時点で 199.7pp)。そのため
    # HIGH-ban の hard rule には使わず、直近 _RECENT_DD_WINDOW trade の
    # 窓内 drawdown (recent_drawdown_pct) を別途計算して使う。窓内なら
    # losing streak を検知しつつ、streak が終われば自然に回復する。
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
        stats["current_drawdown_pct"] = round(peak - cumulative, 2)

        recent_window = chrono_returns[-_RECENT_DD_WINDOW:]
        cumulative = 0.0
        peak = 0.0
        for r in recent_window:
            cumulative += r
            peak = max(peak, cumulative)
        stats["recent_drawdown_pct"] = round(peak - cumulative, 2)
        stats["recent_drawdown_window"] = len(recent_window)

    # Episode-level stats (擬似反復補正)。holdings は同じ建玉が毎日
    # 再予測されるため、resolved の名目 n は独立サンプル数を大きく
    # 水増しする (2026-07-04 監査: 6月の holdings 300 件の実体は 25 銘柄)。
    # 同一 (ticker, source, prediction, actual_price) = 同じポジションの
    # 同じ帰結とみなし最初の 1 件だけ数えた「episode 単位」の成績を併記
    # する。headline との乖離が大きいときは名目 n を信用してはいけない。
    episode_stats = _compute_episode_stats(resolved)
    if episode_stats:
        stats["episode_stats"] = episode_stats

    # Confidence breakdown (the single most important diagnostic — if
    # HIGH < MEDIUM, the AI is over-using HIGH and needs to tighten its
    # bar). ``format_performance_feedback`` reads this back and emits an
    # explicit warning when inverted.
    #
    # Phase 2 (issue #46) で Brier score を追加: accuracy_pct より精密な
    # 確率予測の正解度測定。confidence label → 確率 mapping は
    # HIGH=0.75 / MEDIUM=0.65 / LOW=0.55 (codex 推奨の canonical mapping、
    # Phase 3 で data 由来に置換可能)。outcome = 1 if win else 0。
    # Brier = (predicted_prob - outcome)^2 を bucket 内 average。
    # 0 が完璧、0.25 が coin flip 同等、>0.25 で「無関係」。
    confidence_stats = _compute_confidence_buckets(resolved)
    if confidence_stats:
        stats["by_confidence"] = confidence_stats

    # Reliability diagram data (Phase 2 #46): 信頼度別の予測確率 vs 実
    # 正答率を可視化するためのバケット集計。bin は HIGH/MEDIUM/LOW の
    # 3 段階で十分 (Claude が出す canonical 3 label と一致)。
    # 完璧校正なら observed_acc ≒ predicted_prob、calibration 崩壊時は
    # predicted_prob > observed_acc (HIGH の方が低精度) になる。
    reliability_bins = []
    for conf in ("HIGH", "MEDIUM", "LOW"):
        bucket = confidence_stats.get(conf)
        if bucket and bucket["total"] >= _MIN_BUCKET_N:
            observed_acc = bucket["accuracy_pct"] / 100
            predicted = bucket["predicted_prob"]
            reliability_bins.append(
                {
                    "confidence": conf,
                    "predicted_prob": predicted,
                    "observed_acc": round(observed_acc, 3),
                    "gap_pp": round((observed_acc - predicted) * 100, 1),  # 正で過小評価、負で過大評価
                    "n": bucket["total"],
                }
            )
    if reliability_bins:
        stats["reliability_diagram"] = reliability_bins

    # Confidence-vs-screening 分離分析 (Phase 2 #46, codex D 仮説 1 検証):
    # 「screening weight が直接 confidence に効いてない」を実証するため、
    # bucket 別の平均 signal_count を比較。HIGH ≈ MEDIUM ≈ LOW なら
    # signal の多寡 ≠ Claude の confidence → 「材料の多さ」 という他の
    # heuristic で confidence を付けてる蓋然性が高い。
    # 期待される healthy パターン: HIGH > MEDIUM > LOW (signal 多い方が
    # 自信あり、と素朴に整合)。
    confidence_signal_breakdown = {}
    for conf in ("HIGH", "MEDIUM", "LOW"):
        conf_preds = [
            p for p in resolved if p.get("confidence") == conf and isinstance(p.get("signal_components"), dict)
        ]
        sig_counts = [len(p["signal_components"]) for p in conf_preds if p["signal_components"]]
        if len(sig_counts) >= _MIN_BUCKET_N:
            mean_sig = sum(sig_counts) / len(sig_counts)
            confidence_signal_breakdown[conf] = {
                "n": len(sig_counts),
                "mean_signal_count": round(mean_sig, 2),
            }
    if confidence_signal_breakdown:
        stats["confidence_signal_breakdown"] = confidence_signal_breakdown

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

    # Recent UP hit rate — feeds the prompt-level UP gate. The AI's
    # long-only bias (codex review 2026-05-15: UP predictions hitting
    # 46.8% on 77 samples) only becomes a corrective signal when we
    # measure it on the *recent* sample, since the older window may
    # reflect a different prompt era. Trigger gating off this stat
    # downstream, not off the all-time accuracy.
    up_stat = compute_recent_up_hit_rate(history, recent_n=_UP_GATE_RECENT_N)
    if up_stat is not None:
        stats["recent_up_hit_rate"] = up_stat

    # Strategy drift: compare expectancy on the last 14 resolved trades
    # against the older baseline. A negative delta means today's
    # selection logic / prompt has decayed relative to whatever it
    # was doing earlier — the most actionable early-warning signal we
    # can compute from in-band data. Requires enough samples on both
    # sides so a stray run doesn't trigger the alarm.
    drift = _compute_drift_indicator(resolved, recent_n=14, min_recent=5, min_baseline=10)
    if drift is not None:
        stats["drift_indicator"] = drift

    # Direction-level recent win rate (UP / DOWN separately) over the same
    # window as drift_indicator. Catches the case where the *overall* recent
    # expectancy is negative because one direction (typically UP in a
    # mean-reverting regime) collapsed while the other direction is fine —
    # the per-bucket (confidence × direction) gate misses this when the
    # long-run bucket accuracy is still above 50%. Surfaced separately
    # from drift_indicator so the code-level gate can act on direction
    # without re-deriving it.
    rdw = _compute_recent_direction_winrate(resolved, recent_n=14, min_dir_n=5)
    if rdw is not None:
        stats["recent_direction_winrate"] = rdw

    # by_regime breakdown: split resolved trades by the regime stamped
    # at prediction-save time. Earlier predictions don't carry a regime
    # field (added 2026-06-13); only those with a non-null regime are
    # included so the bucket stays clean. Reads as "in which market
    # backdrop is the AI's selection actually working?" — the data
    # the Bayesian weight proposal needs before we widen defensive
    # boosts.
    by_regime = _compute_by_regime(resolved, min_n=5)
    if by_regime:
        stats["by_regime"] = by_regime

    critic_eff = compute_critic_efficacy(history, min_samples=5)
    if critic_eff:
        stats["critic_efficacy"] = critic_eff

    # Calibration zone: codex 設計相談 (issue #46) に基づく階層化 circuit
    # breaker。HIGH/MEDIUM accuracy ratio + rolling Sharpe + drift を
    # 統合して Red/Yellow/Green を判定。AI prompt に zone を注入し、Red
    # 時は HIGH 出力を禁止、Yellow 時は boldness 控えめ、を強制する。
    # 復帰は「2 連続 window 改善」 (latest + prev の両方 green) が条件。
    zone = _compute_calibration_zone(
        resolved=resolved,
        confidence_stats=stats.get("by_confidence", {}),
        drift=drift,
    )
    if zone is not None:
        stats["calibration_zone"] = zone

    # Phase 4 #46: Net expectancy (手数料込み) と signal correlation 分析
    net_ev = compute_net_expectancy(history)
    if net_ev is not None:
        stats["net_expectancy"] = net_ev

    # 2026-07-04 監査 (#51): UP 買い建て (実現可能) / holdings DOWN
    # (売り助言) / その他 DOWN (情報的) を分離した期待値。名目 EV は
    # ショート不能な DOWN 的中で水増しされるため、収益性の自己評価は
    # realizable_up を正とする。
    realizable = compute_realizable_expectancy(history)
    if realizable is not None:
        stats["realizable_expectancy"] = realizable

    # 市場相対採点 (skill vs β 分離)。benchmark_return_pct を持つ予測が
    # _MIN_BUCKET_N 件未満の間は None (backfill / 今後の review で蓄積)。
    benchmark_rel = _compute_benchmark_relative(resolved)
    if benchmark_rel is not None:
        stats["benchmark_relative"] = benchmark_rel

    correlation_pairs = compute_signal_correlation_pairs(history)
    if correlation_pairs:
        stats["signal_correlation_pairs"] = correlation_pairs

    # Phase 5 #46: HIGH bucket overpriced bias 検証 (codex D 仮説 4):
    # HIGH 予測の銘柄は事前 5/21/63 日 return が MEDIUM/LOW より高ければ
    # 「既に上昇済み = overpriced で平均回帰の caught knife になりやすい」
    # 確証となる。pre_entry_metrics は最近の予測のみに存在 (Phase 5 で
    # 記録 enable)、十分な n に到達するまで None 返す。
    overpriced_bias = _compute_overpriced_bias(resolved)
    if overpriced_bias is not None:
        stats["overpriced_bias"] = overpriced_bias

    # Phase 4 #46: walk-forward CV で現状 weight set の OOS パフォーマンス
    # を測定 (purged + embargo)。Lopez de Prado 手法。Bayesian proposal の
    # 効果検証 (current vs proposed) は次 phase で。
    try:
        from stock_analyzer.strategy_learner import load_screening_weights

        current_weights = load_screening_weights()
        cv_result = evaluate_weights_walkforward_cv(history, weights=current_weights)
        if cv_result is not None:
            stats["walkforward_cv"] = cv_result
    except Exception:
        logger.exception("Walk-forward CV evaluation failed (continuing without)")

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


def compute_recent_up_hit_rate(history: dict, recent_n: int = _UP_GATE_RECENT_N) -> dict | None:
    """Win rate of the most-recent ``recent_n`` resolved UP predictions.

    "Recent" is sample-based, not calendar-based, so a slow week still
    yields the same N data points. Returns ``None`` when the resolved
    UP sample is below ``_UP_GATE_MIN_SAMPLES`` — at that point the
    rate is a coin-flip on a few trades and shouldn't gate anything.

    Returned dict:
        {
          "recent_n": <actual sample used>,
          "wins": <wins among that sample>,
          "hit_rate_pct": <wins / total * 100>,
          "threshold_pct": <_UP_GATE_THRESHOLD_PCT>,
          "below_threshold": <bool>,
        }

    Below-threshold is the trigger the prompt-side helper consumes;
    surfacing it here keeps the threshold definition in one place.
    """
    resolved_up = [
        p for p in history.get("predictions", []) if p.get("status") in ("win", "loss") and p.get("prediction") == "UP"
    ]
    if len(resolved_up) < _UP_GATE_MIN_SAMPLES:
        return None
    # Chronological: most recent first by reviewed_date (the moment we
    # judged the outcome), fall back to entry date if missing.
    chrono = sorted(
        resolved_up,
        key=lambda p: p.get("reviewed_date") or p.get("date", ""),
        reverse=True,
    )
    sample = chrono[:recent_n]
    if len(sample) < _UP_GATE_MIN_SAMPLES:
        return None
    wins = sum(1 for p in sample if p["status"] == "win")
    hit_rate = wins / len(sample) * 100
    # 期待値条件 (2026-07-04 監査): UP は勝率 50% ちょうどでも期待値が
    # 負 (勝ち幅 < 負け幅) の期間が続いた。hit rate だけだと閾値上に
    # 張り付いて gate が開いたままになるため、EV <= 0 でも発火させる。
    dir_returns = [r for r in (_directional_return(p) for p in sample) if r is not None]
    mean_dir_ret = round(sum(dir_returns) / len(dir_returns), 2) if dir_returns else None
    ev_negative = mean_dir_ret is not None and mean_dir_ret <= 0.0
    return {
        "recent_n": len(sample),
        "wins": wins,
        "hit_rate_pct": round(hit_rate, 1),
        "threshold_pct": _UP_GATE_THRESHOLD_PCT,
        "mean_dir_return_pct": mean_dir_ret,
        "ev_negative": ev_negative,
        "below_threshold": hit_rate < _UP_GATE_THRESHOLD_PCT or ev_negative,
    }


def build_recent_failure_block(history: dict) -> str:
    """Render a compact "what didn't work recently" block for the daily
    cron prompt.

    Distinct from ``format_performance_feedback``'s broad statistical
    feedback. This block is *short* on purpose — codex 2026-06-13
    warning: long failure analyses pull AI selection toward statistical
    noise, so we surface only the "avoid these specific conditions"
    items the AI can actually use to filter candidates.

    At most five lines:
    1. recent expectancy vs baseline + drift p-value
    2. UP / DOWN recent win rate
    3. signal_efficacy worst-3 (negative lift, n >= 15)
    4. by_regime bucket whose accuracy fell below 50 %
    5. active verify in progress (governor's latest change_id)

    Returns ``""`` when nothing actionable is present so the caller
    can unconditionally concatenate without wrapping.
    """
    stats = history.get("performance_stats") or {}
    lines: list[str] = []

    drift = stats.get("drift_indicator")
    if isinstance(drift, dict) and drift.get("is_drift"):
        lines.append(
            f"  - 直近 {drift.get('recent_n', '?')} trades expectancy "
            f"{drift.get('recent_expectancy_pct', '?')}% (baseline "
            f"{drift.get('baseline_expectancy_pct', '?')}%, p={drift.get('p_value', '?')}) "
            "— statistically significant な実損 drift。新規 UP picks は強い独立 catalyst 必須"
        )

    rdw = stats.get("recent_direction_winrate")
    if isinstance(rdw, dict):
        bits = []
        for direction in ("UP", "DOWN"):
            d = rdw.get(direction)
            if isinstance(d, dict):
                bits.append(f"{direction} {d.get('winrate_pct', '?')}% (n={d.get('n', '?')})")
        if bits:
            lines.append("  - 直近 direction 別勝率: " + " / ".join(bits))

    # Signal efficacy worst-3 negative lift, gated by min_samples=15 so we
    # don't flag a 1-trade fluke. The full table goes into the weekly
    # review prompt via format_signal_efficacy; here we only surface
    # the actionable bottom rows.
    try:
        efficacy = compute_signal_efficacy(history, min_samples=15)
    except Exception:
        efficacy = {}
    bad_signals: list[tuple[str, float, int]] = []
    for sig, data in (efficacy or {}).items():
        with_block = data.get("with_signal") or {}
        without_block = data.get("without_signal") or {}
        n_with = with_block.get("total", 0)
        acc_with = with_block.get("accuracy_pct")
        acc_without = without_block.get("accuracy_pct")
        if isinstance(acc_with, int | float) and isinstance(acc_without, int | float) and n_with >= 15:
            lift = acc_with - acc_without
            if lift < -2.0:  # signal is actively dragging the bucket down
                bad_signals.append((sig, lift, n_with))
    if bad_signals:
        bad_signals.sort(key=lambda x: x[1])  # most negative first
        worst = bad_signals[:3]
        formatted = ", ".join(f"{sig}({lift:+.1f}pp/n={n})" for sig, lift, n in worst)
        lines.append("  - negative-lift signals (実勝率が baseline 以下): " + formatted)

    by_regime = stats.get("by_regime")
    if isinstance(by_regime, dict):
        bad_regimes = []
        for regime, data in by_regime.items():
            if (
                isinstance(data, dict)
                and isinstance(data.get("accuracy_pct"), int | float)
                and data["accuracy_pct"] < 50.0
                and data.get("n", 0) >= 5
            ):
                bad_regimes.append(f"{regime}: {data['accuracy_pct']}% (n={data['n']})")
        if bad_regimes:
            lines.append("  - 直近 regime 別の勝率 50% 未満: " + " / ".join(bad_regimes))

    # Governor's latest active change with pending verify
    try:
        from stock_analyzer.strategy_governor import load_change_log

        log = load_change_log()
        if log:
            latest = log[-1]
            if isinstance(latest, dict) and latest.get("verify_status") == "pending":
                applied = latest.get("applied") or {}
                sigs = ", ".join(applied.keys()) if applied else "(no weights)"
                lines.append(
                    f"  - 検証中の weight 変更: {latest.get('change_id', '?')} "
                    f"(activated {latest.get('activated_at', '?')}, signals: {sigs}) — "
                    "効果判定が出るまで同方向の signal 強化は当面据え置き"
                )
    except Exception:
        pass

    # System filtering from the previous cron: which tickers the critic
    # rejected / downgraded last time. Lets the AI see "this is what
    # *your own critic* threw out yesterday — don't reissue these
    # without an independent new catalyst." Reads critic_decisions.json
    # if present (P3 wiring); legacy crons without that file get no
    # extra line.
    try:
        from pathlib import Path as _P

        decisions_path = _P(__file__).parent.parent / "data" / "critic_decisions.json"
        if decisions_path.exists():
            with open(decisions_path, encoding="utf-8") as f:
                decisions = json.load(f)
            if isinstance(decisions, dict):
                rejected = [t for t, v in decisions.items() if v == "reject"]
                downgraded = [t for t, v in decisions.items() if v == "downgrade"]
                if rejected or downgraded:
                    parts: list[str] = []
                    if rejected:
                        parts.append(f"reject={','.join(rejected[:8])}{'…' if len(rejected) > 8 else ''}")
                    if downgraded:
                        parts.append(f"downgrade={','.join(downgraded[:8])}{'…' if len(downgraded) > 8 else ''}")
                    lines.append(
                        "  - 前回 cron で critic が落とした銘柄 ("
                        + " / ".join(parts)
                        + ") — 新規 catalyst なしの再 pick 禁止"
                    )
    except Exception:
        pass

    if not lines:
        return ""
    header = "=== 直近の失敗パターン (避けるべき条件) ==="
    return header + "\n" + "\n".join(lines)


def build_up_gate_directive(stats: dict) -> str:
    """Render the UP-gate prompt block when the recent UP hit rate is
    below threshold. Empty string when the gate is inactive (= no
    block in the prompt) so phase_prepare can unconditionally
    concatenate it.

    The directive does two things at the prompt level:
    1. Forbids new short_term UP picks for this run.
    2. Asks the AI to use ``NO_TRADE`` as the prediction for holdings
       where it would otherwise default to UP without strong
       multi-layer evidence.

    Why prompt-level not code-level: the AI has rich per-ticker
    context (news, calendar, sector); a blanket code filter would
    drop legitimately strong UP setups. The directive nudges the
    *selection* logic while leaving room for high-conviction outliers
    that pass the documented bar. We still rely on later layers
    (critic, portfolio_risk, NO_TRADE skipping in save_new_predictions)
    to absorb anything the AI insists on against the gate.
    """
    up_stat = stats.get("recent_up_hit_rate") if isinstance(stats, dict) else None
    if not isinstance(up_stat, dict) or not up_stat.get("below_threshold"):
        return ""
    if up_stat.get("ev_negative") and up_stat["hit_rate_pct"] >= up_stat["threshold_pct"]:
        trigger_text = (
            f"直近 {up_stat['recent_n']} 件の UP 予測は勝率 {up_stat['hit_rate_pct']}% ですが"
            f"期待値が {up_stat.get('mean_dir_return_pct', 0):+.2f}%/件 と負です "
            "(勝ち幅 < 負け幅)。"
        )
    else:
        trigger_text = (
            f"直近 {up_stat['recent_n']} 件の UP 予測勝率が "
            f"{up_stat['hit_rate_pct']}% (< {up_stat['threshold_pct']:.0f}%) です。"
        )
    return (
        "=== UP予測ゲート (本日有効) ===\n"
        f"{trigger_text}"
        "楽観バイアスの兆候があるため、今回の cron では以下のルールが強制適用されます:\n"
        "1. **discovery short_term_picks の UP 推奨は原則禁止** — "
        "テクニカル(SMA/MACD/RSI 全部) + ファンダ(成長 or 割安) + 強いカタリスト の "
        "3 層すべてが UP 方向で揃っている銘柄のみ可。揃わなければ short_term_picks に入れない。\n"
        "2. **holdings の UP 予測は厳格化** — 同じ 3 層基準を満たさない場合、"
        '``prediction`` を `"NO_TRADE"` として short_summary に '
        "「方向不明 — action 単独判断」と記載し、action フィールドだけで保有継続/利確/損切りを判定してください。\n"
        "3. long_term_picks (3-12ヶ月) はゲート対象外、通常通り選定 OK。\n"
        "このゲートは UP 予測勝率が回復するまで継続します。守れない場合の理由を critic / "
        "performance_feedback に残してください。"
    )


def evaluate_weights_walkforward_cv(
    history: dict,
    weights: dict,
    n_folds: int = 4,
    embargo_days: int = 7,
    min_test_n: int = 10,
) -> dict | None:
    """Purged walk-forward CV で weight set の OOS パフォーマンスを評価する。

    Lopez de Prado "Advances in Financial Machine Learning" 章 7 で提唱の
    purged k-fold CV を時系列向け walk-forward 版で実装。

    手順:
      1. resolved 予測を date 順に並べる
      2. n_folds に分割
      3. 各 fold k について:
         a. test = fold k
         b. embargo_days で test の前後を train から除外 (leakage 防止)
         c. train = test 前のみ (walk-forward: 未来データ使わない)
         d. **train slice だけで weight を学習** (DEFAULT_WEIGHTS 起点に
            Bayesian 提案を fold ごとに計算)。旧実装は現在の active
            weights を全 fold に流用しており、その weights 自体が test
            期間を含む全履歴で調整されているため後知恵 (look-ahead) を
            含んでいた (codex 2026-07-04 指摘で修正)
         e. test の上位 N (= top quantile) の predicted vs actual accuracy
      4. fold 別 accuracy の平均と stdev を返す

    NOTE: 引数 ``weights`` (現在の active weights) は採点に使わなくなった。
    signature 互換のため残しているが、渡された値は無視される。

    Returns:
      {
        "n_folds": 4,
        "embargo_days": 7,
        "fold_results": [
          {"fold": 0, "n_train": 40, "n_test": 15, "top_quantile_acc_pct": 65.0},
          ...
        ],
        "mean_top_quantile_acc_pct": 60.5,
        "stdev_acc_pp": 4.2,
        "interpretation": "OOS で上位 quantile の accuracy が ~60%、過去全体
                          accuracy ~58% より高い → weight set は OOS で機能"
      }

    None 返す = データ不足。Phase 5 で hyperparameter search に組込予定。
    """
    # 評価対象は signal_components が **空でない** 予測のみ (空 dict だと
    # 全 score=0 で fold が degenerate)。reviewed_date より prediction date
    # の方が時系列スパン長い (signal_components の記録機能が最近のため
    # reviewed_date は narrow window)、こちらで chrono 並びにする。
    resolved = [
        p
        for p in history.get("predictions", [])
        if p.get("status") in ("win", "loss")
        and isinstance(p.get("signal_components"), dict)
        and p["signal_components"]  # non-empty required
        and p.get("date")
    ]
    if len(resolved) < n_folds * min_test_n * 2:
        return None
    chrono = sorted(resolved, key=lambda p: p["date"])
    fold_size = len(chrono) // n_folds
    fold_results = []
    accuracies: list[float] = []
    for k in range(n_folds):
        test_start = k * fold_size
        test_end = (k + 1) * fold_size if k < n_folds - 1 else len(chrono)
        test = chrono[test_start:test_end]
        if len(test) < min_test_n:
            continue
        # walk-forward: train は test より過去のみ
        # embargo: train の末尾 embargo_days 分を除外
        if test_start == 0:
            train = []  # no past data for first fold
        else:
            embargo_cutoff_date = test[0]["date"]
            from datetime import datetime, timedelta

            cutoff_dt = datetime.strptime(embargo_cutoff_date, "%Y-%m-%d") - timedelta(days=embargo_days)
            cutoff_str = cutoff_dt.strftime("%Y-%m-%d")
            train = [p for p in chrono[:test_start] if p["date"] < cutoff_str]
        if len(train) < min_test_n:
            continue
        # train slice だけで fold 専用 weights を学習 (真の OOS 評価)。
        # 起点は static な DEFAULT_WEIGHTS — 現在の active weights を起点に
        # すると、それ自体が未来データ込みで調整済みなので leakage になる。
        from stock_analyzer.strategy_learner import DEFAULT_WEIGHTS, compute_bayesian_weight_proposal

        fold_weights: dict[str, float] = dict(DEFAULT_WEIGHTS)
        try:
            proposal = compute_bayesian_weight_proposal({"predictions": train}, current_weights=dict(DEFAULT_WEIGHTS))
            for sig, d in (proposal.get("proposed") or {}).items():
                if isinstance(d, dict) and "proposed_weight" in d:
                    fold_weights[sig] = d["proposed_weight"]
        except Exception:
            logger.warning("walkforward: train-slice weight fit failed for fold %d — using defaults", k, exc_info=True)

        # test の各予測に score 付与 (= sum of fold weights for fired signals)
        # 上位 quantile (top 30%) を選んで accuracy
        scored = [
            (sum(fold_weights.get(sig, 0) for sig, fired in p["signal_components"].items() if fired), p) for p in test
        ]
        scored.sort(key=lambda x: x[0], reverse=True)
        top_n = max(3, int(len(scored) * 0.30))
        top = scored[:top_n]
        if not top:
            continue
        top_wins = sum(1 for _, p in top if p["status"] == "win")
        acc = top_wins / len(top) * 100
        fold_results.append(
            {
                "fold": k,
                "n_train": len(train),
                "n_test": len(test),
                "top_n": len(top),
                "top_quantile_acc_pct": round(acc, 1),
            }
        )
        accuracies.append(acc)
    if not accuracies:
        return None
    mean_acc = sum(accuracies) / len(accuracies)
    if len(accuracies) >= 2:
        variance = sum((a - mean_acc) ** 2 for a in accuracies) / (len(accuracies) - 1)
        stdev = variance**0.5
    else:
        stdev = 0.0
    return {
        "n_folds_used": len(fold_results),
        "embargo_days": embargo_days,
        "fold_results": fold_results,
        "mean_top_quantile_acc_pct": round(mean_acc, 1),
        "stdev_acc_pp": round(stdev, 1),
    }


def _compute_overpriced_bias(resolved: list[dict], min_n: int = 10) -> dict | None:
    """HIGH bucket の事前 return が MEDIUM/LOW より高いか検証 (codex D 仮説 4)。

    HIGH 予測の銘柄が「既に上昇済み」なら、HIGH bucket の平均 pre_entry
    return (5d/21d/63d) が MEDIUM/LOW より高くなる。これは "buying high" =
    平均回帰の犠牲になりやすいパターンの実証。

    pre_entry_metrics が無い (= 過去予測 with 古い schema) は除外、各
    bucket の n >= min_n を要求。
    """
    with_metrics = [p for p in resolved if isinstance(p.get("pre_entry_metrics"), dict) and p["pre_entry_metrics"]]
    if not with_metrics:
        return None
    by_conf: dict[str, dict[str, list[float]]] = {}
    for conf in ("HIGH", "MEDIUM", "LOW"):
        by_conf[conf] = {"price_change_5d": [], "price_change_1m": [], "price_change_3m": []}
    for p in with_metrics:
        conf = p.get("confidence")
        if conf not in by_conf:
            continue
        m = p["pre_entry_metrics"]
        for key in ("price_change_5d", "price_change_1m", "price_change_3m"):
            val = m.get(key)
            if isinstance(val, (int, float)):
                by_conf[conf][key].append(float(val))
    result: dict[str, dict] = {}
    for conf in ("HIGH", "MEDIUM", "LOW"):
        bucket = by_conf[conf]
        if len(bucket["price_change_5d"]) < min_n:
            continue
        result[conf] = {
            "n": len(bucket["price_change_5d"]),
            "mean_5d_ret_pct": round(sum(bucket["price_change_5d"]) / len(bucket["price_change_5d"]), 2),
            "mean_1m_ret_pct": round(sum(bucket["price_change_1m"]) / len(bucket["price_change_1m"]), 2)
            if bucket["price_change_1m"]
            else None,
            "mean_3m_ret_pct": round(sum(bucket["price_change_3m"]) / len(bucket["price_change_3m"]), 2)
            if bucket["price_change_3m"]
            else None,
        }
    if not result:
        return None
    return result


def compute_signal_correlation_pairs(history: dict, min_samples: int = 10) -> list[dict]:
    """signal_components の binary co-occurrence から signal 間相関を計算。

    codex E 推奨「signal decorrelation で重複投票防止」 を実装。VIF や PCA
    は連続値が必要だが、signal_components は binary (fired or not) なので
    Pearson correlation を直接計算 (= phi coefficient with binary data)。

    |r| > 0.7 は強相関、0.5-0.7 は中相関。highly correlated signal pair は
    「同じ情報で二重カウント」してる可能性大 → どちらか drop or 統合候補。

    Returns: list of {"pair": ["a", "b"], "correlation": 0.78, "n_both": 50,
    "n_a_only": 10, "n_b_only": 8, "n_neither": 100}
    correlation の絶対値降順でソート。
    """
    resolved = [
        p
        for p in history.get("predictions", [])
        if isinstance(p.get("signal_components"), dict) and p["signal_components"]
    ]
    if len(resolved) < min_samples:
        return []

    all_signals: set[str] = set()
    for p in resolved:
        for name, fired in p["signal_components"].items():
            if fired:
                all_signals.add(name)
    signals_sorted = sorted(all_signals)
    n = len(resolved)

    pairs: list[dict] = []
    for i, a in enumerate(signals_sorted):
        for b in signals_sorted[i + 1 :]:
            n_both = sum(1 for p in resolved if p["signal_components"].get(a) and p["signal_components"].get(b))
            n_a = sum(1 for p in resolved if p["signal_components"].get(a))
            n_b = sum(1 for p in resolved if p["signal_components"].get(b))
            n_a_only = n_a - n_both
            n_b_only = n_b - n_both
            n_neither = n - n_a_only - n_b_only - n_both
            # phi coefficient (= Pearson for binary)
            denom_sq = n_a * (n - n_a) * n_b * (n - n_b)
            if denom_sq <= 0:
                continue
            phi = (n_both * n_neither - n_a_only * n_b_only) / math.sqrt(denom_sq)
            # 弱相関は noise として除外 (|r| < 0.3)
            if abs(phi) < 0.3:
                continue
            pairs.append(
                {
                    "pair": [a, b],
                    "correlation": round(phi, 2),
                    "n_both": n_both,
                    "n_a_only": n_a_only,
                    "n_b_only": n_b_only,
                    "n_neither": n_neither,
                }
            )
    pairs.sort(key=lambda x: abs(x["correlation"]), reverse=True)
    return pairs


def _benchmark_dir_excess(p: dict) -> float | None:
    """方向調整後の市場超過リターン。

    UP: 銘柄 return - 市場 return (指数を買う代わりにこの銘柄を買った差分)。
    DOWN: (-銘柄 return) - (-市場 return) = 市場より余計に下がった分だけが
    「相対的弱さを当てた」価値。下落相場で市場と同じだけ下がる DOWN 的中は
    excess ゼロ = β であってスキルではない。
    """
    bench = p.get("benchmark_return_pct")
    dir_ret = _directional_return(p)
    if bench is None or dir_ret is None:
        return None
    direction = p.get("prediction")
    dir_bench = float(bench) if direction == "UP" else -float(bench)
    return dir_ret - dir_bench


def _compute_benchmark_relative(resolved: list[dict]) -> dict | None:
    """市場相対 (vs TOPIX ETF) の成績。benchmark_return_pct を持つ解決済み
    予測のみ対象 (retro backfill + 今後の review で記録される)。

    「スキル vs β」を分離する監査 follow-up の中核 metric。overall / 方向別 /
    source 別に mean dir excess と「ベンチ超え率」を出す。
    """
    covered = [p for p in resolved if _benchmark_dir_excess(p) is not None]
    if len(covered) < _MIN_BUCKET_N:
        return None

    def _bucket(preds: list[dict]) -> dict | None:
        excesses = [e for e in (_benchmark_dir_excess(p) for p in preds) if e is not None]
        if len(excesses) < _MIN_BUCKET_N:
            return None
        beat = sum(1 for e in excesses if e > 0)
        return {
            "n": len(excesses),
            "mean_dir_excess_pct": round(sum(excesses) / len(excesses), 2),
            "beat_benchmark_pct": round(beat / len(excesses) * 100, 1),
        }

    result: dict = {
        "benchmark": _BENCHMARK_TICKER,
        "overall": _bucket(covered),
    }
    up_b = _bucket([p for p in covered if p.get("prediction") == "UP"])
    if up_b:
        result["up"] = up_b
    down_b = _bucket([p for p in covered if p.get("prediction") == "DOWN"])
    if down_b:
        result["down"] = down_b
    by_source: dict[str, dict] = {}
    for src in ("holdings", "short_term", "long_term", "discovery"):
        b = _bucket([p for p in covered if p.get("source") == src])
        if b:
            by_source[src] = b
    if by_source:
        result["by_source"] = by_source
    return result if result.get("overall") else None


def compute_realizable_expectancy(history: dict, transaction_cost_pct: float = 0.2) -> dict | None:
    """予測を「実際に取引で収益化できるか」で分離した期待値 (2026-07-04 監査)。

    名目 expectancy は UP / DOWN の directional return を混ぜるが、この
    システムに空売りは無い。つまり:

    - **UP 予測** = 買い建て可能 → 唯一 P&L に変換できる「実現可能」枠。
    - **holdings への DOWN 予測** = 保有株の売り助言。当たっても利益では
      なく「損失回避」(むしろ保有中の含み損)。助言の質として別枠計測。
    - **その他の DOWN 予測** (non-holdings) = ショートできない情報的
      予測。方向感の学習には使えるが P&L には寄与しない。

    監査時点で名目 expectancy +1.25% の 49% が holdings DOWN 由来で、
    paper portfolio NAV (±0%) との乖離の主因だった。headline を
    realizable 側に寄せるための計測基盤。
    """
    resolved = [
        p
        for p in history.get("predictions", [])
        if p.get("status") in ("win", "loss") and p.get("actual_return_pct") is not None
    ]
    if not resolved:
        return None
    round_trip_cost = transaction_cost_pct * 2

    def _bucket(preds: list[dict]) -> dict | None:
        if not preds:
            return None
        returns = [r for r in (_directional_return(p) for p in preds) if r is not None]
        if not returns:
            return None
        wins = sum(1 for p in preds if p.get("status") == "win")
        gross = sum(returns) / len(returns)
        return {
            "n": len(preds),
            "accuracy_pct": round(wins / len(preds) * 100, 1),
            "gross_expectancy_pct": round(gross, 2),
            "net_expectancy_pct": round(gross - round_trip_cost, 2),
        }

    ups = [p for p in resolved if p.get("prediction") == "UP"]
    holdings_down = [p for p in resolved if p.get("prediction") == "DOWN" and p.get("source") == "holdings"]
    other_down = [p for p in resolved if p.get("prediction") == "DOWN" and p.get("source") != "holdings"]

    result: dict = {"round_trip_cost_pct": round_trip_cost}
    realizable = _bucket(ups)
    if realizable:
        result["realizable_up"] = realizable
    advice = _bucket(holdings_down)
    if advice:
        # 助言枠: dir return が正 = 「売っておけば回避できた下落幅」。
        advice["avoided_loss_note"] = "期待値は損失回避幅であり実現利益ではない"
        result["holdings_down_advice"] = advice
    informational = _bucket(other_down)
    if informational:
        result["informational_down"] = informational
    if "realizable_up" not in result and "holdings_down_advice" not in result:
        return None
    return result


def compute_net_expectancy(history: dict, transaction_cost_pct: float = 0.2) -> dict | None:
    """手数料込み net EV を計算 (codex E 推奨「コスト込み EV」)。

    現状の expectancy_per_trade_pct は gross (手数料前)。日本株の retail
    手数料は 0.1-0.3% (片道、SBI 等)、往復で 0.2-0.6%。default 0.2% を
    片道 cost として、両side 0.4% を expectancy から引く。

    手数料込み EV が正であれば actual に儲かる、負なら名目勝率があっても
    実利益はマイナス。
    """
    resolved = [p for p in history.get("predictions", []) if p.get("status") in ("win", "loss")]
    if not resolved:
        return None
    returns = [r for r in (_directional_return(p) for p in resolved) if r is not None]
    if not returns:
        return None
    gross_ev = sum(returns) / len(returns)
    round_trip_cost = transaction_cost_pct * 2  # 往復
    net_ev = gross_ev - round_trip_cost
    return {
        "gross_expectancy_pct": round(gross_ev, 2),
        "transaction_cost_pct": transaction_cost_pct,
        "round_trip_cost_pct": round_trip_cost,
        "net_expectancy_pct": round(net_ev, 2),
        "n": len(returns),
    }


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


def _welch_t_test_pvalue_lower_tail(recent_returns: list[float], baseline_returns: list[float]) -> float | None:
    """Welch's t-test for "recent mean < baseline mean", one-tailed.

    Returns the lower-tail p-value (probability that we'd see a
    delta this negative under H0: same population). Uses
    Welch-Satterthwaite df because the two samples have unequal
    sizes and we don't assume equal variance.

    Approximates the Student-t survival function via a small
    polynomial good to ~3 decimals for df >= 5 and |t| <= 5,
    avoiding a scipy dependency. None when inputs are degenerate
    (zero variance or empty).
    """
    n1 = len(recent_returns)
    n2 = len(baseline_returns)
    if n1 < 2 or n2 < 2:
        return None
    m1 = sum(recent_returns) / n1
    m2 = sum(baseline_returns) / n2
    var1 = sum((r - m1) ** 2 for r in recent_returns) / (n1 - 1)
    var2 = sum((r - m2) ** 2 for r in baseline_returns) / (n2 - 1)
    if var1 <= 0 and var2 <= 0:
        return None
    se = math.sqrt(var1 / n1 + var2 / n2)
    if se == 0:
        return None
    t_stat = (m1 - m2) / se
    # Welch-Satterthwaite degrees of freedom
    num = (var1 / n1 + var2 / n2) ** 2
    denom = (var1 / n1) ** 2 / max(n1 - 1, 1) + (var2 / n2) ** 2 / max(n2 - 1, 1)
    if denom <= 0:
        return None
    df = num / denom
    # Convert t to lower-tail p via Student-t CDF approximation.
    # For our purposes (decision threshold p < 0.05), a normal
    # approximation with small-df correction is accurate enough.
    # Use the cumulative normal CDF on a tail-corrected t.
    # For df >= 30 the t and normal are within 1% on the body;
    # we apply a Welch-style correction for small df.
    if df < 5:
        # Below 5 df the test is so weak it's not actionable
        return None
    # CDF of standard normal at x
    z = t_stat / math.sqrt(1 + t_stat * t_stat / (4 * df))
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


_DRIFT_PVALUE_THRESHOLD = 0.10
"""p-value threshold for declaring drift. 10% is moderate evidence;
0.05 would be the conventional research bar but with ~14 trades vs
~50 baseline the test is power-limited. 10% trades false-positive
rate for catching drift earlier — appropriate for a warning signal
where the cost of missing real decay > cost of one false alarm."""


def _compute_drift_indicator(
    resolved: list[dict],
    recent_n: int = 14,
    min_recent: int = 5,
    min_baseline: int = 10,
) -> dict | None:
    """Compare per-trade expectancy on the latest ``recent_n`` resolved
    trades against the older baseline, using Welch's t-test.

    Returns ``None`` when either side has fewer than its minimum sample
    count — drift conclusions on tiny windows are noise. Otherwise
    returns ``{"recent_expectancy_pct", "baseline_expectancy_pct",
    "delta_pp", "is_drift", "p_value", "recent_n", "baseline_n"}``.

    ``is_drift`` previously fired on a heuristic 2pp delta. That
    threshold was scale-invariant: a 2pp drop on a strategy with
    1pp/trade std-dev is huge (one stdev), but on a strategy with
    5pp/trade std-dev (high-volatility) it's noise. The t-test
    properly accounts for both sample variances and sample sizes
    so a drift warning fires only when the difference is
    statistically meaningful for the data in hand.

    Threshold: p < _DRIFT_PVALUE_THRESHOLD (10%). Conservative is
    0.05; we use 0.10 so the warning catches genuine decay earlier
    at the cost of more false positives (which only affect prompt
    text, not picks).
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
    recent_returns: list[float] = []
    baseline_returns: list[float] = []
    for p in recent:
        r = _directional_return(p)
        if r is not None:
            recent_returns.append(r)
    for p in baseline:
        r = _directional_return(p)
        if r is not None:
            baseline_returns.append(r)
    recent_exp = sum(recent_returns) / len(recent_returns)
    baseline_exp = sum(baseline_returns) / len(baseline_returns)
    delta_pp = recent_exp - baseline_exp
    p_value = _welch_t_test_pvalue_lower_tail(recent_returns, baseline_returns)
    is_drift = bool(p_value is not None and p_value < _DRIFT_PVALUE_THRESHOLD and delta_pp < 0)
    return {
        "recent_expectancy_pct": round(recent_exp, 2),
        "baseline_expectancy_pct": round(baseline_exp, 2),
        "delta_pp": round(delta_pp, 2),
        "p_value": round(p_value, 3) if p_value is not None else None,
        "is_drift": is_drift,
        "recent_n": len(recent_returns),
        "baseline_n": len(baseline_returns),
    }


def compute_critic_efficacy(history: dict, min_samples: int = 5) -> dict | None:
    """Group resolved predictions by ``critic_verdict`` and report the
    win rate per bucket.

    Goal: detect whether the critic AI's verdicts (keep / downgrade /
    reject) are predictive of outcomes. A healthy critic should show
    ``reject < downgrade < keep`` in win rate — rejected picks (which
    we *did* still record at the time the verdict was issued, before
    discovery_cap dropped them) should empirically be the worst.

    Returns ``None`` when no resolved prediction carries a non-null
    ``critic_verdict`` field (legacy data). Otherwise:

    ``{verdict_name: {n, wins, accuracy_pct, mean_dir_return_pct}}``
    """
    verdict_buckets: dict[str, list[dict]] = {}
    for p in history.get("predictions", []):
        if p.get("status") not in ("win", "loss"):
            continue
        v = p.get("critic_verdict")
        if not isinstance(v, str) or v not in ("keep", "downgrade", "reject"):
            continue
        verdict_buckets.setdefault(v, []).append(p)
    if not verdict_buckets:
        return None

    out: dict[str, dict] = {}
    for v, items in verdict_buckets.items():
        if len(items) < min_samples:
            continue
        wins = sum(1 for p in items if p.get("status") == "win")
        dir_returns = [r for r in (_directional_return(p) for p in items) if r is not None]
        mean_dir = sum(dir_returns) / len(dir_returns) if dir_returns else 0.0
        out[v] = {
            "n": len(items),
            "wins": wins,
            "accuracy_pct": round(wins / len(items) * 100, 1),
            "mean_dir_return_pct": round(mean_dir, 2),
        }
    return out or None


def format_critic_efficacy(efficacy: dict[str, dict] | None) -> str:
    """One-block summary suitable for the weekly review prompt. Empty
    string when no efficacy data is available so the caller can
    unconditionally concatenate."""
    if not efficacy:
        return ""
    order = ("keep", "downgrade", "reject")
    rows = []
    for v in order:
        d = efficacy.get(v)
        if not isinstance(d, dict):
            continue
        rows.append(
            f"  - {v:9} : n={d.get('n', '?'):3} 勝率 {d.get('accuracy_pct', '?')}%  "
            f"mean_dir_return {d.get('mean_dir_return_pct', '?')}%"
        )
    if not rows:
        return ""
    header = "=== Critic 二次評価の予測効果 (verdict 別の勝率) ==="
    return header + "\n" + "\n".join(rows)


def _compute_by_regime(
    resolved: list[dict],
    min_n: int = 5,
) -> dict | None:
    """Group resolved trades by the regime stamped at prediction time.

    Output: ``{regime: {n, wins, accuracy_pct, mean_dir_return_pct,
    by_direction: {UP|DOWN: {...}}}}``. Regimes with fewer than
    ``min_n`` resolved trades are dropped from the result (the
    sample is too small for a reliable rate). Returns ``None``
    when no resolved trade carries a regime field — e.g. when only
    legacy data exists before regime stamping landed.
    """
    from collections import defaultdict

    buckets: dict[str, list[dict]] = defaultdict(list)
    for p in resolved:
        regime = p.get("regime")
        if not isinstance(regime, str) or not regime:
            continue
        buckets[regime].append(p)

    if not buckets:
        return None

    out: dict[str, dict] = {}
    for regime_name, items in buckets.items():
        if len(items) < min_n:
            continue
        wins = sum(1 for p in items if p.get("status") == "win")
        dir_returns = [r for r in (_directional_return(p) for p in items) if r is not None]
        mean_dir = sum(dir_returns) / len(dir_returns) if dir_returns else 0.0
        # Per-direction sub-buckets (no min_n filter — UP/DOWN within a
        # regime are reported even at small n because the size flags
        # itself as caveat).
        by_dir: dict[str, dict] = {}
        for direction in ("UP", "DOWN"):
            in_dir = [p for p in items if p.get("prediction") == direction]
            if not in_dir:
                continue
            d_wins = sum(1 for p in in_dir if p.get("status") == "win")
            d_returns = [r for r in (_directional_return(p) for p in in_dir) if r is not None]
            d_mean = sum(d_returns) / len(d_returns) if d_returns else 0.0
            by_dir[direction] = {
                "n": len(in_dir),
                "wins": d_wins,
                "winrate_pct": round(d_wins / len(in_dir) * 100, 1),
                "mean_dir_return_pct": round(d_mean, 2),
            }
        out[regime_name] = {
            "n": len(items),
            "wins": wins,
            "accuracy_pct": round(wins / len(items) * 100, 1),
            "mean_dir_return_pct": round(mean_dir, 2),
            "by_direction": by_dir,
        }
    return out or None


def _compute_recent_direction_winrate(
    resolved: list[dict],
    recent_n: int = 14,
    min_dir_n: int = 5,
) -> dict | None:
    """Split the most recent ``recent_n`` resolved trades by predicted
    direction and report win-rate + mean directional return per direction.

    The all-time ``by_confidence_direction`` bucket is dominated by older
    trades; if the AI's UP picks just collapsed in the last two weeks but
    the long-run UP win rate is still 58 %, no per-bucket gate fires. This
    function gives the gate a *fresh* signal split by direction so it can
    react.

    Returns ``None`` when the recent window is too small for either
    direction to clear ``min_dir_n``. Otherwise:
    ``{
        "recent_n": <total resolved in window>,
        "UP":   {"n": int, "wins": int, "winrate_pct": float,
                 "mean_dir_return_pct": float} | None,
        "DOWN": {"n": int, "wins": int, "winrate_pct": float,
                 "mean_dir_return_pct": float} | None,
    }``

    A direction key is ``None`` when its sample in the window is below
    ``min_dir_n`` (interpret as "not enough data to gate on this direction").
    """
    chrono = sorted(
        (p for p in resolved if p.get("reviewed_date") and p.get("prediction") in {"UP", "DOWN"}),
        key=lambda p: p["reviewed_date"],
    )
    if len(chrono) < min_dir_n:
        return None
    recent = chrono[-recent_n:]
    if not recent:
        return None
    out: dict = {"recent_n": len(recent)}
    have_any = False
    for direction in ("UP", "DOWN"):
        in_dir = [p for p in recent if p.get("prediction") == direction]
        if len(in_dir) < min_dir_n:
            out[direction] = None
            continue
        wins = sum(1 for p in in_dir if p.get("status") == "win")
        dir_returns = [r for r in (_directional_return(p) for p in in_dir) if r is not None]
        mean_dir_ret = sum(dir_returns) / len(dir_returns) if dir_returns else 0.0
        out[direction] = {
            "n": len(in_dir),
            "wins": wins,
            "winrate_pct": round(wins / len(in_dir) * 100, 1),
            "mean_dir_return_pct": round(mean_dir_ret, 2),
        }
        have_any = True
    return out if have_any else None


# Calibration thresholds (codex 設計相談、issue #46; 2026-06-24 改修)。
#
# 2026-06-24 の重要な設計変更: calibration を **2 軸に分離**した。
#
#   (1) zone (green/yellow/red) = 全体サーキットブレーカー。直近の総合
#       performance (drift / 実損 / rolling Sharpe) だけで決まる。
#       strategy_governor の weight 学習と discovery top_n を gate する。
#   (2) high_status (ok/probation/suppressed) = HIGH ラベルの校正状態。
#       HIGH 出力の可否だけを制御し、weight 学習や top_n は止めない。
#
# 旧実装は「HIGH の *通算* accuracy 逆転」を zone red に直結させていた。
# その結果: red → prompt が HIGH 禁止 → LLM が HIGH を出さなくなる →
# 通算 HIGH バケットが永久凍結 → ratio 逆転のまま永久 red、という吸収
# 状態 (absorbing state) のデッドロックに陥り、weight 学習と discovery
# breadth が巻き添えで凍結していた (2026-06 発覚: 6月の HIGH 出力 0 件、
# discovery 4.7→1.6 銘柄/日)。
#
# high_status は **直近 window** の HIGH バケットで判定し、HIGH が静かに
# なって直近サンプルが枯れたら probation に落として「厳格基準で少数の
# HIGH を再試験」する回復経路を必ず残す。これが唯一デッドロックを抜ける道。
_CALIBRATION_RATIO_RED_THRESHOLD = 0.9
_CALIBRATION_MIN_BUCKET_N = 15
_ROLLING_SHARPE_WINDOW = 14
_CALIBRATION_RECOVERY_WINDOWS = 2
# high_status を判定する直近 window。HIGH が ~3 週間出力されなければ
# (= かつて suppress された) recent HIGH が min サンプルを割って probation
# に落ち、再試験経路が開く。健全期は HIGH ~1.5/日 なので 30 日で >=15 件
# 貯まり「直近データで suppressed」も到達可能 (実データ検証 2026-06-24)。
_CALIBRATION_RECENT_DAYS = 30


def _recent_resolved_within_days(resolved: list[dict], days: int) -> list[dict]:
    """Resolved trades whose ``reviewed_date`` falls within ``days`` of the
    most recent reviewed trade. Anchored on the latest date *in the data*
    (not wall-clock ``now``) so the result is deterministic and test-stable.
    """
    dated = [(p.get("reviewed_date") or p.get("date"), p) for p in resolved]
    dated = [(d, p) for d, p in dated if d]
    if not dated:
        return []
    anchor = max(d for d, _ in dated)
    try:
        cutoff = (datetime.fromisoformat(anchor) - timedelta(days=days)).strftime("%Y-%m-%d")
    except ValueError:
        return [p for _, p in dated]
    return [p for d, p in dated if d >= cutoff]


def _is_high_inverted(buckets: dict) -> tuple[bool, list[str], int]:
    """Given confidence buckets, return (inverted, reasons, high_n).

    Inverted = HIGH の accuracy が MEDIUM の ``_CALIBRATION_RATIO_RED_THRESHOLD``
    倍を下回る、または HIGH Brier > MEDIUM Brier (確率精度が逆転)。
    HIGH と MEDIUM の双方が ``_CALIBRATION_MIN_BUCKET_N`` 件以上ある場合のみ
    判定する (小サンプル断定回避)。閾値割れしなければ (False, [], high_n)。
    """
    high = buckets.get("HIGH", {})
    medium = buckets.get("MEDIUM", {})
    high_n = high.get("total", 0)
    if high_n < _CALIBRATION_MIN_BUCKET_N or medium.get("total", 0) < _CALIBRATION_MIN_BUCKET_N:
        return False, [], high_n
    reasons: list[str] = []
    high_acc, medium_acc = high.get("accuracy_pct"), medium.get("accuracy_pct")
    if high_acc is not None and medium_acc and medium_acc > 0:
        ratio = high_acc / medium_acc
        if ratio < _CALIBRATION_RATIO_RED_THRESHOLD:
            reasons.append(f"HIGH {high_acc:.1f}% / MEDIUM {medium_acc:.1f}% = ratio {ratio:.2f} < 0.90")
    high_brier, medium_brier = high.get("brier_score"), medium.get("brier_score")
    if isinstance(high_brier, (int, float)) and isinstance(medium_brier, (int, float)) and high_brier > medium_brier:
        reasons.append(f"Brier 逆転 (HIGH {high_brier:.3f} > MEDIUM {medium_brier:.3f})")
    return bool(reasons), reasons, high_n


def _compute_high_status(resolved: list[dict], lifetime_conf: dict) -> dict:
    """HIGH ラベルの校正状態を **直近 window** 優先で判定する (zone とは別軸)。

    - ``suppressed``: 直近 window の HIGH サンプルが十分 (>= min) かつ逆転。
      fresh evidence で HIGH が壊れている → HIGH 禁止。
    - ``probation``: HIGH が静か (直近 HIGH < min) かつ *通算* では逆転して
      いた。厳格基準で少数の HIGH を再試験して証拠を再生成する回復状態。
      旧デッドロックを抜ける唯一の経路。
    - ``ok``: 直近 HIGH が校正 OK、または通算でも一度も問題が無かった。
    """
    recent = _recent_resolved_within_days(resolved, _CALIBRATION_RECENT_DAYS)
    recent_buckets = _compute_confidence_buckets(recent)
    recent_high = recent_buckets.get("HIGH", {})
    recent_high_n = recent_high.get("total", 0)

    recent_inverted, recent_reasons, _ = _is_high_inverted(recent_buckets)
    if recent_high_n >= _CALIBRATION_MIN_BUCKET_N:
        # 直近データで判定できる — それが正。
        status = "suppressed" if recent_inverted else "ok"
        reasons = [f"直近{_CALIBRATION_RECENT_DAYS}日: {r}" for r in recent_reasons]
        return {
            "high_status": status,
            "high_reasons": reasons,
            "high_recent_n": recent_high_n,
            "high_recent_acc_pct": recent_high.get("accuracy_pct"),
            "high_lifetime_n": lifetime_conf.get("HIGH", {}).get("total", 0),
            "high_lifetime_acc_pct": lifetime_conf.get("HIGH", {}).get("accuracy_pct"),
        }

    # 直近 HIGH が枯れている — 通算で逆転実績があれば probation で再試験。
    lifetime_inverted, lifetime_reasons, lifetime_high_n = _is_high_inverted(lifetime_conf)
    if lifetime_inverted:
        reasons = [
            f"通算で HIGH 逆転 ({r})、ただし直近{_CALIBRATION_RECENT_DAYS}日の HIGH は "
            f"{recent_high_n} 件 (< {_CALIBRATION_MIN_BUCKET_N}) → probation で再試験"
            for r in lifetime_reasons[:1]
        ]
        status = "probation"
    else:
        reasons = []
        status = "ok"
    return {
        "high_status": status,
        "high_reasons": reasons,
        "high_recent_n": recent_high_n,
        "high_recent_acc_pct": recent_high.get("accuracy_pct"),
        "high_lifetime_n": lifetime_high_n,
        "high_lifetime_acc_pct": lifetime_conf.get("HIGH", {}).get("accuracy_pct"),
    }


def _compute_calibration_zone(
    resolved: list[dict],
    confidence_stats: dict,
    drift: dict | None,
) -> dict | None:
    """2 軸の calibration verdict を返す (詳細は上部の設計コメント参照)。

    - ``zone`` (green/yellow/red): 全体サーキットブレーカー。**直近の総合
      performance** (drift / 実損 / rolling Sharpe) だけで決まる。weight 学習
      と discovery top_n を gate する。HIGH ラベルの過去の傷では発火しない。
    - ``high_status`` (ok/probation/suppressed): HIGH 出力の可否のみ制御。

    サンプル不足で何も判定できないときは None。
    """
    if not resolved:
        return None
    reasons_red: list[str] = []
    reasons_yellow: list[str] = []

    # --- 全体サーキットブレーカー (直近 performance only) ---
    if drift is not None:
        is_drift = drift.get("is_drift", False)
        recent_exp = drift.get("recent_expectancy_pct", 0.0)
        if is_drift and recent_exp < 0:
            reasons_red.append(f"drift + 実損 (recent EV {recent_exp:.2f}%/trade、p={drift.get('p_value')})")
        elif is_drift:
            reasons_yellow.append(f"drift 検知 (recent EV {recent_exp:.2f}%、p={drift.get('p_value')})")

    rolling = _rolling_sharpe(resolved, window=_ROLLING_SHARPE_WINDOW)
    if rolling is not None and rolling < 0:
        reasons_yellow.append(f"rolling Sharpe {rolling:.2f} < 0 (recent {_ROLLING_SHARPE_WINDOW} trades)")

    if reasons_red:
        zone = "red"
    elif reasons_yellow:
        zone = "yellow"
    else:
        zone = "green"

    # Recovery: latest + prev window が両方 green で「2 連続改善」復帰。
    recovery_ok = zone == "green" and _prev_window_was_green(resolved)

    # --- HIGH ラベルの校正状態 (別軸、直近 window 優先) ---
    high = _compute_high_status(resolved, confidence_stats)

    return {
        "zone": zone,
        "reasons": reasons_red + reasons_yellow,
        "recovery_confirmed": recovery_ok,
        "rolling_sharpe": round(rolling, 2) if rolling is not None else None,
        **high,
    }


def _rolling_sharpe(resolved: list[dict], window: int) -> float | None:
    """Sharpe-like ratio (mean / stdev) over the last ``window`` resolved
    trades. Returns None when sample is too small for a meaningful stdev.
    """
    chrono = sorted(
        (p for p in resolved if p.get("reviewed_date") and _directional_return(p) is not None),
        key=lambda p: p["reviewed_date"],
    )
    if len(chrono) < window:
        return None
    recent = chrono[-window:]
    returns = [_directional_return(p) for p in recent if _directional_return(p) is not None]
    if len(returns) < 3:
        return None
    mean_r = sum(returns) / len(returns)
    variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    if variance <= 0:
        return None
    stdev = variance**0.5
    return mean_r / stdev


def _prev_window_was_green(resolved: list[dict]) -> bool:
    """True if the window of trades **just before the latest** is also Green.

    Mirror logic of the main zone computation but on the prior window,
    so 2-consecutive-improvement gate (codex recovery requirement) can
    be evaluated without persistent state — just look one window back.
    """
    window = _ROLLING_SHARPE_WINDOW
    chrono = sorted(
        (p for p in resolved if p.get("reviewed_date") and _directional_return(p) is not None),
        key=lambda p: p["reviewed_date"],
    )
    if len(chrono) < 2 * window:
        return False  # not enough data to confirm "2 連続"
    prev_window_trades = chrono[-2 * window : -window]
    returns = [_directional_return(p) for p in prev_window_trades if _directional_return(p) is not None]
    if len(returns) < 3:
        return False
    mean_r = sum(returns) / len(returns)
    variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    if variance <= 0:
        return False
    stdev = variance**0.5
    sharpe = mean_r / stdev
    # Green proxy: prev window の Sharpe > 0 + win率 > 50%
    wins = sum(1 for r in returns if r > 0)
    win_rate = wins / len(returns)
    return sharpe > 0 and win_rate > 0.5


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

    # ---- 利益基準ブロック (最適化目標) ----------------------------------
    # user の目的は「予測を当てること」ではなく「利益を出すこと」。採点は
    # 市場超過リターン基準で行い、的中率系の統計は参考値として後段に置く
    # (2026-07-04 目的最適化 pivot, #51)。
    lines.append(
        "【最適化目標 = 市場超過リターン】成績は「的中したか」ではなく"
        "「同じ期間に TOPIX ETF を買うより儲かったか」で採点されます。"
        "以下の利益基準 metric の改善が目的で、的中率は参考値にすぎません。"
    )

    # 市場相対採点 (skill vs β)。当たっても市場と同じ動きなら excess は
    # ゼロ = 指数を買えば同じ。UP の excess <= 0 は「銘柄選択がインデックス
    # 投資に負けている」ことを意味する — 利益目的なら最重要の警告。
    br = stats.get("benchmark_relative")
    if isinstance(br, dict) and isinstance(br.get("overall"), dict):
        o = br["overall"]
        lines.append(
            f"📐 市場相対 (vs {br['benchmark']}): 方向調整後の超過リターン "
            f"{o['mean_dir_excess_pct']:+.2f}%/件 / ベンチ超え {o['beat_benchmark_pct']}% (n={o['n']})"
        )
        up_br = br.get("up")
        if isinstance(up_br, dict):
            marker = " ⚠" if up_br["mean_dir_excess_pct"] <= 0 else ""
            lines.append(
                f"  UP: 超過 {up_br['mean_dir_excess_pct']:+.2f}%/件 (ベンチ超え {up_br['beat_benchmark_pct']}%)"
                f"{marker}"
            )
            if up_br["mean_dir_excess_pct"] <= 0:
                lines.append(
                    "  ⚠ UP 予測は「指数を買うだけ」に負けています。銘柄選択が市場超過価値を"
                    "生んでいないので、確信の薄い UP を出すくらいなら NO_TRADE を選んでください"
                )
        down_br = br.get("down")
        if isinstance(down_br, dict):
            lines.append(
                f"  DOWN: 超過 {down_br['mean_dir_excess_pct']:+.2f}%/件 — "
                "市場ごと下がった分 (β) は除外済み。この値が小さいなら DOWN 的中の大半は地合いです"
            )

    # 実現可能 EV (2026-07-04 監査 #51)。名目 EV は holdings への DOWN
    # 的中 (ショート不能 = 実現不可) で水増しされる。収益性の自己評価は
    # 「UP 買い建てのみ」の net EV を正とし、DOWN は助言/情報として別枠
    # 表示する。
    rz = stats.get("realizable_expectancy")
    if isinstance(rz, dict):
        up_b = rz.get("realizable_up")
        if isinstance(up_b, dict):
            up_marker = "⚠" if up_b["net_expectancy_pct"] <= 0 else "✓"
            lines.append(
                f"💴 実現可能 EV (UP 買い建てのみ, {up_b['n']}件): "
                f"勝率 {up_b['accuracy_pct']}% / net {up_b['net_expectancy_pct']:+.2f}%/trade {up_marker}"
            )
            if up_b["net_expectancy_pct"] <= 0:
                lines.append(
                    "  ⚠ 買いで再現できる期待値がゼロ以下です。DOWN 的中は損益に変換できないため、"
                    "収益化には UP 予測の質そのものを上げる必要があります"
                )
        adv = rz.get("holdings_down_advice")
        if isinstance(adv, dict):
            lines.append(
                f"  参考 — holdings DOWN (売り助言, {adv['n']}件): 的中 {adv['accuracy_pct']}% / "
                f"回避可能だった下落幅 {adv['gross_expectancy_pct']:+.2f}%/件 (実現利益ではない)"
            )

    # Net expectancy (Phase 4 #46): 手数料込み EV。gross が正でも net が
    # 負なら実損 → 慎重判断を促す signal。
    net_ev = stats.get("net_expectancy")
    if isinstance(net_ev, dict):
        gross = net_ev["gross_expectancy_pct"]
        net = net_ev["net_expectancy_pct"]
        cost = net_ev["round_trip_cost_pct"]
        marker = "⚠" if net < 0 else "✓"
        lines.append(f"💰 net EV: 名目 {gross:+.2f}%/trade、手数料 {cost:.1f}% 控除後 {net:+.2f}%/trade {marker}")
        if net < 0:
            lines.append("  ⚠ net で実損です。エントリー数を絞るか、勝率自体を上げる必要があります")

    # Paper portfolio NAV one-liner (Phase 6 — DD 172% root cause: trade-level
    # DD inflates daily re-recommendations). 100万円 / 5%-sized / 5並列の
    # NAV ベース DD は trade-level DD と桁違いに小さい。real-money decision
    # は NAV DD で考えるべきなので、日次 feedback に常時表示する。
    try:
        from stock_analyzer.backtest import _DEFAULT_TC_ROUND_TRIP_PCT, simulate_paper_portfolio

        paper = simulate_paper_portfolio(
            history,
            initial_nav=1_000_000.0,
            position_size_pct=5.0,
            max_concurrent=5,
            tc_round_trip_pct=_DEFAULT_TC_ROUND_TRIP_PCT,
        )
        if paper.positions > 0:
            ret_pct = (paper.final_nav - paper.initial_nav) / paper.initial_nav * 100
            lines.append(
                f"📊 paper portfolio (100万 / 5%×5並列): NAV {paper.final_nav:,.0f}円 "
                f"({ret_pct:+.1f}%) / max DD {paper.max_drawdown_pct:.2f}% / "
                f"{paper.positions} ポジ"
            )
    except Exception:
        pass

    # ---- 参考統計 (的中率ベース) ----------------------------------------
    lines.append("--- 以下は参考統計 (的中率ベース、最適化対象ではない) ---")
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

    # Drawdown stop — hard directive when the *recent* equity curve is
    # sitting well below its window peak. Independent of expectancy /
    # calibration: drawdown discipline limits damage from a regime
    # change the rest of the metrics haven't priced in yet.
    # 判定は直近 _RECENT_DD_WINDOW trade の窓内 DD。通算累積和 DD
    # (current_drawdown_pct) を使うと一度沈んだら二度と回復せず HIGH が
    # 恒久禁止になる (2026-07-04 監査で修正、probation 再試験の前提)。
    recent_dd = stats.get("recent_drawdown_pct")
    if recent_dd is not None and recent_dd >= 15.0:
        window_n = stats.get("recent_drawdown_window", _RECENT_DD_WINDOW)
        lines.append(
            f"  ⚠ 直近 {window_n} trade の drawdown が {recent_dd:.1f}pp で 15pp 閾値超過。"
            "今回は新規 HIGH 信頼度の付与を停止し、MEDIUM 以下のみ推奨してください。"
            "直近成績が回復するまで HIGH 復活させない"
        )

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

    # Episode 単位の成績 (擬似反復補正)。holdings の連日再予測が名目 n を
    # 水増しするため、独立サンプルに近い episode 単位を常に併記して
    # headline との乖離を AI に見せる。乖離 3pp 超は明示警告。
    ep = stats.get("episode_stats")
    if isinstance(ep, dict) and ep.get("n_episodes"):
        ep_ev = ep.get("mean_dir_return_pct")
        ev_text = f" / 期待値 {ep_ev:+.2f}%/件" if ep_ev is not None else ""
        lines.append(
            f"重複補正後 (同一ポジション連日再予測を1件に集約): "
            f"{ep['n_episodes']}件 (名目 {ep['n_raw']}件) / 勝率 {ep['accuracy_pct']}%{ev_text}"
        )
        headline_acc = stats.get("accuracy_pct")
        if headline_acc is not None and headline_acc - ep["accuracy_pct"] > 3.0:
            lines.append("  ⚠ 名目勝率が重複計上で上振れしています。自己評価は重複補正後の値を基準にしてください")

    # Recent trend
    recent_acc = stats.get("recent_accuracy_pct")
    overall_acc = stats.get("accuracy_pct")
    if recent_acc is not None and overall_acc is not None:
        if recent_acc > overall_acc + 5:
            lines.append(f"直近トレンド: 改善中（直近{recent_acc}% vs 通算{overall_acc}%）")
        elif recent_acc < overall_acc - 5:
            lines.append(f"直近トレンド: 悪化中（直近{recent_acc}% vs 通算{overall_acc}%）")

    # Recent UP hit rate — surfaced regardless of gate state so the AI
    # can self-correct on the trajectory (recovering above threshold)
    # without waiting for an explicit gate flip. The actual gating
    # text is prepended outside this function via
    # ``build_up_gate_directive`` when below_threshold is true.
    up_stat = stats.get("recent_up_hit_rate")
    if isinstance(up_stat, dict):
        marker = " ⚠" if up_stat.get("below_threshold") else ""
        up_ev = up_stat.get("mean_dir_return_pct")
        ev_text = f", 期待値 {up_ev:+.2f}%/件" if up_ev is not None else ""
        lines.append(
            f"直近 UP 予測勝率: {up_stat['hit_rate_pct']}% "
            f"({up_stat['wins']}/{up_stat['recent_n']}件, 閾値 {up_stat['threshold_pct']:.0f}%{ev_text}){marker}"
        )

    # Strategy-drift early warning. Expectancy (not accuracy) is the
    # right metric here: a strategy can stay 55% accurate but bleed
    # money if win-size shrinks while loss-size grows. ⚠ fires on a
    # 2 percentage-point drop vs older baseline — small enough to
    # catch quietly but large enough to be statistically meaningful
    # on ~14 vs ~10+ samples.
    drift = stats.get("drift_indicator")
    if drift is not None:
        p_text = ""
        p_val = drift.get("p_value")
        if isinstance(p_val, (int, float)):
            p_text = f", t-test p={p_val:.3f}"
        lines.append(
            f"戦略ドリフト: 直近{drift['recent_n']}件期待値 {drift['recent_expectancy_pct']:+.2f}%/件 "
            f"vs ベースライン{drift['baseline_n']}件 {drift['baseline_expectancy_pct']:+.2f}%/件 "
            f"({drift['delta_pp']:+.2f}pp{p_text})"
        )
        if drift.get("is_drift"):
            lines.append(
                "  ⚠ 統計的に有意な期待値低下を検出 (p<0.10)。最近の判断バイアスが効いている可能性。"
                "今回は新規 HIGH の付与をさらに厳格にし、迷ったら MEDIUM に下げてください"
            )

    # Overpriced bias 検証 (Phase 5 #46): HIGH bucket の事前 5/21/63d
    # return が MEDIUM/LOW より大幅高なら "buying high" bias の証拠。
    op_bias = stats.get("overpriced_bias")
    if isinstance(op_bias, dict) and op_bias:
        lines.append("📈 overpriced bias 検証 (HIGH が事前上昇済みか):")
        for conf in ("HIGH", "MEDIUM", "LOW"):
            entry = op_bias.get(conf)
            if entry:
                lines.append(
                    f"  {conf}: 事前5d {entry['mean_5d_ret_pct']:+.2f}%, "
                    f"21d {entry.get('mean_1m_ret_pct', '?')}%, "
                    f"63d {entry.get('mean_3m_ret_pct', '?')}% (n={entry['n']})"
                )
        # HIGH と MEDIUM の比較で大幅差なら警告
        high_e = op_bias.get("HIGH")
        med_e = op_bias.get("MEDIUM")
        if high_e and med_e:
            diff_1m = high_e["mean_1m_ret_pct"] - med_e["mean_1m_ret_pct"]
            if diff_1m > 5.0:
                lines.append(f"  ⚠ HIGH の事前 21d return が MEDIUM より +{diff_1m:.1f}pp 高い (overpriced 疑い)")

    # Walk-forward CV (Phase 4 #46): purged CV で current weight set の
    # OOS パフォーマンス。fold 別 top quantile accuracy の mean ± stdev。
    cv = stats.get("walkforward_cv")
    if isinstance(cv, dict):
        mean_acc = cv["mean_top_quantile_acc_pct"]
        stdev_pp = cv["stdev_acc_pp"]
        n_folds = cv["n_folds_used"]
        lines.append(
            f"🔬 walk-forward CV ({n_folds} folds, embargo {cv['embargo_days']}日): "
            f"上位 30% picks の OOS accuracy {mean_acc:.1f}% ± {stdev_pp:.1f}pp"
        )

    # Signal correlation pairs (Phase 4 #46): 重複投票疑いの signal ペアを
    # 表示。|r| > 0.7 は強相関 = 同情報重複、削除 or 統合候補。
    corr_pairs = stats.get("signal_correlation_pairs")
    if isinstance(corr_pairs, list) and corr_pairs:
        strong = [p for p in corr_pairs if abs(p["correlation"]) >= 0.7]
        if strong:
            lines.append("🔗 高相関 signal ペア (|r|≥0.7、重複投票疑い):")
            for p in strong[:5]:
                a, b = p["pair"]
                r = p["correlation"]
                lines.append(f"  {a} ↔ {b}: r={r:+.2f} (共起 {p['n_both']})")

    # Reliability diagram の可視化 (Phase 2 #46): predicted_prob と
    # observed_acc の gap を表示。HIGH gap が大きく負なら overconfident。
    reliability = stats.get("reliability_diagram")
    if isinstance(reliability, list) and reliability:
        lines.append("📊 信頼度図 (predicted vs observed accuracy):")
        for bin in reliability:
            conf = bin["confidence"]
            pred = bin["predicted_prob"]
            obs = bin["observed_acc"]
            gap = bin["gap_pp"]
            n = bin["n"]
            indicator = "⚠ overconfident" if gap < -10 else "⚠ underconfident" if gap > 10 else "✓ ok"
            lines.append(f"  {conf}: 予測{pred:.0%} → 実観測{obs:.0%} (gap {gap:+.1f}pp, n={n}) {indicator}")

    # Signal-confidence 分離分析 (Phase 2 #46, D 仮説 1 検証):
    # signal_count が confidence に相関してれば「signal の多寡が confidence
    # を駆動」、無相関なら Claude が signal 以外の何か (材料 narrative 等)
    # で confidence を判定してる証拠。
    sig_breakdown = stats.get("confidence_signal_breakdown")
    if isinstance(sig_breakdown, dict) and sig_breakdown:
        lines.append("🔍 confidence × signal_count (signal が confidence を駆動してるか):")
        for conf in ("HIGH", "MEDIUM", "LOW"):
            entry = sig_breakdown.get(conf)
            if entry:
                lines.append(f"  {conf}: 平均 {entry['mean_signal_count']} signals/予測 (n={entry['n']})")
            else:
                lines.append(f"  {conf}: (記録あるサンプル不足、min {_MIN_BUCKET_N})")

    # Calibration zone (Red/Yellow/Green) — issue #46 Phase 1 circuit
    # breaker。Red zone は HIGH 出力禁止、Yellow zone は HIGH 厳格化を
    # prompt に強制する。codex 設計相談 (2026-05-30) の階層化トリガに
    # 基づき、HIGH/MEDIUM accuracy ratio や rolling Sharpe を統合判定。
    zone_info = stats.get("calibration_zone")
    if isinstance(zone_info, dict):
        zone = zone_info.get("zone", "green")
        reasons = zone_info.get("reasons") or []
        # (1) 全体サーキットブレーカー (直近 performance: drift / 実損 / Sharpe)
        if zone == "red":
            lines.append(
                "🟥 サーキットブレーカー Red — 直近で drift + 実損を検知。"
                "新規 picks を厳格化し、short_term は通常の半分以下に絞ること。"
            )
            for r in reasons:
                lines.append(f"  - {r}")
        elif zone == "yellow":
            lines.append(
                "🟨 サーキットブレーカー Yellow — 直近不調の兆候。boldness 控えめ、short_term picks はやや絞る。"
            )
            for r in reasons:
                lines.append(f"  - {r}")
        elif zone_info.get("recovery_confirmed"):
            lines.append("✅ サーキットブレーカー Green (2 連続 window 改善で復帰) — 通常運用に戻して構いません")
        # Green かつ recovery 未確認は静か (= 改善途上、特別指示なし)

        # (2) HIGH ラベルの校正状態 (別軸 — weight 学習や top_n は止めない)
        high_status = zone_info.get("high_status")
        high_reasons = zone_info.get("high_reasons") or []
        if high_status == "suppressed":
            lines.append(
                "🟥 HIGH 校正不良 (直近データで逆転) — HIGH 出力は禁止します。"
                "すべて MEDIUM 以下に降格してください。HIGH_UP は特に出さない。"
            )
            for r in high_reasons:
                lines.append(f"  - {r}")
        elif high_status == "probation":
            lines.append(
                "🟨 HIGH probation — 過去に HIGH の校正不良があったため再試験中。"
                "HIGH は『テクニカル(SMA/MACD/RSI)+ファンダ(成長 or 割安)+強いカタリスト』の "
                "3 層すべてが同方向で揃った稀なケース限定で出してよい。"
                "再校正のため適格な HIGH は歓迎するが乱発禁止、迷ったら MEDIUM。"
                "UP 方向の HIGH は history 最弱なので特に慎重に。"
            )
            for r in high_reasons:
                lines.append(f"  - {r}")

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
