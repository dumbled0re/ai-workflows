"""Second-pass independent critic for the analyser's picks.

The first-pass AI generates holdings_result.json and discovery_result.json
under one prompt context. A well-known failure mode of "ask the same model
to critique its own output in the same call" is sycophancy — the model
defends what it just produced. Running a *separate* Claude Code Action
with only the final picks (no first-pass reasoning) and a rigid five-item
rubric removes that anchor.

The critic outputs a verdict per pick (``keep`` / ``downgrade`` / ``reject``)
and an optional new confidence level. ``apply_critique`` translates those
verdicts back onto the two ``*_result.json`` files. Failure-mode design:

- Critic step crashes / writes nothing → ``apply_critique`` is a no-op
  because ``critique_result.json`` doesn't exist. The unmodified
  first-pass results flow straight to Slack — same behaviour as before
  this module landed, no regression.
- Critic returns garbage JSON → ``load_critique_result`` falls back to
  ``{"critiques": []}`` and again becomes a no-op.
- Critic decisions are ambiguous (missing ticker, unknown verdict) →
  individual entries are skipped, the rest still apply. Never crashes.

The downgrade direction is always toward lower confidence. We deliberately
do not allow the critic to *raise* confidence — that would re-introduce
the calibration inversion the rest of the pipeline is fighting.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CRITIC_SYSTEM_PROMPT = """\
あなたは前段の AI 分析の独立した critic です。前段が選んだ各 pick を、
客観的な checklist に基づき再評価します。

重要な姿勢:
- 前段の judgement に同調しない。厳格に診断する
- 信頼度を「上げる」ことは絶対にしない。「下げる」「除外」のみ
- 5 項目の rubric を厳密に適用し、failing 2+ で downgrade、3+ で reject
- 確信が持てない項目は null (= 評価不能) として扱う。null は failing にカウントしない
- 出力は必ず指定の JSON 形式のみ。説明文や markdown は不要
"""

CRITIC_PROMPT_TEMPLATE = """\
=== Critic Rubric (5 項目) ===

各 pick について以下を Y (true) / N (false) / 不明 (null) で判定してください:

1. signals_match: この pick の screening signals が、前段 prompt の「成功パターン」
   に近い fingerprint を持つか? 「失敗パターン」に近い場合は N。
2. sector_ok: 同セクター集中や相関ペアになっていないか? portfolio_risk 警告
   block が前段 prompt にあれば、その affected_tickers に含まれていれば N。
3. earnings_safe: 決算発表 3 営業日以内ではないか? 前段 prompt の「決算発表
   3 営業日以内の銘柄」block にこの ticker があれば N (=危険)、なければ Y (=安全)。
4. momentum_agrees: prediction direction (UP/DOWN) が、銘柄データの SMA トレンド・
   MACD・RSI と一致しているか? 一致しなければ N。
5. risk_reward: 各 pick の ``risk_reward_ratio`` フィールド (システムが
   AI 文字列を deterministic にパースして算出済み) が >= 2.0 か?
   null の場合は評価不能 (target が指定されてない、文字列がパースできない等)
   として null。0.0 は target が entry の逆側にあるという致命的設定ミスなので必ず N。

=== Verdict 判定ロジック ===

failing 数 (= N の数、null は除く):
- 0-1 → "keep" (信頼度そのまま)
- 2 → "downgrade" (HIGH→MEDIUM, MEDIUM→LOW)
- 3 以上 → "reject" (discovery picks は除外、holdings は LOW にして action 再考)

=== 前段 prompt が参照した情報 ===

{performance_block}

=== Deterministic Portfolio Risk Check (このランで検出された違反) ===

以下は code 側で機械的に検出した violations。critic として **これらに該当する pick は必ず
reject または downgrade** してください。違反に該当する ticker は前段の自信度に関わらず NG:

{portfolio_findings_block}

=== 前段が出した結論 ===

[Holdings 分析]
{holdings_json}

[Discovery 短期 picks]
{short_term_json}

[Discovery 長期 picks]
{long_term_json}

=== 出力 ===

