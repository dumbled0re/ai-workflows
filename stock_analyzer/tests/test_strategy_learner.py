"""Tests for strategy_learner — focus on Bayesian shrinkage retune (Phase 3 #46)."""

from __future__ import annotations

from stock_analyzer.strategy_learner import (
    compute_bayesian_weight_proposal,
    format_weight_proposal_for_prompt,
)


def _resolved(status: str, signals: list[str]) -> dict:
    """Build a minimal resolved prediction with given signal_components."""
    return {
        "status": status,
        "signal_components": dict.fromkeys(signals, True),
    }


def test_bayesian_proposal_shrinks_small_sample_to_overall_rate() -> None:
    """small-n signal の shrunk_rate は overall_rate に近づくべき
    (k=20 prior が dominant な領域)。"""
    history = {
        "predictions": (
            # overall 60% win, 100 件
            [_resolved("win", []) for _ in range(60)]
            + [_resolved("loss", []) for _ in range(40)]
            # tiny_signal: 5 件全 win (raw rate 100%、shrunk 後は ~70% 前後)
            + [_resolved("win", ["tiny_signal"]) for _ in range(5)]
            # ↑ without_signal は 100件 (大サンプル)
        )
    }
    proposal = compute_bayesian_weight_proposal(history, current_weights={"tiny_signal": 20})
    tiny = proposal["proposed"]["tiny_signal"]
    # raw is 100% (5/5), but k=20 prior with overall ~62% (105/105 wins + 40 losses) pulls down
    assert tiny["raw_with_rate"] == 1.0
    # shrunk should be between overall (~0.62) and raw (1.0)、近く 0.7-0.75 range
    assert 0.60 < tiny["shrunk_with_rate"] < 0.90
    # scaling should be capped at 1.20 max
    assert tiny["scaling_factor"] <= 1.20


def test_bayesian_proposal_caps_scaling_at_20_percent() -> None:
    """非常に高 win-rate signal でも scaling は 1.20 で cap。"""
    history = {
        "predictions": (
            # overall: 50% win
            [_resolved("win", []) for _ in range(50)]
            + [_resolved("loss", []) for _ in range(50)]
            # killer_signal: 50 件全 win (raw 100%)
            + [_resolved("win", ["killer_signal"]) for _ in range(50)]
        )
    }
    proposal = compute_bayesian_weight_proposal(history, current_weights={"killer_signal": 20})
    killer = proposal["proposed"]["killer_signal"]
    # raw rate 100%, shrunk ~ (20*0.5 + 50)/(20+50) = 60/70 ≒ 0.857
    # scaling raw = 0.857 / 0.50 = 1.714、cap で 1.20
    assert killer["scaling_factor"] == 1.20
    # proposed_weight = current 20 * 1.20 = 24
    assert killer["proposed_weight"] == 24


def test_bayesian_proposal_skips_negative_lift_signals() -> None:
    """with > without でない signal は scaling=1.0 (weight 変えない)。"""
    history = {
        "predictions": (
            [_resolved("win", []) for _ in range(60)]
            + [_resolved("loss", []) for _ in range(40)]
            # bad_signal: 5 件全 loss (= with_rate 0%、without_rate 高い)
            + [_resolved("loss", ["bad_signal"]) for _ in range(5)]
        )
    }
    proposal = compute_bayesian_weight_proposal(history, current_weights={"bad_signal": 15})
    bad = proposal["proposed"]["bad_signal"]
    # shrunk_with < shrunk_without → scaling=1.0 (weight 変更しない)
    # Phase 3 dry-run では weight down は手動判断、auto では中立
    assert bad["scaling_factor"] == 1.0
    assert bad["proposed_weight"] == 15


def test_proposal_returns_empty_when_no_signals_data() -> None:
    """signal_components が空の predictions → proposal も空。"""
    history = {"predictions": [{"status": "win", "signal_components": {}}]}
    proposal = compute_bayesian_weight_proposal(history)
    assert proposal["proposed"] == {}


def test_format_proposal_for_prompt_renders_top_changes() -> None:
    """提案 prompt block は変化幅大きい順に top_n 件表示。"""
    proposal = {
        "overall_win_rate": 0.58,
        "prior_strength_k": 20.0,
        "proposed": {
            "sig_up": {
                "current_weight": 10,
                "raw_with_rate": 0.80,
                "shrunk_with_rate": 0.70,
                "raw_without_rate": 0.55,
                "shrunk_without_rate": 0.57,
                "shrunk_lift_pp": 13.0,
                "scaling_factor": 1.20,
                "proposed_weight": 12,
                "n_with": 20,
                "n_without": 100,
            },
            "sig_flat": {
                "current_weight": 15,
                "raw_with_rate": 0.55,
                "shrunk_with_rate": 0.56,
                "raw_without_rate": 0.58,
                "shrunk_without_rate": 0.58,
                "shrunk_lift_pp": -2.0,
                "scaling_factor": 1.00,
                "proposed_weight": 15,
                "n_with": 12,
                "n_without": 80,
            },
        },
    }
    text = format_weight_proposal_for_prompt(proposal)
    assert "Bayesian weight 提案" in text
    assert "dry-run" in text
    assert "sig_up" in text
    assert "🔺" in text  # weight up marker
    # 反映方法の指示文があること
    assert "strategy_governor" in text
