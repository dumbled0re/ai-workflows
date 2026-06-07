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


def _build_position_episodes(predictions: list[dict]) -> list[dict]:
    """Collapse consecutive same-(ticker, direction) predictions into position episodes.

    Each episode = one round-trip position:
      entry_date / entry_price: first prediction in the run
      exit_date / exit_price: first prediction with opposite direction (its entry_price
        approximates "current market price" at that moment), or the last available
        actual_price if no reversal happens
      return_pct: directional realized PnL = (exit - entry) / entry * 100, sign-flipped
        for DOWN positions

    The point: trade-level DD treats a single 21-day-held position with 21 daily
    re-recommendations as 21 separate trades, inflating both trade count and DD.
    Position-aware collapsing gives the equity curve a real trader would have
    experienced (one position per signal direction, sized once).

    Multiple same-day same-(ticker, direction) records (e.g. short_term + long_term
    both recommending UP) collapse into one episode — the first record wins for
    entry_price.
    """
    from collections import defaultdict

    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for p in predictions:
        if p.get("ticker") and p.get("date") and p.get("prediction") and p.get("entry_price") is not None:
            by_ticker[p["ticker"]].append(p)

    episodes: list[dict] = []
    for ticker, preds in by_ticker.items():
        preds.sort(key=lambda p: p["date"])
        cur_dir: str | None = None
        entry_price: float | None = None
        entry_date: str | None = None
        last_seen_price: float | None = None
        for p in preds:
            d = p["prediction"]
            ep = float(p["entry_price"])
            ap = p.get("actual_price")
            last_seen_price = float(ap) if ap is not None else ep
            if cur_dir is None:
                cur_dir = d
                entry_price = ep
                entry_date = p["date"]
                continue
            if d == cur_dir:
                continue  # extend existing position, ignore re-recommendation
            # Direction flip → close current position at this prediction's entry_price
            assert entry_price is not None
            ret = (ep - entry_price) / entry_price * 100
            if cur_dir == "DOWN":
                ret = -ret
            episodes.append(
                {
                    "ticker": ticker,
                    "direction": cur_dir,
                    "entry_date": entry_date,
                    "entry_price": entry_price,
                    "exit_date": p["date"],
                    "exit_price": ep,
                    "return_pct": round(ret, 2),
                    "days_held": _days_between(entry_date, p["date"]),
                }
            )
            cur_dir = d
            entry_price = ep
            entry_date = p["date"]
        # Force-close the last open position at the most recent observed price
        # so the equity curve reflects unrealized PnL too. Without this, a still-
        # open losing position is invisible to the DD calc.
        if cur_dir is not None and entry_price is not None and last_seen_price is not None:
            ret = (last_seen_price - entry_price) / entry_price * 100
            if cur_dir == "DOWN":
                ret = -ret
            last_date = preds[-1].get("reviewed_date") or preds[-1]["date"]
            episodes.append(
                {
                    "ticker": ticker,
                    "direction": cur_dir,
                    "entry_date": entry_date,
                    "entry_price": entry_price,
                    "exit_date": last_date,
                    "exit_price": last_seen_price,
                    "return_pct": round(ret, 2),
                    "days_held": _days_between(entry_date, last_date),
                    "unrealized": True,
                }
            )

    episodes.sort(key=lambda e: e["exit_date"])
    return episodes


def _days_between(d1: str | None, d2: str | None) -> int | None:
    if not d1 or not d2:
        return None
    from datetime import datetime

    try:
        return (datetime.strptime(d2, "%Y-%m-%d") - datetime.strptime(d1, "%Y-%m-%d")).days
    except ValueError:
        return None


