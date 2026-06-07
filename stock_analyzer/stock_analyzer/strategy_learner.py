from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"
_STRATEGY_NOTES_FILE = str(_DATA_DIR / "strategy_notes.json")
_SCREENING_WEIGHTS_FILE = str(_DATA_DIR / "screening_weights.json")

# Default screening weights (used when no tuned weights exist)
DEFAULT_WEIGHTS = {
    "rsi_oversold_recovery": 20,
    "rsi_healthy_momentum": 15,
    "volume_spike": 20,
    # Tiered volume surge + volume-confirmed breakout (2026-05-30 追加、
    # technical_indicators.compute_screening_score と同期)。3x / 5x の
    # 階層化で「機関の本気買い候補」を screening 上位に押し上げる。
    "volume_surge": 35,
    "volume_blowoff": 50,
    "volume_breakout": 30,
    "sma25_breakout": 20,
    "macd_crossover": 15,
    "bollinger_lower": 10,
    "per_value": 10,
    "pbr_undervalued": 10,
    "roe_profitable": 10,
    "dividend_yield": 5,
    "revenue_growth": 5,
}

# Bayesian shrinkage weight-retune constants (Phase 3 #46, codex C 推奨)
# β-binomial empirical Bayes:
#   posterior_rate = (k * overall_rate + wins) / (k + n)
# k = prior strength (= 仮想的な「全体勝率に基づく事前観測サンプル数」)。
# 小さい k は per-signal データを信用、大きい k は overall_rate へ強く縮約。
# 20 は ~20 signals × ~5-30 obs each の状況で安全な中庸値。
_BAYESIAN_PRIOR_STRENGTH_K = 20.0
# Scaling 上下限 (codex 推奨 ±10-20%、20% で run)。週次 retune で 1.20^N が
# 暴走しないよう 1 段階で大きく動かさない。
_WEIGHT_SCALE_CAP_UP = 1.20
_WEIGHT_SCALE_CAP_DOWN = 0.80
# 提案 weight の保存先 (active screening_weights.json とは別 file)。
# 自動反映なし、user / strategy_learner.apply_review_results 経由で
# 明示的に有効化する設計。
_PROPOSED_WEIGHTS_FILE = str(_DATA_DIR / "proposed_screening_weights.json")


def load_strategy_notes(path: str = _STRATEGY_NOTES_FILE) -> dict:
    """Load accumulated strategy notes."""
    p = Path(path)
    if not p.exists():
        return {"notes": [], "regime_strategies": {}, "last_review_date": None}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Failed to load strategy notes: %s", e)
        return {"notes": [], "regime_strategies": {}, "last_review_date": None}


