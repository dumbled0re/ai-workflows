from __future__ import annotations

from stock_analyzer.factor_model import (
    FactorExposure,
    _beta,
    _zscores,
    aggregate_portfolio_exposure,
    compute_factor_exposures,
    detect_factor_concentration,
    format_factor_concentration_for_prompt,
)


def test_beta_perfect_correlation_returns_one() -> None:
    """Stock returns identical to market → beta = 1.0."""
    market = [0.01, -0.02, 0.015, -0.005, 0.02] * 5
    stock = market[:]
    b = _beta(stock, market)
    assert b is not None
    assert abs(b - 1.0) < 1e-6


def test_beta_double_amplitude_returns_two() -> None:
    """Stock moves 2x the market → beta = 2.0. Classic high-beta
    profile (tech / consumer discretionary)."""
    market = [0.01, -0.02, 0.015, -0.005, 0.02] * 5
    stock = [r * 2 for r in market]
    b = _beta(stock, market)
    assert b is not None
    assert abs(b - 2.0) < 1e-6


def test_beta_insufficient_data_returns_none() -> None:
    """Below 20 observations on either side → can't estimate
    reliably. The 20-bar floor is a hard requirement before any
    factor exposure is computed."""
    market = [0.01, 0.02] * 5  # only 10 bars
    stock = [0.01, 0.02] * 5
    assert _beta(stock, market) is None


def test_zscores_handles_none_inputs() -> None:
    """Tickers with None raw values stay None in the z-score map.
    The remaining values are standardised against just the non-
    None subset so a missing data point doesn't corrupt others."""
    values = {"A": 10.0, "B": 20.0, "C": None, "D": 30.0, "E": 40.0, "F": 50.0}
    z = _zscores(values)
    # C stays None
    assert z["C"] is None
    # The five present values should have z-mean ≈ 0
    present_z = [z[k] for k in ("A", "B", "D", "E", "F")]
    assert abs(sum(present_z) / 5) < 1e-6


def test_zscores_returns_all_none_when_too_few_values() -> None:
    """Fewer than 5 non-None values is too thin for a meaningful
    z-score distribution — entire result is None."""
    values = {"A": 10.0, "B": 20.0, "C": None, "D": None}
    z = _zscores(values)
    assert all(v is None for v in z.values())


def test_compute_factor_exposures_assigns_small_cap_positive_size_z() -> None:
    """A small-cap stock should have a *positive* size_z (the
    'small minus big' convention). Universe with one small-cap +
    five large-caps: the small-cap z should be most positive."""
    universe = {
        "SMALL.T": {"marketCap": 1e10, "forwardPE": 15.0},
        "BIG1.T": {"marketCap": 1e13, "forwardPE": 15.0},
        "BIG2.T": {"marketCap": 5e12, "forwardPE": 15.0},
        "BIG3.T": {"marketCap": 2e12, "forwardPE": 15.0},
        "BIG4.T": {"marketCap": 8e12, "forwardPE": 15.0},
        "BIG5.T": {"marketCap": 3e12, "forwardPE": 15.0},
    }
    exposures = compute_factor_exposures(universe, market_closes=None, closes_by_ticker={})
    small_z = exposures["SMALL.T"].size_z
    big_z = exposures["BIG1.T"].size_z
    assert small_z is not None and big_z is not None
    assert small_z > big_z  # small-cap has higher (positive) size_z


def test_compute_factor_exposures_assigns_value_z_to_low_pe() -> None:
    """Cheap PE stock should have *positive* value_z. 1/PER weighted
    so low PER → high 1/PER → high z."""
    universe = {
        "CHEAP.T": {"marketCap": 1e12, "forwardPE": 5.0},  # 1/PE = 0.2
        "MID1.T": {"marketCap": 1e12, "forwardPE": 15.0},
        "MID2.T": {"marketCap": 1e12, "forwardPE": 16.0},
        "MID3.T": {"marketCap": 1e12, "forwardPE": 14.0},
        "MID4.T": {"marketCap": 1e12, "forwardPE": 17.0},
        "EXPENSIVE.T": {"marketCap": 1e12, "forwardPE": 50.0},  # 1/PE = 0.02
    }
    exposures = compute_factor_exposures(universe, market_closes=None, closes_by_ticker={})
    assert exposures["CHEAP.T"].value_z is not None
    assert exposures["EXPENSIVE.T"].value_z is not None
    assert exposures["CHEAP.T"].value_z > exposures["EXPENSIVE.T"].value_z


