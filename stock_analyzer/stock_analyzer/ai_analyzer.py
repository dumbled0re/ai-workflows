from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"

SYSTEM_PROMPT = """\
あなたはプロの短期投資家兼アナリストです。東京証券取引所に上場する銘柄を、
テクニカル分析とファンダメンタル分析の両面から総合的に評価します。

あなたの投資哲学:
- 短期（1-4週間）で利益を出せる銘柄を厳選する
- 「今が買い時」の銘柄のみを推奨する。良い銘柄でもタイミングが悪ければ見送る
- テクニカルでエントリータイミングを計り、ファンダメンタルで銘柄の質を担保する
- リスク管理を最重要視し、損切りラインを必ず設定する

分析で重視するポイント:
【テクニカル】
- 移動平均線のパーフェクトオーダー・ゴールデンクロス・デッドクロス
- RSIの過売/過買からの反転シグナル
- MACDのゼロライン突破・ヒストグラム転換
- ボリンジャーバンドのスクイーズからのブレイクアウト
- 出来高急増（機関投資家の参入示唆）
- 52週高値/安値からの距離

【ファンダメンタル】
- PER/PBRの割安度（同業比較）
- ROE/ROAの収益性
- 売上・利益成長率
- 決算発表日の接近（カタリスト）
- 財務健全性（自己資本比率、流動比率）

【市場環境】
- 日経平均・TOPIXのトレンド
- セクターローテーション
- 為替・金利動向の影響
- 地政学リスク

必ず以下を提供してください:
1. 明確なUPまたはDOWN予測（曖昧な表現は不可）
2. 信頼度（HIGH / MEDIUM / LOW）
3. 具体的な根拠（テクニカル+ファンダメンタル）
4. 想定される値動きのシナリオ
5. リスク要因と損切りライン

指定されたJSON形式で回答してください。日本語で回答してください。\
"""

HOLDINGS_PROMPT_TEMPLATE = """\
以下の保有銘柄をプロ投資家の視点で分析してください。
テクニカル指標とファンダメンタル指標の両面から、今後1-2週間の戦略を提案してください。

本日: {date}
分析タイミング: {timing}

{market_context}

{market_news}

{performance_feedback}

{strategy_notes}

=== 保有銘柄 ===

{holdings_data}

各銘柄について以下を判断してください:
- 保有継続すべきか、利確/損切りすべきか
- 追加購入のタイミングか
- 決算発表が近い場合、その前後の戦略

以下のJSON形式で回答してください:
{{
  "holdings_analysis": [
    {{
      "ticker": "7203.T",
      "name": "トヨタ自動車",
      "prediction": "UP",
      "confidence": "HIGH",
      "reasons": ["テクニカル根拠", "ファンダメンタル根拠", "市場環境根拠"],
      "risk_factor": "リスクの説明",
      "stop_loss": "損切りライン（価格）",
      "action": "保有継続/利確/損切り/買い増し",
      "short_summary": "1文の要約"
    }}
  ],
  "market_overview": "日本市場全体の概況と今後1-2週間の見通し（3-4文）"
}}\
"""

DISCOVERY_PROMPT_TEMPLATE = """\
以下のスクリーニング済み候補銘柄から、2つのカテゴリに分けて有望銘柄を選定してください。

本日: {date}

{market_context}

{market_news}

{performance_feedback}

{strategy_notes}

=== スクリーニング済み候補 ({n}銘柄) ===

{candidates_data}

=== 選定カテゴリ ===

【短期トレード候補（1-4週間）】最大{top_n}銘柄
- テクニカル的に「今が買い時」の銘柄のみ（最重要）
- エントリーポイントが来ていない銘柄は選ばない
- リスクリワード比が良い（上昇余地 > 下落リスク）
- カタリスト（決算、材料、出来高急増）がある

【長期投資候補（3-12ヶ月）】最大{top_n}銘柄
- ファンダメンタルが優秀（高ROE、成長性、割安）
- 業界内で競争優位性がある
- 短期的には割安or横ばいでも、中長期で成長が見込める
- 配当利回りや株主還元も考慮

該当銘柄がない場合は正直に空配列で回答してください。
無理に{top_n}銘柄選ぶ必要はありません。

以下のJSON形式で回答してください:
{{
  "short_term_picks": [
    {{
      "rank": 1,
      "ticker": "XXXX.T",
      "name": "企業名",
      "prediction": "UP",
      "confidence": "HIGH",
      "expected_move": "+X%〜+Y% (Z週間)",
      "reasons": ["テクニカル根拠", "ファンダメンタル根拠", "カタリスト"],
      "risk_factor": "リスクの説明",
      "entry_price": "推奨エントリー価格帯",
      "stop_loss": "損切りライン",
      "target_price": "利確目標",
      "entry_strategy": "具体的なエントリー戦略（指値/成行、分割購入など）"
    }}
  ],
  "long_term_picks": [
    {{
      "rank": 1,
      "ticker": "XXXX.T",
      "name": "企業名",
      "prediction": "UP",
      "confidence": "HIGH",
      "investment_thesis": "この銘柄に長期投資する理由（3-5文）",
      "expected_return": "想定リターン（例: +20-30% / 6-12ヶ月）",
      "reasons": ["成長性の根拠", "割安性の根拠", "競争優位性"],
      "risk_factor": "リスクの説明",
      "ideal_entry_zone": "理想的な買い場の価格帯",
      "dividend_info": "配当利回りや株主還元の情報"
    }}
  ],
  "market_condition": "現在の市場環境の評価（短期/長期それぞれの見通し）"
}}\
"""