def save_strategy_notes(data: dict, path: str = _STRATEGY_NOTES_FILE) -> None:
    """Save strategy notes."""
    p = Path(path)
    p.parent.mkdir(exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("Saved %d strategy notes", len(data.get("notes", [])))


def load_screening_weights(path: str = _SCREENING_WEIGHTS_FILE) -> dict:
    """Load screening weights. Falls back to defaults if file doesn't exist."""
    p = Path(path)
    if not p.exists():
        return DEFAULT_WEIGHTS.copy()
    try:
        with open(p, encoding="utf-8") as f:
            weights = json.load(f)
        # Merge with defaults to handle new keys
        merged = DEFAULT_WEIGHTS.copy()
        merged.update(weights)
        return merged
    except Exception as e:
        logger.warning("Failed to load screening weights: %s", e)
        return DEFAULT_WEIGHTS.copy()


def save_screening_weights(weights: dict, path: str = _SCREENING_WEIGHTS_FILE) -> None:
    """Save tuned screening weights."""
    p = Path(path)
    p.parent.mkdir(exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(weights, f, ensure_ascii=False, indent=2)
    logger.info("Saved screening weights")


def compute_bayesian_weight_proposal(
    predictions_history: dict,
    current_weights: dict | None = None,
    prior_strength_k: float = _BAYESIAN_PRIOR_STRENGTH_K,
) -> dict:
    """β-binomial empirical Bayes で signal weight の提案を計算。

    codex 設計 (Phase 3 #46):
      shrunk_rate = (k * overall_rate + wins) / (k + n)
      scaling = shrunk_rate / overall_rate
      new_weight = current_weight * clip(scaling, [0.80, 1.20])

    勝率 だけだと「signal=False のときの勝率」 を無視するので、本来は
    lift (with_signal_rate - without_signal_rate) を見たい。ここでは
    signal_efficacy が両方の数字を返してくれるので、shrunk version の
    両者を比べて、with > without が安定してる signal だけ weight up
    する 2-stage 設計。

    Returns:
      {
        "overall_win_rate": 0.58,
        "prior_strength_k": 20,
        "proposed": {
            "volume_spike": {
                "current_weight": 20,
                "raw_with_rate": 0.65,
                "shrunk_with_rate": 0.62,
                "raw_without_rate": 0.55,
                "shrunk_without_rate": 0.57,
                "shrunk_lift_pp": 5.0,
                "scaling_factor": 1.08,
                "proposed_weight": 22,
                "n_with": 18,
                "n_without": 200,
            },
            ...
        }
      }
    """
    from stock_analyzer.performance_tracker import compute_signal_efficacy

    efficacy = compute_signal_efficacy(predictions_history, min_samples=5)
    if not efficacy:
        return {"overall_win_rate": None, "proposed": {}}

    resolved = [p for p in predictions_history.get("predictions", []) if p.get("status") in ("win", "loss")]
    if not resolved:
        return {"overall_win_rate": None, "proposed": {}}
    overall_wins = sum(1 for p in resolved if p["status"] == "win")
    overall_rate = overall_wins / len(resolved)

    if current_weights is None:
        current_weights = DEFAULT_WEIGHTS.copy()

    proposed: dict[str, dict] = {}
    for signal, data in efficacy.items():
        n_with = data["with_signal"]["total"]
        wins_with = data["with_signal"]["wins"]
        n_without = data["without_signal"]["total"]
        wins_without = data["without_signal"]["wins"]
        # β-binomial shrinkage to overall_rate
        shrunk_with = (prior_strength_k * overall_rate + wins_with) / (prior_strength_k + n_with)
        shrunk_without = (prior_strength_k * overall_rate + wins_without) / (prior_strength_k + n_without)
        shrunk_lift = shrunk_with - shrunk_without
        # scaling は shrunk_with_rate / overall_rate を base、ただし
        # without_rate も考慮: with > without でなければ scaling を 1 に
        # 固定 (この signal は実勝率 boost してない)
        raw_scaling = (shrunk_with / overall_rate if overall_rate > 0 else 1.0) if shrunk_with > shrunk_without else 1.0
        scaling = min(_WEIGHT_SCALE_CAP_UP, max(_WEIGHT_SCALE_CAP_DOWN, raw_scaling))
        current_w = current_weights.get(signal, DEFAULT_WEIGHTS.get(signal, 10))
        new_w = round(current_w * scaling)
        proposed[signal] = {
            "current_weight": current_w,
            "raw_with_rate": round(data["with_signal"]["accuracy_pct"] / 100, 3),
            "shrunk_with_rate": round(shrunk_with, 3),
            "raw_without_rate": round(data["without_signal"]["accuracy_pct"] / 100, 3),
            "shrunk_without_rate": round(shrunk_without, 3),
            "shrunk_lift_pp": round(shrunk_lift * 100, 1),
            "scaling_factor": round(scaling, 2),
            "proposed_weight": new_w,
            "n_with": n_with,
            "n_without": n_without,
        }
    return {
        "overall_win_rate": round(overall_rate, 3),
        "prior_strength_k": prior_strength_k,
        "scale_cap_up": _WEIGHT_SCALE_CAP_UP,
        "scale_cap_down": _WEIGHT_SCALE_CAP_DOWN,
        "proposed": proposed,
    }


def save_proposed_weights(proposal: dict, path: str = _PROPOSED_WEIGHTS_FILE) -> None:
    """提案 weights を別 file に書き出し (active screening_weights.json
    は触らない)。dry-run のため、user が手動で active 化するまで反映なし。
    """
    p = Path(path)
    p.parent.mkdir(exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(proposal, f, ensure_ascii=False, indent=2)
    logger.info("Saved Bayesian weight proposal (dry-run) to %s", path)


def format_weight_proposal_for_prompt(proposal: dict, top_n: int = 8) -> str:
    """提案 weights の上位 / 下位 N 件を AI prompt 用に整形。

    全 signal を出すと冗長なので、scaling が ≠1.0 のものを scale 大きい
    順に並べて上位 N 件。「次回 retune したらこう変わる」見える化。
    Phase 3 dry-run なので「現状は反映してない、参考表示」 と明示。
    """
    proposed = proposal.get("proposed", {})
    if not proposed:
        return ""
    # scaling factor の |1 - x| 順 (= 変化幅 大きい順)
    sorted_items = sorted(
        proposed.items(),
        key=lambda kv: abs(1.0 - kv[1]["scaling_factor"]),
        reverse=True,
    )
    if not sorted_items:
        return ""
    overall = proposal.get("overall_win_rate")
    lines = ["📐 Bayesian weight 提案 (dry-run、現状は反映されません):"]
    if overall is not None:
        lines.append(f"  全体勝率: {overall:.1%} / 事前強度 k={proposal.get('prior_strength_k')}")
    lines.append("  signal | 現重み → 提案 | shrunk lift | n_with")
    for signal, data in sorted_items[:top_n]:
        delta_marker = (
            "🔺"
            if data["proposed_weight"] > data["current_weight"]
            else ("🔻" if data["proposed_weight"] < data["current_weight"] else "—")
        )
        lines.append(
            f"  {delta_marker} {signal}: {data['current_weight']} → {data['proposed_weight']}"
            f" (scale {data['scaling_factor']:.2f}, lift {data['shrunk_lift_pp']:+.1f}pp,"
            f" n={data['n_with']})"
        )
    lines.append("  → 反映するには `data/screening_weights.json` に proposed_weight を手動コピー")
    return "\n".join(lines)


def format_strategy_notes_for_prompt(notes_data: dict) -> str:
    """Format strategy notes for inclusion in Claude's analysis prompt."""
    notes = notes_data.get("notes", [])
    valid_notes = [n for n in notes if n.get("still_valid", True)]

    if not valid_notes:
        return ""

    lines = ["=== 蓄積された戦略メモ（過去の学習） ==="]
    lines.append("以下は過去の分析結果から学んだ教訓です。これらを考慮して分析してください。")
    lines.append("")

    # Group by category
    by_category: dict[str, list] = {}
    for note in valid_notes:
        cat = note.get("category", "general")
        by_category.setdefault(cat, []).append(note)

    category_labels = {
        "technical_pattern": "テクニカルパターン",
        "fundamental_insight": "ファンダメンタル知見",
        "market_regime": "市場レジーム",
        "sector_insight": "セクター知見",
        "risk_management": "リスク管理",
        "general": "一般",
    }

    for cat, cat_notes in by_category.items():
        label = category_labels.get(cat, cat)
        lines.append(f"【{label}】")
        for note in cat_notes:
            confidence_mark = {"HIGH": "★★★", "MEDIUM": "★★", "LOW": "★"}.get(note.get("confidence", ""), "★")
            lines.append(f"  {confidence_mark} {note['insight']}")
            if note.get("evidence"):
                lines.append(f"    根拠: {note['evidence']}")
        lines.append("")

    # Regime strategies
    regime = notes_data.get("regime_strategies", {})
    if regime:
        lines.append("【レジーム別推奨戦略】")
        for regime_name, strategy in regime.items():
            lines.append(f"  {regime_name}: {strategy}")
        lines.append("")

    return "\n".join(lines)


def _signal_efficacy_block(predictions_history: dict) -> str:
    """Build the signal-efficacy section for the weekly review prompt.

    Pulled out so callers that don't have predictions_history (older
    fixtures, edge cases) just get an empty string instead of an import
    cycle through performance_tracker.
    """
    try:
        from stock_analyzer.performance_tracker import (
            compute_signal_efficacy,
            format_signal_efficacy,
        )

        efficacy = compute_signal_efficacy(predictions_history)
        return format_signal_efficacy(efficacy)
    except Exception:
        return ""


def build_weekly_review_prompt(predictions_history: dict, strategy_notes: dict) -> str:
    """Build the prompt for Claude's weekly strategy review.

    This is the key mechanism for self-improvement. Claude deeply analyzes
    past predictions and generates actionable strategy insights.
    """
    predictions = predictions_history.get("predictions", [])
    stats = predictions_history.get("performance_stats", {})
    current_notes = strategy_notes.get("notes", [])

    resolved = [p for p in predictions if p["status"] in ("win", "loss")]
    recent_resolved = sorted(resolved, key=lambda p: p.get("reviewed_date", ""), reverse=True)[:20]

    wins = [p for p in recent_resolved if p["status"] == "win"]
    losses = [p for p in recent_resolved if p["status"] == "loss"]

    prompt = f"""\
あなたは株式投資戦略のリサーチャーです。
過去の予測結果を深く分析し、戦略を改善するための具体的な教訓を抽出してください。

=== 全体パフォーマンス ===
通算: {stats.get("wins", 0)}勝 {stats.get("losses", 0)}敗 (勝率{stats.get("accuracy_pct", "N/A")}%)
"""

    # Risk-adjusted P&L — accuracy alone is insufficient. A 60%
    # win-rate with -3% avg-win and +5% avg-loss is still net negative.
    risk_lines = []
    exp = stats.get("expectancy_per_trade_pct")
    if exp is not None:
        risk_lines.append(f"  期待値: {exp:+.2f}%/件 (= 平均的に 1 トレードでどれだけ得るか)")
    pf = stats.get("profit_factor")
    if pf is not None:
        risk_lines.append(f"  プロフィットファクター: {pf:.2f} (= 累積勝ち / 累積負け、1.0 超で P&L プラス)")
    sharpe = stats.get("sharpe_like_per_trade")
    if sharpe is not None:
        risk_lines.append(f"  Sharpe-like (per trade): {sharpe:+.2f}")
    max_dd = stats.get("max_drawdown_pct")
    if max_dd is not None:
        risk_lines.append(f"  最大ドローダウン: {max_dd:.1f}% (= 累積 P&L の最悪 peak→trough)")
    avg_w = stats.get("avg_return_wins")
    avg_l = stats.get("avg_return_losses")
    if avg_w is not None and avg_l is not None:
        risk_lines.append(f"  方向調整 avg-return: 的中 {avg_w:+.1f}% / 外れ {avg_l:+.1f}% (direction-aware)")
    if risk_lines:
        prompt += "リスク調整 P&L:\n" + "\n".join(risk_lines) + "\n"

    # Confidence breakdown + calibration inversion warning
    by_conf = stats.get("by_confidence", {})
    if by_conf:
        prompt += "信頼度別:\n"
        for conf in ("HIGH", "MEDIUM", "LOW"):
            c = by_conf.get(conf)
            if c:
                prompt += f"  {conf}: {c['accuracy_pct']}% ({c['wins']}/{c['total']}件)\n"
        high = by_conf.get("HIGH", {})
        medium = by_conf.get("MEDIUM", {})
        if (
            high.get("accuracy_pct") is not None
            and medium.get("accuracy_pct") is not None
            and high.get("total", 0) >= 5
            and medium.get("total", 0) >= 5
            and high["accuracy_pct"] < medium["accuracy_pct"]
        ):
            prompt += (
                f"⚠️ キャリブレーション逆転: "
                f"HIGH({high['accuracy_pct']}%) < MEDIUM({medium['accuracy_pct']}%) → "
                "HIGH の判定基準を厳格化する `confidence_calibration` の "
                "更新を必ず行ってください\n"
            )

    # Confidence × direction breakdown (cross-tab)
    by_cd = stats.get("by_confidence_direction", {})
    if by_cd:
        prompt += "信頼度 × 方向 (N>=5 のバケットのみ):\n"
        for key, val in sorted(by_cd.items()):
            prompt += f"  {key}: {val['accuracy_pct']}% ({val['wins']}/{val['total']}件)\n"

    # Source breakdown
    src_lines = []
    for src in ("holdings", "short_term", "long_term", "discovery"):
        v = stats.get(f"{src}_accuracy_pct")
        if v is not None:
            src_lines.append(f"  {src}: {v}%")
    if src_lines:
        prompt += "ソース別的中率:\n" + "\n".join(src_lines) + "\n"

    # Recent wins
    if wins:
        prompt += "\n=== 直近の成功予測 ===\n"
        for p in wins:
            prompt += (
                f"- {p.get('name', '')} ({p['ticker']}): "
                f"{p['prediction']}予測 → {p.get('actual_return_pct', 0):+.1f}% "
                f"信頼度:{p.get('confidence', '?')} "
                f"[{p['date']}]\n"
            )

    # Recent losses
    if losses:
        prompt += "\n=== 直近の失敗予測 ===\n"
        for p in losses:
            prompt += (
                f"- {p.get('name', '')} ({p['ticker']}): "
                f"{p['prediction']}予測 → {p.get('actual_return_pct', 0):+.1f}% "
                f"信頼度:{p.get('confidence', '?')} "
                f"[{p['date']}]\n"
            )

    # Signal efficacy — per-screening-signal realised win rate
    efficacy_block = _signal_efficacy_block(predictions_history)
    if efficacy_block:
        prompt += "\n" + efficacy_block + "\n"

    # Counterfactual backtest — re-run resolved history under canned
    # filters (HIGH-only, by direction, by source) to surface "what
    # filter would have most-improved Sharpe" as actionable evidence.
    # Two layers: gross sharpe ordering, plus net P&L with TC applied
    # so the AI sees which filters survive the realistic round-trip
    # cost and which ones look profitable on paper but bleed money.
    try:
        from stock_analyzer.backtest import (
            _DEFAULT_TC_ROUND_TRIP_PCT,
            compare_gross_vs_net,
            format_counterfactuals_for_prompt,
            format_gross_vs_net_for_prompt,
            standard_counterfactuals,
        )

        sims = standard_counterfactuals(predictions_history)
        bt_block = format_counterfactuals_for_prompt(sims)
        if bt_block:
            prompt += "\n" + bt_block + "\n"

        net_report = compare_gross_vs_net(predictions_history, _DEFAULT_TC_ROUND_TRIP_PCT)
        net_block = format_gross_vs_net_for_prompt(net_report)
        if net_block:
            prompt += "\n" + net_block + "\n"

        # Position-aware & paper portfolio. Trade-level DD is inflated when
        # the bot recommends the same (ticker, direction) daily (e.g. 3777.T
        # ran 21 DOWN preds in 30 days = 6x DD inflation). These views
        # collapse re-recommendations into single positions and simulate
        # what a 100万円 paper portfolio with 5%/position sizing would
        # actually experience — far closer to live-money decision context.
        from stock_analyzer.backtest import (
            format_paper_portfolio_summary,
            format_position_aware_summary,
            simulate_paper_portfolio,
            simulate_position_aware,
        )

        pos_result, episodes = simulate_position_aware(
            predictions_history, tc_round_trip_pct=_DEFAULT_TC_ROUND_TRIP_PCT
        )
        pos_block = format_position_aware_summary(pos_result, episodes)
        if pos_block:
            prompt += "\n" + pos_block + "\n"

        paper_result = simulate_paper_portfolio(
            predictions_history,
            initial_nav=1_000_000.0,
            position_size_pct=5.0,
            max_concurrent=5,
            tc_round_trip_pct=_DEFAULT_TC_ROUND_TRIP_PCT,
        )
        paper_block = format_paper_portfolio_summary(paper_result)
        if paper_block:
            prompt += "\n" + paper_block + "\n"
    except Exception:
        pass

    # Current strategy notes
    if current_notes:
        prompt += "\n=== 現在の戦略メモ ===\n"
        for note in current_notes:
            valid = "有効" if note.get("still_valid", True) else "要検証"
            prompt += f"- [{valid}] {note['insight']} (根拠: {note.get('evidence', 'N/A')})\n"

    prompt += """
=== タスク ===
上記の予測結果を分析し、以下のJSON形式で回答してください:

{
  "analysis_summary": "全体的な傾向の分析（3-5文）",
  "notes": [
    {
      "insight": "具体的な教訓（例：出来高が2倍以上の銘柄はRSI30台からの反発確率が高い）",
      "evidence": "根拠となるデータ（例：該当5件中4件的中）",
      "confidence": "HIGH/MEDIUM/LOW",
      "category": "technical_pattern/fundamental_insight/market_regime/sector_insight/risk_management",
      "still_valid": true
    }
  ],
  "deprecated_notes": ["無効になった既存メモのinsight文"],
  "regime_strategies": {
    "上昇トレンド": "この局面での推奨戦略",
    "下降トレンド": "この局面での推奨戦略",
    "レンジ相場": "この局面での推奨戦略",
    "高ボラティリティ": "この局面での推奨戦略"
  },
  "screening_weight_adjustments": {
    "rsi_oversold_recovery": 20,
    "rsi_healthy_momentum": 15,
    "volume_spike": 20,
    "sma25_breakout": 20,
    "macd_crossover": 15,
    "bollinger_lower": 10,
    "per_value": 10,
    "pbr_undervalued": 10,
    "roe_profitable": 10,
    "dividend_yield": 5,
    "revenue_growth": 5
  },
  "confidence_calibration": "信頼度の使い方についてのアドバイス（例：HIGH は70%以上確信がある場合のみ使う）"
}

重要:
- 教訓は具体的かつ再現可能なものにしてください（「気をつける」ではなく「PER20以上の銘柄のUP予測は避ける」）
- 既存の戦略メモで結果と矛盾するものは deprecated_notes に含めてください
- screening_weight_adjustments は成功パターンに対応する項目の重みを上げ、失敗パターンの重みを下げてください
- 根拠が十分でない教訓は confidence を LOW にしてください
- **勝率より「期待値 (expectancy) × プロフィットファクター」を優先**して戦略を評価してください
- **キャリブレーション逆転が出ている場合は最優先で対応**:
  `confidence_calibration` の HIGH 判定基準を厳格化する文言を必ず更新してください
- **信頼度×方向のクロス集計**で偏りが見えた場合 (例: HIGH-DOWN が特に外れる) は
  方向別の追加ルールを `entry_rules` に書いてください
- **direction-aware 表記**: 表示される avg-return は方向調整済 (DOWN-win も
  正の値)。「的中時 +5% / 外れ時 -3%」が正常。負の的中時リターンが出ていたら
  集計バグなので無視せず報告してください

JSONのみを出力してください。
"""
    return prompt


def apply_review_results(
    review_result: dict,
    strategy_notes: dict,
    screening_weights: dict,
) -> tuple[dict, dict]:
    """Apply Claude's weekly review results to strategy notes and weights.

    Returns:
        tuple of (updated strategy_notes, updated screening_weights)
    """
    today = datetime.now().strftime("%Y-%m-%d")

    # Add new notes
    new_notes = review_result.get("notes", [])
    existing_insights = {n["insight"] for n in strategy_notes.get("notes", [])}
    for note in new_notes:
        if note.get("insight") and note["insight"] not in existing_insights:
            note["date_added"] = today
            note["last_validated"] = today
            strategy_notes.setdefault("notes", []).append(note)

    # Deprecate old notes
    deprecated = set(review_result.get("deprecated_notes", []))
    for note in strategy_notes.get("notes", []):
        if note.get("insight") in deprecated:
            note["still_valid"] = False
            note["deprecated_date"] = today

    # Update regime strategies
    regime = review_result.get("regime_strategies")
    if regime:
        strategy_notes["regime_strategies"] = regime

    strategy_notes["last_review_date"] = today

    # Update screening weights
    new_weights = review_result.get("screening_weight_adjustments")
    if new_weights and isinstance(new_weights, dict):
        for key, value in new_weights.items():
            if key in DEFAULT_WEIGHTS and isinstance(value, (int, float)):
                # Clamp to reasonable range (1-50)
                screening_weights[key] = max(1, min(50, int(value)))

    return strategy_notes, screening_weights
