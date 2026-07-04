from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"

# Unpaired UTF-16 surrogates (high 0xD800-DBFF / low 0xDC00-DFFF) are
# legal in Python str but produce invalid JSON when ensure_ascii=False.
# Real cause from the 2026-05-12 manual run was a TDnet / news title
# containing a stray surrogate that Anthropic's API rejected with
# "no low surrogate in string". Strip them at the JSON-write boundary.
_SURROGATE_RE = re.compile(r"[\uD800-\uDFFF]")


def _sanitize_unicode(value: object) -> object:
    """Recursively strip unpaired UTF-16 surrogates from any string
    inside a JSON-serialisable structure.

    Python allows lone surrogates as str code points (they survive
    slicing, concatenation, etc.) but ``json.dumps(ensure_ascii=False)``
    emits them verbatim and downstream JSON parsers reject the bytes.
    Sanitizing at the dump boundary covers every source that flows
    into the prompt (TDnet titles, news headlines, AI summaries,
    fundamentals strings) without forcing each fetcher to handle
    Unicode hygiene itself.
    """
    if isinstance(value, str):
        return _SURROGATE_RE.sub("", value)
    if isinstance(value, dict):
        return {k: _sanitize_unicode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_unicode(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_unicode(v) for v in value)
    return value


SYSTEM_PROMPT = """\
あなたはプロの短期投資家兼アナリストです。東京証券取引所に上場する銘柄を、
テクニカル分析とファンダメンタル分析の両面から総合的に評価します。

あなたの投資哲学:
- **最終目的は利益であって的中ではない。** あなたの全予測は同一期間の
  TOPIX ETF (1306.T) リターンと比較され、「指数を買うだけ」を上回った分
  (市場超過リターン) だけが評価される。市場と同じだけ動く銘柄を当てても
  付加価値はゼロ
- 指数に勝てる確信が持てない銘柄は推奨しない。見送り (NO_TRADE / 空配列)
  はインデックス投資という有効な代替案を選んだという明確な判断である
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
  - volume_spike (1.5x↑): 浅いシグナル、補助材料
  - volume_surge (3x↑): 機関のエントリー有力、reasons に「🔥出来高Nx急増」と明記
  - volume_blowoff (5x↑): 異常出来高、本気買いの可能性、reasons の最上段に明記
  - volume_breakout (出来高3x↑+SMA25抜け): Stan Weinstein Stage 2 buyable breakout、最強信号
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
1. 予測方向（UP / DOWN / NO_TRADE のいずれか — 曖昧な表現は不可）
   - UP: テクニカル + ファンダ + 需給/カタリスト が買い方向で揃っている
   - DOWN: 同じ 3 層が売り方向で揃っている
   - NO_TRADE: 方向の確信が持てない（UP/DOWN どちらにも 50/50 で振れうる）。
     見送りも明確な判断であり、無理に UP/DOWN を出すより正確な signal です。
     holdings の場合は prediction=NO_TRADE でも action (保有継続/利確/損切り) は出力してください。
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
- 方向の確信が持てない場合は prediction を "NO_TRADE" にして、action だけで判断する
  （保有しているからといって毎回 UP を付ける必要はない。方向不明は方向不明と書くこと）
- UP/DOWN は市場相対で判断する: 「市場全体と一緒に動きそう」は方向シグナル
  ではない (β)。市場より強い / 弱い銘柄固有の根拠がある場合のみ方向を出す

以下のJSON形式で回答してください。example は UP / DOWN / NO_TRADE 3 種をフォーマット
説明として示しているだけで、分布の示唆ではありません:
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
    }},
    {{
      "ticker": "9999.T",
      "name": "サンプル B (フォーマット例)",
      "prediction": "DOWN",
      "confidence": "MEDIUM",
      "reasons": ["52週高値圏で momentum 失速", "forward 連続下方修正", "セクター rotation 逆風"],
      "risk_factor": "短期反発時の含み益縮小",
      "stop_loss": "直近高値 (DOWN 視点の損切り = 価格上方ブレイク)",
      "action": "利確/損切り",
      "short_summary": "中期売り目線、含み益のうちに利確"
    }},
    {{
      "ticker": "8888.T",
      "name": "サンプル C (フォーマット例)",
      "prediction": "NO_TRADE",
      "confidence": "MEDIUM",
      "reasons": ["短期 momentum と長期 trend が逆方向", "決算発表 2 営業日前で event risk 高い"],
      "risk_factor": "イベント結果次第で両方向に振れる",
      "stop_loss": "決算後の動き次第、現状は事前指値しない",
      "action": "保有継続",
      "short_summary": "方向不明 — action 単独判断"
    }}
  ],
  "market_overview": "日本市場全体の概況と今後1-2週間の見通し（3-4文）"
}}

prediction は "UP" / "DOWN" / "NO_TRADE" のいずれか。NO_TRADE の場合も他のフィールドは
通常通り埋め、short_summary に「方向不明 — action 単独判断」と記載してください。
holdings は既保有なので、UP に偏ることなく DOWN (= 利確/損切り推奨) や NO_TRADE
(= 方向不明、action 単独判断) も同等に正当な出力です。「保有してるからとりあえず UP」
は禁止 — テクニカル + ファンダ + 需給 が UP 方向に揃ってない場合は明示的に DOWN か
NO_TRADE にしてください。\
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
- **市場 (TOPIX) を上回る根拠を reasons に必ず含める** — 相対強度、銘柄固有
  カタリスト、セクター独自の追い風など。「地合いが良いから上がりそう」は
  指数を買えば済む話で、銘柄推奨の理由にならない
- エントリーポイントが来ていない銘柄は選ばない
- リスクリワード比が良い（上昇余地 > 下落リスク）
- カタリスト（決算、材料、出来高急増）がある
- prediction は UP / DOWN / NO_TRADE のいずれか。買いだけでなく、明確な売り setup
  (テクニカル + ファンダ + 需給がいずれも下方向) なら DOWN を出してよい。確信が
  持てない銘柄は NO_TRADE または picks に入れない

【長期投資候補（3-12ヶ月）】最大{top_n}銘柄
- ファンダメンタルが優秀（高ROE、成長性、割安）であることは必要条件であって十分条件
  ではない。エントリータイミング(price action / regime / catalyst freshness) も必ず確認
- **「TOPIX を 3-12 ヶ月で上回る」根拠を investment_thesis に含める** — 市場並みの
  成長しか見込めないなら指数を買う方が合理的で、pick する意味がない
- 業界内で競争優位性がある
- 短期的には割安or横ばいでも、中長期で成長が見込める
- 配当利回りや株主還元も考慮
- prediction = UP に偏らないこと。長期 short / 長期 NO_TRADE も同等に正当な出力。
  「ファンダ良いから取り敢えず UP」は禁止 — 価格が天井圏 / momentum が逆方向 /
  regime mismatch / catalyst stale なら NO_TRADE か DOWN を出すこと
- 同一銘柄を 30 日以内に再 pick する場合、前回採用 thesis (同じ reasons 集合) のまま
  再提案するのは禁止。new catalyst / valuation reset / new technical setup のいずれか
  を reasons[0] に明記。それが無い場合は再 pick せず別銘柄に回す

該当銘柄がない場合は正直に空配列で回答してください。
無理に{top_n}銘柄選ぶ必要はありません。short_term_picks は「今が買い時」と確信できる
銘柄のみ。確信が持てない銘柄は picks に入れず、必要なら long_term_picks 側で再評価
してください。long_term_picks も「とりあえずファンダ良いもの」 を入れる場所ではない —
入れない判断 (空配列) は明確な signal です。「上記の UP予測ゲート」が prompt 内に存在
する場合は、その指示を優先してください。

以下のJSON形式で回答してください。example はフォーマット説明用であり、
UP/DOWN/NO_TRADE の分布バイアスを示唆するものではありません:
{{
  "short_term_picks": [
    {{
      "rank": 1,
      "ticker": "XXXX.T",
      "name": "企業名",
      "prediction": "UP",
      "confidence": "HIGH",
      "expected_move": "+X%〜+Y% (Z週間) または -X%〜-Y% / range",
      "reasons": ["テクニカル根拠", "ファンダメンタル根拠", "カタリスト"],
      "risk_factor": "リスクの説明",
      "entry_price": "推奨エントリー価格帯 (DOWN の場合は売り建てエントリー帯)",
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
      "prediction": "DOWN",
      "confidence": "MEDIUM",
      "investment_thesis": "ファンダ表面割安だが forward 連続下方修正 + sector 否定、中期売り目線",
      "expected_return": "-15〜-25% / 6-12ヶ月",
      "reasons": ["成長性の劣化", "割安性の罠 (value trap)", "競争優位性の喪失"],
      "risk_factor": "リスクの説明",
      "ideal_entry_zone": "理想的な売り建てゾーンの価格帯",
      "dividend_info": "配当利回りや株主還元の情報"
    }},
    {{
      "rank": 2,
      "ticker": "YYYY.T",
      "name": "企業名B",
      "prediction": "NO_TRADE",
      "confidence": "MEDIUM",
      "investment_thesis": "ファンダ優秀だが価格が 52週高値圏で momentum 逆風。caught knife リスクが reward 超過",
      "expected_return": "N/A (entry zone まで待ち)",
      "reasons": ["fundamental は強い", "entry timing が悪い", "regime mismatch"],
      "risk_factor": "上昇継続して機会損失",
      "ideal_entry_zone": "再エントリー検討の価格帯 (e.g. -10% 押し)",
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
    prompt_data = _sanitize_unicode(prompt_data)
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

        # Forward earnings/revenue growth from sell-side consensus
        # estimates. Both current-Q and next-Q growth being positive
        # signals aggregated "raising" — the strongest forward setup.
        fwd_parts: list[str] = []
        for src_key, label in (
            ("current_q_growth_pct", "今Q"),
            ("next_q_growth_pct", "来Q"),
            ("current_y_growth_pct", "今期"),
            ("next_y_growth_pct", "来期"),
        ):
            v = s.get(src_key)
            if isinstance(v, (int, float)):
                fwd_parts.append(f"{label} {v:+.1f}%")
        if fwd_parts:
            lines.append("予想成長率 (アナリスト): " + " / ".join(fwd_parts))

        # Liquidity warning — wide bid/ask spread tags the pick as a
        # low-liquidity swing candidate. The AI sees the absolute
        # average volume too so it can sanity-check independently.
        bid = s.get("bid")
        ask = s.get("ask")
        if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and bid > 0 and ask > bid:
            cur = s.get("current_price")
            if isinstance(cur, (int, float)) and cur > 0:
                spread_pct = (ask - bid) / cur * 100
                if spread_pct > 0.5:
                    lines.append(f"流動性: bid {bid:.0f} / ask {ask:.0f} (スプレッド {spread_pct:.2f}%)")
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

        # Macro sensitivity tags from cross-asset moves (USDJPY /
        # yield / oil). Renders as "マクロ: [usdjpy +2.1%] tailwind"
        # so the AI sees which macro factor is currently helping or
        # hurting this sector.
        macro_tags = s.get("macro_tags")
        if isinstance(macro_tags, list) and macro_tags:
            lines.append(f"マクロ感応: {' / '.join(macro_tags)}")

        # Trailing-stop recommendation for holdings already in the
        # green (>+3% unrealized). Pros raise the stop as positions
        # move favourably — this surfaces the suggestion alongside
        # the AI's own stop_loss for the operator to act on.
        ts = s.get("trailing_stop_suggestion")
        if isinstance(ts, dict):
            lines.append(f"トレーリングストップ提案: {ts.get('new_stop_price', 'N/A')}円 ({ts.get('rationale', '')})")

        # Vol-targeted + Kelly + stop-aware position size. The
        # rendered number is the most conservative of the three —
        # always smaller than the most-aggressive single method so
        # the operator can never accidentally over-allocate.
        sized = s.get("suggested_position_pct")
        if isinstance(sized, (int, float)) and sized > 0:
            atr = s.get("daily_atr_pct")
            stop_sized = s.get("stop_aware_position_pct")
            kelly_sized = s.get("kelly_position_pct")
            extras: list[str] = []
            if isinstance(atr, (int, float)):
                extras.append(f"日次ボラ {atr:.1f}%")
            if isinstance(kelly_sized, (int, float)):
                extras.append(f"Kelly {kelly_sized:.1f}%")
            if isinstance(stop_sized, (int, float)):
                extras.append(f"stop ベース {stop_sized:.1f}%")
            extra_text = f" ({' / '.join(extras)})" if extras else ""
            lines.append(f"推奨ポジションサイズ: 資金の {sized:.1f}%{extra_text}")

        # TDnet 適時開示 — official disclosure feed, distinct from
        # general news (TOB / 業績修正 / 自己株式取得 etc. land here
        # first). Rendered above general news so the AI processes
        # canonical sources before noisy headline streams.
        if s.get("tdnet_disclosures_text"):
            lines.append(s["tdnet_disclosures_text"])

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
