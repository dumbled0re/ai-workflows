from __future__ import annotations

import logging

import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import MACD, SMAIndicator
from ta.volatility import BollingerBands

logger = logging.getLogger(__name__)


def compute_indicators(
    df: pd.DataFrame, ticker: str, name: str, shares: int = 0, avg_cost: float | None = None
) -> dict:
    """Compute technical indicators and return a summary dict.

    Args:
        df: OHLCV DataFrame with columns: Open, High, Low, Close, Volume
        ticker: Ticker symbol
        name: Company name
        shares: Number of shares held (0 for screened stocks)
        avg_cost: Average purchase cost (optional)

    Returns:
        Summary dict with all computed indicators
    """
    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    volume = df["Volume"].astype(float)

    current_price = float(close.iloc[-1])

    # Moving averages
    sma_5 = _safe_sma(close, 5)
    sma_25 = _safe_sma(close, 25)
    sma_75 = _safe_sma(close, 75)

    # Trend signal
    trend_signal = _determine_trend(sma_5, sma_25, sma_75)

    # RSI
    rsi_14 = _safe_rsi(close, 14)

    # MACD
    macd_value, macd_signal, macd_histogram = _safe_macd(close)

    # Bollinger Bands
    bb_upper, bb_middle, bb_lower, bb_position_pct = _safe_bollinger(close)

    # Volume ratio (current vs 20-day average)
    volume_ratio = _safe_volume_ratio(volume)

    # Price change percentages
    price_change_1d = _pct_change(close, 1)
    price_change_5d = _pct_change(close, 5)
    price_change_1m = _pct_change(close, 21)
    price_change_3m = _pct_change(close, 63)

    # 52-week high/low distance
    high_52w = float(high.tail(252).max()) if len(high) >= 252 else float(high.max())
    low_52w = float(low.tail(252).min()) if len(low) >= 252 else float(low.min())
    distance_from_52w_high = ((current_price - high_52w) / high_52w) * 100
    distance_from_52w_low = ((current_price - low_52w) / low_52w) * 100

    summary: dict = {
        "ticker": ticker,
        "name": name,
        "current_price": round(current_price, 1),
        "shares": shares,
        "price_change_1d": round(price_change_1d, 2),
        "price_change_5d": round(price_change_5d, 2),
        "price_change_1m": round(price_change_1m, 2),
        "price_change_3m": round(price_change_3m, 2),
        "sma_5": _round_or_none(sma_5),
        "sma_25": _round_or_none(sma_25),
        "sma_75": _round_or_none(sma_75),
        "trend_signal": trend_signal,
        "rsi_14": _round_or_none(rsi_14),
        "macd_value": _round_or_none(macd_value),
        "macd_signal": _round_or_none(macd_signal),
        "macd_histogram": _round_or_none(macd_histogram),
        "bb_upper": _round_or_none(bb_upper),
        "bb_middle": _round_or_none(bb_middle),
        "bb_lower": _round_or_none(bb_lower),
        "bb_position_pct": _round_or_none(bb_position_pct),
        "volume_ratio": _round_or_none(volume_ratio),
        "distance_from_52w_high": round(distance_from_52w_high, 2),
        "distance_from_52w_low": round(distance_from_52w_low, 2),
    }

    if avg_cost is not None:
        summary["avg_cost"] = avg_cost
        summary["unrealized_pnl_pct"] = round(
            ((current_price - avg_cost) / avg_cost) * 100, 2
        )

    return summary


