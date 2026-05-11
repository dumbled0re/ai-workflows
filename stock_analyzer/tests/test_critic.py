from __future__ import annotations

import json
from pathlib import Path

from stock_analyzer.critic import (
    _coerce_downgrade,
    apply_critique,
    build_critic_prompt,
    format_summary_for_slack,
    load_critique_result,
)


def _pick(ticker: str, prediction: str = "UP", confidence: str = "HIGH", **extra: object) -> dict:
    base = {
        "ticker": ticker,
        "name": ticker,
        "prediction": prediction,
        "confidence": confidence,
        "stop_loss": "1000",
        "target_price": "1200",
    }
    base.update(extra)
    return base


def test_apply_keep_does_nothing() -> None:
    """A 'keep' verdict leaves the pick byte-identical so downstream
    Slack rendering is undisturbed."""
    holdings = {"holdings_analysis": [_pick("A.T", confidence="HIGH")]}
    discovery: dict = {"short_term_picks": [], "long_term_picks": []}
    critique = {"critiques": [{"ticker": "A.T", "source": "holdings", "verdict": "keep"}]}

    h2, _d2, summary = apply_critique(holdings, discovery, critique)
    assert h2["holdings_analysis"][0]["confidence"] == "HIGH"
    assert "A.T" in summary["kept"]
    assert summary["downgraded"] == []
    assert summary["rejected"] == []


def test_apply_downgrade_uses_suggested_when_lower() -> None:
    """The critic's suggested level is honoured when it strictly lowers
    confidence — sycophancy guard."""
    holdings = {"holdings_analysis": [_pick("A.T", confidence="HIGH")]}
    discovery: dict = {"short_term_picks": [], "long_term_picks": []}
    critique = {
        "critiques": [
            {
                "ticker": "A.T",
                "source": "holdings",
                "verdict": "downgrade",
                "downgraded_confidence": "LOW",
                "reason": "earnings_safe=N, momentum_agrees=N",
            }
        ]
    }

    h2, _, summary = apply_critique(holdings, discovery, critique)
    assert h2["holdings_analysis"][0]["confidence"] == "LOW"
    assert h2["holdings_analysis"][0]["confidence_pre_critique"] == "HIGH"
    assert "earnings_safe" in h2["holdings_analysis"][0]["critic_reason"]
    assert "A.T" in summary["downgraded"]


def test_apply_downgrade_never_raises_confidence() -> None:
    """If the critic asks to set HIGH on a MEDIUM pick, fall back to a
    one-notch step instead. The critic must never increase confidence."""
    holdings = {"holdings_analysis": [_pick("A.T", confidence="MEDIUM")]}
    critique = {
        "critiques": [
            {
                "ticker": "A.T",
                "source": "holdings",
                "verdict": "downgrade",
                "downgraded_confidence": "HIGH",  # sycophancy attempt
            }
        ]
    }
    h2, _, _ = apply_critique(holdings, {}, critique)
    assert h2["holdings_analysis"][0]["confidence"] == "LOW"


def test_apply_downgrade_default_step_when_no_suggestion() -> None:
    holdings = {"holdings_analysis": [_pick("A.T", confidence="HIGH")]}
    critique = {"critiques": [{"ticker": "A.T", "source": "holdings", "verdict": "downgrade"}]}
    h2, _, _ = apply_critique(holdings, {}, critique)
    assert h2["holdings_analysis"][0]["confidence"] == "MEDIUM"


def test_apply_reject_removes_discovery_pick() -> None:
    """Reject on a discovery pick removes the entry — it must not reach
    Slack as a 'recommended' stock."""
    discovery = {
        "short_term_picks": [_pick("A.T"), _pick("B.T")],
        "long_term_picks": [],
    }
    critique = {
        "critiques": [
            {"ticker": "A.T", "source": "short_term", "verdict": "reject", "reason": "3 failing"},
        ]
    }
    _, d2, summary = apply_critique({}, discovery, critique)
    assert [p["ticker"] for p in d2["short_term_picks"]] == ["B.T"]
    assert summary["rejected"] == ["A.T"]


def test_apply_reject_keeps_holdings_but_downgrades_to_low() -> None:
    """Holdings: we still own the stock, so reject doesn't remove the
    entry but flags it and pins confidence at LOW so action / sizing
    downstream stays conservative."""
    holdings = {"holdings_analysis": [_pick("A.T", confidence="HIGH")]}
    critique = {
        "critiques": [
            {"ticker": "A.T", "source": "holdings", "verdict": "reject", "reason": "3 failing"},
        ]
    }
    h2, _, summary = apply_critique(holdings, {}, critique)
    pick = h2["holdings_analysis"][0]
    assert pick["confidence"] == "LOW"
    assert pick["critic_rejected"] is True
    assert pick["confidence_pre_critique"] == "HIGH"
    assert summary["rejected"] == ["A.T"]


def test_apply_handles_legacy_recommended_stocks_key() -> None:
    """Old runs may have used ``recommended_stocks`` instead of
    ``short_term_picks``. Apply-critique should still find rejections."""
    discovery = {"recommended_stocks": [_pick("A.T"), _pick("B.T")]}
    critique = {
        "critiques": [{"ticker": "A.T", "source": "short_term", "verdict": "reject"}],
    }
    _, d2, summary = apply_critique({}, discovery, critique)
    assert [p["ticker"] for p in d2["recommended_stocks"]] == ["B.T"]
    assert summary["rejected"] == ["A.T"]


