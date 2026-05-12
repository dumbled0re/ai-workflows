"""Counterfactual backtest over the live predictions_history.

A full historical-OHLCV backtest is overkill for a personal pipeline:
the predictions_history already accumulates real-world entry / exit /
return data. What's missing is the ability to ask **counterfactual**
questions — "if I had filtered out HIGH confidence picks, what would
my equity curve look like?" — which is what actually drives strategy
iteration.

This module takes resolved predictions, applies a filter function,
and re-computes the per-trade and aggregate stats with the surviving
trades only. Filters compose, so you can stack "HIGH only + has
volume_spike signal + UP direction" and get a clean comparison
against the unfiltered baseline.

Direction-aware throughout (uses ``performance_tracker._directional_return``
so DOWN-wins count positively just like the live stats).

Outputs an equity curve so the result is plot-friendly and the
weekly review prompt can compare "with vs without filter" Sharpe /
max-drawdown side by side instead of just the headline win-rate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from stock_analyzer.performance_tracker import _directional_return

# Type alias for filter callbacks. Each takes one prediction dict and
# returns True to keep it.
PredicateFn = Callable[[dict], bool]


@dataclass(frozen=True)
class SimResult:
    """Aggregate metrics for a single filtered simulation."""

    label: str
    trades: int
    wins: int
    losses: int
    win_rate_pct: float
    mean_return_pct: float
    expectancy_per_trade_pct: float
    profit_factor: float | None
    sharpe_like: float | None
    max_drawdown_pct: float
    equity_curve: list[float]  # cumulative return (% units) after each trade

    @property
    def total_return_pct(self) -> float:
        return self.equity_curve[-1] if self.equity_curve else 0.0


_DEFAULT_TC_ROUND_TRIP_PCT = 0.4
"""Round-trip transaction cost for typical JP-equity retail swing trade.

Decomposition (rough):
- Bid-ask spread half-cost on entry + exit: 0.1-0.2% each, ~0.3% total
- Broker commission (SBI / Rakuten retail tier): ~0.05% each side
- Slippage on market orders: ~0.05% each side

