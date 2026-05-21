"""Pending-verify runner.

Reads ``verify/**/<id>.yml`` schemas referenced from GitHub issues
labeled ``pending-verify``, executes the verification when its
``verify_after`` has passed, and reports outcomes back to the issue
(comment + close on success, comment + retry on failure, Slack alert
on hard failure).

Design rationale (codex consult 2026-05-15):
- Issues hold *state* (open / comments / labels). Repo YAML holds
  the *execution plan* (declarative, reviewable, schema-validated).
- No ``bash -c`` from issue body — verify YAML can only invoke a
  small registered set of ``kind``s, each of which has a vetted
  Python implementation. New verifications can ship a new YAML
  without touching the runner.
- Distinguishes ``inconclusive`` (transient: network / regex / run
  still in flight) from ``failure`` (regression). Only failure
  triggers Slack noise.
- Per-issue ``max_attempts`` + ``retry_after_hours`` keep brittle
  verifications from spamming the channel.

Schema (single verify file, one kind per file):

    verify_id: point_sites/2026-05-16-pointtown-cookie
    verify_after: 2026-05-16T22:00:00+09:00
    project: point_sites
    description: 自由記述、issue にも転記される
    kind: workflow_run_grep      # or workflow_run_no_grep / manual
    args:
      workflow: pointtown.yml
      pattern: "login verified"
      timeout_seconds: 300        # optional, default 300
    max_attempts: 3               # optional, default 3
    retry_after_hours: 24         # optional, default 24
    relates_to: fa0666f           # optional, free-form
    success_message: "..."        # optional, shown on issue close

Issue body must contain a YAML front matter with at minimum
``verify_id`` matching the repo YAML's ``verify_id``:

    ---
    verify_id: point_sites/2026-05-16-pointtown-cookie
    ---
    pointtown cookie filter (commit fa0666f) の 16h 寿命確認。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

logger = logging.getLogger("pending_verify")

REPO_ROOT = Path(__file__).resolve().parent.parent
VERIFY_DIR = REPO_ROOT / "verify"
LABEL = "pending-verify"
ATTEMPT_LABEL_PREFIX = "verify-attempt:"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_AFTER_HOURS = 24
DEFAULT_TIMEOUT_SECONDS = 300
SLACK_POST_URL = "https://slack.com/api/chat.postMessage"

# ---------- Result types ----------------------------------------------------


@dataclass(frozen=True)
class VerifyResult:
    status: str  # "success" | "failure" | "inconclusive" | "not_due"
    detail: str

    @property
    def is_success(self) -> bool:
        return self.status == "success"

    @property
    def is_failure(self) -> bool:
        return self.status == "failure"

    @property
    def is_inconclusive(self) -> bool:
        return self.status == "inconclusive"


# ---------- gh CLI helpers --------------------------------------------------


def gh(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a ``gh`` subcommand and return the completed process.

    ``GH_TOKEN`` is expected in the environment (provided by GitHub
    Actions). Returns stdout as ``str`` when ``capture=True``.
    """
    cmd = ["gh", *args]
    logger.debug("$ %s", " ".join(cmd))
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def list_pending_issues() -> list[dict]:
    """List open issues with the pending-verify label."""
    proc = gh(
        "issue",
        "list",
        "--label",
        LABEL,
        "--state",
        "open",
        "--json",
        "number,title,body,labels,createdAt",
        "--limit",
        "100",
    )
    return json.loads(proc.stdout)


def parse_issue_front_matter(body: str) -> dict | None:
    """Extract the front-matter YAML block from an issue body.

    Front matter is the first ``---`` ... ``---`` block. Returns the
    parsed YAML dict or ``None`` if the block is absent / malformed.
    """
    match = re.match(r"^---\s*\n(.+?)\n---\s*\n", body, re.DOTALL)
    if match is None:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def issue_attempt_count(labels: list[dict]) -> int:
    """Count of past verify attempts encoded in issue labels.

    We store the attempt counter as a label ``verify-attempt:N`` so it
    survives across runs without needing a separate state file. Returns
    0 if no such label exists yet.
    """
    for lbl in labels:
        name = lbl.get("name", "")
        if name.startswith(ATTEMPT_LABEL_PREFIX):
            try:
                return int(name[len(ATTEMPT_LABEL_PREFIX) :])
            except ValueError:
                continue
    return 0