def simulate_position_aware(
    history: dict,
    *,
    filter_fn: PredicateFn | None = None,
    label: str = "position-aware",
    tc_round_trip_pct: float = _DEFAULT_TC_ROUND_TRIP_PCT,
) -> tuple[SimResult, list[dict]]:
    """Position-aware variant of ``simulate``.

    Collapses same-(ticker, direction) prediction runs into a single position
    episode so DD/Sharpe reflect what a trader holding actual positions would
    have seen. The trade-level ``simulate`` inflates these metrics because each
    daily re-recommendation gets counted as an independent trade — for a ticker
    like 3777.T with 21 DOWN predictions over 30 days, the trade-level DD
    counts the same drawdown ~6x.

    Returns ``(SimResult, episodes)`` so callers can render per-position detail
    (entry/exit/days_held) in addition to the aggregate metrics.
    """
    predictions = list(history.get("predictions", []))
    if filter_fn is not None:
        predictions = [p for p in predictions if filter_fn(p)]
    episodes = _build_position_episodes(predictions)
    n = len(episodes)
    if n == 0:
        return (
            SimResult(
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
            ),
            [],
        )

    equity: list[float] = []
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    returns: list[float] = []
    win_returns: list[float] = []
    loss_returns: list[float] = []
    for ep in episodes:
        net_r = ep["return_pct"] - tc_round_trip_pct
        returns.append(net_r)
        if net_r > 0:
            win_returns.append(net_r)
        elif net_r < 0:
            loss_returns.append(net_r)
        cumulative += net_r
        equity.append(round(cumulative, 2))
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)

    wins = len(win_returns)
    losses = len(loss_returns)
    mean_r = sum(returns) / n
    expectancy = 0.0
    if win_returns and loss_returns:
        win_rate = wins / n
        avg_w = sum(win_returns) / wins
        avg_l_abs = abs(sum(loss_returns) / losses)
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

    return (
        SimResult(
            label=label,
            trades=n,
            wins=wins,
            losses=losses,
            win_rate_pct=round(wins / n * 100, 1),
            mean_return_pct=round(mean_r, 2),
            expectancy_per_trade_pct=round(expectancy, 2),
            profit_factor=round(profit_factor, 2) if profit_factor is not None else None,
            sharpe_like=round(sharpe, 2) if sharpe is not None else None,
            max_drawdown_pct=round(max_dd, 2),
            equity_curve=equity,
        ),
        episodes,
    )


@dataclass(frozen=True)
class PaperPortfolioResult:
    """Realistic-sized portfolio simulation outcome.

    Unlike ``SimResult`` which accumulates % returns, this tracks notional
    NAV (yen). Each position is sized at ``position_size_pct`` of current NAV
    at entry, simulating what a real trader using fixed-fractional sizing
    would experience. Concurrent positions reduce per-position sizing so
    total exposure doesn't exceed 100% NAV.
    """

    initial_nav: float
    final_nav: float
    peak_nav: float
    max_drawdown_pct: float
    positions: int
    win_rate_pct: float
    nav_series: list[tuple[str, float]]  # (date, nav) after each event
    returns_pct: list[float]  # per-position % return
    position_size_pct: float


def simulate_paper_portfolio(
    history: dict,
    *,
    initial_nav: float = 1_000_000.0,
    position_size_pct: float = 5.0,
    max_concurrent: int = 5,
    tc_round_trip_pct: float = _DEFAULT_TC_ROUND_TRIP_PCT,
) -> PaperPortfolioResult:
    """Simulate a real-money portfolio following the bot's signals.

    Starts with ``initial_nav`` yen. Each position takes ``position_size_pct``
    of current NAV at entry. Up to ``max_concurrent`` positions held at once
    (mirrors investment_rules' 同時5銘柄). When a position closes (direction
    flip or end-of-history force-close), realized PnL is added to NAV.

    Why this matters: trade-level DD inflates because each daily re-recommendation
    counts as a separate trade. Position-aware DD removes that inflation but still
    reports DD as raw % of cumulative returns. **Real DD = % of NAV at peak**,
    which is what actually concerns a trader. With 5% sizing on 100万円, a
    -77% cumulative-position DD translates to ~-4% NAV DD — totally survivable.

    The simulation is purely informational; nothing in the live pipeline acts
    on it. It exists so the weekly review can show "if you actually traded
    this, you would have made N% with max DD M%".
    """
    predictions = list(history.get("predictions", []))
    episodes = _build_position_episodes(predictions)
    if not episodes:
        return PaperPortfolioResult(
            initial_nav=initial_nav,
            final_nav=initial_nav,
            peak_nav=initial_nav,
            max_drawdown_pct=0.0,
            positions=0,
            win_rate_pct=0.0,
            nav_series=[],
            returns_pct=[],
            position_size_pct=position_size_pct,
        )

    # Episodes already sorted by exit_date. We need a chronological event
    # stream: each episode contributes (entry_date, "open", episode) and
    # (exit_date, "close", episode). Sort by date, processing closes first
    # when dates tie (so capital frees up before new opens on the same day).
    events: list[tuple[str, int, dict]] = []
    for ep in episodes:
        events.append((ep["entry_date"] or "", 1, ep))  # open  (order 1)
        events.append((ep["exit_date"] or "", 0, ep))  # close (order 0, processed first on tie)
    events.sort(key=lambda x: (x[0], x[1]))

    nav = initial_nav
    peak = initial_nav
    max_dd_pct = 0.0
    open_positions: dict[int, dict] = {}  # id(ep) -> {"size_yen": float, "entry_price": float, "direction": str}
    nav_series: list[tuple[str, float]] = [(events[0][0] if events else "", nav)]
    returns_pct: list[float] = []
    wins = 0

    for date, kind, ep in events:
        if kind == 1:  # open
            if len(open_positions) >= max_concurrent:
                continue  # skip — at capacity
            size_yen = nav * (position_size_pct / 100.0)
            open_positions[id(ep)] = {
                "size_yen": size_yen,
                "entry_price": ep["entry_price"],
                "direction": ep["direction"],
            }
        else:  # close
            pos = open_positions.pop(id(ep), None)
            if pos is None:
                continue
            ret_pct = ep["return_pct"] - tc_round_trip_pct
            pnl = pos["size_yen"] * (ret_pct / 100.0)
            nav += pnl
            returns_pct.append(ret_pct)
            if ret_pct > 0:
                wins += 1
            peak = max(peak, nav)
            dd_pct = (peak - nav) / peak * 100 if peak > 0 else 0
            max_dd_pct = max(max_dd_pct, dd_pct)
            nav_series.append((date, round(nav, 0)))

    n = len(returns_pct)
    return PaperPortfolioResult(
        initial_nav=initial_nav,
        final_nav=round(nav, 0),
        peak_nav=round(peak, 0),
        max_drawdown_pct=round(max_dd_pct, 2),
        positions=n,
        win_rate_pct=round(wins / n * 100, 1) if n else 0.0,
        nav_series=nav_series,
        returns_pct=returns_pct,
        position_size_pct=position_size_pct,
    )


