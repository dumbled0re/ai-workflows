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
    "sma25_breakout": 20,
    "macd_crossover": 15,
    "bollinger_lower": 10,
    "per_value": 10,
    "pbr_undervalued": 10,
    "roe_profitable": 10,
    "dividend_yield": 5,
    "revenue_growth": 5,
}


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
