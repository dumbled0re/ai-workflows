"""Four-factor exposure computation per stock + portfolio aggregate.

Pro institutional desks decompose returns into factor exposures
(market / size / value / momentum) — the classic Fama-French 3+1
canonical set. The portfolio risk check then flags positions where
factor exposure is *concentrated* (z-score |x| >= 1.5) even when
sector diversification looks fine. Two stocks in different sectors
can still share a small-cap-momentum tilt that the sector check
misses.

This is the practical implementation: we compute each factor as a
z-score within the screening universe (not against the broad market
since we don't have a Fama-French JP factor series). The four
factors:

- **Market beta**: 60-day regression slope of stock returns vs N225
  returns. Higher beta → more market exposure.
- **Size**: -log(marketCap). Negative log so small-caps have
  positive z-scores (matches academic convention of "small minus
  big" factor having positive sign for small-cap exposure).
- **Value**: 1/forwardPE (when available, else 1/trailingPE). High
  inverse-PE means cheap on earnings. Negative-earnings tickers
  drop out of the value calculation.
- **Momentum**: 60-day total price return. Higher → more momentum.

Each per-stock raw value is normalised to a z-score within the
universe of screened candidates so "concentrated exposure" can be
measured uniformly across factors.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FactorExposure:
    """Per-stock four-factor exposure z-scores.

    Values are normalised within the screening universe so a
    z-score of +1.5 means "1.5 stdev more loaded on this factor
    than the median screened stock". None means the input was
    unavailable for that factor (e.g. no PER → no value exposure).
    """

    ticker: str
    market_beta_z: float | None
    size_z: float | None
    value_z: float | None
    momentum_z: float | None


def _safe_returns(closes: list[float]) -> list[float]:
    """Daily simple-returns from a closes list. Drops days where
    prev-close is zero to avoid division blowup."""
    out: list[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev <= 0:
            continue
        out.append((closes[i] - prev) / prev)
    return out


def _beta(stock_rets: list[float], market_rets: list[float]) -> float | None:
    """OLS beta of stock returns vs market returns. None when
    insufficient data or zero market variance."""
    n = min(len(stock_rets), len(market_rets))
    if n < 20:
        return None
    s = stock_rets[-n:]
    m = market_rets[-n:]
    mean_s = sum(s) / n
    mean_m = sum(m) / n
    cov = sum((s[i] - mean_s) * (m[i] - mean_m) for i in range(n)) / n
    var_m = sum((m[i] - mean_m) ** 2 for i in range(n)) / n
    if var_m <= 0:
        return None
    return cov / var_m


def _zscores(values: dict[str, float | None]) -> dict[str, float | None]:
    """Convert per-ticker raw values to within-universe z-scores.

    Tickers whose value is None stay None. Z-scores use the
    mean/stdev across the non-None subset; when stdev is zero
    (all equal), every z is set to 0.0.
    """
    present = [v for v in values.values() if v is not None]
    if len(present) < 5:
        return dict.fromkeys(values)
    mean = sum(present) / len(present)
    variance = sum((v - mean) ** 2 for v in present) / len(present)
    stdev = math.sqrt(variance)
    out: dict[str, float | None] = {}
    for ticker, v in values.items():
        if v is None:
            out[ticker] = None
        elif stdev == 0:
            out[ticker] = 0.0
        else:
            out[ticker] = round((v - mean) / stdev, 2)
    return out


def compute_factor_exposures(
    universe: dict[str, dict],
    market_closes: list[float] | None,
    closes_by_ticker: dict[str, list[float]],
) -> dict[str, FactorExposure]:
    """Compute four-factor z-scored exposures for the universe.

    ``universe`` maps ticker → fundamentals dict (must carry
    marketCap and at least one of forwardPE / trailingPE for the
    size and value factors; otherwise those slots stay None).
    ``closes_by_ticker`` provides recent daily closes per ticker for
    momentum + beta. ``market_closes`` is the same for the
    benchmark (typically N225).

    Returns ticker → ``FactorExposure``. Tickers with insufficient
    data are present in the result but their factor slots may all
    be None — callers handle that by skipping the position in the
    aggregate.
    """
    market_rets = _safe_returns(market_closes) if market_closes else []

    # Raw per-ticker values for each factor (pre-z-score).
    raw_market: dict[str, float | None] = {}
    raw_size: dict[str, float | None] = {}
    raw_value: dict[str, float | None] = {}
    raw_momentum: dict[str, float | None] = {}

    for ticker, fundamentals in universe.items():
        closes = closes_by_ticker.get(ticker) or []

        # Market beta vs benchmark
        beta = _beta(_safe_returns(closes), market_rets) if market_rets and closes else None
        raw_market[ticker] = beta

        # Size — negative log(marketCap) so small-caps have higher z
        mcap = fundamentals.get("marketCap")
        if isinstance(mcap, (int, float)) and mcap > 0:
            raw_size[ticker] = -math.log(float(mcap))
        else:
            raw_size[ticker] = None

        # Value — 1 / forward or trailing PE; negative or missing → None
        per = fundamentals.get("forwardPE") or fundamentals.get("trailingPE")
        if isinstance(per, (int, float)) and per > 0:
            raw_value[ticker] = 1.0 / float(per)
        else:
            raw_value[ticker] = None

        # Momentum — 60-day total return %, ignore short series
        if len(closes) >= 60:
            try:
                old = closes[-60]
                cur = closes[-1]
                if old > 0:
                    raw_momentum[ticker] = (cur - old) / old * 100
                else:
                    raw_momentum[ticker] = None
            except (IndexError, TypeError):
                raw_momentum[ticker] = None
        else:
            raw_momentum[ticker] = None

    z_market = _zscores(raw_market)
    z_size = _zscores(raw_size)
    z_value = _zscores(raw_value)
    z_momentum = _zscores(raw_momentum)

    return {
        ticker: FactorExposure(
            ticker=ticker,
            market_beta_z=z_market.get(ticker),
            size_z=z_size.get(ticker),
            value_z=z_value.get(ticker),
            momentum_z=z_momentum.get(ticker),
        )
        for ticker in universe
    }


def aggregate_portfolio_exposure(
    recommendations: list[dict],
    exposures: dict[str, FactorExposure],
) -> dict[str, float | None]:
    """Weighted average of per-pick factor exposures across the recs.

    Equal-weight aggregation (each pick contributes 1/N). Tickers
    not in the exposures dict are silently skipped — that ticker
    simply doesn't contribute to the aggregate.

    Returns ``{"market_beta_z": x, "size_z": y, ...}``. Any factor
    where fewer than 2 picks have data returns None for that slot.
    """
    out: dict[str, float | None] = {}
    for factor in ("market_beta_z", "size_z", "value_z", "momentum_z"):
        contributions: list[float] = []
        for r in recommendations:
            ticker = r.get("ticker")
            if not ticker:
                continue
            ex = exposures.get(str(ticker))
            if ex is None:
                continue
            value = getattr(ex, factor, None)
            if value is None:
                continue
            contributions.append(float(value))
        if len(contributions) < 2:
            out[factor] = None
        else:
            out[factor] = round(sum(contributions) / len(contributions), 2)
    return out


_CONCENTRATION_THRESHOLD_Z = 1.5


def detect_factor_concentration(
    aggregate: dict[str, float | None],
    threshold: float = _CONCENTRATION_THRESHOLD_Z,
) -> list[dict]:
    """Flag factor slots whose aggregate exposure exceeds threshold.

    Returns a list of ``{"factor", "value", "severity"}`` dicts —
    one per factor that's concentrated. Empty list = portfolio is
    factor-balanced. The threshold is symmetric: +1.5z on momentum
    is "too momentum-tilted", -1.5z is "too anti-momentum".
    """
    findings: list[dict] = []
    factor_labels = {
        "market_beta_z": "市場ベータ",
        "size_z": "サイズ (小型 vs 大型)",
        "value_z": "バリュー (低 PER vs 高 PER)",
        "momentum_z": "モメンタム (60d 上昇 vs 下落)",
    }
    for key, value in aggregate.items():
        if value is None:
            continue
        if abs(value) >= threshold:
            findings.append(
                {
                    "factor": factor_labels.get(key, key),
                    "value": value,
                    "severity": "warning",
                }
            )
    return findings


def format_factor_concentration_for_prompt(
    findings: list[dict],
    aggregate: dict[str, float | None],
) -> str:
    """Render the factor concentration warning + full exposure profile.

    Always emits the profile (so the AI sees the system's overall
    tilt even on a balanced portfolio); the warnings section only
    appears when there are flagged factors.
    """
    if not aggregate:
        return ""
    factor_labels = {
        "market_beta_z": "市場ベータ",
        "size_z": "サイズ",
        "value_z": "バリュー",
        "momentum_z": "モメンタム",
    }
    lines = ["=== ポートフォリオ Factor 露出 (推奨銘柄の集合特性) ==="]
    for key, label in factor_labels.items():
        v = aggregate.get(key)
        if v is None:
            lines.append(f"- {label}: データ不足")
        else:
            tilt = ""
            if v >= 0.5:
                tilt = " (強)"
            elif v <= -0.5:
                tilt = " (逆方向に強)"
            lines.append(f"- {label}: z={v:+.2f}{tilt}")
    if findings:
        lines.append("")
        lines.append("⚠ Factor 集中警告:")
        for f in findings:
            lines.append(
                f"  - {f['factor']} の集合 z={f['value']:+.2f} (|z|>=1.5): "
                "単一 factor に偏った持ち合わせは sector 分散と独立に portfolio リスク。"
                "1-2 銘柄を逆 factor 露出の picks と入れ替えると分散効果が出ます"
            )
    return "\n".join(lines)
