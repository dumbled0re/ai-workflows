from __future__ import annotations

import json
from pathlib import Path

from stock_analyzer.critic import (
    _coerce_downgrade,
    apply_critique,
    build_critic_prompt,
    enforce_calibration_gate,
    enforce_discovery_cap,
    enforce_thesis_reuse_guard,
    enforce_walkforward_soft_gate,
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


def test_enforce_discovery_cap_drops_lowest_confidence_first() -> None:
    """7 discovery picks → must be trimmed to 5. Drop order is LOW
    then MEDIUM, then long_term ahead of short_term at the same
    confidence. HIGH picks survive."""
    discovery = {
        "short_term_picks": [
            _pick("S1.T", confidence="HIGH"),
            _pick("S2.T", confidence="MEDIUM"),
            _pick("S3.T", confidence="LOW"),
            _pick("S4.T", confidence="MEDIUM"),
        ],
        "long_term_picks": [
            _pick("L1.T", confidence="HIGH"),
            _pick("L2.T", confidence="MEDIUM"),
            _pick("L3.T", confidence="LOW"),
        ],
    }
    summary: dict = {"kept": [], "downgraded": [], "rejected": []}
    enforce_discovery_cap(discovery, summary, max_total=5)
    remaining = [p["ticker"] for p in discovery["short_term_picks"] + discovery["long_term_picks"]]
    # 5 survivors, both LOW dropped first, then long_term MEDIUM (L2.T)
    assert len(remaining) == 5
    assert "S3.T" not in remaining  # LOW dropped
    assert "L3.T" not in remaining  # LOW dropped
    # HIGH always survives
    assert "S1.T" in remaining
    assert "L1.T" in remaining
    # Dropped tickers logged into summary.rejected for Slack visibility
    assert "S3.T" in summary["rejected"]
    assert "L3.T" in summary["rejected"]


def test_enforce_discovery_cap_no_op_when_under_cap() -> None:
    """3 picks <= cap of 5 → no trimming, no summary mutation."""
    discovery = {
        "short_term_picks": [_pick("A.T"), _pick("B.T")],
        "long_term_picks": [_pick("C.T")],
    }
    summary: dict = {"kept": [], "downgraded": [], "rejected": []}
    enforce_discovery_cap(discovery, summary, max_total=5)
    assert len(discovery["short_term_picks"]) == 2
    assert len(discovery["long_term_picks"]) == 1
    assert summary["rejected"] == []


def test_enforce_discovery_cap_prioritises_critic_prelow() -> None:
    """Picks the critic flagged with confidence_pre_critique=LOW are
    drop-first regardless of current confidence — they're the
    'almost rejected' bucket and should go before plain LOWs."""
    pre_low = _pick("PRE.T", confidence="MEDIUM")
    pre_low["confidence_pre_critique"] = "LOW"
    discovery = {
        "short_term_picks": [
            _pick("HI.T", confidence="HIGH"),
            _pick("MED.T", confidence="MEDIUM"),
            _pick("LOW.T", confidence="LOW"),
            pre_low,
        ],
        "long_term_picks": [],
    }
    summary: dict = {"kept": [], "downgraded": [], "rejected": []}
    enforce_discovery_cap(discovery, summary, max_total=2)
    remaining = [p["ticker"] for p in discovery["short_term_picks"]]
    assert "PRE.T" not in remaining  # critic-pre-LOW dropped first
    assert "LOW.T" not in remaining  # then plain LOW
    assert "HI.T" in remaining
    assert "MED.T" in remaining


def test_sanitize_unicode_strips_lone_surrogates_from_prompt() -> None:
    """A pick whose reasons / risk_factor accidentally carries a lone
    UTF-16 surrogate (real cause of the 2026-05-12 API 400 'no low
    surrogate in string' failure) must not bleed into the rendered
    critic prompt — otherwise the second-pass Claude Code Action
    rejects the request body and the whole improvement loop breaks."""
    from stock_analyzer.ai_analyzer import _sanitize_unicode

    bad = "正常テキスト\ud800lone-high-surrogate"
    cleaned = _sanitize_unicode(bad)
    assert isinstance(cleaned, str)
    assert "\ud800" not in cleaned
    assert "正常テキスト" in cleaned
    assert "lone-high-surrogate" in cleaned

    # Critic prompt construction with a pick carrying a surrogate
    # in its reasons[] must render cleanly.
    holdings = {
        "holdings_analysis": [
            {
                "ticker": "X.T",
                "name": "Bad Co",
                "prediction": "UP",
                "confidence": "MEDIUM",
                "reasons": ["clean reason", "polluted\ud800tail"],
            }
        ]
    }
    prompt = build_critic_prompt(holdings, {}, performance_block="")
    assert "\ud800" not in prompt
    # Verify the surrogate-stripped string still survives in the prompt
    assert "polluted" in prompt
    assert "tail" in prompt


def test_calibration_gate_long_run_high_up_downgrades_to_medium() -> None:
    """Existing per-bucket trigger: HIGH_UP < 50% & n>=10 → HIGH→MEDIUM."""
    discovery = {
        "short_term_picks": [_pick("A.T", prediction="UP", confidence="HIGH")],
        "long_term_picks": [],
    }
    summary: dict[str, list[str]] = {"kept": [], "downgraded": [], "rejected": []}
    perf_stats = {
        "by_confidence_direction": {
            "HIGH_UP": {"total": 13, "wins": 5, "accuracy_pct": 38.5},
            "MEDIUM_UP": {"total": 100, "wins": 60, "accuracy_pct": 60.0},
        }
    }
    _, d2 = enforce_calibration_gate(None, discovery, summary, perf_stats)
    assert d2 is not None
    pick = d2["short_term_picks"][0]
    assert pick["confidence"] == "MEDIUM"
    assert pick["confidence_pre_calibration_gate"] == "HIGH"
    assert any("HIGH→MEDIUM" in d for d in summary["downgraded"])


def test_calibration_gate_long_run_low_up_rejects_pick() -> None:
    """LOW_<dir> < 50% & n>=10 → drop from discovery."""
    discovery = {
        "short_term_picks": [
            _pick("A.T", prediction="UP", confidence="LOW"),
            _pick("B.T", prediction="DOWN", confidence="MEDIUM"),
        ],
        "long_term_picks": [],
    }
    summary: dict[str, list[str]] = {"kept": [], "downgraded": [], "rejected": []}
    perf_stats = {
        "by_confidence_direction": {
            "LOW_UP": {"total": 48, "wins": 20, "accuracy_pct": 41.7},
        }
    }
    _, d2 = enforce_calibration_gate(None, discovery, summary, perf_stats)
    assert d2 is not None
    surviving = [p["ticker"] for p in d2["short_term_picks"]]
    assert "A.T" not in surviving
    assert "B.T" in surviving
    assert any("A.T" in r for r in summary["rejected"])


def test_calibration_gate_recent_drift_downgrades_medium_up_to_low() -> None:
    """The recent-drift widened gate: when drift_indicator says we're
    actively bleeding AND recent_direction_winrate.UP is below 50, a
    MEDIUM_UP pick steps down to LOW even though long-run MEDIUM_UP is
    still above 50."""
    discovery = {
        "short_term_picks": [
            _pick("UP1.T", prediction="UP", confidence="MEDIUM"),
            _pick("DN1.T", prediction="DOWN", confidence="MEDIUM"),
        ],
        "long_term_picks": [],
    }
    summary: dict[str, list[str]] = {"kept": [], "downgraded": [], "rejected": []}
    perf_stats = {
        # Long-run MEDIUM_UP is fine (existing gate would no-op).
        "by_confidence_direction": {
            "MEDIUM_UP": {"total": 244, "wins": 142, "accuracy_pct": 58.2},
            "MEDIUM_DOWN": {"total": 252, "wins": 169, "accuracy_pct": 67.1},
        },
        "drift_indicator": {
            "is_drift": True,
            "recent_expectancy_pct": -3.66,
            "baseline_expectancy_pct": 1.49,
            "p_value": 0.004,
            "recent_n": 14,
            "baseline_n": 632,
            "delta_pp": -5.15,
        },
        # UP just collapsed in the recent window; DOWN is fine.
        "recent_direction_winrate": {
            "recent_n": 14,
            "UP": {"n": 9, "wins": 3, "winrate_pct": 33.3, "mean_dir_return_pct": -4.0},
            "DOWN": {"n": 5, "wins": 3, "winrate_pct": 60.0, "mean_dir_return_pct": 1.5},
        },
    }
    _, d2 = enforce_calibration_gate(None, discovery, summary, perf_stats)
    assert d2 is not None
    by_ticker = {p["ticker"]: p for p in d2["short_term_picks"]}
    # MEDIUM_UP stepped down to LOW with marker preserved.
    assert by_ticker["UP1.T"]["confidence"] == "LOW"
    assert by_ticker["UP1.T"]["confidence_pre_calibration_gate"] == "MEDIUM"
    # MEDIUM_DOWN unchanged — DOWN direction is fine in the recent window.
    assert by_ticker["DN1.T"]["confidence"] == "MEDIUM"
    assert "confidence_pre_calibration_gate" not in by_ticker["DN1.T"]
    assert any("UP1.T" in d and "recent-drift" in d for d in summary["downgraded"])


def test_calibration_gate_recent_drift_does_not_touch_holdings_medium() -> None:
    """Source-aware: the recent-drift MEDIUM→LOW step only applies to
    discovery picks. Holdings MEDIUM is left alone even when the drift
    gate fires for the same direction. Long-run per-bucket downgrades
    (e.g. HIGH→MEDIUM) still apply to holdings — only the drift-only
    widening is source-restricted."""
    holdings = {
        "holdings": [
            {"ticker": "HOLD_UP.T", "prediction": "UP", "confidence": "MEDIUM"},
            {"ticker": "HOLD_HIGH.T", "prediction": "UP", "confidence": "HIGH"},
        ]
    }
    discovery = {
        "short_term_picks": [_pick("DISC_UP.T", prediction="UP", confidence="MEDIUM")],
        "long_term_picks": [],
    }
    summary: dict[str, list[str]] = {"kept": [], "downgraded": [], "rejected": []}
    perf_stats = {
        "by_confidence_direction": {
            # Long-run UP buckets fine — only drift extends the gate.
            "MEDIUM_UP": {"total": 244, "wins": 142, "accuracy_pct": 58.2},
            # HIGH_UP broken at long-run level — so HIGH should still get
            # downgraded across BOTH sources.
            "HIGH_UP": {"total": 13, "wins": 5, "accuracy_pct": 38.5},
        },
        "drift_indicator": {
            "is_drift": True,
            "recent_expectancy_pct": -3.66,
            "p_value": 0.004,
        },
        "recent_direction_winrate": {
            "recent_n": 14,
            "UP": {"n": 9, "wins": 1, "winrate_pct": 11.1, "mean_dir_return_pct": -5.0},
            "DOWN": None,
        },
    }
    h2, d2 = enforce_calibration_gate(holdings, discovery, summary, perf_stats)
    # Holdings MEDIUM_UP → untouched (source-aware drift exemption).
    holdings_picks = h2["holdings"] if h2 else []
    hu_medium = next(p for p in holdings_picks if p["ticker"] == "HOLD_UP.T")
    assert hu_medium["confidence"] == "MEDIUM"
    assert "confidence_pre_calibration_gate" not in hu_medium
    # Holdings HIGH_UP → still downgraded to MEDIUM (long-run gate applies).
    hu_high = next(p for p in holdings_picks if p["ticker"] == "HOLD_HIGH.T")
    assert hu_high["confidence"] == "MEDIUM"
    assert hu_high["confidence_pre_calibration_gate"] == "HIGH"
    # Discovery MEDIUM_UP → still stepped down to LOW by the drift gate.
    disc_picks = d2["short_term_picks"] if d2 else []
    disc = disc_picks[0]
    assert disc["confidence"] == "LOW"
    assert disc["confidence_pre_calibration_gate"] == "MEDIUM"


def test_calibration_gate_recent_drift_inactive_when_recent_expectancy_positive() -> None:
    """Drift signal alone isn't enough — we require recent EV < 0 too so
    a positive-EV drift (improvement) doesn't punish picks."""
    discovery = {
        "short_term_picks": [_pick("UP1.T", prediction="UP", confidence="MEDIUM")],
        "long_term_picks": [],
    }
    summary: dict[str, list[str]] = {"kept": [], "downgraded": [], "rejected": []}
    perf_stats = {
        "by_confidence_direction": {
            "MEDIUM_UP": {"total": 244, "wins": 142, "accuracy_pct": 58.2},
        },
        "drift_indicator": {
            "is_drift": True,
            "recent_expectancy_pct": 2.0,  # positive: improving, not bleeding
            "baseline_expectancy_pct": 0.5,
            "p_value": 0.04,
            "recent_n": 14,
            "baseline_n": 100,
            "delta_pp": 1.5,
        },
        "recent_direction_winrate": {
            "recent_n": 14,
            "UP": {"n": 9, "wins": 3, "winrate_pct": 33.3, "mean_dir_return_pct": -4.0},
            "DOWN": None,
        },
    }
    _, d2 = enforce_calibration_gate(None, discovery, summary, perf_stats)
    assert d2 is not None
    assert d2["short_term_picks"][0]["confidence"] == "MEDIUM"
    assert "confidence_pre_calibration_gate" not in d2["short_term_picks"][0]


def test_calibration_gate_no_op_when_no_performance_stats() -> None:
    discovery = {"short_term_picks": [_pick("A.T")], "long_term_picks": []}
    summary: dict[str, list[str]] = {"kept": [], "downgraded": [], "rejected": []}
    h2, d2 = enforce_calibration_gate(None, discovery, summary, None)
    assert h2 is None
    assert d2 is not None and d2["short_term_picks"][0]["confidence"] == "HIGH"


def test_thesis_reuse_guard_caps_high_to_medium_on_jaccard_overlap() -> None:
    """Same ticker re-picked within 30 days with ~identical signal
    fingerprint → HIGH→MEDIUM with marker fields set."""
    discovery = {
        "short_term_picks": [],
        "long_term_picks": [_pick("5032.T", prediction="UP", confidence="HIGH")],
    }
    summary: dict[str, list[str]] = {"kept": [], "downgraded": [], "rejected": []}
    history = {
        "predictions": [
            {
                "ticker": "5032.T",
                "date": "2026-05-15",
                "status": "win",
                "signal_components": {
                    "rsi_healthy_momentum": True,
                    "sma25_breakout": True,
                    "per_value": True,
                    "roe_profitable": True,
                    "revenue_growth": True,
                    "low_peg_ratio": True,
                    "analyst_target_upside": True,
                },
            }
        ]
    }
    components = {
        "5032.T": {
            "rsi_healthy_momentum": True,
            "sma25_breakout": True,
            "per_value": True,
            "roe_profitable": True,
            "revenue_growth": True,
            "low_peg_ratio": True,
            "analyst_target_upside": True,
        }
    }
    _, d2 = enforce_thesis_reuse_guard(
        None,
        discovery,
        summary,
        history,
        components,
        today_iso="2026-06-05",
    )
    assert d2 is not None
    pick = d2["long_term_picks"][0]
    assert pick["confidence"] == "MEDIUM"
    assert pick["confidence_pre_thesis_guard"] == "HIGH"
    assert pick["thesis_reuse_jaccard"] == 1.0
    assert pick["thesis_reuse_prior_date"] == "2026-05-15"
    assert pick["thesis_reuse_prior_status"] == "win"
    assert any("5032.T" in d and "reuse" in d for d in summary["downgraded"])


def test_thesis_reuse_guard_skips_when_outside_window() -> None:
    """Prior pick older than 30 days → guard does not fire."""
    discovery = {
        "short_term_picks": [],
        "long_term_picks": [_pick("5032.T", prediction="UP", confidence="HIGH")],
    }
    summary: dict[str, list[str]] = {"kept": [], "downgraded": [], "rejected": []}
    history = {
        "predictions": [
            {
                "ticker": "5032.T",
                "date": "2026-04-01",  # > 30 days before today_iso
                "status": "win",
                "signal_components": {"rsi_healthy_momentum": True, "per_value": True},
            }
        ]
    }
    components = {"5032.T": {"rsi_healthy_momentum": True, "per_value": True}}
    _, d2 = enforce_thesis_reuse_guard(
        None,
        discovery,
        summary,
        history,
        components,
        today_iso="2026-06-05",
    )
    assert d2 is not None
    pick = d2["long_term_picks"][0]
    assert pick["confidence"] == "HIGH"
    assert "confidence_pre_thesis_guard" not in pick


def test_thesis_reuse_guard_skips_low_jaccard() -> None:
    """Same ticker but signal fingerprint is mostly different
    (new catalyst signature) → guard does not fire."""
    discovery = {
        "short_term_picks": [],
        "long_term_picks": [_pick("5032.T", prediction="UP", confidence="HIGH")],
    }
    summary: dict[str, list[str]] = {"kept": [], "downgraded": [], "rejected": []}
    history = {
        "predictions": [
            {
                "ticker": "5032.T",
                "date": "2026-05-20",
                "status": "win",
                "signal_components": {"rsi_healthy_momentum": True, "per_value": True},
            }
        ]
    }
    # Mostly disjoint new fingerprint — only 1 of 5 overlaps.
    components = {
        "5032.T": {
            "earnings_beat": True,
            "volume_breakout": True,
            "forward_estimate_raise": True,
            "sector_rotation": True,
            "rsi_healthy_momentum": True,
        }
    }
    _, d2 = enforce_thesis_reuse_guard(
        None,
        discovery,
        summary,
        history,
        components,
        today_iso="2026-06-05",
    )
    assert d2 is not None
    pick = d2["long_term_picks"][0]
    assert pick["confidence"] == "HIGH"
    assert "confidence_pre_thesis_guard" not in pick


def test_thesis_reuse_guard_only_caps_high() -> None:
    """MEDIUM and LOW are left alone — compound demotion with the
    calibration gate would over-correct."""
    discovery = {
        "short_term_picks": [],
        "long_term_picks": [
            _pick("A.T", prediction="UP", confidence="MEDIUM"),
            _pick("B.T", prediction="UP", confidence="LOW"),
        ],
    }
    summary: dict[str, list[str]] = {"kept": [], "downgraded": [], "rejected": []}
    sigs = {"per_value": True, "revenue_growth": True}
    history = {
        "predictions": [
            {"ticker": "A.T", "date": "2026-05-20", "status": "win", "signal_components": sigs},
            {"ticker": "B.T", "date": "2026-05-20", "status": "loss", "signal_components": sigs},
        ]
    }
    components = {"A.T": dict(sigs), "B.T": dict(sigs)}
    _, d2 = enforce_thesis_reuse_guard(
        None,
        discovery,
        summary,
        history,
        components,
        today_iso="2026-06-05",
    )
    assert d2 is not None
    assert d2["long_term_picks"][0]["confidence"] == "MEDIUM"
    assert d2["long_term_picks"][1]["confidence"] == "LOW"


def test_thesis_reuse_guard_holdings_out_of_scope() -> None:
    """Holdings entries are not inspected — we already own them."""
    holdings = {
        "holdings_analysis": [_pick("A.T", prediction="UP", confidence="HIGH")],
    }
    summary: dict[str, list[str]] = {"kept": [], "downgraded": [], "rejected": []}
    sigs = {"per_value": True, "revenue_growth": True}
    history = {
        "predictions": [
            {"ticker": "A.T", "date": "2026-05-20", "status": "win", "signal_components": sigs},
        ]
    }
    components = {"A.T": dict(sigs)}
    h2, _ = enforce_thesis_reuse_guard(
        holdings,
        {"short_term_picks": [], "long_term_picks": []},
        summary,
        history,
        components,
        today_iso="2026-06-05",
    )
    assert h2 is not None
    assert h2["holdings_analysis"][0]["confidence"] == "HIGH"


def test_thesis_reuse_guard_no_op_without_history() -> None:
    discovery = {"short_term_picks": [], "long_term_picks": [_pick("A.T", confidence="HIGH")]}
    summary: dict[str, list[str]] = {"kept": [], "downgraded": [], "rejected": []}
    _, d2 = enforce_thesis_reuse_guard(None, discovery, summary, None, None)
    assert d2 is not None
    assert d2["long_term_picks"][0]["confidence"] == "HIGH"


def test_walkforward_gate_downgrades_picks_below_top_n_threshold() -> None:
    """A HIGH pick whose deterministic screening score is below the
    Nth-ranked candidate's score gets HIGH→MEDIUM with marker fields."""
    discovery = {
        "short_term_picks": [_pick("BELOW.T", confidence="HIGH")],
        "long_term_picks": [_pick("ABOVE.T", confidence="HIGH"), _pick("MED.T", confidence="MEDIUM")],
    }
    summary: dict[str, list[str]] = {"kept": [], "downgraded": [], "rejected": []}
    weights = {
        "rsi_oversold_recovery": 8,
        "volume_spike": 10,
        "per_value": 2,
        "revenue_growth": 3,
        "pbr_undervalued": 8,
        "dividend_yield": 4,
    }
    components = {
        "TOP1.T": {"volume_spike": True, "pbr_undervalued": True, "dividend_yield": True},  # 22
        "TOP2.T": {"volume_spike": True, "rsi_oversold_recovery": True},  # 18
        "TOP3.T": {"volume_spike": True, "per_value": True, "revenue_growth": True},  # 15
        "TOP4.T": {"pbr_undervalued": True, "rsi_oversold_recovery": True},  # 16
        "TOP5.T": {"pbr_undervalued": True, "per_value": True, "revenue_growth": True},  # 13
        "TOP6.T": {"per_value": True, "revenue_growth": True, "dividend_yield": True},  # 9
        "ABOVE.T": {"volume_spike": True, "pbr_undervalued": True},  # 18 — above 6th
        "BELOW.T": {"per_value": True, "revenue_growth": True},  # 5 — below 6th
        "MED.T": {"per_value": True},  # 2 — also below 6th
    }
    d2 = enforce_walkforward_soft_gate(discovery, summary, components, weights, top_n=6)
    assert d2 is not None
    # ABOVE.T survives untouched (its score 18 ≥ threshold).
    above = next(p for p in d2["long_term_picks"] if p["ticker"] == "ABOVE.T")
    assert above["confidence"] == "HIGH"
    assert "confidence_pre_walkforward_gate" not in above
    # BELOW.T HIGH → MEDIUM (its score 5 < threshold).
    below = next(p for p in d2["short_term_picks"] if p["ticker"] == "BELOW.T")
    assert below["confidence"] == "MEDIUM"
    assert below["confidence_pre_walkforward_gate"] == "HIGH"
    assert below["walkforward_score"] == 5.0
    assert isinstance(below["walkforward_threshold"], float)
    # MED.T MEDIUM → LOW (same below-threshold but starts a notch lower).
    med = next(p for p in d2["long_term_picks"] if p["ticker"] == "MED.T")
    assert med["confidence"] == "LOW"
    assert med["confidence_pre_walkforward_gate"] == "MEDIUM"


def test_walkforward_gate_no_op_when_fewer_than_top_n_candidates() -> None:
    """Without enough screened candidates, the threshold would be
    trivially satisfied and gating becomes meaningless — no-op."""
    discovery = {"short_term_picks": [_pick("A.T", confidence="HIGH")], "long_term_picks": []}
    summary: dict[str, list[str]] = {"kept": [], "downgraded": [], "rejected": []}
    components = {"A.T": {"per_value": True}}  # only 1 candidate, top_n=6
    weights = {"per_value": 2}
    d2 = enforce_walkforward_soft_gate(discovery, summary, components, weights, top_n=6)
    assert d2 is not None
    assert d2["short_term_picks"][0]["confidence"] == "HIGH"


def test_walkforward_gate_no_op_without_weights() -> None:
    discovery = {"short_term_picks": [_pick("A.T", confidence="HIGH")], "long_term_picks": []}
    summary: dict[str, list[str]] = {"kept": [], "downgraded": [], "rejected": []}
    components = {f"T{i}.T": {"per_value": True} for i in range(10)}
    d2 = enforce_walkforward_soft_gate(discovery, summary, components, None, top_n=6)
    assert d2 is not None
    assert d2["short_term_picks"][0]["confidence"] == "HIGH"


def test_coerce_downgrade_handles_unknown_values_safely() -> None:
    # Random / empty / lowercase suggestions all fall back to a single
    # notch down from the current — keeps the function total.
    assert _coerce_downgrade("HIGH", None) == "MEDIUM"
    assert _coerce_downgrade("HIGH", "") == "MEDIUM"
    assert _coerce_downgrade("HIGH", "garbage") == "MEDIUM"
    assert _coerce_downgrade("MEDIUM", "LOW") == "LOW"
    # Unknown current → fallback assumes MEDIUM → LOW.
    assert _coerce_downgrade(None, None) == "LOW"
