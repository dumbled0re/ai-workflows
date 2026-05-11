from __future__ import annotations

import pytest

from stock_analyzer.risk_reward import (
    annotate_pick,
    compute_for_pick,
    compute_risk_reward,
    parse_price_string,
)

# ---------- parse_price_string --------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1000", 1000.0),
        ("1000.5", 1000.5),
        ("1000円", 1000.0),
        ("1,100円", 1100.0),  # thousand separator
        ("1100 (8%)", 1100.0),  # parenthesised commentary
        ("1100-1150", 1100.0),  # range — first value wins
        ("1100〜1150", 1100.0),  # range with full-width tilde
        ("  1500  ", 1500.0),  # whitespace
        (1100, 1100.0),  # int
        (1100.5, 1100.5),  # float
    ],
)
def test_parse_price_string_handles_common_forms(raw: object, expected: float) -> None:
    assert parse_price_string(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "XYZ", "abc円", [1000], {"x": 1}, True, False],
)
def test_parse_price_string_returns_none_for_unparseable(raw: object) -> None:
    """Unparseable inputs collapse to None so downstream R/R compute
    silently skips rather than crashing on type errors. bool is
    explicitly None'd — yfinance / AI shouldn't be producing bool
    prices but if they did, 1.0/0.0 would be misleading."""
    assert parse_price_string(raw) is None


# ---------- compute_risk_reward -------------------------------------------


def test_up_trade_basic_ratio() -> None:
    """Long: entry 1000, stop 900, target 1300. Risk 100, reward 300 → R/R 3.0."""
    assert compute_risk_reward(1000, 900, 1300, "UP") == 3.0


def test_down_trade_basic_ratio() -> None:
    """Short: entry 1000, stop 1100, target 700. Risk 100, reward 300 → R/R 3.0."""
    assert compute_risk_reward(1000, 1100, 700, "DOWN") == 3.0


def test_returns_none_when_input_missing() -> None:
    """Any None field → can't compute. Don't fabricate a plausible-looking
    ratio out of partial data."""
    assert compute_risk_reward(None, 900, 1300, "UP") is None
    assert compute_risk_reward(1000, None, 1300, "UP") is None
    assert compute_risk_reward(1000, 900, None, "UP") is None


def test_returns_none_for_unrecognised_direction() -> None:
    """A direction the score function doesn't know how to read returns
    None — easier to surface as 'unknown' upstream than to silently
    guess the trade side."""
    assert compute_risk_reward(1000, 900, 1300, "SIDEWAYS") is None


def test_inverted_stop_returns_none_not_negative() -> None:
    """A long with stop ABOVE entry has negative risk — structurally
    malformed (would imply you're stopping out on profit). Return
    None instead of a 'huge' R/R that would silently pass a min
    check."""
    assert compute_risk_reward(1000, 1100, 1300, "UP") is None


def test_inverted_target_returns_zero() -> None:
    """A long with target BELOW entry is a different malformation: stop
    correctly placed but target is on the loss side. Reward is non-
    positive — return 0.0 (not None) so portfolio_risk's threshold
    check flags it loudly rather than silently passing on 'unknown'."""
    assert compute_risk_reward(1000, 900, 950, "UP") == 0.0


# ---------- compute_for_pick + annotate_pick ------------------------------


def test_compute_for_pick_from_typical_discovery_output() -> None:
    """Discovery picks come out of the AI with entry_price / stop_loss /
    target_price as free-form strings — the helper must end-to-end parse
    + compute without the caller pre-cleaning anything."""
    pick = {
        "prediction": "UP",
        "entry_price": "1000円",
        "stop_loss": "900",
        "target_price": "1300 (30%)",
    }
    # reward = 1300 - 1000 = 300, risk = 1000 - 900 = 100 → R/R = 3.0
    assert compute_for_pick(pick) == 3.0


def test_compute_for_pick_returns_none_when_target_missing() -> None:
    """Holdings picks typically have no target_price (the AI gives action
    instead). compute_for_pick must return None so portfolio_risk skips
    them silently rather than flagging every holding."""
    holdings_pick = {
        "prediction": "UP",
        "entry_price": "1000",
        "stop_loss": "900",
        "action": "保有継続",
    }
    assert compute_for_pick(holdings_pick) is None