以下の JSON 形式のみで critique_result.json に書き込んでください:

{{
  "critiques": [
    {{
      "ticker": "XXXX.T",
      "source": "holdings" | "short_term" | "long_term",
      "rubric": {{
        "signals_match": true | false | null,
        "sector_ok": true | false | null,
        "earnings_safe": true | false | null,
        "momentum_agrees": true | false | null,
        "risk_reward": true | false | null
      }},
      "verdict": "keep" | "downgrade" | "reject",
      "downgraded_confidence": "MEDIUM" | "LOW" | null,
      "reason": "1-2 文の判定理由 (どの項目が failing か明示)"
    }}
  ]
}}

全ての pick を 1 件ずつ評価してください。前段が選んだ pick の数だけ critiques
配列のエントリが必要です。
"""


_CONFIDENCE_DOWNGRADE = {"HIGH": "MEDIUM", "MEDIUM": "LOW", "LOW": "LOW"}


def build_critic_prompt(
    holdings_result: dict,
    discovery_result: dict,
    performance_block: str = "",
    portfolio_findings_text: str = "",
) -> str:
    """Render the full critic prompt as a single string ready for the AI step.

    ``performance_block`` is the same ``performance_feedback`` text the
    first-pass AI saw — handing it to the critic gives a shared frame of
    reference for "is this fingerprint similar to past winners?" without
    re-deriving anything. Pass empty string when unavailable.

    Before serialising, every pick is annotated with a deterministically
    computed ``risk_reward_ratio`` field (or ``None`` when not derivable).
    This pre-empties the critic's rubric item 5 (risk_reward) so the AI
    consumes a precomputed number instead of re-parsing the AI-generated
    free-form ``stop_loss`` / ``target_price`` strings itself — a step
    that empirically produced inconsistent verdicts on identical inputs.
    """
    from stock_analyzer.risk_reward import annotate_pick

    holdings_picks = holdings_result.get("holdings_analysis", []) or []
    short_term = discovery_result.get("short_term_picks") or discovery_result.get("recommended_stocks") or []
    long_term = discovery_result.get("long_term_picks") or []

    for collection in (holdings_picks, short_term, long_term):
        for pick in collection:
            if isinstance(pick, dict):
                annotate_pick(pick)

    # Strip unpaired UTF-16 surrogates before they hit the prompt text —
    # see ai_analyzer._sanitize_unicode for the upstream rationale.
    # critic prompts hit the API on a second pass so the same hygiene
    # applies. A single stray surrogate from a TDnet / news title that
    # bled into a pick's reasons / risk_factor would otherwise crash
    # the critic step with "API Error: 400 invalid JSON".
    from stock_analyzer.ai_analyzer import _sanitize_unicode

    holdings_picks = _sanitize_unicode(holdings_picks)
    short_term = _sanitize_unicode(short_term)
    long_term = _sanitize_unicode(long_term)
    performance_block = _sanitize_unicode(performance_block)
    portfolio_findings_text = _sanitize_unicode(portfolio_findings_text)

    return CRITIC_PROMPT_TEMPLATE.format(
        performance_block=performance_block or "(過去のパフォーマンスデータなし)",
        portfolio_findings_block=portfolio_findings_text or "(deterministic check で violations なし)",
        holdings_json=json.dumps(holdings_picks, ensure_ascii=False, indent=2),
        short_term_json=json.dumps(short_term, ensure_ascii=False, indent=2),
        long_term_json=json.dumps(long_term, ensure_ascii=False, indent=2),
    )


def load_critique_result(path: str | Path) -> dict:
    """Read critique_result.json with the same markdown-strip fallback the
    other ``load_*`` helpers use, returning ``{"critiques": []}`` on any
    failure so callers can chain unconditionally.

    A missing or corrupt critique is *not* an error — it means the
    critic step didn't produce usable output, and we want to fall back
    to the unmodified first-pass results, not crash the cron.
    """
    p = Path(path)
    if not p.exists():
        return {"critiques": []}
    try:
        with open(p, encoding="utf-8") as f:
            content = f.read().strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass
        # Markdown code-block fence handling — Claude Code Action
        # occasionally wraps output despite explicit instructions.
        if "```json" in content:
            start = content.index("```json") + 7
            end = content.index("```", start)
            return json.loads(content[start:end].strip())
        if "```" in content:
            start = content.index("```") + 3
            end = content.index("```", start)
            return json.loads(content[start:end].strip())
    except Exception:
        logger.warning("Failed to parse critique result at %s", path, exc_info=True)
    return {"critiques": []}


def _coerce_downgrade(original_confidence: str | None, suggested: str | None) -> str:
    """Pick the new confidence value for a ``downgrade`` verdict.

    Honour the critic's suggestion when it is a valid level and is
    actually lower than the current; otherwise fall back to a single
    notch down. This prevents the critic from accidentally *raising*
    confidence by suggesting "HIGH" on a MEDIUM pick (sycophancy risk).
    """
    current = (original_confidence or "MEDIUM").upper()
    proposed = (suggested or "").upper()
    valid = {"HIGH", "MEDIUM", "LOW"}
    rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    if proposed in valid and rank.get(proposed, 0) < rank.get(current, 2):
        return proposed
    return _CONFIDENCE_DOWNGRADE.get(current, "LOW")


def apply_critique(
    holdings_result: dict,
    discovery_result: dict,
    critique_result: dict,
) -> tuple[dict, dict, dict]:
    """Apply the critic's verdicts and return (holdings, discovery, summary).

    Verdicts translate to in-place edits on the two result dicts:
    - ``keep``: no change.
    - ``downgrade``: ``confidence`` field is rewritten to the suggested
      (or one-notch-lower) level. The original level is preserved on a
      sibling ``confidence_pre_critique`` key so review can see what
      changed.
    - ``reject``: for discovery picks, the entry is *removed* entirely
      from the list so it never reaches Slack as a recommendation. For
      holdings, the entry stays (we still own the stock) but confidence
      drops to LOW and ``critic_rejected = true`` is set so the AI on
      the next cron sees the warning context.

    ``summary`` captures aggregate counts + per-verdict ticker lists so
    the Slack notifier can show "Critic: N kept / M downgraded / K
    rejected" without parsing the result files itself.
    """
    critiques = critique_result.get("critiques") or []
    by_key: dict[tuple[str, str], dict] = {}
    for c in critiques:
        if not isinstance(c, dict):
            continue
        ticker = c.get("ticker")
        source = c.get("source")
        if not ticker or source not in {"holdings", "short_term", "long_term"}:
            continue
        by_key[(str(ticker), str(source))] = c

    summary: dict[str, list[str]] = {"kept": [], "downgraded": [], "rejected": []}

    # Holdings: never remove entries, only adjust confidence / flag.
    for h in holdings_result.get("holdings_analysis", []) or []:
        ticker = h.get("ticker")
        if not ticker:
            continue
        c = by_key.get((ticker, "holdings"))
        if c is None:
            summary["kept"].append(str(ticker))
            continue
        verdict = c.get("verdict", "keep")
        if verdict == "downgrade":
            h["confidence_pre_critique"] = h.get("confidence")
            h["confidence"] = _coerce_downgrade(h.get("confidence"), c.get("downgraded_confidence"))
            h["critic_reason"] = c.get("reason", "")
            summary["downgraded"].append(str(ticker))
        elif verdict == "reject":
            h["confidence_pre_critique"] = h.get("confidence")
            h["confidence"] = "LOW"
            h["critic_rejected"] = True
            h["critic_reason"] = c.get("reason", "")
            summary["rejected"].append(str(ticker))
        else:
            summary["kept"].append(str(ticker))

    # Discovery short/long term: drop rejected entries entirely.
    for list_key, source in (("short_term_picks", "short_term"), ("long_term_picks", "long_term")):
        picks = discovery_result.get(list_key) or []
        # Also accept the legacy ``recommended_stocks`` key on short_term.
        legacy = source == "short_term" and not picks and discovery_result.get("recommended_stocks")
        if legacy:
            picks = discovery_result.get("recommended_stocks") or []
            list_key = "recommended_stocks"
        kept: list[dict] = []
        for r in picks:
            ticker = r.get("ticker")
            if not ticker:
                kept.append(r)
                continue
            c = by_key.get((ticker, source))
            if c is None:
                summary["kept"].append(str(ticker))
                kept.append(r)
                continue
            verdict = c.get("verdict", "keep")
            if verdict == "downgrade":
                r["confidence_pre_critique"] = r.get("confidence")
                r["confidence"] = _coerce_downgrade(r.get("confidence"), c.get("downgraded_confidence"))
                r["critic_reason"] = c.get("reason", "")
                summary["downgraded"].append(str(ticker))
                kept.append(r)
            elif verdict == "reject":
                summary["rejected"].append(str(ticker))
                # Skip — entry is removed from the output.
            else:
                summary["kept"].append(str(ticker))
                kept.append(r)
        discovery_result[list_key] = kept

    return holdings_result, discovery_result, summary


_MAX_DISCOVERY_PICKS = 5
_CONFIDENCE_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}


def enforce_discovery_cap(
    discovery_result: dict,
    summary: dict[str, list[str]],
    max_total: int = _MAX_DISCOVERY_PICKS,
) -> dict:
    """Deterministically trim discovery picks down to ``max_total``.

    The critic AI is meant to reject obvious violations, but in
    practice it sometimes returns lenient verdicts (28 keep / 1
    downgrade out of 29 in real-world output). This step runs
    *after* apply_critique and force-removes the lowest-confidence
    picks across short_term + long_term combined until the
    discovery total is within the configured cap. holdings are
    untouched — we own those positions, the cap is a forward-entry
    constraint.

    Trim ordering, lowest-first (= dropped first):
    1. confidence_pre_critique = LOW (critic-rejected stragglers)
    2. confidence = LOW
    3. confidence = MEDIUM (long_term entries first, then short_term)
    4. confidence = HIGH (only as last resort)

    The ``summary`` dict gets the dropped tickers appended to
    ``rejected`` so Slack shows them and the next-cron portfolio_
    findings injection picks them up for the AI's next-run prompt.
    """
    short_term = discovery_result.get("short_term_picks") or discovery_result.get("recommended_stocks") or []
    long_term = discovery_result.get("long_term_picks") or []

    def sort_key(p: dict) -> tuple:
        pre = (p.get("confidence_pre_critique") or "").upper()
        cur = (p.get("confidence") or "MEDIUM").upper()
        return (
            0 if pre == "LOW" else 1,  # critic-pre-LOW first
            _CONFIDENCE_RANK.get(cur, 2),
            0 if p in long_term else 1,  # drop long_term before short_term at same conf
        )

    combined: list[tuple[str, dict]] = []
    for p in short_term:
        combined.append(("short_term_picks", p))
    for p in long_term:
        combined.append(("long_term_picks", p))
    total = len(combined)
    if total <= max_total:
        return discovery_result

    # Sort by drop-priority ascending; head of list goes first.
    combined.sort(key=lambda x: sort_key(x[1]))
    drop_count = total - max_total
    to_drop = combined[:drop_count]
    keep = combined[drop_count:]

    # Rebuild lists from the survivors.
    new_short: list[dict] = []
    new_long: list[dict] = []
    for collection, pick in keep:
        if collection == "short_term_picks":
            new_short.append(pick)
        else:
            new_long.append(pick)
    # Honour legacy key when the original used recommended_stocks
    if "recommended_stocks" in discovery_result and "short_term_picks" not in discovery_result:
        discovery_result["recommended_stocks"] = new_short
    else:
        discovery_result["short_term_picks"] = new_short
    discovery_result["long_term_picks"] = new_long

    dropped_tickers = [p.get("ticker", "?") for _coll, p in to_drop]
    summary.setdefault("rejected", []).extend(str(t) for t in dropped_tickers)
    return discovery_result


_CALIBRATION_BAD_THRESHOLD_PCT = 50.0
"""Win rate below which a (confidence × direction) bucket is considered
broken. The historical baseline for resolved trades is ~59% overall.
A bucket under 50% is worse than coin-flip = pure noise, and historically
HIGH_UP has been sitting at 38.5% which is the canonical "broken" case
this gate targets."""

_CALIBRATION_MIN_SAMPLES = 10
"""Minimum bucket sample size before the gate fires. Below this, the
win rate is too noisy to act on — e.g. n=3 with 0 wins would otherwise
trigger the gate spuriously."""

_RECENT_DRIFT_DIR_BAD_THRESHOLD_PCT = 50.0
"""Win rate below which a direction (UP/DOWN) is considered broken in
the *recent* window. Lower than long-run threshold would over-correct on
noise; equal threshold means a direction needs to be coin-flip-bad in the
recent 14-trade window AND there's a statistically significant drift
AND recent expectancy is negative — three independent signals stacking
before the code-level gate widens its scope beyond the per-bucket
long-run check."""


def enforce_calibration_gate(
    holdings_result: dict | None,
    discovery_result: dict | None,
    summary: dict[str, list[str]],
    performance_stats: dict | None,
) -> tuple[dict | None, dict | None]:
    """Code-level downgrade of (confidence × direction) buckets with poor
    realized accuracy. Catches anything the AI insists on against the
    calibration_zone red signal in the prompt.

    Historical 2026-06-06 case: HIGH_UP sits at 38.5% (n=13) — coin-flip
    or worse. ``performance_stats.by_confidence_direction`` carries the
    realized rates; this gate reads them and, for any bucket under 50%
    with adequate sample size, downgrades incoming picks from that bucket.

    Action by direction:
    - HIGH_UP broken → downgrade HIGH to MEDIUM (preserves the trade
      signal but removes the over-confident label which would otherwise
      drive larger position sizing via Kelly).
    - LOW (either direction) broken → drop entirely. LOW is supposed to
      mean "weak conviction"; if even that is wrong half the time, the
      pick is noise. Goes to the ``rejected`` summary so the next-cron
      portfolio inject picks it up.

    holdings are gated too — a HIGH-confidence DOWN recommendation on a
    holding (= sell signal) at 50% accuracy is just as expensive as a
    bad new-trade signal because the user acts on it.
    """
    if not performance_stats:
        return holdings_result, discovery_result
    by_cd = performance_stats.get("by_confidence_direction") or {}
    if not isinstance(by_cd, dict):
        return holdings_result, discovery_result

    # Identify broken buckets: win rate below threshold AND n >= min samples.
    broken: set[tuple[str, str]] = set()
    for key, bucket in by_cd.items():
        if not isinstance(bucket, dict):
            continue
        n = bucket.get("total", 0)
        acc = bucket.get("accuracy_pct")
        if n < _CALIBRATION_MIN_SAMPLES or acc is None:
            continue
        # key format: "HIGH_UP" / "MEDIUM_DOWN" / etc
        if acc < _CALIBRATION_BAD_THRESHOLD_PCT and "_" in key:
            conf, direction = key.split("_", 1)
            broken.add((conf.upper(), direction.upper()))

    # Direction-level recent drift gate: only triggers when ALL of
    #   1. drift_indicator.is_drift = True (statistically significant decay)
    #   2. drift_indicator.recent_expectancy_pct < 0 (recent EV is negative)
    #   3. recent_direction_winrate[<dir>].winrate_pct < 50 (this direction
    #      is dragging the recent window)
    # are true. Goal: catch the case where e.g. MEDIUM_UP has 58% long-run
    # accuracy (existing gate inactive) but in the last 14 trades UP went
    # 2-of-6 — picks in that direction should be downgraded one notch
    # regardless of confidence label until the drift recovers.
    #
    # Adds (HIGH, dir) and (MEDIUM, dir) for the offending direction(s) to
    # ``broken``; existing per-bucket LOW gating already covers (LOW, dir).
    drift = performance_stats.get("drift_indicator") if isinstance(performance_stats, dict) else None
    rdw = performance_stats.get("recent_direction_winrate") if isinstance(performance_stats, dict) else None
    drift_dir_widened: list[str] = []
    if (
        isinstance(drift, dict)
        and drift.get("is_drift")
        and isinstance(drift.get("recent_expectancy_pct"), int | float)
        and drift["recent_expectancy_pct"] < 0
        and isinstance(rdw, dict)
    ):
        for direction in ("UP", "DOWN"):
            dir_stat = rdw.get(direction)
            if not isinstance(dir_stat, dict):
                continue
            winrate = dir_stat.get("winrate_pct")
            if not isinstance(winrate, int | float):
                continue
            if winrate < _RECENT_DRIFT_DIR_BAD_THRESHOLD_PCT:
                # MEDIUM downgrade is the meaningful step here — HIGH for
                # this direction is also added so a still-issued HIGH in
                # the dragging direction lands on MEDIUM instead.
                broken.add(("HIGH", direction))
                broken.add(("MEDIUM", direction))
                drift_dir_widened.append(direction)

    if not broken:
        return holdings_result, discovery_result

    downgraded: list[str] = []
    rejected: list[str] = []
    drift_dir_set = set(drift_dir_widened)

    def filter_picks(picks: list[dict]) -> list[dict]:
        """Apply gate to a list of picks in-place — returns surviving picks."""
        out: list[dict] = []
        for p in picks:
            conf = (p.get("confidence") or "MEDIUM").upper()
            direction = (p.get("prediction") or p.get("direction") or "").upper()
            ticker = p.get("ticker", "?")
            if (conf, direction) in broken:
                # LOW → drop. HIGH → downgrade to MEDIUM.
                if conf == "LOW":
                    rejected.append(f"{ticker}({direction}/LOW)")
                    continue
                if conf == "HIGH":
                    p["confidence_pre_calibration_gate"] = "HIGH"
                    p["confidence"] = "MEDIUM"
                    downgraded.append(f"{ticker}({direction}/HIGH→MEDIUM)")
                elif conf == "MEDIUM" and direction in drift_dir_set:
                    # Recent direction-drift widened the gate to (MEDIUM, dir).
                    # Step MEDIUM down to LOW so position sizing / Kelly use
                    # the weak-signal level, but keep the pick (don't drop)
                    # since the AI's signal is still the best we have.
                    # Long-run per-bucket trigger keeps MEDIUM untouched (see
                    # below: existing behaviour preserved when this direction
                    # isn't in drift_dir_set).
                    p["confidence_pre_calibration_gate"] = "MEDIUM"
                    p["confidence"] = "LOW"
                    downgraded.append(f"{ticker}({direction}/MEDIUM→LOW recent-drift)")
                # MEDIUM with long-run-broken bucket (not in drift_dir_set):
                # leave alone — dropping or downgrading on long-run alone
                # would over-correct given the all-time win rate is OK.
            out.append(p)
        return out

    # Apply to discovery (short_term + long_term)
    if discovery_result is not None:
        for key in ("short_term_picks", "long_term_picks", "recommended_stocks"):
            picks = discovery_result.get(key)
            if isinstance(picks, list):
                discovery_result[key] = filter_picks(picks)
    # Apply to holdings
    if holdings_result is not None:
        for key in ("holdings", "holdings_review", "recommendations"):
            picks = holdings_result.get(key)
            if isinstance(picks, list):
                holdings_result[key] = filter_picks(picks)

    summary.setdefault("downgraded", []).extend(downgraded)
    summary.setdefault("rejected", []).extend(rejected)
    return holdings_result, discovery_result


def format_summary_for_slack(summary: dict[str, list[str]]) -> str:
    """One-line Slack message: 'Critic: N kept / M downgraded (tickers) / K rejected (tickers)'."""
    if not any(summary.values()):
        return ""
    parts: list[str] = []
    parts.append(f"keep={len(summary.get('kept') or [])}")
    downgraded = summary.get("downgraded") or []
    if downgraded:
        parts.append(f"downgrade={len(downgraded)} ({', '.join(downgraded)})")
    rejected = summary.get("rejected") or []
    if rejected:
        parts.append(f"reject={len(rejected)} ({', '.join(rejected)})")
    return "Critic 二次評価: " + " / ".join(parts)
