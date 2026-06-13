"""Lightweight lint + patch-candidate generator for the AI prompts.

codex 2026-06-13 P2: automated prompt rewriting carries real risk
(unstable improvement loop, hard-to-attribute regressions) so this
module deliberately stops at *candidate generation*. The cron writes a
human-readable diff suggestion to ``data/prompt_patch_candidate.md``
during red/yellow zones; a maintainer reviews and applies it by hand.

Checks (all return a list of ``(severity, message)``):

- ``check_direction_balance``: counts ``prediction: "UP"`` vs
  ``prediction: "DOWN"`` vs ``prediction: "NO_TRADE"`` occurrences
  in each template body. A 4× imbalance between UP and the others
  (where examples ≥ 4 lines total) flags an example-side selection
  bias.
- ``check_no_trade_documented``: every prompt template that asks
  the model for a directional ``prediction`` should mention
  ``NO_TRADE`` at least once. Without that mention the AI defaults
  back to UP/DOWN-only behaviour.
- ``check_forbidden_phrases``: scans for hard-coded direction
  pushes ("常に UP", "原則 UP", "デフォルト UP", "DOWN は最終手段") —
  none should be in prompts after the 2026-06-13 pivot.
- ``check_example_diversity``: when ≥ 3 example entries exist in
  the prompt, at least one should be non-UP. Pure-UP examples
  guide the AI toward UP outputs even when description text is
  balanced.

``generate_patch_candidate`` runs all checks, picks up the current
``calibration_zone``, and emits the markdown candidate file. When the
zone is green and no findings exist, it skips writing entirely so a
stale file doesn't linger.
"""

from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"
_PATCH_PATH = _DATA_DIR / "prompt_patch_candidate.md"

_FORBIDDEN_PHRASES = (
    "常に UP",
    "常にUP",
    "原則 UP",
    "原則UP",
    "デフォルト UP",
    "デフォルトUP",
    "DOWN は最終手段",
    "DOWNは最終手段",
)

_UP_BIAS_RATIO_THRESHOLD = 4
"""When UP examples outnumber DOWN+NO_TRADE by 4× or more, flag the
template for re-balancing. 4× is conservative — the structural concern
is a complete absence of DOWN/NO_TRADE examples, not exact parity."""


@dataclass(frozen=True)
class LintFinding:
    template_name: str
    severity: str  # "warn" | "error"
    message: str


def _count_prediction_values(body: str) -> tuple[int, int, int]:
    """Returns ``(up, down, no_trade)`` counts of literal
    ``"prediction": "UP"`` etc. occurrences in the template body.

    The check is structural — it looks at example JSON snippets, not
    natural-language descriptions, because the AI mirrors the JSON
    example distribution far more than the prose intent.
    """
    up = len(re.findall(r'"prediction"\s*:\s*"UP"', body))
    down = len(re.findall(r'"prediction"\s*:\s*"DOWN"', body))
    no_trade = len(re.findall(r'"prediction"\s*:\s*"NO_TRADE"', body))
    return up, down, no_trade


def check_direction_balance(template_name: str, body: str) -> list[LintFinding]:
    up, down, no_trade = _count_prediction_values(body)
    other = down + no_trade
    if up + other < 2:
        return []  # Too few examples to judge.
    if up == 0 and other > 0:
        # All-DOWN/NO_TRADE prompt — also a bias, but in the opposite
        # direction. Still worth flagging because the AI mirrors it.
        return [
            LintFinding(
                template_name=template_name,
                severity="warn",
                message=f"UP example が 0 件 (down={down}, no_trade={no_trade}) — "
                "DOWN/NO_TRADE 側に偏り過ぎ。バランス調整推奨",
            )
        ]
    if other == 0 and up > 0:
        return [
            LintFinding(
                template_name=template_name,
                severity="warn",
                message=f"DOWN / NO_TRADE example が 0 件 (up={up}) — "
                "UP 側に偏り過ぎ。少なくとも 1 件は DOWN または NO_TRADE を混ぜる",
            )
        ]
    if up >= _UP_BIAS_RATIO_THRESHOLD * max(1, other):
        return [
            LintFinding(
                template_name=template_name,
                severity="warn",
                message=f"UP example 比率 {up}:{other} (UP : DOWN+NO_TRADE) で "
                f"{_UP_BIAS_RATIO_THRESHOLD}× 超の偏り。AI が UP に引っ張られやすい",
            )
        ]
    return []


def check_no_trade_documented(template_name: str, body: str) -> list[LintFinding]:
    if "NO_TRADE" not in body:
        return [
            LintFinding(
                template_name=template_name,
                severity="warn",
                message="prompt body に NO_TRADE への明示的言及なし — "
                "directional 選択肢を狭めている。説明 or example に追加推奨",
            )
        ]
    return []


