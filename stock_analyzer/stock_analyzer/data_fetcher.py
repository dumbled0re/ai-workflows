from __future__ import annotations

import logging
import random
import time

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_SLEEP_MIN = 0.5
_SLEEP_MAX = 1.5
_MAX_RETRIES = 3
_BACKOFF_BASE_SEC = 5

# Fundamental keys to extract from yfinance .info
_FUNDAMENTAL_KEYS = [
    "trailingPE",
    "forwardPE",
    "priceToBook",
    "returnOnEquity",
    "returnOnAssets",
    "dividendYield",
    "marketCap",
    "profitMargins",
    "revenueGrowth",
    "earningsGrowth",
    "earningsQuarterlyGrowth",
    "debtToEquity",
    "currentRatio",
    "fiftyTwoWeekHigh",
    "fiftyTwoWeekLow",
    "sector",
    "industry",
    # Analyst consensus (free — already part of tk.info, no extra HTTP).
    # targetMeanPrice gives consensus price target; recommendationMean
    # maps Buy=1 / Hold=3 / Sell=5 (lower = more bullish). Both feed
    # screening signals (target_upside / consensus_buy) and the prompt.
    "targetMeanPrice",
    "targetMedianPrice",
    "targetHighPrice",
    "targetLowPrice",
    "numberOfAnalystOpinions",
    "recommendationMean",
    "recommendationKey",
    # Bid-ask spread for liquidity filter — wide-spread stocks are
    # poor swing-trade candidates regardless of technical setup.
    "bid",
    "ask",
    "averageDailyVolume10Day",
]


def fetch_batch(
    tickers: list[str],
    period: str = "3mo",
    fetch_fundamentals: bool = False,
) -> tuple[dict[str, pd.DataFrame], list[str], dict[str, dict]]:
    """Fetch historical OHLCV data for multiple tickers one by one.

    Returns:
        tuple of (successful data dict, list of failed tickers, fundamentals dict)
    """
    results: dict[str, pd.DataFrame] = {}
    failed: list[str] = []
    fundamentals: dict[str, dict] = {}

    for i, ticker in enumerate(tickers):
        if i > 0:
            time.sleep(random.uniform(_SLEEP_MIN, _SLEEP_MAX))

        if (i + 1) % 20 == 0:
            logger.info("Progress: %d/%d tickers fetched", i + 1, len(tickers))

        data, info = _download_ticker(ticker, period, fetch_fundamentals)

        if data is not None and len(data) >= 20:
            results[ticker] = data
            if info:
                fundamentals[ticker] = info
        else:
            if data is not None and not data.empty:
                logger.warning("Insufficient data for %s (%d rows)", ticker, len(data))
            failed.append(ticker)

    logger.info("Data fetch complete: %d succeeded, %d failed", len(results), len(failed))
    return results, failed, fundamentals


def fetch_split_factor(ticker: str, since_date: str) -> float:
    """``since_date`` (YYYY-MM-DD) より後に発生した株式分割 / 併合の累積比率を返す。

    4:1 分割 → 4.0、2 株を 1 株に併合 → 0.5、イベント無し / 取得失敗 → 1.0。
    predictions_history の entry_price は予測時の生値で固定されるため、途中で
    分割が入ると現在価格とのスケールがずれる。review_predictions が大型 move
    の予測に対してこの関数で entry_price を補正する。
    """
    try:
        splits = yf.Ticker(ticker).splits
        if splits is None or len(splits) == 0:
            return 1.0
        factor = 1.0
        for ts, ratio in splits.items():
            try:
                if ts.strftime("%Y-%m-%d") > since_date and float(ratio) > 0:
                    factor *= float(ratio)
            except Exception:
                continue
        return factor
    except Exception:
        logger.warning("Failed to fetch splits for %s — assuming no split", ticker)
        return 1.0


def fetch_latest_close_batch(tickers: list[str]) -> dict[str, float]:
    """当日の universe に含まれない ticker の最新終値をまとめて取得する。

    screening 対象から外れた銘柄の pending 予測は価格が渡らず永久に
    解決されない (= 生存者バイアス)。review の前にこの関数で補完する。
    取得できなかった ticker は結果 dict に含めない。
    """
    prices: dict[str, float] = {}
    for i, ticker in enumerate(tickers):
        if i > 0:
            time.sleep(random.uniform(_SLEEP_MIN, _SLEEP_MAX))
        try:
            df = yf.Ticker(ticker).history(period="5d", auto_adjust=True)
            if df is not None and not df.empty and "Close" in df.columns:
                close = df["Close"].dropna()
                if len(close) > 0:
                    prices[ticker] = float(close.iloc[-1])
        except Exception:
            logger.warning("Failed to fetch latest close for %s", ticker)
    if tickers:
        logger.info("Fetched latest close for %d/%d off-universe tickers", len(prices), len(tickers))
    return prices


