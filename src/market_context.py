from __future__ import annotations

import logging
import warnings

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

warnings.filterwarnings("ignore", category=FutureWarning)


def fetch_market_context() -> dict:
    """Fetch overall market context including indices, forex, and trends.

    Returns a dict with market data that will be included in Claude's analysis prompt.
    """
    context: dict = {}

    # 1. Nikkei 225
    nikkei = _fetch_index("^N225", "日経平均")
    if nikkei:
        context["nikkei225"] = nikkei

    # 2. USD/JPY
    usdjpy = _fetch_index("JPY=X", "USD/JPY")
    if usdjpy:
        context["usdjpy"] = usdjpy

    # 3. S&P 500 (US market sentiment affects Japan next day)
    sp500 = _fetch_index("^GSPC", "S&P500")
    if sp500:
        context["sp500"] = sp500

    # 4. VIX (fear index)
    vix = _fetch_index("^VIX", "VIX恐怖指数")
    if vix:
        context["vix"] = vix

    return context


def _fetch_index(symbol: str, name: str) -> dict | None:
    """Fetch index data and compute trend metrics."""
    try:
        tk = yf.Ticker(symbol)
        df = tk.history(period="1mo", auto_adjust=True)
        if df is None or df.empty or len(df) < 5:
            logger.warning("Insufficient data for %s", symbol)
            return None

        close = df["Close"].astype(float)
        current = float(close.iloc[-1])

        # Price changes
        change_1d = _pct(close, 1)
        change_5d = _pct(close, 5)
        change_1m = _pct(close, len(close) - 1) if len(close) > 1 else 0.0

        # Simple trend: above/below 5-day and 20-day SMA
        sma_5 = float(close.tail(5).mean())
        sma_20 = float(close.tail(20).mean()) if len(close) >= 20 else float(close.mean())

        if current > sma_5 > sma_20:
            trend = "上昇トレンド"
        elif current < sma_5 < sma_20:
            trend = "下降トレンド"
        elif current > sma_5:
            trend = "短期反発中"
        elif current < sma_5:
            trend = "短期調整中"
        else:
            trend = "横ばい"

        return {
            "name": name,
            "current": round(current, 2),
            "change_1d_pct": round(change_1d, 2),
            "change_5d_pct": round(change_5d, 2),
            "change_1m_pct": round(change_1m, 2),
            "sma_5": round(sma_5, 2),
            "sma_20": round(sma_20, 2),
            "trend": trend,
        }
    except Exception as e:
        logger.warning("Failed to fetch %s (%s): %s", name, symbol, e)
        return None


def format_market_context(context: dict) -> str:
    """Format market context dict into readable text for Claude prompt."""
    if not context:
        return "市場データ取得失敗"

    parts: list[str] = ["=== 市場環境 ==="]

    for key in ["nikkei225", "sp500", "usdjpy", "vix"]:
        data = context.get(key)
        if not data:
            continue
        parts.append(
            f"{data['name']}: {data['current']} "
            f"(1日: {data['change_1d_pct']:+.2f}% | "
            f"5日: {data['change_5d_pct']:+.2f}% | "
            f"1ヶ月: {data['change_1m_pct']:+.2f}%) "
            f"【{data['trend']}】"
        )

    # Market sentiment summary
    nikkei = context.get("nikkei225", {})
    vix = context.get("vix", {})
    usdjpy = context.get("usdjpy", {})

    sentiment_parts = []
    if nikkei:
        if nikkei.get("change_5d_pct", 0) > 2:
            sentiment_parts.append("日本株は強い上昇基調")
        elif nikkei.get("change_5d_pct", 0) < -2:
            sentiment_parts.append("日本株は軟調")
    if vix:
        vix_val = vix.get("current", 20)
        if vix_val > 25:
            sentiment_parts.append(f"VIX={vix_val}で市場不安感が高い")
        elif vix_val < 15:
            sentiment_parts.append(f"VIX={vix_val}で市場は安定")
    if usdjpy:
        if usdjpy.get("change_5d_pct", 0) > 1:
            sentiment_parts.append("円安進行中（輸出企業に追い風）")
        elif usdjpy.get("change_5d_pct", 0) < -1:
            sentiment_parts.append("円高進行中（輸入企業に追い風）")

    if sentiment_parts:
        parts.append(f"\n市場センチメント: {' / '.join(sentiment_parts)}")

    return "\n".join(parts)


def _pct(series: pd.Series, periods: int) -> float:
    if len(series) <= periods:
        return 0.0
    old = float(series.iloc[-periods - 1])
    if old == 0:
        return 0.0
    return ((float(series.iloc[-1]) - old) / old) * 100