def test_compute_factor_exposures_momentum_from_60d_return() -> None:
    """Momentum factor is the 60-day return. A stock up 30% should
    have higher momentum_z than a flat one."""
    universe = {f"T{i}.T": {"marketCap": 1e12} for i in range(6)}
    closes_by_ticker = {
        "T0.T": [100.0] * 60 + [130.0],  # +30%
        "T1.T": [100.0] * 60 + [101.0],  # +1%
        "T2.T": [100.0] * 60 + [100.0],  # flat
        "T3.T": [100.0] * 60 + [100.0],
        "T4.T": [100.0] * 60 + [99.0],
        "T5.T": [100.0] * 60 + [98.0],  # -2%
    }
    exposures = compute_factor_exposures(universe, market_closes=None, closes_by_ticker=closes_by_ticker)
    high_mom = exposures["T0.T"].momentum_z
    low_mom = exposures["T5.T"].momentum_z
    assert high_mom is not None and low_mom is not None
    assert high_mom > low_mom


def test_aggregate_portfolio_returns_equal_weight_mean() -> None:
    """Three picks with momentum z-scores [1.0, 1.0, 2.0] →
    aggregate momentum_z = 1.33."""
    recs = [{"ticker": "A.T"}, {"ticker": "B.T"}, {"ticker": "C.T"}]
    exposures = {
        "A.T": FactorExposure("A.T", market_beta_z=None, size_z=None, value_z=None, momentum_z=1.0),
        "B.T": FactorExposure("B.T", market_beta_z=None, size_z=None, value_z=None, momentum_z=1.0),
        "C.T": FactorExposure("C.T", market_beta_z=None, size_z=None, value_z=None, momentum_z=2.0),
    }
    agg = aggregate_portfolio_exposure(recs, exposures)
    assert agg["momentum_z"] == 1.33
    assert agg["market_beta_z"] is None  # all None inputs


def test_detect_factor_concentration_flags_high_z() -> None:
    """|z| >= 1.5 on any factor triggers a finding. Three picks all
    momentum-loaded → flagged. Below threshold → silent."""
    flagged = detect_factor_concentration({"momentum_z": 1.8, "size_z": 0.5})
    assert len(flagged) == 1
    assert "モメンタム" in flagged[0]["factor"]

    silent = detect_factor_concentration({"momentum_z": 0.3, "size_z": -0.4})
    assert silent == []


def test_detect_factor_concentration_handles_negative_z() -> None:
    """A -1.6 z is concentrated in the opposite direction — equally
    flagged as +1.6. Both indicate the portfolio is over-exposed
    to one side of the factor."""
    flagged = detect_factor_concentration({"size_z": -1.6})
    assert len(flagged) == 1
    assert flagged[0]["value"] == -1.6


def test_format_factor_concentration_always_shows_profile() -> None:
    """The exposure profile is always rendered (no findings = just
    profile), so the AI sees the system's tilt on every run."""
    text = format_factor_concentration_for_prompt(
        findings=[],
        aggregate={"market_beta_z": 0.2, "size_z": -0.1, "value_z": 0.0, "momentum_z": 0.3},
    )
    assert "Factor 露出" in text
    assert "市場ベータ" in text
    assert "モメンタム" in text
    # No warning section
    assert "⚠" not in text


def test_format_factor_concentration_adds_warning_when_flagged() -> None:
    text = format_factor_concentration_for_prompt(
        findings=[{"factor": "モメンタム", "value": 1.8, "severity": "warning"}],
        aggregate={"market_beta_z": 0.5, "size_z": 0.3, "value_z": 0.1, "momentum_z": 1.8},
    )
    assert "Factor 集中警告" in text
    assert "モメンタム" in text
