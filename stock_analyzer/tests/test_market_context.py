"""Tests for market_context — regime detection feeds Claude's prompt
with the "what kind of market are we in" classifier. The regime
controls position sizing rules (per investment_rules.json), so a
miscategorisation directly affects recommendation behaviour.
"""

from __future__ import annotations

from stock_analyzer.market_context import detect_market_regime, format_market_context


def _ctx(
    nikkei_1d: float = 0.0,
    nikkei_5d: float = 0.0,
    nikkei_1m: float = 0.0,
    vix: float = 15.0,
    usdjpy_5d: float = 0.0,
) -> dict:
    return {
        "nikkei225": {
            "name": "日経平均",
            "current": 30000.0,
            "change_1d_pct": nikkei_1d,
            "change_5d_pct": nikkei_5d,
            "change_1m_pct": nikkei_1m,
            "trend": "横ばい",
            "sma_5": 30000.0,
            "sma_20": 30000.0,
        },
        "vix": {
            "name": "VIX",
            "current": vix,
            "change_1d_pct": 0.0,
            "change_5d_pct": 0.0,
            "change_1m_pct": 0.0,
            "trend": "横ばい",
            "sma_5": vix,
            "sma_20": vix,
        },
        "usdjpy": {
            "name": "USD/JPY",
            "current": 150.0,
            "change_1d_pct": 0.0,
            "change_5d_pct": usdjpy_5d,
            "change_1m_pct": 0.0,
            "trend": "横ばい",
            "sma_5": 150.0,
            "sma_20": 150.0,
        },
    }


def test_bull_regime_detected_when_uptrend_and_low_vix() -> None:
    regime = detect_market_regime(_ctx(nikkei_5d=2.0, nikkei_1m=5.0, vix=15.0))
    assert regime["regime"] == "強気トレンド"


def test_bear_regime_detected_when_downtrend() -> None:
    regime = detect_market_regime(_ctx(nikkei_5d=-2.0, nikkei_1m=-5.0, vix=20.0))
    assert regime["regime"] == "弱気トレンド"


def test_high_volatility_regime_when_vix_elevated() -> None:
    regime = detect_market_regime(_ctx(nikkei_5d=0.5, nikkei_1m=1.0, vix=30.0))
    assert regime["regime"] == "高ボラティリティ"


def test_high_volatility_regime_on_large_daily_swing() -> None:
    """|N225 1day| > 2% triggers high-vol regime even with subdued VIX."""
    regime = detect_market_regime(_ctx(nikkei_1d=-3.0, nikkei_5d=0.5, vix=18.0))
    assert regime["regime"] == "高ボラティリティ"


def test_range_regime_when_no_meaningful_move() -> None:
    regime = detect_market_regime(_ctx(nikkei_5d=0.2, nikkei_1m=1.0, vix=16.0))
    assert regime["regime"] == "レンジ相場"


def test_recovery_regime_when_recent_bounces_after_monthly_drop() -> None:
    regime = detect_market_regime(_ctx(nikkei_5d=2.0, nikkei_1m=-2.0, vix=18.0))
    assert regime["regime"] == "回復局面"


def test_format_market_context_renders_regime_and_sentiment() -> None:
    ctx = _ctx(nikkei_5d=2.5, nikkei_1m=5.0, vix=14.0, usdjpy_5d=1.5)
    ctx["regime"] = detect_market_regime(ctx)
    rendered = format_market_context(ctx)
    assert "市場環境" in rendered
    assert "日本株は強い上昇基調" in rendered  # nikkei sentiment
    assert "市場は安定" in rendered  # vix < 15
    assert "円安進行中" in rendered  # usdjpy +1.5%
    assert "強気トレンド" in rendered  # regime


def test_format_market_context_returns_fallback_for_empty_context() -> None:
    assert format_market_context({}) == "市場データ取得失敗"


def test_default_regime_when_signals_mixed() -> None:
    """No specific regime applies → fall back to mixed/unknown."""
    regime = detect_market_regime(_ctx(nikkei_5d=1.5, nikkei_1m=2.5, vix=20.0))
    # Doesn't match bull (needs n_1m > 3), doesn't match bear (positive),
    # doesn't match high-vol (VIX 20, n_1d 0), doesn't match range (n_5d > 1),
    # doesn't match recovery (n_1m positive). → 混合/不明
    assert regime["regime"] == "混合/不明"
