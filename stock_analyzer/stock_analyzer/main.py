from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jpholiday

JST = timezone(timedelta(hours=9))
_DATA_DIR = Path(__file__).parent.parent / "data"


class _ClosesShim:
    """Minimal shim presenting a list of closes as ``df["Close"].tail(N).iloc[i]``.

    Used by ``phase_notify`` so ``portfolio_risk.check_pairwise_correlation``
    can read the close-history JSON saved in Phase 1 without pulling
    pandas back into Phase 3 just for a few lookups.
    """

    def __init__(self, closes: list[float]) -> None:
        self._closes = list(closes)

    def __getitem__(self, key: str | int) -> _ClosesShim | float:
        # ``df["Close"]`` returns the same closes view; ``tail.iloc[i]``
        # passes through to a numeric close. The branch keeps both call
        # patterns satisfied without dragging pandas in.
        if isinstance(key, str):
            if key == "Close":
                return self
            raise KeyError(key)
        return float(self._closes[key])

    def tail(self, n: int) -> _ClosesShim:
        return _ClosesShim(self._closes[-n:])

    def __len__(self) -> int:
        return len(self._closes)

    @property
    def iloc(self) -> _ClosesShim:
        return self

    def tolist(self) -> list[float]:
        return list(self._closes)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def is_market_day(date: datetime) -> bool:
    """Check if the given date is a Tokyo Stock Exchange trading day."""
    d = date.date()
    if d.weekday() >= 5:
        return False
    return not jpholiday.is_holiday(d)


