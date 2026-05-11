"""Portfolio-level risk checks for the daily analysis output.

The investment rules in ``data/investment_rules.json`` already say things
like "同一セクターに2銘柄以上の推奨は避ける (相関リスク)" and
``max_concurrent_recommendations: 5`` — but those live in the prompt
text, which the AI may or may not honour. This module turns those rules
into deterministic post-hoc checks that flag violations before the
recommendations land in Slack, so the operator always sees an explicit
warning instead of trusting prompt compliance.

The checks here are intentionally cheap (no extra HTTP, just the dicts
we already have): sector concentration, total position count, and a
pairwise-correlation heuristic that uses the close-price DataFrames
already pulled in ``main.py`` (avoids any new data dependency).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# Defaults mirror what investment_rules.json declares; the constants are
# overridable so a future weekly review could tune them via the same
# `apply_review_results` path that updates screening_weights.
MAX_RECOMMENDATIONS = 5
MAX_PER_SECTOR = 1
# Above this absolute Pearson correlation between two recommendations'
# 60-day daily returns, treat the pair as "too correlated" → only the
# higher-confidence one survives. Tuned to be lenient (real same-sector
# pairs commonly hit 0.7+) so the check only fires on near-duplicates.
HIGH_CORRELATION_THRESHOLD = 0.85
CORRELATION_WINDOW_DAYS = 60


@dataclass(frozen=True)
class RiskFinding:
    """One violation surfaced by the portfolio check."""

    severity: str  # "warning" or "info"
    kind: str  # "sector_concentration" | "total_count" | "correlation"
    message: str
    affected_tickers: tuple[str, ...] = field(default_factory=tuple)


def check_sector_concentration(
    recommendations: list[dict],
    ticker_info: dict[str, dict],
    max_per_sector: int = MAX_PER_SECTOR,
) -> list[RiskFinding]:
    """Flag sectors holding more than ``max_per_sector`` recommendations.

    ``recommendations`` is a flat list of dicts with at least ``ticker``
    set; ``ticker_info`` maps ticker → ``{"sector": "..."}`` from
    ``yfinance``. Tickers with unknown sector are grouped under "不明"
    and excluded from the concentration check (would otherwise pile up
    spuriously). The default ``max_per_sector=1`` matches the
    investment-rules.json "同セクター2銘柄以上は避ける" wording (= max 1
    per sector is the target).
    """
    findings: list[RiskFinding] = []
    sector_map: dict[str, list[str]] = {}
    for r in recommendations:
        ticker = r.get("ticker")
        if not ticker:
            continue
        info = ticker_info.get(ticker, {}) or {}
        sector = info.get("sector") or "不明"
        if sector == "不明":
            continue
        sector_map.setdefault(sector, []).append(ticker)
    for sector, tickers in sector_map.items():
        if len(tickers) > max_per_sector:
            findings.append(
                RiskFinding(
                    severity="warning",
                    kind="sector_concentration",
                    message=(
                        f"セクター「{sector}」に {len(tickers)} 銘柄推奨 "
                        f"(上限 {max_per_sector}): 相関リスクで分散効果が薄れます。"
                        "信頼度の高い 1 銘柄に絞るか、別セクターへ振替えてください"
                    ),
                    affected_tickers=tuple(tickers),
                )
            )
    return findings


def check_total_recommendations(
    recommendations: list[dict],
    max_count: int = MAX_RECOMMENDATIONS,
) -> list[RiskFinding]:
    """Flag when total recommendations exceed the configured cap."""
    count = sum(1 for r in recommendations if r.get("ticker"))
    if count > max_count:
        return [
            RiskFinding(
                severity="warning",
                kind="total_count",
                message=(
                    f"推奨銘柄 {count} 件が上限 {max_count} を超過。"
                    "集中度を下げる + ポジション管理の観点で {max_count} 件以下に絞ってください"
                ),
            )
        ]
    return []


def check_pairwise_correlation(
    recommendations: list[dict],
    price_data: dict[str, object],
    window_days: int = CORRELATION_WINDOW_DAYS,
    threshold: float = HIGH_CORRELATION_THRESHOLD,
) -> list[RiskFinding]:
    """Flag recommendation pairs whose recent daily returns are near-duplicate.

    Uses only the close-price DataFrames already loaded for screening /
    holdings; no extra HTTP. When two recommendations have absolute
    Pearson correlation above ``threshold`` over the last
    ``window_days`` of daily returns, surface the pair so the operator
    can drop one.

    ``price_data`` maps ticker → pandas DataFrame with a ``Close`` column.
    Tickers without enough data (or not in ``price_data``) are skipped
    silently; this is best-effort, not authoritative.
    """
    findings: list[RiskFinding] = []
    # Build per-ticker daily-return arrays from the supplied DataFrames.
    returns: dict[str, list[float]] = {}
    for r in recommendations:
        ticker = r.get("ticker")
        if not ticker or ticker in returns:
            continue
        df = price_data.get(ticker)
        if df is None:
            continue
        try:
            close = df["Close"]  # type: ignore[index]
            tail = close.tail(window_days + 1)
            if len(tail) < 20:
                # Too short to compute a meaningful correlation
                continue
            rets = [
                (float(tail.iloc[i]) - float(tail.iloc[i - 1])) / float(tail.iloc[i - 1])
                for i in range(1, len(tail))
                if float(tail.iloc[i - 1]) > 0
            ]
            if len(rets) >= 20:
                returns[ticker] = rets
        except Exception:
            # best-effort: a malformed close series just excludes the
            # ticker from the correlation check rather than aborting.
            logger.debug("pairwise correlation: skipping %s", ticker, exc_info=True)

    tickers = list(returns)
    for i, a in enumerate(tickers):
        for b in tickers[i + 1 :]:
            ra, rb = returns[a], returns[b]
            n = min(len(ra), len(rb))
            if n < 20:
                continue
            corr = _pearson(ra[-n:], rb[-n:])
            if corr is None:
                continue
            if abs(corr) >= threshold:
                findings.append(
                    RiskFinding(
                        severity="warning",
                        kind="correlation",
                        message=(
                            f"{a} と {b} の直近 {n}日 日次リターン相関 = {corr:+.2f}: "
                            "ほぼ同じ動きをするので分散になっていません。信頼度の高い方を残してください"
                        ),
                        affected_tickers=(a, b),
                    )
                )
    return findings


def check_risk_reward(
    recommendations: list[dict],
    min_ratio: float | None = None,
) -> list[RiskFinding]:
    """Flag picks whose deterministically-computed R/R is below ``min_ratio``.

    Reads ``entry_price`` / ``stop_loss`` / ``target_price`` /
    ``prediction`` from each rec, parses the AI's free-form strings
    into numerics, and computes the actual risk/reward ratio. Picks
    that can't be parsed (missing target, malformed string) skip
    silently — we cannot meaningfully flag a setup we don't
    understand. Inverted setups (target on the wrong side of entry)
    parse to ratio 0.0 and are explicitly flagged.

    Holdings picks generally don't carry ``target_price`` and so go
    unchecked here; the constraint is meaningful primarily for
    discovery short_term / long_term picks where the AI sets a
    target. This is intentional: holdings already represent capital
    at work, not a fresh entry choice.
    """
    from stock_analyzer.risk_reward import DEFAULT_MIN_RATIO, compute_for_pick

    threshold = DEFAULT_MIN_RATIO if min_ratio is None else min_ratio
    findings: list[RiskFinding] = []
    for r in recommendations:
        ticker = r.get("ticker")
        if not ticker:
            continue
        rr = compute_for_pick(r)
        if rr is None:
            continue
        if rr < threshold:
            findings.append(
                RiskFinding(
                    severity="warning",
                    kind="risk_reward",
                    message=(
                        f"{ticker} の R/R = {rr:.2f} < {threshold:.1f}: "
                        "想定上昇幅に対し損切り幅が広すぎます。"
                        f"target_price を引き上げる or stop_loss を縮めて R/R >= {threshold:.1f} を確保してください"
                    ),
                    affected_tickers=(str(ticker),),
                )
            )
    return findings


def check_stop_loss_consistency(
    recommendations: list[dict],
) -> list[RiskFinding]:
    """Flag picks where stop_loss is on the wrong side of entry for the
    predicted direction.

    A long pick (UP) needs stop < entry; a short pick (DOWN) needs
    stop > entry. Anything else is a structurally malformed setup —
    the AI is contradicting its own direction call. The R/R check
    catches the inverted-target case (target on wrong side) but not
    the inverted-stop case, since inverted stop returns None from
    compute_risk_reward and silently passes through.

    This is the explicit safety net for the second malformation mode.
    Picks where either field can't be parsed skip silently.
    """
    from stock_analyzer.risk_reward import parse_price_string

    findings: list[RiskFinding] = []
    for r in recommendations:
        ticker = r.get("ticker")
        if not ticker:
            continue
        direction = (r.get("prediction") or "").upper()
        if direction not in {"UP", "DOWN"}:
            continue
        entry = parse_price_string(r.get("entry_price"))
        stop = parse_price_string(r.get("stop_loss"))
        if entry is None or stop is None:
            continue
        bad = (direction == "UP" and stop >= entry) or (direction == "DOWN" and stop <= entry)
        if bad:
            hint = "UP なら stop < entry" if direction == "UP" else "DOWN なら stop > entry"
            findings.append(
                RiskFinding(
                    severity="warning",
                    kind="stop_loss_inconsistent",
                    message=(
                        f"{ticker} {direction}予測なのに stop_loss が entry の同方向or上下逆 "
                        f"(entry {entry} / stop {stop}): 損切りが効かない設定です。{hint} に修正してください"
                    ),
                    affected_tickers=(str(ticker),),
                )
            )
    return findings


def check_all(
    recommendations: list[dict],
    ticker_info: dict[str, dict] | None = None,
    price_data: dict[str, object] | None = None,
) -> list[RiskFinding]:
    """Run every available check and return findings sorted by severity."""
    findings: list[RiskFinding] = []
    findings.extend(check_total_recommendations(recommendations))
    if ticker_info is not None:
        findings.extend(check_sector_concentration(recommendations, ticker_info))
    if price_data is not None:
        findings.extend(check_pairwise_correlation(recommendations, price_data))
    findings.extend(check_risk_reward(recommendations))
    findings.extend(check_stop_loss_consistency(recommendations))
    # Stable order: warnings before info, then by kind for determinism
    severity_rank = {"warning": 0, "info": 1}
    findings.sort(key=lambda f: (severity_rank.get(f.severity, 2), f.kind))
    return findings


def format_findings_for_slack(findings: list[RiskFinding]) -> str:
    """Render findings as a Slack-ready block, or '' when none."""
    if not findings:
        return ""
    lines = ["⚠️ ポートフォリオリスク警告"]
    for f in findings:
        icon = "🔴" if f.severity == "warning" else "🟡"
        lines.append(f"{icon} {f.message}")
        if f.affected_tickers:
            lines.append(f"   対象: {', '.join(f.affected_tickers)}")
    return "\n".join(lines)


def format_findings_for_prompt(findings: list[RiskFinding]) -> str:
    """Render findings as a prompt-injection block fed back to Claude.

    Same content as the Slack block but headed with an instruction so
    the AI accounts for the violation in the next analysis cycle.
    """
    if not findings:
        return ""
    lines = [
        "=== 前回のポートフォリオリスク警告 ===",
        "前回の推奨で以下の制約違反がありました。次回はこれを発生させないよう銘柄選定を調整してください:",
    ]
    for f in findings:
        lines.append(f"- [{f.kind}] {f.message}")
        if f.affected_tickers:
            lines.append(f"  対象: {', '.join(f.affected_tickers)}")
    return "\n".join(lines)


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation. None when undefined (constant series)."""
    if len(xs) != len(ys) or not xs:
        return None
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return None
    return float(num / (var_x**0.5 * var_y**0.5))
