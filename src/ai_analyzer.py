from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
あなたは日本株市場の専門アナリストです。東京証券取引所に上場する銘柄のテクニカル指標、
市場データ、価格パターンを分析します。

分析時に考慮すべき要素:
- テクニカル指標シグナル（RSI, MACD, ボリンジャーバンド, 移動平均線）
- 出来高パターンと機関投資家の動向示唆
- 価格モメンタムとトレンド方向
- 日本市場特有の要因（日銀政策, 為替, セクターローテーション）

必ず以下を提供してください:
1. 明確なUPまたはDOWN予測
2. 信頼度（HIGH / MEDIUM / LOW）
3. 2-3個の根拠（箇条書き）
4. 注意すべきリスク要因

指定されたJSON形式で回答してください。日本語で回答してください。\
"""

HOLDINGS_PROMPT_TEMPLATE = """\
以下の保有銘柄を分析してください。各銘柄について、テクニカルデータに基づき
今後1-2週間の価格トレンドがUP（上昇）かDOWN（下降）かを予測してください。

本日: {date}
分析タイミング: {timing}

=== 保有銘柄 ===

{holdings_data}

以下のJSON形式で回答してください:
{{
  "holdings_analysis": [
    {{
      "ticker": "7203.T",
      "name": "トヨタ自動車",
      "prediction": "UP",
      "confidence": "HIGH",
      "reasons": ["理由1", "理由2", "理由3"],
      "risk_factor": "リスクの説明",
      "short_summary": "1文の要約"
    }}
  ],
  "market_overview": "日本市場全体の概況（2-3文）"
}}\
"""

DISCOVERY_PROMPT_TEMPLATE = """\
以下のスクリーニング済み候補銘柄から、今後1-4週間で最も値上がりが期待できる
上位{top_n}銘柄を選定してください。各銘柄について、なぜ有望かを説明してください。

本日: {date}

=== スクリーニング済み候補 ({n}銘柄) ===

{candidates_data}

以下のJSON形式で回答してください:
{{
  "recommended_stocks": [
    {{
      "rank": 1,
      "ticker": "XXXX.T",
      "name": "企業名",
      "prediction": "UP",
      "confidence": "HIGH",
      "expected_move": "+X%〜+Y% (Z週間)",
      "reasons": ["理由1", "理由2", "理由3"],
      "risk_factor": "リスクの説明",
      "entry_strategy": "エントリー戦略の提案"
    }}
  ]
}}\
"""


def prepare_prompts(
    holdings_summaries: list[dict],
    candidates: list[dict],
    timing: str,
    top_n: int,
    output_dir: str = "data",
) -> Path:
    """Prepare analysis prompts and save to a JSON file for Claude Code Action.

    Instead of calling the Anthropic API directly, this prepares a prompt file
    that Claude Code Action will read and process.

    Returns:
        Path to the generated prompt file
    """
    out = Path(output_dir)
    out.mkdir(exist_ok=True)

    holdings_text = _format_stock_data(holdings_summaries)
    candidates_text = _format_stock_data(candidates)

    now_str = datetime.now().strftime("%Y-%m-%d")
    timing_label = "市場開場前（朝）" if timing == "morning" else "市場閉場後（夕）"

    holdings_prompt = HOLDINGS_PROMPT_TEMPLATE.format(
        date=now_str,
        timing=timing_label,
        holdings_data=holdings_text,
    )
    discovery_prompt = DISCOVERY_PROMPT_TEMPLATE.format(
        top_n=top_n,
        date=now_str,
        n=len(candidates),
        candidates_data=candidates_text,
    )

    prompt_data = {
        "system_prompt": SYSTEM_PROMPT,
        "holdings_prompt": holdings_prompt,
        "discovery_prompt": discovery_prompt,
        "metadata": {
            "date": now_str,
            "timing": timing,
            "holdings_count": len(holdings_summaries),
            "candidates_count": len(candidates),
        },
    }

    prompt_file = out / "analysis_input.json"
    with open(prompt_file, "w", encoding="utf-8") as f:
        json.dump(prompt_data, f, ensure_ascii=False, indent=2)

    logger.info("Prompt file saved to %s", prompt_file)
    return prompt_file


def load_analysis_results(output_dir: str = "data") -> tuple[dict, dict]:
    """Load analysis results written by Claude Code Action.

    Returns:
        tuple of (holdings_analysis, discovery_results)
    """
    out = Path(output_dir)

    holdings_file = out / "holdings_result.json"
    discovery_file = out / "discovery_result.json"

    holdings_result = _load_json(holdings_file)
    discovery_result = _load_json(discovery_file)

    return holdings_result, discovery_result


def _load_json(path: Path) -> dict:
    """Load and parse a JSON file with fallback."""
    if not path.exists():
        logger.error("Result file not found: %s", path)
        return _fallback_response()

    try:
        with open(path, encoding="utf-8") as f:
            content = f.read().strip()

        # Try direct parse
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        # Try extracting from markdown code block
        if "```json" in content:
            start = content.index("```json") + 7
            end = content.index("```", start)
            return json.loads(content[start:end].strip())
        elif "```" in content:
            start = content.index("```") + 3
            end = content.index("```", start)
            return json.loads(content[start:end].strip())

        logger.error("Could not parse JSON from %s", path)
        return _fallback_response()
    except Exception:
        logger.error("Failed to load %s", path, exc_info=True)
        return _fallback_response()


def _format_stock_data(summaries: list[dict]) -> str:
    """Format stock summaries into text for Claude prompt."""
    parts: list[str] = []
    for s in summaries:
        lines = [
            f"--- {s['name']} ({s['ticker']}) ---",
            f"現在値: {s['current_price']} 円",
        ]
        if s.get("shares"):
            lines.append(f"保有株数: {s['shares']}")
        if s.get("avg_cost"):
            lines.append(
                f"平均取得単価: {s['avg_cost']} 円 | 含み損益: {s.get('unrealized_pnl_pct', 'N/A')}%"
            )
        lines.extend([
            f"価格変動: 1日: {s['price_change_1d']}% | 5日: {s['price_change_5d']}% | 1ヶ月: {s['price_change_1m']}% | 3ヶ月: {s['price_change_3m']}%",
            f"移動平均: SMA5={s['sma_5']} | SMA25={s['sma_25']} | SMA75={s['sma_75']} | {s['trend_signal']}",
            f"RSI(14): {s['rsi_14']} | MACD: {s['macd_value']} (Signal: {s['macd_signal']}, Hist: {s['macd_histogram']})",
            f"ボリンジャーバンド: Upper={s['bb_upper']} | Middle={s['bb_middle']} | Lower={s['bb_lower']} | ポジション: {s['bb_position_pct']}",
            f"出来高比率(対20日平均): {s['volume_ratio']}x",
            f"52週: 高値から{s['distance_from_52w_high']}% | 安値から{s['distance_from_52w_low']}%",
        ])
        if s.get("sector"):
            lines.append(f"セクター: {s['sector']}")
        if s.get("screening_score") is not None:
            lines.append(f"スクリーニングスコア: {s['screening_score']}/100")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _fallback_response() -> dict:
    """Return a fallback response when analysis results are unavailable."""
    return {
        "error": True,
        "message": "AI分析の結果を読み込めませんでした。再実行してください。",
    }