def _download_ticker(ticker: str, period: str, fetch_fundamentals: bool) -> tuple[pd.DataFrame | None, dict | None]:
    """Download a single ticker from Yahoo Finance via yfinance."""
    for attempt in range(_MAX_RETRIES):
        try:
            tk = yf.Ticker(ticker)
            df = tk.history(period=period, auto_adjust=True)

            if df is None or df.empty:
                logger.warning("No data returned for %s", ticker)
                return None, None

            # Keep only OHLCV columns
            expected_cols = ["Open", "High", "Low", "Close", "Volume"]
            available = [c for c in expected_cols if c in df.columns]
            if "Close" not in available:
                logger.warning("Missing Close column for %s", ticker)
                return None, None

            df = df[available]
            df = df.dropna(how="all")
            df = df.sort_index()

            info = None
            if fetch_fundamentals and not df.empty:
                info = _extract_fundamentals(tk, ticker)

            if not df.empty:
                return df, info

        except Exception as e:
            logger.warning(
                "Download attempt %d/%d failed for %s: %s",
                attempt + 1,
                _MAX_RETRIES,
                ticker,
                e,
            )

        wait = _BACKOFF_BASE_SEC * (2**attempt) + random.uniform(0, 3)
        time.sleep(wait)

    logger.error("All %d download attempts failed for %s", _MAX_RETRIES, ticker)
    return None, None


def _extract_fundamentals(tk: yf.Ticker, ticker: str) -> dict | None:
    """Extract fundamental data from a yfinance Ticker object."""
    try:
        raw = tk.info
        info: dict = {}
        for key in _FUNDAMENTAL_KEYS:
            val = raw.get(key)
            if val is not None:
                info[key] = val

        # Earnings date from calendar
        try:
            cal = tk.calendar
            if cal and "Earnings Date" in cal:
                dates = cal["Earnings Date"]
                if dates:
                    info["next_earnings_date"] = str(dates[0])
        except Exception:
            pass

        return info if info else None
    except Exception as e:
        logger.debug("Failed to fetch fundamentals for %s: %s", ticker, e)
        return None


def fetch_earnings_momentum(ticker: str) -> dict | None:
    """Fetch quarterly income statement and compute latest-Q YoY growth.

    Returns ``{"revenue_yoy_pct": ..., "net_income_yoy_pct": ...,
    "latest_quarter": "YYYY-MM"}`` when the data is parseable, else None.

    Two HTTP calls per ticker, so this is run only on top-N screened
    candidates + holdings — not the full universe. The signal answers
    "are last quarter's fundamentals improving or deteriorating
    year-over-year?", which is the core "業績進捗" check serious JP-
    equity traders run before adding a position.
    """
    try:
        tk = yf.Ticker(ticker)
        qs = tk.quarterly_income_stmt
        if qs is None or len(qs.columns) < 5:
            return None
        # Columns are quarterly period-end dates, descending. To get
        # YoY we compare column[0] (latest Q) with column[4] (4
        # quarters ago = same Q last year).
        latest_col = qs.columns[0]
        yoy_col = qs.columns[4]
        result: dict[str, object] = {"latest_quarter": latest_col.strftime("%Y-%m")}
        for src_row, dst_key in (
            ("Total Revenue", "revenue_yoy_pct"),
            ("Operating Revenue", "revenue_yoy_pct"),  # fallback alias
            ("Net Income", "net_income_yoy_pct"),
            ("Net Income Common Stockholders", "net_income_yoy_pct"),
        ):
            if dst_key in result:
                continue  # already filled by an earlier alias
            if src_row not in qs.index:
                continue
            row = qs.loc[src_row]
            latest = row.get(latest_col)
            yoy_base = row.get(yoy_col)
            if latest is None or yoy_base is None:
                continue
            try:
                lv = float(latest)
                bv = float(yoy_base)
            except (TypeError, ValueError):
                continue
            if bv == 0 or pd.isna(lv) or pd.isna(bv):
                continue
            result[dst_key] = round((lv - bv) / abs(bv) * 100, 2)
        if "revenue_yoy_pct" not in result and "net_income_yoy_pct" not in result:
            return None
        return result
    except Exception as e:
        logger.debug("earnings_momentum fetch failed for %s: %s", ticker, e)
        return None