def check_forbidden_phrases(template_name: str, body: str) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for phrase in _FORBIDDEN_PHRASES:
        if phrase in body:
            findings.append(
                LintFinding(
                    template_name=template_name,
                    severity="error",
                    message=f"禁止語 '{phrase}' を検出 — 固定方向誘導は 2026-06-13 pivot で除去済のはず",
                )
            )
    return findings


def check_example_diversity(template_name: str, body: str) -> list[LintFinding]:
    """When the template includes ≥ 3 example entries, require at
    least one with a non-UP ``prediction`` value."""
    up, down, no_trade = _count_prediction_values(body)
    if up + down + no_trade < 3:
        return []
    if down + no_trade == 0:
        return [
            LintFinding(
                template_name=template_name,
                severity="warn",
                message=f"3 件以上の example のうち全て UP ({up} 件) — "
                "DOWN / NO_TRADE example を 1 件以上混ぜて diversity を確保",
            )
        ]
    return []


def lint_template(template_name: str, body: str) -> list[LintFinding]:
    """Run every check against a single template body."""
    findings: list[LintFinding] = []
    findings.extend(check_direction_balance(template_name, body))
    findings.extend(check_no_trade_documented(template_name, body))
    findings.extend(check_forbidden_phrases(template_name, body))
    findings.extend(check_example_diversity(template_name, body))
    return findings


def lint_all_templates(templates: dict[str, str]) -> list[LintFinding]:
    """``templates`` maps template name (e.g. "DISCOVERY_PROMPT_TEMPLATE")
    to its raw body string. Returns all findings concatenated."""
    out: list[LintFinding] = []
    for name, body in templates.items():
        out.extend(lint_template(name, body))
    return out


def generate_patch_candidate(
    findings: list[LintFinding],
    calibration_zone: dict | None,
    output_path: Path | None = None,
) -> Path | None:
    """Write a maintainer-facing markdown summary when there's
    something to act on. Returns the written path, or ``None`` when
    nothing was written (green zone + no findings).

    The candidate file is intentionally **not** auto-committed. The
    cron-end commit step bundles ``data/`` so the file rides along
    for visibility, but the actual edit to the template stays with a
    human reviewer.
    """
    zone = (calibration_zone or {}).get("zone", "unknown")
    if not findings and zone == "green":
        # Nothing to suggest right now. Clean up stale file if present
        # so the maintainer's view stays accurate.
        if output_path is None:
            output_path = _PATCH_PATH
        if output_path.exists():
            with contextlib.suppress(OSError):
                output_path.unlink()
        return None

    if output_path is None:
        output_path = _PATCH_PATH
    output_path.parent.mkdir(exist_ok=True)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S JST")
    body_lines = [
        f"# Prompt Patch Candidate ({now})",
        "",
        f"calibration_zone: **{zone}**",
        "",
        "本ファイルは prompt_lint が自動生成。**自動 commit 対象ではない** — ",
        "human reviewer が内容を確認して必要な箇所だけ template に反映してください。",
        "",
    ]

    if findings:
        body_lines.append("## 検出された懸念")
        body_lines.append("")
        # Group by template_name for readability.
        by_template: dict[str, list[LintFinding]] = {}
        for f in findings:
            by_template.setdefault(f.template_name, []).append(f)
        for name, ff in by_template.items():
            body_lines.append(f"### `{name}`")
            body_lines.append("")
            for f in ff:
                tag = "❌" if f.severity == "error" else "⚠"
                body_lines.append(f"- {tag} **{f.severity}**: {f.message}")
            body_lines.append("")
    else:
        body_lines.append("## 検出された懸念")
        body_lines.append("")
        body_lines.append("- (lint findings なし)")
        body_lines.append("")

    if zone in ("red", "yellow"):
        body_lines.append("## 推奨アクション")
        body_lines.append("")
        body_lines.append("- calibration_zone が green に戻るまで、prompt の方向誘導 example を見直してください。")
        body_lines.append(
            "- 直近の drift と direction-winrate に応じて、UP 主体の example を DOWN/NO_TRADE に置換することを検討。"
        )
        body_lines.append("- 変更後は次の cron で AI の prediction 分布が緩むか確認してから commit。")
        body_lines.append("")

    output_path.write_text("\n".join(body_lines), encoding="utf-8")
    logger.info("prompt_lint: patch candidate written (findings=%d, zone=%s) → %s", len(findings), zone, output_path)
    return output_path