def bump_attempt_label(issue_number: int, current: int) -> None:
    """Replace ``verify-attempt:N`` with ``verify-attempt:N+1``."""
    if current > 0:
        gh(
            "issue",
            "edit",
            str(issue_number),
            "--remove-label",
            f"{ATTEMPT_LABEL_PREFIX}{current}",
            check=False,
        )
    new_label = f"{ATTEMPT_LABEL_PREFIX}{current + 1}"
    # Create the label if absent (idempotent).
    gh(
        "label",
        "create",
        new_label,
        "--description",
        f"verify run attempt {current + 1}",
        "--color",
        "ededed",
        check=False,
    )
    gh("issue", "edit", str(issue_number), "--add-label", new_label, check=False)


def comment_on_issue(issue_number: int, body: str) -> None:
    gh("issue", "comment", str(issue_number), "--body", body, check=False)


def close_issue(issue_number: int, reason: str = "completed") -> None:
    gh("issue", "close", str(issue_number), "--reason", reason, check=False)


# ---------- Verify "kind" implementations -----------------------------------


def _wait_for_run_completion(workflow_basename: str, before_ts: str, timeout_seconds: int) -> dict | None:
    """Wait for a new run of ``workflow_basename`` (created after ``before_ts``) to complete.

    Returns the run dict or ``None`` on timeout. ``before_ts`` should
    be an ISO 8601 timestamp captured *before* triggering the
    workflow so we don't pick up an unrelated earlier run.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        proc = gh(
            "run",
            "list",
            "--workflow",
            workflow_basename,
            "--limit",
            "1",
            "--json",
            "databaseId,status,conclusion,createdAt",
        )
        runs = json.loads(proc.stdout)
        if runs:
            run = runs[0]
            if run.get("createdAt", "") > before_ts and run.get("status") == "completed":
                return run
        time.sleep(15)
    return None


def kind_workflow_run_grep(args: dict, *, negate: bool = False) -> VerifyResult:
    """Trigger ``args.workflow`` and grep the resulting log for ``args.pattern``.

    Without ``negate``: success = pattern found.
    With ``negate=True``: success = pattern NOT found (used for
    "verify the warning has stopped firing").
    """
    workflow = args.get("workflow")
    pattern = args.get("pattern")
    timeout_s = int(args.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    if not workflow or not pattern:
        return VerifyResult("failure", "schema invalid: workflow / pattern required")

    before = datetime.now(UTC).isoformat()
    try:
        gh("workflow", "run", workflow, "--ref", "master")
    except subprocess.CalledProcessError as exc:
        return VerifyResult("inconclusive", f"gh workflow run failed: {exc}")
    # Give GH a few seconds to register the run before we start polling.
    time.sleep(5)
    run = _wait_for_run_completion(workflow, before, timeout_s)
    if run is None:
        return VerifyResult("inconclusive", f"timed out waiting for {workflow} to complete")
    if run.get("conclusion") not in {"success", "failure"}:
        return VerifyResult("inconclusive", f"unexpected conclusion: {run.get('conclusion')}")

    log_proc = gh("run", "view", str(run["databaseId"]), "--log", check=False)
    log = (log_proc.stdout or "") + (log_proc.stderr or "")
    found = re.search(pattern, log) is not None
    if negate:
        if not found:
            return VerifyResult("success", f"pattern absent as expected (run {run['databaseId']})")
        return VerifyResult("failure", f"pattern still present (run {run['databaseId']}): {pattern!r}")
    if found:
        return VerifyResult("success", f"pattern matched (run {run['databaseId']})")
    return VerifyResult("failure", f"pattern not found (run {run['databaseId']}): {pattern!r}")


def kind_workflow_run_no_grep(args: dict) -> VerifyResult:
    return kind_workflow_run_grep(args, negate=True)


def kind_manual(args: dict) -> VerifyResult:
    """Always inconclusive — flags the issue for human review.

    Used when the verification can't be automated (e.g. requires
    eyeballing a Slack screenshot). The issue gets a comment with the
    instructions; the human flips the label / closes manually.
    """
    instructions = args.get("instructions", "no instructions")
    return VerifyResult(
        "inconclusive",
        f"manual verification required — please check:\n{instructions}",
    )


def kind_recent_run_log_grep(args: dict) -> VerifyResult:
    """Scan the latest few completed runs of each listed workflow for ``pattern``.

    Does NOT trigger a new run — useful when the verification depends
    on the system having received external input (e.g. a real click-
    mail arriving in Gmail, which can't be forced from CI). Triggering
    a new workflow run won't help: if no mail has arrived, the run
    will just report 0 candidates again. Instead we passively check
    whether the cron-scheduled runs that have ALREADY happened show
    evidence that the awaited event occurred.

    ``args``:
      workflows: list[str] — workflow filenames to scan
      pattern: regex — match in run log (use ``\\d`` etc. with raw-style)
      lookback: int — recent completed runs per workflow to scan (default 5)
      require_all: bool — if true (default) every workflow must match
                          for success; if false, ANY match counts

    Returns:
      success — match found per the require_all rule
      inconclusive — no match yet (re-check next cron). NOT a failure
                     because "no mail yet" is normal during the wait.
    """
    workflows: list[str] = list(args.get("workflows") or [])
    pattern = args.get("pattern")
    lookback = int(args.get("lookback", 5))
    require_all = bool(args.get("require_all", True))
    if not workflows or not pattern:
        return VerifyResult("failure", "schema invalid: workflows / pattern required")

    matches: dict[str, int] = {}  # workflow → matched run id
    for wf in workflows:
        try:
            proc = gh(
                "run",
                "list",
                "--workflow",
                wf,
                "--limit",
                str(lookback),
                "--json",
                "databaseId,conclusion,status",
                check=False,
            )
            runs = json.loads(proc.stdout) if proc.stdout else []
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            logger.warning("gh run list %s failed: %s", wf, exc)
            continue
        for run in runs:
            if run.get("status") != "completed" or run.get("conclusion") != "success":
                continue
            log_proc = gh("run", "view", str(run["databaseId"]), "--log", check=False)
            log = (log_proc.stdout or "") + (log_proc.stderr or "")
            if re.search(pattern, log):
                matches[wf] = run["databaseId"]
                break

    matched_count = len(matches)
    if require_all and matched_count == len(workflows):
        return VerifyResult("success", f"all workflows matched (runs={matches})")
    if (not require_all) and matched_count >= 1:
        return VerifyResult("success", f"at least one workflow matched (runs={matches})")
    missing = [wf for wf in workflows if wf not in matches]
    return VerifyResult(
        "inconclusive",
        f"awaiting event in: {missing} (matched: {list(matches.keys()) or 'none'}; lookback={lookback})",
    )


KIND_REGISTRY = {
    "workflow_run_grep": kind_workflow_run_grep,
    "workflow_run_no_grep": kind_workflow_run_no_grep,
    "recent_run_log_grep": kind_recent_run_log_grep,
    "manual": kind_manual,
}


# ---------- Slack notification ----------------------------------------------


def slack_notify(text: str) -> None:
    """Post ``text`` to ``SLACK_CHANNEL_VERIFY``.

    All pending-verify alerts share a single dedicated channel so they
    don't mix with per-site operational alerts (cookie failure,
    degradation, etc.). Failures are logged and swallowed — Slack
    delivery hiccups should never break the verify loop itself.
    """
    import urllib.error
    import urllib.request

    token = os.environ.get("SLACK_BOT_TOKEN")
    channel = os.environ.get("SLACK_CHANNEL_VERIFY")
    if not token or not channel:
        logger.warning(
            "Slack notify skipped — SLACK_BOT_TOKEN / SLACK_CHANNEL_VERIFY missing",
        )
        return
    payload = json.dumps({"channel": channel, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        SLACK_POST_URL,
        data=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            response = json.loads(resp.read().decode("utf-8"))
            if not response.get("ok"):
                logger.warning("Slack post failed: %s", response.get("error"))
    except (urllib.error.URLError, TimeoutError) as exc:
        logger.warning("Slack post failed: %s", exc)


# ---------- Main loop -------------------------------------------------------


def load_verify_schema(verify_id: str) -> dict | None:
    """Load and basic-validate a verify YAML by id."""
    path = VERIFY_DIR / f"{verify_id}.yml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        logger.error("verify YAML parse failed: %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    if data.get("verify_id") != verify_id:
        logger.error("verify_id mismatch in %s: file says %s", path, data.get("verify_id"))
        return None
    if data.get("kind") not in KIND_REGISTRY:
        logger.error("unknown kind %r in %s", data.get("kind"), path)
        return None
    return data


def is_due(schema: dict, *, now: datetime | None = None) -> bool:
    raw = schema.get("verify_after")
    if not raw:
        return True  # missing == always due
    now = now or datetime.now(UTC)
    try:
        due = datetime.fromisoformat(str(raw))
    except ValueError:
        logger.warning("verify_after not ISO 8601: %r in %s", raw, schema.get("verify_id"))
        return True
    if due.tzinfo is None:
        due = due.replace(tzinfo=UTC)
    return now >= due


def run_one(schema: dict) -> VerifyResult:
    fn = KIND_REGISTRY[schema["kind"]]
    args = schema.get("args", {}) or {}
    return fn(args)


def process_issue(issue: dict, *, dry_run: bool) -> dict | None:
    """Process a single issue and return failure context on hard failure.

    Returns a structured dict on ``failure`` so ``main`` can hand the
    list to the auto-fix step. ``None`` on success / inconclusive /
    skip — those need no downstream action.
    """
    number = issue["number"]
    title = issue.get("title", f"#{number}")
    body = issue.get("body") or ""
    fm = parse_issue_front_matter(body)
    if not fm or "verify_id" not in fm:
        logger.warning("issue %s missing verify_id front-matter; skipping", number)
        return None
    verify_id = str(fm["verify_id"])
    schema = load_verify_schema(verify_id)
    if schema is None:
        msg = f"⚠ verify_id ``{verify_id}`` の schema が見つからない / 不正です。"
        logger.warning("schema not found: %s", verify_id)
        if not dry_run:
            comment_on_issue(number, msg)
        return None
    if not is_due(schema):
        logger.info("issue %s (%s): not yet due", number, verify_id)
        return None

    attempt = issue_attempt_count(issue.get("labels", []))
    max_attempts = int(schema.get("max_attempts", DEFAULT_MAX_ATTEMPTS))
    logger.info("issue %s (%s): current attempt counter %d/%d", number, verify_id, attempt, max_attempts)

    if attempt >= max_attempts:
        logger.info(
            "issue %s (%s): max_attempts already reached (%d/%d), skipping",
            number,
            verify_id,
            attempt,
            max_attempts,
        )
        return None

    if dry_run:
        logger.info("DRY RUN — would execute kind=%s args=%s", schema["kind"], schema.get("args"))
        return None

    # NOTE: ``verify-attempt:N`` is bumped **only on hard failure**.
    # success and inconclusive don't consume attempts, so canary
    # (always-inconclusive heartbeat) doesn't accumulate labels in
    # the repo, and oscillating inconclusive→failure→inconclusive
    # only counts the failures toward max_attempts.
    result = run_one(schema)
    logger.info("issue %s result: %s — %s", number, result.status, result.detail)

    issue_url = f"<https://github.com/dumbled0re/ai-workflows/issues/{number}|#{number}>"

    if result.is_success:
        success_msg = schema.get("success_message") or "✅ 自動検証 OK"
        comment_on_issue(number, f"{success_msg}\n\n```\n{result.detail}\n```")
        close_issue(number)
        slack_notify(
            f":white_check_mark: verify success — {issue_url} {title}\n"
            f"verify_id: `{verify_id}`\n"
            f"detail: {result.detail}\n"
            f"issue 自動 close 済"
        )
        return None

    if result.is_inconclusive:
        retry_h = schema.get("retry_after_hours", DEFAULT_RETRY_AFTER_HOURS)
        comment_on_issue(
            number,
            f"🟡 inconclusive (再試行は {retry_h}h 後)\n\n```\n{result.detail}\n```",
        )
        slack_notify(
            f":hourglass_flowing_sand: verify inconclusive — {issue_url} {title}\n"
            f"verify_id: `{verify_id}`\n"
            f"detail: {result.detail}\n"
            f"次回 cron で再試行"
        )
        return None

    # Hard failure — this counts as an attempt. Bump the counter label
    # so subsequent runs see the updated state.
    bump_attempt_label(number, attempt)

    if attempt + 1 >= max_attempts:
        slack_notify(
            f":rotating_light: verify FAILED (final attempt) — {issue_url} {title}\n"
            f"verify_id: `{verify_id}`\n"
            f"detail: {result.detail}\n"
            f"relates_to: {schema.get('relates_to', 'n/a')}\n"
            f"Stage 2 (Claude 自動修正) が動いた後の状態を issue で確認してください"
        )
        comment_on_issue(
            number,
            f"❌ verify 失敗 (attempt {attempt + 1}/{max_attempts}) — user 介入待ち\n\n```\n{result.detail}\n```",
        )
    else:
        slack_notify(
            f":x: verify failed (attempt {attempt + 1}/{max_attempts}) — {issue_url} {title}\n"
            f"verify_id: `{verify_id}`\n"
            f"detail: {result.detail}\n"
            f"Stage 2 (Claude) が自動修正を試行中"
        )
        comment_on_issue(
            number,
            f"❌ attempt {attempt + 1}/{max_attempts} 失敗。Claude 自動修正を試行 (失敗ならまた次回 cron)"
            f"\n\n```\n{result.detail}\n```",
        )

    # Always emit failure context so the auto-fix step can pick it up.
    # The auto-fix workflow respects ``max_attempts`` upstream by reading
    # the same label, so we don't gate here.
    return {
        "issue_number": number,
        "title": title,
        "verify_id": verify_id,
        "project": schema.get("project", "default"),
        "kind": schema.get("kind"),
        "args": schema.get("args"),
        "relates_to": schema.get("relates_to"),
        "description": schema.get("description"),
        "detail": result.detail,
        "attempt": attempt + 1,
        "max_attempts": max_attempts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run pending verifications")
    parser.add_argument("--dry-run", action="store_true", help="don't trigger workflows or comment")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    issues = list_pending_issues()
    logger.info("found %d open pending-verify issue(s)", len(issues))
    failures: list[dict] = []
    for issue in issues:
        try:
            failure = process_issue(issue, dry_run=args.dry_run)
            if failure is not None:
                failures.append(failure)
        except Exception:
            logger.exception("issue %s processing failed", issue.get("number"))

    # Hand-off to the auto-fix step in the workflow. The workflow reads
    # ``has_failures=true`` to gate the Claude Code Action invocation, and
    # ``failures.json`` is the prompt context. dry-run never emits these
    # so a manual smoke run doesn't accidentally trigger an auto-fix.
    if failures and not args.dry_run:
        out_path = Path(os.environ.get("VERIFY_FAILURES_PATH", "verify_failures.json"))
        out_path.write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("emitted %d failure(s) to %s", len(failures), out_path)
        gh_output = os.environ.get("GITHUB_OUTPUT")
        if gh_output:
            with open(gh_output, "a", encoding="utf-8") as fh:
                fh.write("has_failures=true\n")
                fh.write(f"failures_path={out_path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