def phase_prepare() -> None:
    """Phase 1: Fetch data, compute indicators, prepare prompts for Claude."""
    from stock_analyzer.ai_analyzer import prepare_prompts
    from stock_analyzer.config_loader import load_config
    from stock_analyzer.data_fetcher import fetch_batch
    from stock_analyzer.market_context import fetch_market_context, format_market_context
    from stock_analyzer.news_fetcher import (
        fetch_margin_data,
        fetch_market_news,
        fetch_stock_news,
        format_market_news,
        format_stock_news,
    )
    from stock_analyzer.sector_analysis import compute_sector_rankings, format_sector_ranking
    from stock_analyzer.slack_notifier import send_market_closed_to_slack
    from stock_analyzer.stock_screener import screen_stocks
    from stock_analyzer.technical_indicators import compute_indicators

    now_jst = datetime.now(JST)
    timing = "morning" if now_jst.hour < 12 else "evening"
    date_str = now_jst.strftime("%Y-%m-%d")

    logger.info("Phase 1 (Prepare): %s (%s)", date_str, timing)

    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_channel = os.environ.get("SLACK_CHANNEL_STOCK")

    # Market calendar check
    if not is_market_day(now_jst):
        logger.info("Market is closed today (%s)", date_str)
        if slack_token and slack_channel:
            send_market_closed_to_slack(slack_token, slack_channel, date_str)
        sys.exit(0)

    # Load config
    config = load_config()
    logger.info("Loaded %d holdings", len(config.holdings))

    # Track data quality
    import json

    data_quality: dict = {"success": 0, "failed": 0}

    # Performance tracking: load history and prepare for review
    from stock_analyzer.performance_tracker import (
        format_performance_feedback,
        load_history,
        review_predictions,
        save_history,
    )
    from stock_analyzer.strategy_learner import (
        format_strategy_notes_for_prompt,
        load_screening_weights,
        load_strategy_notes,
    )

    perf_history = load_history()
    logger.info(
        "Loaded prediction history: %d predictions",
        len(perf_history.get("predictions", [])),
    )

    # Load strategy notes and screening weights (from weekly review)
    strategy_notes = load_strategy_notes()
    screening_weights = load_screening_weights()
    strategy_notes_text = format_strategy_notes_for_prompt(strategy_notes)
    logger.info(
        "Loaded %d strategy notes, screening weights ready",
        len(strategy_notes.get("notes", [])),
    )

    # Fetch market context (indices, forex, sentiment)
    logger.info("Fetching market context...")
    market_context = fetch_market_context()
    market_context_text = format_market_context(market_context)

    # Calendar/seasonality context (earnings concentration windows,
    # dividend ex-date approach, year-end thinning, GW, summer doldrums).
    # Append to the same block so the AI sees it inline with the
    # price-based regime.
    from stock_analyzer.calendar_context import detect_calendar_signals, format_signals_for_prompt

    calendar_signals = detect_calendar_signals(now_jst.date())
    if calendar_signals:
        market_context_text = market_context_text + "\n\n" + format_signals_for_prompt(calendar_signals)
        logger.info(
            "Calendar signals: %d active (%s)", len(calendar_signals), ", ".join(s.kind for s in calendar_signals)
        )
    logger.info("Market context ready")

    # Fetch market news
    logger.info("Fetching market news...")
    market_news = fetch_market_news(max_items=10)
    market_news_text = format_market_news(market_news)
    logger.info("Fetched %d market news items", len(market_news))

    # Fetch and compute indicators for holdings
    holdings_summaries: list[dict] = []
    if config.holdings:
        tickers = [h.ticker for h in config.holdings]
        holdings_data, holdings_failed, holdings_fundamentals = fetch_batch(
            tickers, period=f"{config.settings.history_days}d", fetch_fundamentals=True
        )
        data_quality["success"] += len(holdings_data)
        data_quality["failed"] += len(holdings_failed)

        # Fetch news for holdings
        holdings_news = fetch_stock_news(tickers, max_per_stock=3)
        holdings_news_formatted = format_stock_news(holdings_news)

        for holding in config.holdings:
            df = holdings_data.get(holding.ticker)
            if df is not None:
                summary = compute_indicators(
                    df=df,
                    ticker=holding.ticker,
                    name=holding.name,
                    shares=holding.shares,
                    avg_cost=holding.avg_cost,
                    fundamentals=holdings_fundamentals.get(holding.ticker),
                )
                # Attach news headlines
                news_text = holdings_news_formatted.get(holding.ticker)
                if news_text:
                    summary["recent_news"] = news_text
                holdings_summaries.append(summary)
            else:
                logger.warning("No data for holding: %s", holding.ticker)

    # Screen stocks (Nikkei 225 + JPX400)
    logger.info("Starting stock screening (Nikkei 225 + JPX400)")
    screened_candidates, screened_total, screened_failed, all_fundamentals, all_ticker_info = screen_stocks(
        config.settings, screening_weights=screening_weights
    )
    data_quality["success"] += screened_total
    data_quality["failed"] += screened_failed

    # Compute sector rankings
    logger.info("Computing sector rankings...")
    sector_rankings = compute_sector_rankings(all_fundamentals, all_ticker_info)
    for candidate in screened_candidates:
        ticker = candidate["ticker"]
        ranking = sector_rankings.get(ticker)
        if ranking:
            candidate["sector_ranking"] = format_sector_ranking(ranking)
            candidate["sector_score"] = ranking.get("sector_score", 0)

    # Fetch news and margin data for top candidates
    candidate_tickers = [c["ticker"] for c in screened_candidates[:20]]
    candidate_news = fetch_stock_news(candidate_tickers, max_per_stock=3)
    candidate_news_formatted = format_stock_news(candidate_news)

    logger.info("Fetching margin (信用残) data for top candidates...")
    all_margin_tickers = candidate_tickers[:]
    if config.holdings:
        all_margin_tickers += [h.ticker for h in config.holdings]
    margin_data = fetch_margin_data(list(set(all_margin_tickers)))
    logger.info("Fetched margin data for %d stocks", len(margin_data))

    for candidate in screened_candidates:
        news_text = candidate_news_formatted.get(candidate["ticker"])
        if news_text:
            candidate["recent_news"] = news_text
        margin = margin_data.get(candidate["ticker"])
        if margin:
            candidate["margin_ratio"] = margin.get("margin_ratio")
            candidate["margin_signal"] = margin.get("signal", "")
            candidate["margin_trend"] = margin.get("margin_trend", "")

    # Also attach margin data to holdings
    for s in holdings_summaries:
        margin = margin_data.get(s["ticker"])
        if margin:
            s["margin_ratio"] = margin.get("margin_ratio")
            s["margin_signal"] = margin.get("signal", "")
            s["margin_trend"] = margin.get("margin_trend", "")

    # Margin-based signal tags. margin_data is fetched only for the
    # top-20 screened candidates + holdings, so this runs *after*
    # screening rather than feeding into the screening score itself.
    # The tags still flow through to predictions_history via
    # signal_components, which lets the weekly signal-efficacy report
    # surface "did margin_low_pressure / margin_overhang correlate
    # with wins?" without us having to wire margin into the pre-screen.
    from stock_analyzer.signal_tags import (
        annotate_analyst_drift,
        annotate_earnings_momentum,
        annotate_earnings_surprise,
        annotate_forward_estimates,
        annotate_liquidity,
        annotate_margin_signals,
    )

    for s in holdings_summaries + screened_candidates:
        annotate_margin_signals(s)
        annotate_liquidity(s)

    # Earnings momentum (quarterly YoY) + surprise (PEAD) + analyst
    # consensus drift. All three are one extra HTTP per ticker so we
    # only run on top-20 screened + all holdings. YoY answers "are
    # fundamentals improving?", surprise answers "is the company
    # beating expectations?", analyst drift answers "is the sell-side
    # net-upgrading or downgrading the name?" — three distinct
    # leading indicators that round out the screening signal set.
    from stock_analyzer.data_fetcher import (
        fetch_analyst_drift_batch,
        fetch_earnings_momentum_batch,
        fetch_earnings_surprise_batch,
        fetch_forward_estimate_batch,
    )

    em_targets = [c["ticker"] for c in screened_candidates[:20]] + [h.ticker for h in (config.holdings or [])]
    em_target_set = list({t for t in em_targets if t})
    em_data = fetch_earnings_momentum_batch(em_target_set)
    surprise_data = fetch_earnings_surprise_batch(em_target_set)
    drift_data = fetch_analyst_drift_batch(em_target_set)
    forward_data = fetch_forward_estimate_batch(em_target_set)
    for s in holdings_summaries + screened_candidates:
        em = em_data.get(s["ticker"])
        if em:
            if em.get("revenue_yoy_pct") is not None:
                s["revenue_yoy_pct"] = em["revenue_yoy_pct"]
            if em.get("net_income_yoy_pct") is not None:
                s["net_income_yoy_pct"] = em["net_income_yoy_pct"]
            if em.get("latest_quarter"):
                s["latest_quarter"] = em["latest_quarter"]
        sp = surprise_data.get(s["ticker"])
        if sp:
            if sp.get("latest_surprise_pct") is not None:
                s["latest_surprise_pct"] = sp["latest_surprise_pct"]
            if sp.get("consecutive_beats") is not None:
                s["consecutive_beats"] = sp["consecutive_beats"]
            if sp.get("consecutive_misses") is not None:
                s["consecutive_misses"] = sp["consecutive_misses"]
        ad = drift_data.get(s["ticker"])
        if ad:
            if ad.get("drift_pp") is not None:
                s["analyst_drift_pp"] = ad["drift_pp"]
            if ad.get("bullish_pct_current") is not None:
                s["analyst_bullish_pct"] = ad["bullish_pct_current"]
        fe = forward_data.get(s["ticker"])
        if fe:
            for k in ("current_q_growth_pct", "next_q_growth_pct", "current_y_growth_pct", "next_y_growth_pct"):
                if fe.get(k) is not None:
                    s[k] = fe[k]
        annotate_earnings_momentum(s)
        annotate_earnings_surprise(s)
        annotate_analyst_drift(s)
        annotate_forward_estimates(s)

    # Per-ticker earnings-imminence: calendar_context covers the season
    # window (true for thousands of stocks for 5 weeks); this narrows
    # it down to the specific tickers reporting within the next 3
    # trading days, where gap risk is concrete and entry should be
    # avoided. Annotates each summary in place so the inline prompt
    # row can mark the date with a ⚠, and produces a top-of-prompt
    # block listing every imminent ticker so the AI cannot miss them.
    from stock_analyzer.earnings_calendar import (
        collect_imminent,
        format_warnings_for_prompt,
    )

    imminent = collect_imminent(holdings_summaries + screened_candidates, now_jst.date())
    earnings_warnings_text = format_warnings_for_prompt(imminent)
    if imminent:
        market_context_text = market_context_text + "\n\n" + earnings_warnings_text
        logger.info(
            "Earnings imminent: %d ticker(s) within 3 trading days (%s)",
            len(imminent),
            ", ".join(f"{w.ticker}@{w.trading_days_until}d" for w in imminent),
        )

    # Urgent disclosures: scan classified per-stock news for TDnet-style
    # urgent categories (TOB / 業績修正 / 自己株取得 / 大量保有 / M&A /
    # 増資 etc.) and surface them as a top-of-prompt warning. The per-
    # ticker prompt rows already render the classified headlines inline,
    # but a consolidated block makes sure the AI considers them
    # explicitly before drafting picks even when scanning a long list.
    from stock_analyzer.news_classifier import classify_news_list, extract_urgent

    urgent_lines: list[str] = []
    all_news_pool: list[dict] = []
    for ticker, items in holdings_news.items() if config.holdings else []:  # type: ignore[possibly-undefined]
        classify_news_list(items)
        for it in extract_urgent(items):
            it["__ticker"] = ticker
            all_news_pool.append(it)
    for ticker, items in candidate_news.items():
        classify_news_list(items)
        for it in extract_urgent(items):
            it["__ticker"] = ticker
            all_news_pool.append(it)
    if all_news_pool:
        urgent_lines.append("=== 緊急開示 (TDnet 相当) ===")
        for n in all_news_pool[:15]:  # cap at 15 to avoid prompt bloat
            direction = n.get("direction_hint") or ""
            dir_tag = f" ({direction})" if direction else ""
            urgent_lines.append(f"🔴 [{n.get('category')}{dir_tag}] {n.get('__ticker')} — {n.get('title', '')}")
        urgent_lines.append(
            "上記の緊急開示は当該銘柄の株価に直接影響します。entry / 信頼度 / direction の判断時に必ず反映してください"
        )
        market_context_text = market_context_text + "\n\n" + "\n".join(urgent_lines)
        logger.info("Urgent disclosures: %d items injected into prompt", len(all_news_pool))

    # Review past predictions against current prices
    holdings_data_dict = {}
    if config.holdings:
        for h in config.holdings:
            df = holdings_data.get(h.ticker)  # type: ignore[possibly-undefined]
            if df is not None:
                holdings_data_dict[h.ticker] = df

    # screen_stocks already fetched all screening data; extract current prices
    # from the candidates we have (they include current_price)
    current_prices: dict[str, float] = {}
    for c in screened_candidates:
        if c.get("current_price") is not None:
            current_prices[c["ticker"]] = c["current_price"]
    for s in holdings_summaries:
        if s.get("current_price") is not None:
            current_prices[s["ticker"]] = s["current_price"]

    perf_history = review_predictions(perf_history, current_prices, date_str)
    performance_feedback = format_performance_feedback(perf_history)
    save_history(perf_history)
    logger.info("Performance review complete")

    # Inject the previous run's portfolio-risk violations into the
    # feedback block so the AI gets a chance to fix the pattern (e.g.
    # "you put 3 banks in last run — don't again"). Same mechanism as
    # `performance_feedback`, just for portfolio-level rules.
    findings_path = _DATA_DIR / "portfolio_findings.json"
    if findings_path.exists():
        try:
            from stock_analyzer.portfolio_risk import RiskFinding, format_findings_for_prompt

            with open(findings_path, encoding="utf-8") as f:
                prev = json.load(f)
            # JSON round-trips tuple → list; re-tupleise so the dataclass
            # invariant holds when we feed it back through the formatter.
            prev_findings: list[RiskFinding] = []
            for d in prev.get("findings", []):
                if not isinstance(d, dict):
                    continue
                prev_findings.append(
                    RiskFinding(
                        severity=str(d.get("severity", "warning")),
                        kind=str(d.get("kind", "")),
                        message=str(d.get("message", "")),
                        affected_tickers=tuple(d.get("affected_tickers") or []),
                    )
                )
            risk_feedback = format_findings_for_prompt(prev_findings)
            if risk_feedback:
                performance_feedback = (performance_feedback + "\n\n" + risk_feedback).strip()
                logger.info("Injected %d portfolio-risk findings from previous run into prompt", len(prev_findings))
        except Exception:
            logger.exception("Failed to inject prior portfolio-risk findings")

    # Save prompts for Claude Code Action
    prepare_prompts(
        holdings_summaries=holdings_summaries,
        candidates=screened_candidates,
        timing=timing,
        top_n=config.settings.discovery_top_n,
        market_context=market_context_text,
        market_news=market_news_text,
        performance_feedback=performance_feedback,
        strategy_notes=strategy_notes_text,
    )

    # Save current prices for Phase 3 (prediction tracking)
    meta_dir = _DATA_DIR
    meta_dir.mkdir(exist_ok=True)
    with open(meta_dir / "current_prices.json", "w", encoding="utf-8") as f:
        json.dump(current_prices, f)

    # Save data quality and timing info for Phase 3
    with open(meta_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump({"timing": timing, "data_quality": data_quality}, f)

    # Save portfolio-risk auxiliary data so Phase 3 can run sector +
    # correlation checks against Claude's recommendations without
    # re-fetching anything. Keep it compact: sector per ticker, plus
    # last 60 daily closes (~3 months) per ticker — enough for a
    # pairwise daily-return correlation that flags near-duplicates.
    portfolio_aux: dict = {"ticker_info": {}, "close_history": {}}
    for ticker, info in all_ticker_info.items():
        sector = info.get("sector")
        if sector:
            portfolio_aux["ticker_info"][ticker] = {"sector": sector}
    all_dfs: dict[str, object] = {}
    if config.holdings:
        all_dfs.update(holdings_data)  # type: ignore[possibly-undefined]
    # Also grab DataFrames from screening — screen_stocks didn't return
    # them directly, but the screened_candidates list carries per-ticker
    # closes via current_price only. Re-build from holdings_data alone
    # is fine: a real close history is the typical screening data path,
    # and a candidate without history can still be checked for sector.
    for ticker, df in all_dfs.items():
        try:
            close = df["Close"]  # type: ignore[index]
            tail = close.tail(60)
            if len(tail) >= 20:
                portfolio_aux["close_history"][ticker] = [round(float(v), 2) for v in tail.tolist()]
        except Exception:
            # best-effort: a missing close history for one ticker just
            # excludes it from the correlation check, never aborts.
            continue
    with open(meta_dir / "portfolio_aux.json", "w", encoding="utf-8") as f:
        json.dump(portfolio_aux, f, ensure_ascii=False)

    # Save signal components per ticker so Phase 3 can attach them to
    # each saved prediction. Ties the screening signals that fired at
    # entry to the eventual win/loss outcome — the data input that
    # ``compute_signal_efficacy`` consumes for per-signal win-rate
    # reporting in the weekly review prompt.
    signal_components_by_ticker: dict[str, dict[str, bool]] = {}
    for c in screened_candidates:
        comps = c.get("signal_components")
        if comps:
            signal_components_by_ticker[c["ticker"]] = dict(comps)
    with open(meta_dir / "signal_components.json", "w", encoding="utf-8") as f:
        json.dump(signal_components_by_ticker, f, ensure_ascii=False)

    # Universe staleness check — runs at most once per day (weekly
    # would be fine, but the daily cost is negligible). Failures are
    # silent: a missing live source just means "no opinion", we don't
    # want a fetch outage on Wikipedia to spam Slack.
    try:
        from stock_analyzer.universe_refresh import diff_against_static

        universe_diff = diff_against_static()
        with open(meta_dir / "universe_diff.json", "w", encoding="utf-8") as f:
            json.dump(
                {
                    "added": list(universe_diff.added),
                    "removed": list(universe_diff.removed),
                    "static_count": universe_diff.static_count,
                    "live_count": universe_diff.live_count,
                    "source": universe_diff.source,
                    "is_stale": universe_diff.is_stale,
                },
                f,
                ensure_ascii=False,
            )
        if universe_diff.is_stale:
            logger.warning(
                "Universe staleness detected: static=%d live=%d (+%d/-%d)",
                universe_diff.static_count,
                universe_diff.live_count,
                len(universe_diff.added),
                len(universe_diff.removed),
            )
    except Exception:
        logger.exception("Universe staleness check failed; continuing without it")

    logger.info("Phase 1 complete. Prompt file ready for Claude Code Action.")


def phase_critique() -> None:
    """Phase 2.5: Build a critic prompt that re-evaluates the first-pass picks.

    Reads the holdings + discovery results that Claude Code Action wrote
    in Phase 2 and emits ``data/critic_prompt.txt`` for a second AI
    invocation. Fail-soft on every leg: a missing or malformed input
    just leaves no prompt file, which makes the downstream
    ``phase-apply-critique`` step a no-op and the original picks flow
    unchanged to Slack.
    """
    import json

    from stock_analyzer.ai_analyzer import load_analysis_results
    from stock_analyzer.critic import build_critic_prompt

    holdings_result, discovery_result = load_analysis_results()

    # Reuse the same performance_feedback the first-pass AI saw so the
    # critic shares one frame of reference for "is this fingerprint
    # similar to past winners?" — embedded in analysis_input.json so we
    # don't reload the history file separately.
    performance_block = ""
    input_path = _DATA_DIR / "analysis_input.json"
    if input_path.exists():
        try:
            with open(input_path, encoding="utf-8") as f:
                ai = json.load(f)
            # Strip out the per-ticker data sections; the critic only
            # needs the performance / few-shot block from each prompt.
            # Both prompts share the same performance_feedback text, so
            # one extraction suffices.
            holdings_prompt = ai.get("holdings_prompt", "")
            marker = "=== あなたの過去の予測パフォーマンス ==="
            if marker in holdings_prompt:
                start = holdings_prompt.index(marker)
                end = holdings_prompt.find("=== 保有銘柄 ===", start)
                if end < 0:
                    end = len(holdings_prompt)
                performance_block = holdings_prompt[start:end].strip()
        except Exception:
            logger.warning("Failed to extract performance_block for critic", exc_info=True)

    prompt_text = build_critic_prompt(holdings_result, discovery_result, performance_block)
    out_path = _DATA_DIR / "critic_prompt.txt"
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(prompt_text)
    logger.info("Critic prompt saved to %s", out_path)


def phase_apply_critique() -> None:
    """Phase 2.7: Apply critic verdicts to holdings_result / discovery_result.

    Reads ``critique_result.json`` produced by the critic AI step,
    rewrites the two result files in place with downgraded / rejected
    picks adjusted, and stashes a Slack-ready summary string for
    ``phase-notify`` to surface alongside the analysis.

    No-op safe: missing critique file, malformed JSON, empty critiques
    array, or unknown verdicts all leave the original results
    untouched. The summary is also persisted so phase-notify can pick
    it up without re-running the critique logic.
    """
    import json

    from stock_analyzer.ai_analyzer import load_analysis_results
    from stock_analyzer.critic import apply_critique, format_summary_for_slack, load_critique_result

    holdings_result, discovery_result = load_analysis_results()
    critique_result = load_critique_result(_DATA_DIR / "critique_result.json")

    holdings_result, discovery_result, summary = apply_critique(holdings_result, discovery_result, critique_result)

    # Persist updated results so phase-notify reads the critique-adjusted
    # picks. Original first-pass output is backed up alongside for
    # post-mortem comparison.
    holdings_path = _DATA_DIR / "holdings_result.json"
    discovery_path = _DATA_DIR / "discovery_result.json"
    if holdings_path.exists():
        backup = _DATA_DIR / "holdings_result.pre_critique.json"
        backup.write_text(holdings_path.read_text(encoding="utf-8"), encoding="utf-8")
    if discovery_path.exists():
        backup = _DATA_DIR / "discovery_result.pre_critique.json"
        backup.write_text(discovery_path.read_text(encoding="utf-8"), encoding="utf-8")

    with open(holdings_path, "w", encoding="utf-8") as f:
        json.dump(holdings_result, f, ensure_ascii=False, indent=2)
    with open(discovery_path, "w", encoding="utf-8") as f:
        json.dump(discovery_result, f, ensure_ascii=False, indent=2)

    summary_text = format_summary_for_slack(summary)
    with open(_DATA_DIR / "critic_summary.json", "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "text": summary_text}, f, ensure_ascii=False)
    if summary_text:
        logger.info("Critic verdicts applied: %s", summary_text)
    else:
        logger.info("Critic produced no actionable verdicts (no-op)")


def phase_notify() -> None:
    """Phase 3: Read Claude's analysis results, save predictions, send to Slack."""
    import json

    from stock_analyzer.ai_analyzer import load_analysis_results
    from stock_analyzer.performance_tracker import load_history, save_history, save_new_predictions
    from stock_analyzer.slack_notifier import send_analysis_to_slack

    logger.info("Phase 3 (Notify): Sending results to Slack")

    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_channel = os.environ.get("SLACK_CHANNEL_STOCK")
    if not slack_token:
        logger.error("SLACK_BOT_TOKEN not set")
        sys.exit(1)
    if not slack_channel:
        logger.error("SLACK_CHANNEL_STOCK not set")
        sys.exit(1)

    # Load metadata
    meta_path = _DATA_DIR / "meta.json"
    if meta_path.exists():
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    else:
        meta = {"timing": "morning", "data_quality": None}

    # Load Claude's analysis results
    holdings_result, discovery_result = load_analysis_results()

    # Save new predictions for tracking
    perf_history = load_history()
    # Load current prices from the analysis input (saved during prepare phase)
    current_prices: dict[str, float] = {}

    # Extract current prices from meta or re-derive from results
    # The simplest approach: load from the previously saved candidates data
    prices_path = _DATA_DIR / "current_prices.json"
    if prices_path.exists():
        try:
            with open(prices_path, encoding="utf-8") as f:
                current_prices = json.load(f)
        except Exception:
            pass

    # Load signal components saved in phase_prepare so each new
    # prediction can record which screening signals fired at entry.
    signal_components: dict[str, dict[str, bool]] = {}
    signal_path = _DATA_DIR / "signal_components.json"
    if signal_path.exists():
        try:
            with open(signal_path, encoding="utf-8") as f:
                signal_components = json.load(f)
        except Exception:
            logger.warning("Failed to load signal_components.json", exc_info=True)

    if current_prices:
        perf_history = save_new_predictions(
            perf_history,
            holdings_result,
            discovery_result,
            current_prices,
            signal_components=signal_components,
        )
        save_history(perf_history)
        logger.info("New predictions saved to tracking history")
    else:
        logger.warning("No current prices available; skipping prediction tracking")

    # Portfolio-level risk check: sector concentration / total count /
    # near-duplicate correlation. Findings flow two directions:
    #  - Slack notification (immediate operator visibility, inline)
    #  - portfolio_findings.json on disk so the next ``phase_prepare``
    #    can inject them back into Claude's analysis prompt and the
    #    AI gets a chance to fix the pattern next run.
    portfolio_findings_text = ""
    aux_path = _DATA_DIR / "portfolio_aux.json"
    if aux_path.exists():
        try:
            from stock_analyzer.portfolio_risk import check_all, format_findings_for_slack

            with open(aux_path, encoding="utf-8") as f:
                portfolio_aux = json.load(f)
            ticker_info = portfolio_aux.get("ticker_info") or {}
            close_history = portfolio_aux.get("close_history") or {}
            # Re-wrap close_history values as objects with a ``Close``
            # attribute so portfolio_risk's DataFrame-style access path
            # works without pulling pandas into Phase 3. The shim is
            # minimal: only ``df["Close"].tail(N).iloc[i]`` is used.
            price_data: dict[str, object] = {t: _ClosesShim(closes) for t, closes in close_history.items()}
            recommendations: list[dict] = []
            for h in holdings_result.get("holdings_analysis", []) or []:
                if h.get("prediction") in ("UP", "DOWN"):
                    recommendations.append(h)
            for key in ("short_term_picks", "long_term_picks", "recommended_stocks"):
                for r in discovery_result.get(key, []) or []:
                    if r.get("prediction") in ("UP", "DOWN"):
                        recommendations.append(r)
            findings = check_all(recommendations, ticker_info=ticker_info, price_data=price_data)
            portfolio_findings_text = format_findings_for_slack(findings)
            # Persist a compact dict so phase_prepare's next run can
            # inject the violations back into Claude's prompt without
            # importing portfolio_risk just to deserialise.
            findings_payload = [
                {
                    "severity": f.severity,
                    "kind": f.kind,
                    "message": f.message,
                    "affected_tickers": list(f.affected_tickers),
                }
                for f in findings
            ]
            with open(_DATA_DIR / "portfolio_findings.json", "w", encoding="utf-8") as f:
                json.dump({"findings": findings_payload}, f, ensure_ascii=False)
            if findings:
                logger.warning("Portfolio risk findings: %d (see Slack)", len(findings))
        except Exception:
            logger.exception("Portfolio risk check failed; continuing without it")

    # Universe staleness — surface in Slack only when actually stale,
    # so a clean run stays uncluttered. Reads the universe_diff.json
    # cached by Phase 1.
    universe_text = ""
    universe_diff_path = _DATA_DIR / "universe_diff.json"
    if universe_diff_path.exists():
        try:
            from stock_analyzer.universe_refresh import UniverseDiff, format_diff_for_slack

            with open(universe_diff_path, encoding="utf-8") as f:
                ud = json.load(f)
            universe_text = format_diff_for_slack(
                UniverseDiff(
                    added=tuple(ud.get("added") or []),
                    removed=tuple(ud.get("removed") or []),
                    static_count=int(ud.get("static_count", 0)),
                    live_count=int(ud.get("live_count", 0)),
                    source=str(ud.get("source", "")),
                )
            )
        except Exception:
            logger.exception("Universe staleness rendering failed; continuing without it")
    # Merge into the portfolio findings block so Slack gets one block
    # for "operational warnings". Empty text adds nothing.
    if universe_text:
        if portfolio_findings_text:
            portfolio_findings_text = (portfolio_findings_text + "\n\n" + universe_text).strip()
        else:
            portfolio_findings_text = universe_text

    # Critic summary — only present when apply-critique ran in this cron.
    # Surfaced in the same operational-warnings block so the operator
    # can see at a glance whether picks were downgraded or rejected.
    critic_summary_path = _DATA_DIR / "critic_summary.json"
    if critic_summary_path.exists():
        try:
            with open(critic_summary_path, encoding="utf-8") as f:
                cs = json.load(f)
            critic_text = (cs.get("text") or "").strip()
            if critic_text:
                if portfolio_findings_text:
                    portfolio_findings_text = (portfolio_findings_text + "\n\n" + critic_text).strip()
                else:
                    portfolio_findings_text = critic_text
        except Exception:
            logger.exception("Failed to load critic summary; continuing without it")

    # Send to Slack
    success = send_analysis_to_slack(
        bot_token=slack_token,
        channel=slack_channel,
        holdings_analysis=holdings_result,
        discovery_results=discovery_result,
        timing=meta["timing"],
        data_quality=meta.get("data_quality"),
        portfolio_risk_text=portfolio_findings_text or None,
    )

    if success:
        logger.info("Results sent to Slack successfully")
    else:
        logger.error("Failed to send results to Slack")
        print("=== Holdings Analysis ===")
        print(holdings_result)
        print("=== Discovery Results ===")
        print(discovery_result)
        sys.exit(1)


def phase_review() -> None:
    """Build the weekly review prompt for Claude to analyze past performance."""

    from stock_analyzer.performance_tracker import load_history
    from stock_analyzer.strategy_learner import build_weekly_review_prompt, load_strategy_notes

    logger.info("Building weekly review prompt")

    perf_history = load_history()
    strategy_notes = load_strategy_notes()

    prompt = build_weekly_review_prompt(perf_history, strategy_notes)

    out = _DATA_DIR
    out.mkdir(exist_ok=True)
    with open(out / "review_prompt.txt", "w", encoding="utf-8") as f:
        f.write(prompt)

    logger.info("Review prompt saved to data/review_prompt.txt")


def phase_apply_review() -> None:
    """Apply Claude's weekly review results to strategy notes and weights."""
    import json

    from stock_analyzer.strategy_learner import (
        apply_review_results,
        load_screening_weights,
        load_strategy_notes,
        save_screening_weights,
        save_strategy_notes,
    )

    logger.info("Applying weekly review results")

    review_path = _DATA_DIR / "review_result.json"
    if not review_path.exists():
        logger.error("Review result not found: %s", review_path)
        sys.exit(1)

    with open(review_path, encoding="utf-8") as f:
        content = f.read().strip()

    # Parse JSON (handle markdown code blocks)
    try:
        review_result = json.loads(content)
    except json.JSONDecodeError:
        if "```json" in content:
            start = content.index("```json") + 7
            end = content.index("```", start)
            review_result = json.loads(content[start:end].strip())
        elif "```" in content:
            start = content.index("```") + 3
            end = content.index("```", start)
            review_result = json.loads(content[start:end].strip())
        else:
            logger.error("Could not parse review result JSON")
            sys.exit(1)

    strategy_notes = load_strategy_notes()
    screening_weights = load_screening_weights()

    strategy_notes, screening_weights = apply_review_results(review_result, strategy_notes, screening_weights)

    save_strategy_notes(strategy_notes)
    save_screening_weights(screening_weights)

    logger.info(
        "Applied review: %d strategy notes, screening weights updated",
        len(strategy_notes.get("notes", [])),
    )


def phase_notify_save_failure() -> None:
    """Slack-notify when prediction-tracking data fails to commit/push."""
    from stock_analyzer.slack_notifier import send_save_failure_to_slack

    token = os.environ.get("SLACK_BOT_TOKEN")
    channel = os.environ.get("SLACK_CHANNEL_STOCK")
    if not token or not channel:
        logger.error("SLACK_BOT_TOKEN / SLACK_CHANNEL_STOCK not set")
        sys.exit(1)
    if not send_save_failure_to_slack(token, channel):
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="JP Stock Analyzer")
    parser.add_argument(
        "phase",
        choices=[
            "prepare",
            "critique",
            "apply-critique",
            "notify",
            "review",
            "apply-review",
            "notify-save-failure",
        ],
        help=(
            "Phase to run: 'prepare' (fetch data & build prompts), "
            "'critique' (build critic prompt from first-pass results), "
            "'apply-critique' (apply critic verdicts to results), "
            "'notify' (send results to Slack), "
            "'review' (build weekly review prompt), "
            "'apply-review' (apply review results), "
            "'notify-save-failure' (notify Slack about a tracking-data push failure)"
        ),
    )
    args = parser.parse_args()

    if args.phase == "prepare":
        phase_prepare()
    elif args.phase == "critique":
        phase_critique()
    elif args.phase == "apply-critique":
        phase_apply_critique()
    elif args.phase == "notify":
        phase_notify()
    elif args.phase == "review":
        phase_review()
    elif args.phase == "apply-review":
        phase_apply_review()
    elif args.phase == "notify-save-failure":
        phase_notify_save_failure()


if __name__ == "__main__":
    import contextlib

    from stock_analyzer.slack_notifier import send_error_to_slack

    try:
        main()
    except Exception as e:
        logger.exception("Fatal error in stock analysis")
        token = os.environ.get("SLACK_BOT_TOKEN")
        channel = os.environ.get("SLACK_CHANNEL_STOCK")
        if token and channel:
            with contextlib.suppress(Exception):
                send_error_to_slack(token, channel, str(e))
        sys.exit(1)
