from __future__ import annotations

import json
import logging
from datetime import datetime

from anthropic import Anthropic

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


class AIAnalyzer:
    def __init__(self, api_key: str, model: str):
        self.client = Anthropic(api_key=api_key)
        self.model = model

    def analyze_holdings(
        self, holdings_summaries: list[dict], timing: str
    ) -> dict:
        """Analyze user's holdings and return predictions."""
        holdings_text = _format_stock_data(holdings_summaries)
        prompt = HOLDINGS_PROMPT_TEMPLATE.format(
            date=datetime.now().strftime("%Y-%m-%d"),
            timing="市場開場前（朝）" if timing == "morning" else "市場閉場後（夕）",
            holdings_data=holdings_text,
        )
        return self._call_claude(prompt)

    def discover_stocks(
        self, candidates: list[dict], top_n: int
    ) -> dict:
        """Analyze screened candidates and return top picks."""
        candidates_text = _format_stock_data(candidates)
        prompt = DISCOVERY_PROMPT_TEMPLATE.format(
            top_n=top_n,
            date=datetime.now().strftime("%Y-%m-%d"),
            n=len(candidates),
            candidates_data=candidates_text,
        )
        return self._call_claude(prompt)

    def _call_claude(self, user_message: str) -> dict:
        """Make Claude API call and parse JSON response."""
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            text = response.content[0].text
            return self._parse_json_response(text)
        except Exception:
            logger.error("Claude API call failed", exc_info=True)
            raise

    def _parse_json_response(self, text: str) -> dict:
        """Parse JSON from Claude's response, with retry on failure."""
        # Try direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try extracting JSON from markdown code block
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            try:
                return json.loads(text[start:end].strip())
            except json.JSONDecodeError:
                pass
        elif "```" in text:
            start = text.index("```") + 3
            end = text.index("```", start)
            try:
                return json.loads(text[start:end].strip())
            except json.JSONDecodeError:
                pass

        # Retry with Claude to fix JSON
        logger.warning("JSON parse failed, requesting fix from Claude")
        try:
            fix_response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                messages=[
                    {"role": "user", "content": f"以下のテキストから正しいJSONを抽出して、JSONのみを返してください:\n\n{text}"},
                ],
            )
            fixed_text = fix_response.content[0].text
            return json.loads(fixed_text)
        except (json.JSONDecodeError, Exception):
            logger.error("JSON fix attempt also failed")
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
    """Return a fallback response when Claude JSON parsing fails."""
    return {
        "error": True,
        "message": "AI分析の応答をパースできませんでした。再実行してください。",
    }