def test_apply_skips_critique_with_unknown_source() -> None:
    holdings = {"holdings_analysis": [_pick("A.T", confidence="HIGH")]}
    critique = {
        "critiques": [
            {"ticker": "A.T", "source": "weird_source", "verdict": "reject"},
        ]
    }
    h2, _, summary = apply_critique(holdings, {}, critique)
    assert h2["holdings_analysis"][0]["confidence"] == "HIGH"
    assert "A.T" in summary["kept"]
    assert summary["rejected"] == []


def test_apply_empty_critique_is_pure_noop() -> None:
    holdings = {"holdings_analysis": [_pick("A.T")]}
    discovery = {"short_term_picks": [_pick("B.T")]}
    h2, d2, summary = apply_critique(holdings, discovery, {"critiques": []})
    # Picks are byte-identical; apply may normalise the discovery dict
    # by ensuring both short_term_picks and long_term_picks exist as
    # lists, which is benign for downstream Slack rendering.
    assert h2["holdings_analysis"] == [_pick("A.T")]
    assert d2["short_term_picks"] == [_pick("B.T")]
    # All picks fall into "kept" because there is no critique entry
    # for them — that's the desired silent-pass behaviour.
    assert set(summary["kept"]) == {"A.T", "B.T"}


def test_apply_ignores_malformed_critique_entries() -> None:
    """Garbage entries (missing ticker, non-dict, unknown verdict) are
    skipped individually — never crash, never overwrite siblings."""
    holdings = {"holdings_analysis": [_pick("A.T", confidence="HIGH")]}
    discovery = {"short_term_picks": [_pick("B.T", confidence="HIGH")]}
    critique = {
        "critiques": [
            "not a dict",  # skip
            {"verdict": "reject"},  # no ticker → skip
            {"ticker": "A.T", "source": "holdings"},  # no verdict → defaults to keep
            {"ticker": "B.T", "source": "short_term", "verdict": "downgrade"},
        ]
    }
    h2, d2, summary = apply_critique(holdings, discovery, critique)
    assert h2["holdings_analysis"][0]["confidence"] == "HIGH"
    assert d2["short_term_picks"][0]["confidence"] == "MEDIUM"
    assert "B.T" in summary["downgraded"]


def test_load_critique_returns_empty_on_missing_file(tmp_path: Path) -> None:
    assert load_critique_result(tmp_path / "nope.json") == {"critiques": []}


def test_load_critique_returns_empty_on_garbage(tmp_path: Path) -> None:
    p = tmp_path / "garbage.json"
    p.write_text("not json at all", encoding="utf-8")
    assert load_critique_result(p) == {"critiques": []}


def test_load_critique_strips_markdown_fence(tmp_path: Path) -> None:
    p = tmp_path / "fenced.json"
    p.write_text('```json\n{"critiques": [{"ticker": "A.T"}]}\n```\n', encoding="utf-8")
    out = load_critique_result(p)
    assert out["critiques"][0]["ticker"] == "A.T"


def test_load_critique_parses_clean_json(tmp_path: Path) -> None:
    p = tmp_path / "clean.json"
    p.write_text(json.dumps({"critiques": [{"ticker": "B.T"}]}), encoding="utf-8")
    out = load_critique_result(p)
    assert out["critiques"][0]["ticker"] == "B.T"


def test_build_critic_prompt_embeds_all_three_pick_categories() -> None:
    """Holdings + short-term + long-term blocks must all reach the
    critic; otherwise picks from a missing category go un-reviewed."""
    holdings = {"holdings_analysis": [_pick("H.T")]}
    discovery = {
        "short_term_picks": [_pick("S.T")],
        "long_term_picks": [_pick("L.T")],
    }
    prompt = build_critic_prompt(holdings, discovery, performance_block="(過去パフォーマンス)")
    assert "H.T" in prompt
    assert "S.T" in prompt
    assert "L.T" in prompt
    # Anchor the rubric labels so the prompt schema isn't accidentally
    # stripped in a future edit.
    for axis in ("signals_match", "sector_ok", "earnings_safe", "momentum_agrees", "risk_reward"):
        assert axis in prompt
    # The "never raise confidence" guardrail must remain in the system-
    # intent text the critic reads — pin a sentinel.
    assert "信頼度" in prompt or "downgrade" in prompt


def test_format_summary_empty_when_no_verdicts() -> None:
    assert format_summary_for_slack({"kept": [], "downgraded": [], "rejected": []}) == ""


def test_format_summary_lists_tickers_per_bucket() -> None:
    summary = {"kept": ["A.T"], "downgraded": ["B.T", "C.T"], "rejected": ["D.T"]}
    out = format_summary_for_slack(summary)
    # All non-empty buckets should appear; tickers should be visible
    # so the operator can grep without re-reading the file.
    assert "keep=1" in out
    assert "B.T" in out and "C.T" in out
    assert "D.T" in out


def test_coerce_downgrade_handles_unknown_values_safely() -> None:
    # Random / empty / lowercase suggestions all fall back to a single
    # notch down from the current — keeps the function total.
    assert _coerce_downgrade("HIGH", None) == "MEDIUM"
    assert _coerce_downgrade("HIGH", "") == "MEDIUM"
    assert _coerce_downgrade("HIGH", "garbage") == "MEDIUM"
    assert _coerce_downgrade("MEDIUM", "LOW") == "LOW"
    # Unknown current → fallback assumes MEDIUM → LOW.
    assert _coerce_downgrade(None, None) == "LOW"