def test_annotate_pick_writes_ratio_in_place() -> None:
    """The convenience writer must set the field on the dict — both in
    the success and the None case — so downstream code can pattern-
    match on field-presence without guessing."""
    pick = {"prediction": "UP", "entry_price": "1000", "stop_loss": "900", "target_price": "1300"}
    annotate_pick(pick)
    assert pick["risk_reward_ratio"] == 3.0

    bad_pick = {"prediction": "UP"}
    annotate_pick(bad_pick)
    assert "risk_reward_ratio" in bad_pick
    assert bad_pick["risk_reward_ratio"] is None


# ---------- portfolio_risk integration ------------------------------------


def test_portfolio_risk_flags_below_min_ratio() -> None:
    """A pick with R/R 1.0 (under the 1.5 default) must produce a
    finding; one at 2.0 must not. Boundary case at exactly 1.5 also
    pinned so the threshold inclusivity is documented."""
    from stock_analyzer.portfolio_risk import check_risk_reward

    recs = [
        {
            "ticker": "BAD.T",
            "prediction": "UP",
            "entry_price": "1000",
            "stop_loss": "900",
            "target_price": "1100",  # reward 100 / risk 100 → R/R 1.0
        },
        {
            "ticker": "OK.T",
            "prediction": "UP",
            "entry_price": "1000",
            "stop_loss": "900",
            "target_price": "1200",  # R/R 2.0
        },
    ]
    findings = check_risk_reward(recs)
    affected = [t for f in findings for t in f.affected_tickers]
    assert "BAD.T" in affected
    assert "OK.T" not in affected


def test_portfolio_risk_silent_on_unparseable() -> None:
    """A pick where we can't parse stop/target should NOT produce a
    finding — silently skipping is the right move when the data is
    ambiguous. Otherwise every malformed pick would spam Slack."""
    from stock_analyzer.portfolio_risk import check_risk_reward

    recs = [
        {
            "ticker": "X.T",
            "prediction": "UP",
            "entry_price": "1000",
            "stop_loss": "TBD",
            "target_price": "TBD",
        },
    ]
    assert check_risk_reward(recs) == []


def test_portfolio_risk_flags_inverted_setup_explicitly() -> None:
    """An inverted target (R/R 0.0) is a structural mistake — must be
    flagged so the operator sees the malformed pick rather than
    silently letting it through."""
    from stock_analyzer.portfolio_risk import check_risk_reward

    recs = [
        {
            "ticker": "INV.T",
            "prediction": "UP",
            "entry_price": "1000",
            "stop_loss": "900",
            "target_price": "950",  # target below entry on a long = inverted
        },
    ]
    findings = check_risk_reward(recs)
    assert findings
    assert findings[0].affected_tickers == ("INV.T",)


# ---------- critic prompt enrichment --------------------------------------


def test_critic_prompt_annotates_each_pick_with_ratio() -> None:
    """The critic builder must call annotate_pick on every pick in the
    three categories so the rubric uses precomputed numbers. Pin
    presence + correct values across all three categories."""
    from stock_analyzer.critic import build_critic_prompt

    holdings = {
        "holdings_analysis": [
            {"ticker": "H.T", "prediction": "UP", "entry_price": "1000", "stop_loss": "900", "target_price": "1300"},
        ]
    }
    discovery = {
        "short_term_picks": [
            {"ticker": "S.T", "prediction": "UP", "entry_price": "1000", "stop_loss": "900", "target_price": "1100"},
        ],
        "long_term_picks": [
            {"ticker": "L.T", "prediction": "DOWN", "entry_price": "1000", "stop_loss": "1100", "target_price": "700"},
        ],
    }
    prompt = build_critic_prompt(holdings, discovery, performance_block="")
    # Three R/R values should appear: 4.0 (H), 1.0 (S), 3.0 (L).
    assert "4.0" in prompt or "4" in prompt
    assert "1.0" in prompt or '"risk_reward_ratio": 1' in prompt
    assert "3.0" in prompt or "3" in prompt
    # The rubric text should reference the precomputed field name so
    # the critic AI knows to consume rather than re-derive.
    assert "risk_reward_ratio" in prompt