def format_paper_portfolio_summary(result: PaperPortfolioResult) -> str:
    """Slack-style block: paper portfolio NAV / DD / final P&L.

    Designed to give the operator a concrete "if you had traded this you'd
    be at ¥X with -Y% drawdown" view that's directly comparable to live
    money decisions.
    """
    if result.positions == 0:
        return ""
    total_pnl = result.final_nav - result.initial_nav
    total_return_pct = total_pnl / result.initial_nav * 100
    lines = [
        "=== Paper Portfolio NAV シミュレーション ===",
        f"初期 NAV {result.initial_nav:,.0f}円 / 1ポジ {result.position_size_pct:.1f}% sized / 同時保有上限あり",
        f"  最終 NAV: {result.final_nav:,.0f}円 ({total_return_pct:+.1f}%)",
        f"  ピーク NAV: {result.peak_nav:,.0f}円",
        f"  最大 DD (NAV基準): {result.max_drawdown_pct:.2f}%",
        f"  処理ポジション数: {result.positions} / 勝率 {result.win_rate_pct}%",
    ]
    return "\n".join(lines)


def format_position_aware_summary(result: SimResult, episodes: list[dict], top_n_worst: int = 5) -> str:
    """Slack-style block: trade-aware vs position-aware comparison + worst episodes.

    Designed to surface in the weekly review when trade-level DD diverges
    materially from position-level DD (= sign of recommendation spamming on
    the same ticker, e.g. 3777.T running 21 DOWN predictions).
    """
    if result.trades == 0:
        return ""
    lines = [
        "=== Position-aware シミュレーション ===",
        "「同じ (ticker, direction) 連続予測 = 1 ポジション」 として集計した equity curve。",
        f"  ポジション数: {result.trades} (= round-trip)",
        f"  勝率: {result.win_rate_pct}% / 期待値 {result.expectancy_per_trade_pct:+.2f}%/ポジ",
        f"  Sharpe-like: {result.sharpe_like:+.2f}" if result.sharpe_like is not None else "  Sharpe-like: N/A",
        f"  累積: {result.equity_curve[-1] if result.equity_curve else 0:+.1f}%",
        f"  最大DD: {result.max_drawdown_pct:.1f}% (trade-level の inflate を排除)",
    ]
    losers = sorted([e for e in episodes if e["return_pct"] < 0], key=lambda e: e["return_pct"])[:top_n_worst]
    if losers:
        lines.append("")
        lines.append(f"--- 最悪 {len(losers)} ポジション ---")
        for ep in losers:
            unrz = " [未実現]" if ep.get("unrealized") else ""
            days = ep.get("days_held")
            days_str = f" / {days}日" if days is not None else ""
            lines.append(
                f"  {ep['ticker']:8} {ep['direction']:4} {ep['entry_date']} → "
                f"{ep['exit_date']}{days_str} d_ret={ep['return_pct']:+.2f}%{unrz}"
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