def prepare_prompts(
    holdings_summaries: list[dict],
    candidates: list[dict],
    timing: str,
    top_n: int,
    output_dir: str | Path | None = None,
    market_context: str = "",
    market_news: str = "",
    performance_feedback: str = "",
    strategy_notes: str = "",
) -> Path:
    """Prepare analysis prompts and save to a JSON file for Claude Code Action.

    Instead of calling the Anthropic API directly, this prepares a prompt file
    that Claude Code Action will read and process.

    Returns:
        Path to the generated prompt file
    """
    out = Path(output_dir) if output_dir is not None else _DATA_DIR
    out.mkdir(exist_ok=True)

    holdings_text = _format_stock_data(holdings_summaries)
    candidates_text = _format_stock_data(candidates)

    now_str = datetime.now().strftime("%Y-%m-%d")
    timing_label = "市場開場前（朝）" if timing == "morning" else "市場閉場後（夕）"

    holdings_prompt = HOLDINGS_PROMPT_TEMPLATE.format(
        date=now_str,
        timing=timing_label,
        holdings_data=holdings_text,
        market_context=market_context,
        market_news=market_news,
        performance_feedback=performance_feedback,
        strategy_notes=strategy_notes,
    )
    discovery_prompt = DISCOVERY_PROMPT_TEMPLATE.format(
        top_n=top_n,
        date=now_str,
        n=len(candidates),
        candidates_data=candidates_text,
        market_context=market_context,
        market_news=market_news,
        performance_feedback=performance_feedback,
        strategy_notes=strategy_notes,
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


def load_analysis_results(output_dir: str | Path | None = None) -> tuple[dict, dict]:
    """Load analysis results written by Claude Code Action.

    Returns:
        tuple of (holdings_analysis, discovery_results)
    """
    out = Path(output_dir) if output_dir is not None else _DATA_DIR

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
            lines.append(f"平均取得単価: {s['avg_cost']} 円 | 含み損益: {s.get('unrealized_pnl_pct', 'N/A')}%")
        lines.extend(
            [
                (
                    f"価格変動: 1日: {s['price_change_1d']}% | 5日: {s['price_change_5d']}% | "
                    f"1ヶ月: {s['price_change_1m']}% | 3ヶ月: {s['price_change_3m']}%"
                ),
                f"移動平均: SMA5={s['sma_5']} | SMA25={s['sma_25']} | SMA75={s['sma_75']} | {s['trend_signal']}",
                (
                    f"RSI(14): {s['rsi_14']} | MACD: {s['macd_value']} "
                    f"(Signal: {s['macd_signal']}, Hist: {s['macd_histogram']})"
                ),
                (
                    f"ボリンジャーバンド: Upper={s['bb_upper']} | Middle={s['bb_middle']} | "
                    f"Lower={s['bb_lower']} | ポジション: {s['bb_position_pct']}"
                ),
                f"出来高比率(対20日平均): {s['volume_ratio']}x",
                f"52週: 高値から{s['distance_from_52w_high']}% | 安値から{s['distance_from_52w_low']}%",
            ]
        )
        if s.get("sector"):
            lines.append(f"セクター: {s['sector']}")
        if s.get("screening_score") is not None:
            lines.append(f"スクリーニングスコア: {s['screening_score']}/100")
        # Fundamental data
        fund_parts = []
        if s.get("per") is not None:
            fund_parts.append(f"PER={s['per']}")
        if s.get("forward_per") is not None:
            fund_parts.append(f"予想PER={s['forward_per']}")
        if s.get("pbr") is not None:
            fund_parts.append(f"PBR={s['pbr']}")
        if s.get("roe") is not None:
            fund_parts.append(f"ROE={s['roe']}%")
        if s.get("roa") is not None:
            fund_parts.append(f"ROA={s['roa']}%")
        if fund_parts:
            lines.append(f"バリュエーション: {' | '.join(fund_parts)}")

        fund_parts2 = []
        if s.get("dividend_yield") is not None:
            fund_parts2.append(f"配当利回り={s['dividend_yield']}%")
        if s.get("profit_margin") is not None:
            fund_parts2.append(f"利益率={s['profit_margin']}%")
        if s.get("revenue_growth") is not None:
            fund_parts2.append(f"売上成長率={s['revenue_growth']}%")
        if s.get("earnings_growth") is not None:
            fund_parts2.append(f"利益成長率={s['earnings_growth']}%")
        if fund_parts2:
            lines.append(f"収益性: {' | '.join(fund_parts2)}")

        fund_parts3 = []
        if s.get("debt_to_equity") is not None:
            fund_parts3.append(f"D/E={s['debt_to_equity']}")
        if s.get("current_ratio") is not None:
            fund_parts3.append(f"流動比率={s['current_ratio']}")
        if s.get("market_cap_billion") is not None:
            fund_parts3.append(f"時価総額={s['market_cap_billion']}億円")
        if fund_parts3:
            lines.append(f"財務: {' | '.join(fund_parts3)}")

        if s.get("next_earnings_date"):
            # Suffix is appended by ``earnings_calendar.annotate_summary``
            # via ``days_until_earnings`` when the date is within the
            # imminent threshold; empty otherwise.
            from stock_analyzer.earnings_calendar import format_inline_for_summary

            suffix = format_inline_for_summary(s.get("days_until_earnings"))
            lines.append(f"次回決算発表日: {s['next_earnings_date']}{suffix}")

        # Analyst consensus block — mirrors what JP-equity research
        # desks read first when sizing a position. Show target upside
        # only when computable, plus the buy/hold/sell rating with
        # sample size for context.
        analyst_parts: list[str] = []
        if s.get("analyst_target_mean") is not None:
            up = s.get("analyst_target_upside_pct")
            up_text = f" ({up:+.1f}% upside)" if isinstance(up, (int, float)) else ""
            analyst_parts.append(f"平均目標 {s['analyst_target_mean']:.0f}円{up_text}")
        if s.get("analyst_rating_key"):
            n = s.get("analyst_count")
            mean = s.get("analyst_rating_mean")
            extra = ""
            if mean is not None:
                extra += f" (mean {mean:.2f}"
                if n is not None:
                    extra += f", {n}名"
                extra += ")"
            elif n is not None:
                extra = f" ({n}名)"
            analyst_parts.append(f"consensus {s['analyst_rating_key']}{extra}")
        if analyst_parts:
            lines.append("アナリスト: " + " / ".join(analyst_parts))

        # Earnings momentum (YoY) — populated by data_fetcher.fetch_
        # earnings_momentum for top candidates + holdings only. Absent
        # for the rest of the universe to save HTTP.
        em_parts: list[str] = []
        rev_yoy = s.get("revenue_yoy_pct")
        ni_yoy = s.get("net_income_yoy_pct")
        if isinstance(rev_yoy, (int, float)):
            em_parts.append(f"売上 YoY {rev_yoy:+.1f}%")
        if isinstance(ni_yoy, (int, float)):
            em_parts.append(f"純利益 YoY {ni_yoy:+.1f}%")
        if em_parts:
            q = s.get("latest_quarter")
            q_suffix = f" [{q}決算]" if q else ""
            lines.append("業績進捗: " + " / ".join(em_parts) + q_suffix)

        # Earnings surprise (PEAD): explicit beat / miss with streak.
        # Among the strongest single-signal predictors in academic
        # finance — surface prominently rather than burying in the
        # signal_components fingerprint.
        sp = s.get("latest_surprise_pct")
        if isinstance(sp, (int, float)):
            cb = s.get("consecutive_beats") or 0
            cm = s.get("consecutive_misses") or 0
            streak_text = ""
            if isinstance(cb, int) and cb >= 2:
                streak_text = f" / {cb}Q連続 beat"
            elif isinstance(cm, int) and cm >= 2:
                streak_text = f" / {cm}Q連続 miss"
            lines.append(f"決算サプライズ: 最新 {sp:+.1f}% vs アナリスト予想{streak_text}")

        # Analyst consensus drift: shifting opinions over the trailing
        # 3 months are a well-documented leading indicator independent
        # of the rating's absolute level. Surface drift_pp with the
        # current bullish-share so the AI can reason about both
        # magnitude and direction.
        drift = s.get("analyst_drift_pp")
        if isinstance(drift, (int, float)):
            bullish = s.get("analyst_bullish_pct")
            cur_text = f" (現 bullish {bullish:.0f}%)" if isinstance(bullish, (int, float)) else ""
            lines.append(f"アナリスト意見ドリフト: 直近3ヶ月で {drift:+.1f}pp{cur_text}")
        if s.get("industry"):
            lines.append(f"業種: {s['industry']}")

        # Sector relative ranking
        if s.get("sector_ranking"):
            lines.append(f"業界内評価: {s['sector_ranking']}")

        # Margin trading data (信用残)
        if s.get("margin_ratio") is not None:
            margin_line = f"信用倍率: {s['margin_ratio']}倍"
            if s.get("margin_signal"):
                margin_line += f" ({s['margin_signal']})"
            if s.get("margin_trend"):
                margin_line += f" | 推移: {s['margin_trend']}"
            lines.append(margin_line)

        # Recent news
        if s.get("recent_news"):
            lines.append(f"最新ニュース: {s['recent_news']}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _fallback_response() -> dict:
    """Return a fallback response when analysis results are unavailable."""
    return {
        "error": True,
        "message": "AI分析の結果を読み込めませんでした。再実行してください。",
    }
