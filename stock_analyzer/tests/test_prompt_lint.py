"""Tests for prompt_lint — checks that the static template lints
catch the failure modes the 2026-06-13 prompt pivot was meant to fix.
"""

from __future__ import annotations

from pathlib import Path

from stock_analyzer.prompt_lint import (
    LintFinding,
    check_direction_balance,
    check_example_diversity,
    check_forbidden_phrases,
    check_no_trade_documented,
    generate_patch_candidate,
    lint_all_templates,
)


def test_direction_balance_flags_all_up_examples() -> None:
    body = """
    {"prediction": "UP", "confidence": "HIGH"}
    {"prediction": "UP", "confidence": "MEDIUM"}
    {"prediction": "UP", "confidence": "LOW"}
    """
    findings = check_direction_balance("X", body)
    assert any("UP 側に偏り過ぎ" in f.message for f in findings)


def test_direction_balance_clean_when_mixed() -> None:
    body = """
    {"prediction": "UP", "confidence": "HIGH"}
    {"prediction": "DOWN", "confidence": "MEDIUM"}
    {"prediction": "NO_TRADE", "confidence": "LOW"}
    """
    assert check_direction_balance("X", body) == []


def test_no_trade_documented_flags_missing_token() -> None:
    body = '{"prediction": "UP"}'
    findings = check_no_trade_documented("X", body)
    assert findings and "NO_TRADE" in findings[0].message


def test_no_trade_documented_clean_when_token_present() -> None:
    body = "NO_TRADE は明確な見送り signal です"
    assert check_no_trade_documented("X", body) == []


def test_forbidden_phrases_detected() -> None:
    body = "原則 UP を出すこと"
    findings = check_forbidden_phrases("X", body)
    assert findings and findings[0].severity == "error"


def test_example_diversity_requires_one_non_up() -> None:
    body = """
    {"prediction": "UP"}
    {"prediction": "UP"}
    {"prediction": "UP"}
    """
    findings = check_example_diversity("X", body)
    assert findings and "diversity" in findings[0].message


def test_example_diversity_passes_when_down_present() -> None:
    body = """
    {"prediction": "UP"}
    {"prediction": "UP"}
    {"prediction": "DOWN"}
    """
    assert check_example_diversity("X", body) == []


def test_lint_all_aggregates_per_template() -> None:
    templates = {
        "BAD": '原則 UP\n{"prediction": "UP"}\n{"prediction": "UP"}\n{"prediction": "UP"}',
        "OK": 'NO_TRADE 含む説明と {"prediction": "DOWN"} の example',
    }
    findings = lint_all_templates(templates)
    bad_findings = [f for f in findings if f.template_name == "BAD"]
    ok_findings = [f for f in findings if f.template_name == "OK"]
    assert bad_findings, "BAD template should produce findings"
    assert not ok_findings, f"OK template should have no findings; got {ok_findings}"


def test_patch_candidate_skipped_when_green_zone_and_no_findings(tmp_path: Path) -> None:
    """Green + clean → no file written, and any stale file is removed."""
    out = tmp_path / "patch.md"
    out.write_text("stale content")
    result = generate_patch_candidate(findings=[], calibration_zone={"zone": "green"}, output_path=out)
    assert result is None
    assert not out.exists()


def test_patch_candidate_written_for_findings(tmp_path: Path) -> None:
    out = tmp_path / "patch.md"
    findings = [
        LintFinding(template_name="DISCOVERY_PROMPT_TEMPLATE", severity="warn", message="UP 側に偏り過ぎ"),
    ]
    result = generate_patch_candidate(findings=findings, calibration_zone={"zone": "red"}, output_path=out)
    assert result is not None and result.exists()
    text = out.read_text()
    assert "DISCOVERY_PROMPT_TEMPLATE" in text
    assert "UP 側に偏り過ぎ" in text
    assert "red" in text
    # Red zone triggers the action guidance section.
    assert "推奨アクション" in text


def test_patch_candidate_written_for_non_green_even_without_findings(tmp_path: Path) -> None:
    out = tmp_path / "patch.md"
    result = generate_patch_candidate(findings=[], calibration_zone={"zone": "yellow"}, output_path=out)
    assert result is not None
    text = out.read_text()
    assert "yellow" in text
