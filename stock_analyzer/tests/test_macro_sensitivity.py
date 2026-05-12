from __future__ import annotations

from stock_analyzer.macro_sensitivity import (
    derive_sector_signals,
    format_macro_context_for_prompt,
    per_ticker_tags,
)


def test_derive_sector_signals_usdjpy_up_flags_exporters_tailwind() -> None:
    """USDJPY +2% → exporters (輸送用機器 / 電気機器) tailwind,
    importers (小売業 / 食料品) headwind. The 1% threshold is
    cleared so the signal fires."""
    signals = derive_sector_signals({"usdjpy": 2.0})
    assert any("usdjpy" in label for label in signals)
    label = next(iter(signals.keys()))
    assert "輸送用機器" in signals[label]["tailwind"]
    assert "小売業" in signals[label]["headwind"]


def test_derive_sector_signals_usdjpy_down_inverts() -> None:
    signals = derive_sector_signals({"usdjpy": -2.0})
    label = next(iter(signals.keys()))
    assert "輸送用機器" in signals[label]["headwind"]
    assert "小売業" in signals[label]["tailwind"]


def test_derive_sector_signals_yield_up_helps_banks_hurts_reits() -> None:
    signals = derive_sector_signals({"yield": 1.5})
    label = next(iter(signals.keys()))
    assert "銀行業" in signals[label]["tailwind"]
    assert "不動産業" in signals[label]["headwind"]


def test_derive_sector_signals_below_threshold_silent() -> None:
    """Sub-1% moves are noise — no signal fires."""
    signals = derive_sector_signals({"usdjpy": 0.5, "yield": -0.3, "oil": 0.8})
    assert signals == {}


def test_derive_sector_signals_combines_multiple_factors() -> None:
    """A run with USDJPY up + yield up generates two separate
    signal entries. Each labelled with its own factor + delta."""
    signals = derive_sector_signals({"usdjpy": 2.0, "yield": 1.5, "oil": 0.2})
    # 2 signals: usdjpy + yield (oil below threshold)
    assert len(signals) == 2
    factors = [label.split()[0] for label in signals]
    assert "usdjpy" in factors
    assert "yield" in factors


def test_per_ticker_tags_assigns_tailwind_to_matching_sector() -> None:
    """A Toyota-sector ticker in a USDJPY-up environment carries the
    tailwind label. An importer ticker in the same environment
    carries the headwind label. Sectors with no match → empty list."""
    ticker_info = {
        "TOYOTA.T": {"sector": "輸送用機器"},
        "AEON.T": {"sector": "小売業"},
        "BANK.T": {"sector": "銀行業"},
    }
    signals = derive_sector_signals({"usdjpy": 2.0})
    tags = per_ticker_tags(ticker_info, signals)
    assert any("tailwind" in t for t in tags["TOYOTA.T"])
    assert any("headwind" in t for t in tags["AEON.T"])
    # Bank in a USDJPY-only signal context — neither tailwind nor headwind
    assert tags["BANK.T"] == []


def test_per_ticker_tags_picks_up_multiple_factors() -> None:
    """A bank in a yield-up + USDJPY-up environment carries the
    yield tailwind tag but not a USDJPY tag (banks aren't in the
    USDJPY sensitivity matrix)."""
    ticker_info = {"BANK.T": {"sector": "銀行業"}}
    signals = derive_sector_signals({"usdjpy": 2.0, "yield": 1.5})
    tags = per_ticker_tags(ticker_info, signals)
    assert len(tags["BANK.T"]) == 1
    assert "yield" in tags["BANK.T"][0]
    assert "tailwind" in tags["BANK.T"][0]


def test_per_ticker_tags_skips_tickers_without_sector() -> None:
    """Sector field missing → empty tag list, no crash."""
    ticker_info = {"X.T": {}}
    signals = derive_sector_signals({"usdjpy": 2.0})
    tags = per_ticker_tags(ticker_info, signals)
    assert tags["X.T"] == []


def test_format_macro_context_renders_deltas_and_sectors() -> None:
    """The rendered block must include both the raw deltas and the
    per-factor sector splits so the AI sees the cause + effect
    chain in one go."""
    deltas = {"usdjpy": 2.0, "yield": -0.4, "oil": 1.5}
    signals = derive_sector_signals(deltas)
    text = format_macro_context_for_prompt(deltas, signals)
    assert "マクロ" in text
    assert "USDJPY" in text
    assert "+2.00%" in text
    assert "輸送用機器" in text  # tailwind for USDJPY up
    assert "小売業" in text  # headwind


def test_format_macro_context_empty_when_no_data() -> None:
    """No deltas / no signals → empty string so the caller's
    truthy-check skip works."""
    assert format_macro_context_for_prompt({}, {}) == ""