def fetch_forward_estimate(ticker: str) -> dict | None:
    """Fetch forward earnings/revenue growth estimates.

    Returns ``{"current_q_growth_pct", "next_q_growth_pct",
    "current_y_growth_pct", "next_y_growth_pct"}`` (each in percent
    points). The forward earnings/revenue growth panel is sell-side's
    consolidated view of "where is this company going" — distinct
    from trailing growth metrics already in tk.info.

    Stocks where analysts are quietly *raising* forward estimates
    (positive growth across 0q / +1q / 0y / +1y) are the ones with
    the strongest forward returns in academic backtests.
    """
    try:
        tk = yf.Ticker(ticker)
        est = tk.earnings_estimate
        if est is None or len(est) == 0 or "growth" not in est.columns:
            return None
        out: dict[str, float] = {}
        period_map = {
            "0q": "current_q_growth_pct",
            "+1q": "next_q_growth_pct",
            "0y": "current_y_growth_pct",
            "+1y": "next_y_growth_pct",
        }
        for period, dst_key in period_map.items():
            if period in est.index:
                g = est.loc[period, "growth"]
                if g is not None and not pd.isna(g):
                    out[dst_key] = round(float(g) * 100, 2)
        return out if out else None
    except Exception as e:
        logger.debug("forward_estimate fetch failed for %s: %s", ticker, e)
        return None


def fetch_forward_estimate_batch(tickers: list[str]) -> dict[str, dict]:
    """Run ``fetch_forward_estimate`` over a list, rate-limited."""
    out: dict[str, dict] = {}
    for i, ticker in enumerate(tickers):
        if i > 0:
            time.sleep(random.uniform(_SLEEP_MIN, _SLEEP_MAX))
        result = fetch_forward_estimate(ticker)
        if result is not None:
            out[ticker] = result
    logger.info("forward_estimate fetched: %d/%d tickers", len(out), len(tickers))
    return out


def fetch_analyst_drift(ticker: str) -> dict | None:
    """Fetch the 4-period analyst-recommendation history and compute drift.

    Returns ``{"bullish_pct_current", "bullish_pct_3m_ago", "drift_pp",
    "total_analysts_current"}`` when parseable, else None.

    Analyst consensus drift (= QoQ change in the share of buy / strong
    buy ratings) is a well-known leading indicator: stocks whose
    sell-side coverage is *improving* tend to outperform, independent
    of the level. The static ``recommendationMean`` from tk.info only
    captures the level; this extra fetch is what catches the
    momentum of opinion.

    yfinance's ``recommendations`` DataFrame has period (0m / -1m /
    -2m / -3m) with strongBuy / buy / hold / sell / strongSell
    counts. We compute the bullish share (strongBuy + buy / total)
    for the latest and the 3m-ago row and take the difference.
    """
    try:
        tk = yf.Ticker(ticker)
        rec = tk.recommendations
        if rec is None or len(rec) == 0 or "period" not in rec.columns:
            return None
        # Index by period for safe lookup; periods present vary.
        by_period = {str(r.period): r for r in rec.itertuples(index=False)}
        cur = by_period.get("0m")
        old = by_period.get("-3m")
        if cur is None or old is None:
            return None

        def bullish_share(row: object) -> float | None:
            try:
                sb = int(getattr(row, "strongBuy", 0))
                b = int(getattr(row, "buy", 0))
                h = int(getattr(row, "hold", 0))
                s = int(getattr(row, "sell", 0))
                ss = int(getattr(row, "strongSell", 0))
            except (TypeError, ValueError):
                return None
            total = sb + b + h + s + ss
            if total == 0:
                return None
            return (sb + b) / total * 100

        cur_share = bullish_share(cur)
        old_share = bullish_share(old)
        if cur_share is None or old_share is None:
            return None
        total_now = sum(int(getattr(cur, f, 0)) for f in ("strongBuy", "buy", "hold", "sell", "strongSell"))
        return {
            "bullish_pct_current": round(cur_share, 1),
            "bullish_pct_3m_ago": round(old_share, 1),
            "drift_pp": round(cur_share - old_share, 1),
            "total_analysts_current": total_now,
        }
    except Exception as e:
        logger.debug("analyst_drift fetch failed for %s: %s", ticker, e)
        return None


