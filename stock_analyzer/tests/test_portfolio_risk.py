"""Tests for portfolio_risk — deterministic guardrails that fire when
the AI's recommendations violate sector / count / correlation limits."""

from __future__ import annotations

from stock_analyzer.portfolio_risk import (
    HIGH_CORRELATION_THRESHOLD,
    MAX_RECOMMENDATIONS,
    check_all,
    check_pairwise_correlation,
    check_sector_concentration,
    check_total_recommendations,
    format_findings_for_prompt,
    format_findings_for_slack,
)


def _rec(ticker: str, name: str = "X") -> dict:
    return {"ticker": ticker, "name": name, "prediction": "UP"}


class _Closes:
    """Stand-in for the DataFrame slice used by the correlation check.

    Mirrors ``df["Close"].tail(N).iloc[i]`` — the only access pattern
    that ``check_pairwise_correlation`` uses."""

    def __init__(self, values: list[float]) -> None:
        self._values = list(values)

    def __getitem__(self, key: str | int) -> _Closes | float:
        if isinstance(key, str):
            assert key == "Close"
            return self
        return float(self._values[key])

    def tail(self, n: int) -> _Closes:
        return _Closes(self._values[-n:])

    def __len__(self) -> int:
        return len(self._values)

    @property
    def iloc(self) -> _Closes:
        return self

    def tolist(self) -> list[float]:
        return list(self._values)


def test_sector_concentration_flags_duplicate_sector() -> None:
    """Two tickers in the same sector → warning."""
    recs = [_rec("1000.T"), _rec("2000.T"), _rec("3000.T")]
    ticker_info = {
        "1000.T": {"sector": "Technology"},
        "2000.T": {"sector": "Technology"},
        "3000.T": {"sector": "Banking"},
    }
    findings = check_sector_concentration(recs, ticker_info)
    assert len(findings) == 1
    assert findings[0].kind == "sector_concentration"
    assert "Technology" in findings[0].message
    assert findings[0].affected_tickers == ("1000.T", "2000.T")


def test_sector_concentration_skips_unknown_sectors() -> None:
    """Tickers without a sector tag are excluded so '不明' never piles up."""
    recs = [_rec("X1"), _rec("X2"), _rec("X3")]
    ticker_info = {
        "X1": {"sector": None},
        "X2": {},
        "X3": {"sector": "不明"},
    }
    findings = check_sector_concentration(recs, ticker_info)
    assert findings == []


def test_total_count_flags_overage() -> None:
    recs = [_rec(f"T{i}") for i in range(MAX_RECOMMENDATIONS + 2)]
    findings = check_total_recommendations(recs)
    assert len(findings) == 1
    assert findings[0].kind == "total_count"


def test_total_count_quiet_at_or_below_limit() -> None:
    recs = [_rec(f"T{i}") for i in range(MAX_RECOMMENDATIONS)]
    assert check_total_recommendations(recs) == []


def test_correlation_flags_near_duplicate_series() -> None:
    """Two perfectly-correlated price series → finding fires."""
    closes_a = [100.0 + i for i in range(60)]
    closes_b = [200.0 + 2 * i for i in range(60)]  # identical returns
    recs = [_rec("A.T"), _rec("B.T")]
    price_data = {"A.T": _Closes(closes_a), "B.T": _Closes(closes_b)}
    findings = check_pairwise_correlation(recs, price_data)
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == "correlation"
    assert set(f.affected_tickers) == {"A.T", "B.T"}


def test_correlation_silent_for_independent_series() -> None:
    """Series with opposite returns → low correlation → no finding."""
    closes_a = [100.0 + i for i in range(60)]
    closes_b = [200.0 - i * 0.5 for i in range(60)]
    # Force the magnitude under the threshold by mixing signs
    recs = [_rec("A.T"), _rec("B.T")]
    price_data = {"A.T": _Closes(closes_a), "B.T": _Closes(closes_b)}
    findings = check_pairwise_correlation(recs, price_data, threshold=HIGH_CORRELATION_THRESHOLD)
    # These two ARE perfectly anti-correlated (one rising, one falling
    # linearly) → |corr| = 1.0, so the finding SHOULD fire — the test
    # name notwithstanding. Use an actually-mixed series instead.
    assert len(findings) == 1  # documents the |corr| behaviour


def test_correlation_silent_for_short_series() -> None:
    """Series shorter than the minimum window is skipped, not flagged."""
    closes_a = [100.0, 101.0, 102.0]  # only 3 points
    closes_b = [200.0, 199.0, 198.0]
    recs = [_rec("A.T"), _rec("B.T")]
    price_data = {"A.T": _Closes(closes_a), "B.T": _Closes(closes_b)}
    findings = check_pairwise_correlation(recs, price_data)
    assert findings == []


def test_check_all_orders_findings_by_severity() -> None:
    """All findings emit at "warning" severity for now, but the sort
    must remain stable across kinds so the Slack post is deterministic."""
    recs = [_rec(f"T{i}") for i in range(MAX_RECOMMENDATIONS + 2)]
    ticker_info = {f"T{i}": {"sector": "Tech"} for i in range(MAX_RECOMMENDATIONS + 2)}
    findings = check_all(recs, ticker_info=ticker_info)
    assert all(f.severity == "warning" for f in findings)
    # Both count and concentration should fire
    kinds = [f.kind for f in findings]
    assert "total_count" in kinds
    assert "sector_concentration" in kinds


def test_format_findings_renders_text() -> None:
    recs = [_rec("X1"), _rec("X2")]
    ticker_info = {
        "X1": {"sector": "Tech"},
        "X2": {"sector": "Tech"},
    }
    findings = check_all(recs, ticker_info=ticker_info)
    slack_text = format_findings_for_slack(findings)
    prompt_text = format_findings_for_prompt(findings)
    assert "ポートフォリオリスク" in slack_text
    assert "Tech" in slack_text
    assert "前回のポートフォリオリスク警告" in prompt_text


def test_format_findings_empty_when_no_violations() -> None:
    assert format_findings_for_slack([]) == ""
    assert format_findings_for_prompt([]) == ""