def compute_screening_score(df: pd.DataFrame) -> float:
    """Compute a quick screening score (0-100) for stock discovery.

    Uses lightweight indicators for fast evaluation.
    """
    score = 0.0
    close = df["Close"].astype(float)
    volume = df["Volume"].astype(float)

    # RSI check
    rsi = _safe_rsi(close, 14)
    if rsi is not None:
        if 30 <= rsi <= 50:
            score += 20  # Oversold recovery
        elif 50 < rsi <= 65:
            score += 15  # Healthy momentum

    # Volume spike
    vol_ratio = _safe_volume_ratio(volume)
    if vol_ratio is not None and vol_ratio > 1.5:
        score += 20

    # SMA25 breakout in last 3 days
    sma_25 = _safe_sma(close, 25)
    if sma_25 is not None and len(close) >= 4:
        recent_prices = close.iloc[-3:]
        sma_25_series = SMAIndicator(close, window=25).sma_indicator().iloc[-3:]
        breakout = any(
            p > s and close.iloc[-4] <= SMAIndicator(close, window=25).sma_indicator().iloc[-4]
            for p, s in zip(recent_prices, sma_25_series, strict=False)
            if pd.notna(p) and pd.notna(s)
        ) if len(close) > 4 and pd.notna(SMAIndicator(close, window=25).sma_indicator().iloc[-4]) else False
        if breakout:
            score += 20

    # MACD histogram turning positive
    _, _, hist = _safe_macd(close)
    if hist is not None and len(close) >= 2:
        macd_ind = MACD(close)
        hist_series = macd_ind.macd_diff()
        if len(hist_series.dropna()) >= 2:
            prev = hist_series.dropna().iloc[-2]
            curr = hist_series.dropna().iloc[-1]
            if prev < 0 and curr > 0:
                score += 15

    # Near Bollinger Band lower (within 5%)
    _, _, bb_lower, bb_pos = _safe_bollinger(close)
    if bb_pos is not None and bb_pos <= 0.15:
        score += 10

    return score


# --- Private helpers ---


def _safe_sma(close: pd.Series, window: int) -> float | None:
    if len(close) < window:
        return None
    val = SMAIndicator(close, window=window).sma_indicator().iloc[-1]
    return float(val) if pd.notna(val) else None


def _safe_rsi(close: pd.Series, window: int = 14) -> float | None:
    if len(close) < window + 1:
        return None
    val = RSIIndicator(close, window=window).rsi().iloc[-1]
    return float(val) if pd.notna(val) else None


def _safe_macd(
    close: pd.Series,
) -> tuple[float | None, float | None, float | None]:
    if len(close) < 26:
        return None, None, None
    macd = MACD(close)
    v = macd.macd().iloc[-1]
    s = macd.macd_signal().iloc[-1]
    h = macd.macd_diff().iloc[-1]
    return (
        float(v) if pd.notna(v) else None,
        float(s) if pd.notna(s) else None,
        float(h) if pd.notna(h) else None,
    )


def _safe_bollinger(
    close: pd.Series,
) -> tuple[float | None, float | None, float | None, float | None]:
    if len(close) < 20:
        return None, None, None, None
    bb = BollingerBands(close)
    upper = bb.bollinger_hband().iloc[-1]
    middle = bb.bollinger_mavg().iloc[-1]
    lower = bb.bollinger_lband().iloc[-1]

    if pd.isna(upper) or pd.isna(lower) or pd.isna(middle):
        return None, None, None, None

    upper_f, middle_f, lower_f = float(upper), float(middle), float(lower)
    band_width = upper_f - lower_f
    position = (float(close.iloc[-1]) - lower_f) / band_width if band_width > 0 else 0.5

    return upper_f, middle_f, lower_f, position


def _safe_volume_ratio(volume: pd.Series) -> float | None:
    if len(volume) < 20:
        return None
    avg_20 = volume.tail(20).mean()
    if avg_20 == 0:
        return None
    return float(volume.iloc[-1] / avg_20)


def _pct_change(close: pd.Series, periods: int) -> float:
    if len(close) <= periods:
        return 0.0
    old = float(close.iloc[-periods - 1])
    if old == 0:
        return 0.0
    return ((float(close.iloc[-1]) - old) / old) * 100


def _round_or_none(val: float | None, decimals: int = 2) -> float | None:
    return round(val, decimals) if val is not None else None


def _determine_trend(
    sma_5: float | None, sma_25: float | None, sma_75: float | None
) -> str:
    vals = [sma_5, sma_25, sma_75]
    if any(v is None for v in vals):
        return "データ不足"
    if sma_5 > sma_25 > sma_75:  # type: ignore[operator]
        return "SMA5 > SMA25 > SMA75 (強気パーフェクトオーダー)"
    if sma_5 < sma_25 < sma_75:  # type: ignore[operator]
        return "SMA5 < SMA25 < SMA75 (弱気パーフェクトオーダー)"
    if sma_5 > sma_25:  # type: ignore[operator]
        return "SMA5 > SMA25 (短期上昇トレンド)"
    if sma_5 < sma_25:  # type: ignore[operator]
        return "SMA5 < SMA25 (短期下降トレンド)"
    return "横ばい"