def fetch_analyst_drift_batch(tickers: list[str]) -> dict[str, dict]:
    """Run ``fetch_analyst_drift`` over a list, rate-limited."""
    out: dict[str, dict] = {}
    for i, ticker in enumerate(tickers):
        if i > 0:
            time.sleep(random.uniform(_SLEEP_MIN, _SLEEP_MAX))
        result = fetch_analyst_drift(ticker)
        if result is not None:
            out[ticker] = result
    logger.info("analyst_drift fetched: %d/%d tickers", len(out), len(tickers))
    return out


def fetch_earnings_surprise(ticker: str) -> dict | None:
    """Fetch the last 4 quarters of EPS surprise vs analyst estimate.

    Returns ``{"latest_surprise_pct", "consecutive_beats", "consecutive_misses",
    "quarters": [...]}`` when parseable, else None.

    Earnings surprise is one of the most-documented predictive signals
    in academic finance (PEAD — Post-Earnings-Announcement Drift): a
    stock that beats expectations tends to drift up for weeks
    afterwards, and the magnitude of the surprise correlates with the
    drift's magnitude. Single-quarter beats are signal; 3-4 consecutive
    beats are very strong signal (= the company is structurally out-
    executing expectations).

    The data is in yfinance's ``earnings_history`` DataFrame, which is
    one extra HTTP per ticker, so callers should restrict to top-N
    candidates + holdings.
    """
    try:
        tk = yf.Ticker(ticker)
        eh = tk.earnings_history
        if eh is None or len(eh) == 0:
            return None
        if "surprisePercent" not in eh.columns:
            return None
        # earnings_history is indexed by quarter (ascending), most
        # recent at the bottom. Reverse so [0] = latest.
        rows = list(eh.itertuples())[::-1]
        surprises: list[float] = []
        for r in rows[:4]:
            sp = getattr(r, "surprisePercent", None)
            if sp is None or pd.isna(sp):
                continue
            surprises.append(float(sp) * 100)  # yfinance returns ratio, not %
        if not surprises:
            return None
        # Count run of consecutive beats / misses from latest backward.
        latest_sign = 1 if surprises[0] >= 0 else -1
        run = 0
        for s in surprises:
            if (s >= 0) == (latest_sign >= 0):
                run += 1
            else:
                break
        return {
            "latest_surprise_pct": round(surprises[0], 2),
            "consecutive_beats": run if latest_sign > 0 else 0,
            "consecutive_misses": run if latest_sign < 0 else 0,
            "quarters_evaluated": len(surprises),
        }
    except Exception as e:
        logger.debug("earnings_surprise fetch failed for %s: %s", ticker, e)
        return None


def fetch_earnings_surprise_batch(tickers: list[str]) -> dict[str, dict]:
    """Run ``fetch_earnings_surprise`` over a list, rate-limited.

    Same fail-soft pattern as ``fetch_earnings_momentum_batch``: any
    single ticker failure is logged silently and excluded from the
    result.
    """
    out: dict[str, dict] = {}
    for i, ticker in enumerate(tickers):
        if i > 0:
            time.sleep(random.uniform(_SLEEP_MIN, _SLEEP_MAX))
        result = fetch_earnings_surprise(ticker)
        if result is not None:
            out[ticker] = result
    logger.info("earnings_surprise fetched: %d/%d tickers", len(out), len(tickers))
    return out


def fetch_earnings_momentum_batch(tickers: list[str]) -> dict[str, dict]:
    """Run ``fetch_earnings_momentum`` over a list, rate-limited.

    Failure on any single ticker just excludes it from the result —
    upstream callers attach the data when present and the AI prompt /
    screening simply skips the YoY signals for absent tickers.
    """
    out: dict[str, dict] = {}
    for i, ticker in enumerate(tickers):
        if i > 0:
            time.sleep(random.uniform(_SLEEP_MIN, _SLEEP_MAX))
        result = fetch_earnings_momentum(ticker)
        if result is not None:
            out[ticker] = result
    logger.info("earnings_momentum fetched: %d/%d tickers", len(out), len(tickers))
    return out
