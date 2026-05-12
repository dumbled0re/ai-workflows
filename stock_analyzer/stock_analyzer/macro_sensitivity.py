"""Cross-asset macro sensitivity injection for sector-aware analysis.

JP equities have well-documented cross-asset sensitivities that drive
sector returns: USDJPY affects exporters vs importers, 10y JGB yield
affects banks vs REITs, oil prices affect refiners and shippers. The
AI sees per-stock fundamentals and technicals but didn't see the
macro regime context that determines whether the sector tailwind is
on or off this week.

This module computes:
1. **Macro deltas** — recent (5d) percent change in USDJPY / 金利 /
   oil, fetched via yfinance free tier.
2. **Per-sector sensitivity tags** — which sectors are tailwind /
   headwind given the current deltas.
3. **Prompt block** — rendered into market_context so the AI sees
   "USDJPY +2% this week → exporters tailwind / importers headwind"
   alongside the sector ranking.

The sensitivity matrix is curated from JP-equity research convention:
- USDJPY up → exporters (自動車 / 電気機器 / 精密機器) tailwind,
  importers (小売 / サービス / 食料品) headwind
- 10y yield up → banks (銀行業 / 保険業) tailwind,
  REITs (不動産業) headwind, growth (情報・通信) mild headwind
- Oil up → 石油・石炭製品 tailwind, 海運業 / 空運業 headwind,
  電気・ガス業 mild headwind

Fail-soft: a yfinance fetch failure for any macro series just omits
that signal — the AI still sees the rest of the context.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Threshold for declaring a macro move "material" enough to mention.
# JP daily macro moves of <0.5% are noise; >=1% is the trader-
# attention threshold.
_MATERIAL_DELTA_PCT = 1.0

# 5-day window matches the swing-trading horizon — a 5-day move
# tells you what's happening this week, not noise from one session.
_LOOKBACK_DAYS = 5

# Curated JP-equity macro sensitivity matrix. Each entry maps a
# (macro_factor, direction) pair to sectors that benefit / hurt.
# direction is "up" / "down". Sectors are the JP TSE convention
# (matches the values in nikkei225_components / jpx400_components
# sector fields).
_SENSITIVITY: dict[tuple[str, str], dict[str, list[str]]] = {
    ("usdjpy", "up"): {
        "tailwind": ["輸送用機器", "電気機器", "精密機器", "機械"],
        "headwind": ["小売業", "食料品", "サービス業"],
    },
    ("usdjpy", "down"): {
        "tailwind": ["小売業", "食料品", "サービス業"],
        "headwind": ["輸送用機器", "電気機器", "精密機器", "機械"],
    },
    ("yield", "up"): {
        "tailwind": ["銀行業", "保険業"],
        "headwind": ["不動産業", "情報・通信業"],
    },
    ("yield", "down"): {
        "tailwind": ["不動産業", "情報・通信業"],
        "headwind": ["銀行業", "保険業"],
    },
    ("oil", "up"): {
        "tailwind": ["石油・石炭製品", "鉱業"],
        "headwind": ["海運業", "空運業", "電気・ガス業"],
    },
    ("oil", "down"): {
        "tailwind": ["海運業", "空運業", "電気・ガス業"],
        "headwind": ["石油・石炭製品", "鉱業"],
    },
}


def fetch_macro_deltas(lookback_days: int = _LOOKBACK_DAYS) -> dict[str, float]:
    """Fetch recent USDJPY / 10y JGB yield / WTI oil % changes.

    Returns ``{"usdjpy": +1.5, "yield": -0.3, "oil": +3.0}`` where
    each value is the percent change from ``lookback_days`` sessions
    ago to the most recent close. Missing entries (yfinance fetch
    failed) are simply absent from the dict — callers tolerate
    missing keys via .get().
    """
    import yfinance as yf

    result: dict[str, float] = {}
    series_map = {
        "usdjpy": "JPY=X",  # USDJPY exchange rate
        "yield": "^TNX",  # 10-year Treasury Note yield (proxy for global yield trend)
        "oil": "CL=F",  # WTI crude futures
    }
    for label, ticker in series_map.items():
        try:
            df = yf.Ticker(ticker).history(period="1mo", auto_adjust=True)
            if df is None or "Close" not in df.columns or len(df) < lookback_days + 1:
                continue
            closes = df["Close"].dropna()
            if len(closes) < lookback_days + 1:
                continue
            cur = float(closes.iloc[-1])
            old = float(closes.iloc[-(lookback_days + 1)])
            if old <= 0:
                continue
            result[label] = round((cur - old) / old * 100, 2)
        except Exception:
            logger.debug("macro_sensitivity: %s fetch failed", ticker, exc_info=True)
            continue
    return result


def derive_sector_signals(deltas: dict[str, float]) -> dict[str, dict[str, list[str]]]:
    """For each macro factor with a material delta, return the
    sector tailwind / headwind sets from the sensitivity matrix.

    Returns ``{"usdjpy +2.1%": {"tailwind": [...], "headwind":
    [...]}, ...}``. Only factors clearing ``_MATERIAL_DELTA_PCT``
    are included — sub-1% moves are below the noise floor.
    """
    signals: dict[str, dict[str, list[str]]] = {}
    for factor, delta in deltas.items():
        if abs(delta) < _MATERIAL_DELTA_PCT:
            continue
        direction = "up" if delta > 0 else "down"
        entry = _SENSITIVITY.get((factor, direction))
        if entry is None:
            continue
        label = f"{factor} {delta:+.2f}%"
        signals[label] = {
            "tailwind": list(entry.get("tailwind", [])),
            "headwind": list(entry.get("headwind", [])),
        }
    return signals


def per_ticker_tags(
    ticker_info: dict[str, dict],
    signals: dict[str, dict[str, list[str]]],
) -> dict[str, list[str]]:
    """Map ticker → list of "[マクロ要因] tailwind/headwind" labels.

    For each ticker, look up its sector and check the signals dict
    for membership. Returns a per-ticker list of label strings
    (potentially multiple — a bank in a +yield environment with
    a -USDJPY move would carry two tags). Tickers whose sector
    doesn't appear in any signal map get an empty list.
    """
    tags: dict[str, list[str]] = {}
    for ticker, info in ticker_info.items():
        sector = info.get("sector")
        if not sector:
            tags[ticker] = []
            continue
        labels: list[str] = []
        for signal_label, sector_map in signals.items():
            if sector in sector_map.get("tailwind", []):
                labels.append(f"[{signal_label}] tailwind")
            elif sector in sector_map.get("headwind", []):
                labels.append(f"[{signal_label}] headwind")
        tags[ticker] = labels
    return tags


def format_macro_context_for_prompt(
    deltas: dict[str, float],
    signals: dict[str, dict[str, list[str]]],
) -> str:
    """Render macro deltas + sector implications as a market_context
    block. Empty when no material moves."""
    if not signals and not deltas:
        return ""
    lines = ["=== マクロ・クロスアセット動向 (直近 5 営業日) ==="]
    factor_labels = {
        "usdjpy": "USDJPY",
        "yield": "米10年金利",
        "oil": "WTI 原油",
    }
    if deltas:
        delta_parts = []
        for f, d in deltas.items():
            tag = " (material)" if abs(d) >= _MATERIAL_DELTA_PCT else ""
            delta_parts.append(f"{factor_labels.get(f, f)} {d:+.2f}%{tag}")
        lines.append("変動: " + " / ".join(delta_parts))
    if signals:
        lines.append("セクター影響:")
        for label, sector_map in signals.items():
            tailwind = ", ".join(sector_map.get("tailwind", []))
            headwind = ", ".join(sector_map.get("headwind", []))
            lines.append(f"  - {label}: 追い風 [{tailwind}] / 逆風 [{headwind}]")
        lines.append(
            "→ 各銘柄の per-stock 行で [tailwind] / [headwind] タグを参照して entry / 信頼度に反映してください"
        )
    return "\n".join(lines)