Total round-trip ~0.4%. For a 14-day swing targeting ~2% expected return,
this is 20% of gross return — material enough that ignoring it makes
gross-metric reports systematically optimistic. Pass 0.0 to retain
gross-only behaviour, e.g. for comparison."""


def simulate(
    history: dict,
    *,
    filter_fn: PredicateFn | None = None,
    label: str = "all",
    tc_round_trip_pct: float = 0.0,
) -> SimResult:
    """Run a counterfactual sim over resolved predictions.

    ``filter_fn`` receives each prediction (raw dict) and returns
    True to include it. Pass ``None`` for the unfiltered baseline.
    Returns a ``SimResult`` carrying both the headline metrics and
    the per-trade equity curve.

    ``tc_round_trip_pct`` deducts a per-trade transaction cost from
    every directional return before metrics aggregate. Default 0.0
    preserves the old "gross only" behaviour so existing callers
    keep their results stable; pass ``_DEFAULT_TC_ROUND_TRIP_PCT``
    (0.4) for realistic net-of-cost numbers on JP retail swing.
    The TC is applied uniformly — a more rigorous model would scale
    by liquidity, but at this granularity uniform is the best
    available estimate.
    """
    resolved = [p for p in history.get("predictions", []) if p.get("status") in ("win", "loss")]
    if filter_fn is not None:
        resolved = [p for p in resolved if filter_fn(p)]

    # Chronological by reviewed_date for the equity curve. Predictions
    # without ``reviewed_date`` fall to the front (treated as 0).
    resolved.sort(key=lambda p: p.get("reviewed_date") or p.get("date", ""))

    wins = [p for p in resolved if p["status"] == "win"]
    losses = [p for p in resolved if p["status"] == "loss"]
    n = len(resolved)
    if n == 0:
        return SimResult(
            label=label,
            trades=0,
            wins=0,
            losses=0,
            win_rate_pct=0.0,
            mean_return_pct=0.0,
            expectancy_per_trade_pct=0.0,
            profit_factor=None,
            sharpe_like=None,
            max_drawdown_pct=0.0,
            equity_curve=[],
        )

    # Equity curve: cumulative directional return after each trade,
    # net of TC when configured.
    equity: list[float] = []
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    returns: list[float] = []
    for p in resolved:
        r = _directional_return(p)
        if r is None:
            continue
        net_r = r - tc_round_trip_pct
        returns.append(net_r)
        cumulative += net_r
        equity.append(round(cumulative, 2))
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)

    win_returns = [r - tc_round_trip_pct for r in (_directional_return(p) for p in wins) if r is not None]
    loss_returns = [r - tc_round_trip_pct for r in (_directional_return(p) for p in losses) if r is not None]

    mean_r = sum(returns) / len(returns) if returns else 0.0
    expectancy = 0.0
    if win_returns and loss_returns:
        win_rate = len(wins) / n
        avg_w = sum(win_returns) / len(win_returns)
        avg_l_abs = abs(sum(loss_returns) / len(loss_returns))
        expectancy = win_rate * avg_w - (1 - win_rate) * avg_l_abs

    profit_factor: float | None = None
    if win_returns and loss_returns:
        gross_loss = abs(sum(loss_returns))
        if gross_loss > 0:
            profit_factor = sum(win_returns) / gross_loss

    sharpe: float | None = None
    if len(returns) >= 2:
        variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        stdev = variance**0.5
        if stdev > 0:
            sharpe = mean_r / stdev

    return SimResult(
        label=label,
        trades=n,
        wins=len(wins),
        losses=len(losses),
        win_rate_pct=round(len(wins) / n * 100, 1),
        mean_return_pct=round(mean_r, 2),
        expectancy_per_trade_pct=round(expectancy, 2),
        profit_factor=round(profit_factor, 2) if profit_factor is not None else None,
        sharpe_like=round(sharpe, 2) if sharpe is not None else None,
        max_drawdown_pct=round(max_dd, 2),
        equity_curve=equity,
    )


# --- canned filters used by the weekly review ----------------------------


def only_confidence(confidence: str) -> PredicateFn:
    return lambda p: p.get("confidence") == confidence


def only_direction(direction: str) -> PredicateFn:
    return lambda p: p.get("prediction") == direction


def has_signal(name: str) -> PredicateFn:
    return lambda p: bool((p.get("signal_components") or {}).get(name))


def lacks_signal(name: str) -> PredicateFn:
    return lambda p: not (p.get("signal_components") or {}).get(name)


def from_source(source: str) -> PredicateFn:
    return lambda p: p.get("source") == source


def combine_and(*filters: PredicateFn) -> PredicateFn:
    """All filters must pass."""
    return lambda p: all(f(p) for f in filters)


# --- canned comparison set ----------------------------------------------


def standard_counterfactuals(history: dict, tc_round_trip_pct: float = 0.0) -> list[SimResult]:
    """Run a small fixed battery of "what if I had filtered" sims.

    The point isn't to be exhaustive — it's to surface the obvious
    filter that would have most-improved Sharpe, so the weekly review
    prompt can recommend a targeted rule (e.g. "drop HIGH-DOWN" or
    "only trades with macd_crossover").

    Pass ``tc_round_trip_pct=_DEFAULT_TC_ROUND_TRIP_PCT`` (0.4) to see
    the same battery net of transaction costs — useful when comparing
    filter candidates whose gross-return advantage gets eaten by
    higher trade frequency.
    """
    sims: list[SimResult] = [simulate(history, label="baseline (all)", tc_round_trip_pct=tc_round_trip_pct)]
    for conf in ("HIGH", "MEDIUM"):
        sims.append(
            simulate(
                history,
                filter_fn=only_confidence(conf),
                label=f"confidence={conf}",
                tc_round_trip_pct=tc_round_trip_pct,
            )
        )
    for direction in ("UP", "DOWN"):
        sims.append(
            simulate(
                history,
                filter_fn=only_direction(direction),
                label=f"direction={direction}",
                tc_round_trip_pct=tc_round_trip_pct,
            )
        )
    for source in ("holdings", "short_term", "long_term"):
        sims.append(
            simulate(
                history,
                filter_fn=from_source(source),
                label=f"source={source}",
                tc_round_trip_pct=tc_round_trip_pct,
            )
        )
    return [s for s in sims if s.trades >= 5]


def compare_gross_vs_net(history: dict, tc_round_trip_pct: float = _DEFAULT_TC_ROUND_TRIP_PCT) -> dict:
    """Run baseline gross vs net (TC-applied) to quantify cost drag.

    For each filter in the standard battery, returns a pair of
    SimResult dicts (one with tc=0, one with tc=tc_round_trip_pct)
    so the weekly review can show "you would have made X% gross but
    only Y% net". When net is negative on a gross-positive strategy,
    that's a strong signal the filter has too many marginal trades.
    """
    gross = standard_counterfactuals(history, tc_round_trip_pct=0.0)
    net = standard_counterfactuals(history, tc_round_trip_pct=tc_round_trip_pct)
    by_label_gross = {s.label: s for s in gross}
    by_label_net = {s.label: s for s in net}
    rows: list[dict] = []
    for label in by_label_gross:
        g = by_label_gross.get(label)
        n = by_label_net.get(label)
        if g is None or n is None:
            continue
        rows.append(
            {
                "label": label,
                "trades": g.trades,
                "gross_total_return_pct": g.total_return_pct,
                "net_total_return_pct": n.total_return_pct,
                "tc_drag_pct": round(g.total_return_pct - n.total_return_pct, 2),
                "gross_expectancy_pct": g.expectancy_per_trade_pct,
                "net_expectancy_pct": n.expectancy_per_trade_pct,
                "gross_sharpe": g.sharpe_like,
                "net_sharpe": n.sharpe_like,
                "gross_max_dd_pct": g.max_drawdown_pct,
                "net_max_dd_pct": n.max_drawdown_pct,
            }
        )
    return {
        "tc_round_trip_pct": tc_round_trip_pct,
        "rows": rows,
    }


def format_gross_vs_net_for_prompt(report: dict) -> str:
    """Weekly-review-style block showing TC drag per filter strategy.

    Sorted by net total return descending so the strategies that stay
    profitable after costs surface at the top. Negative net is
    explicitly flagged so the AI can recommend dropping the filter.
    """
    rows = report.get("rows") or []
    if not rows:
        return ""
    tc = report.get("tc_round_trip_pct", 0.0)
    lines = [
        f"=== Gross vs Net (TC = {tc:.2f}%/round-trip) ===",
        "filter ごとに TC を引いた net P&L 比較。net がマイナスなら gross が黒字でも実質損。",
    ]
    rows_sorted = sorted(rows, key=lambda r: r.get("net_total_return_pct", -1e9), reverse=True)
    for r in rows_sorted:
        net = r.get("net_total_return_pct", 0.0)
        gross = r.get("gross_total_return_pct", 0.0)
        drag = r.get("tc_drag_pct", 0.0)
        flag = "  ⚠ net マイナス" if net < 0 < gross else ""
        lines.append(
            f"- {r.get('label', '?')}: trades={r.get('trades', 0)} / "
            f"gross {gross:+.1f}% → net {net:+.1f}% (TC drag {drag:.1f}%){flag}"
        )
    return "\n".join(lines)


def format_counterfactuals_for_prompt(sims: list[SimResult]) -> str:
    """Render the canned comparison as a weekly-review prompt block.

    The block is sorted by Sharpe-like (descending), so the filter that
    most-improves risk-adjusted return floats to the top and Claude can
    cite it directly in the strategy notes.
    """
    if not sims:
        return ""
    lines = ["=== 反実仮想バックテスト (現 history を filter で再評価) ==="]
    lines.append(
        "「もしこの条件だけに絞っていたら累積成績はどうなっていたか」の比較。"
        "Sharpe / 期待値 / max-DD で baseline を上回る filter があれば、その条件を "
        "entry_rules や confidence_calibration に反映してください。"
    )

    def sortkey(s: SimResult) -> float:
        return s.sharpe_like if s.sharpe_like is not None else -1.0

    sims_sorted = sorted(sims, key=sortkey, reverse=True)
    for s in sims_sorted:
        sharpe_str = f"Sharpe {s.sharpe_like:+.2f}" if s.sharpe_like is not None else "Sharpe N/A"
        pf_str = f"PF {s.profit_factor:.2f}" if s.profit_factor is not None else "PF N/A"
        lines.append(
            f"- {s.label}: {s.trades}件 勝率{s.win_rate_pct}% "
            f"期待値{s.expectancy_per_trade_pct:+.2f}% / {sharpe_str} / {pf_str} / "
            f"DD {s.max_drawdown_pct:.1f}% / 累積{s.total_return_pct:+.1f}%"
        )
    return "\n".join(lines)
