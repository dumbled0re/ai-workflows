"""TODO list parser and Slack notifier.

todos.md format:

    ## やること

    - [ ] 2026-05-10 タスクA
    - [ ] - 期限なしのタスク

完了したタスクは履歴として残さず、ファイルから完全に削除する。
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
TODOS_PATH = Path(__file__).parent.parent / "todos.md"
TODO_LINE_RE = re.compile(r"^- \[ \] (?P<rest>.+)$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\s+(.+)$")


@dataclass
class Todo:
    deadline: date | None
    text: str

    @property
    def days_until(self) -> int | None:
        if self.deadline is None:
            return None
        today = datetime.now(JST).date()
        return (self.deadline - today).days


def parse_todos(markdown: str) -> list[Todo]:
    """Extract pending todos from the `やること` section."""
    lines = markdown.splitlines()
    in_yaru = False
    todos: list[Todo] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            in_yaru = stripped == "## やること"
            continue
        if not in_yaru:
            continue
        m = TODO_LINE_RE.match(stripped)
        if not m:
            continue
        rest = m.group("rest").strip()
        date_match = DATE_RE.match(rest)
        if date_match:
            year, month, day, text = date_match.groups()
            try:
                deadline = date(int(year), int(month), int(day))
            except ValueError:
                logger.warning("Invalid date in line: %r", line)
                deadline = None
                text = rest
        elif rest.startswith("- "):
            deadline = None
            text = rest[2:].strip()
        else:
            deadline = None
            text = rest
        todos.append(Todo(deadline=deadline, text=text))
    return todos


def sort_todos(todos: list[Todo]) -> list[Todo]:
    """Earliest deadline first; no-deadline tasks last."""
    far_future = date(9999, 12, 31)
    return sorted(todos, key=lambda t: (t.deadline or far_future, t.text))


WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]


def _bucket(todos: list[Todo]) -> dict[str, list[Todo]]:
    today = datetime.now(JST).date()
    buckets: dict[str, list[Todo]] = {
        "overdue": [],
        "today": [],
        "this_week": [],
        "later": [],
        "no_deadline": [],
    }
    for t in todos:
        if t.deadline is None:
            buckets["no_deadline"].append(t)
        elif t.deadline < today:
            buckets["overdue"].append(t)
        elif t.deadline == today:
            buckets["today"].append(t)
        elif t.deadline <= today + timedelta(days=7):
            buckets["this_week"].append(t)
        else:
            buckets["later"].append(t)
    return buckets


def _format_item(t: Todo, *, show_date: bool) -> str:
    if not show_date or t.deadline is None:
        return f"•  {t.text}"
    d = t.deadline
    date_str = f"{d.month}/{d.day}({WEEKDAY_JP[d.weekday()]})"
    days = t.days_until
    if days is None:
        suffix = ""
    elif days < 0:
        suffix = f"  _← {-days}日超過_"
    elif days == 0:
        suffix = "  _← 今日_"
    else:
        suffix = f"  _← あと{days}日_"
    return f"•  `{date_str}`  {t.text}{suffix}"


def _section_block(title_emoji: str, title: str, items: list[Todo], *, show_date: bool) -> dict | None:
    if not items:
        return None
    lines = [f"{title_emoji}  *{title}*  ({len(items)})"]
    lines.extend(_format_item(t, show_date=show_date) for t in items)
    return {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}


def format_for_slack_blocks(todos: list[Todo]) -> tuple[list[dict], str]:
    """Return (blocks, fallback_text) for chat.postMessage."""
    today = datetime.now(JST).date()
    today_str = f"{today.isoformat()} ({WEEKDAY_JP[today.weekday()]})"

    if not todos:
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "📝 TODO リマインダー", "emoji": True},
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": today_str}],
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": ":white_check_mark:  *やることはありません！*"},
            },
        ]
        return blocks, f"TODO ({today_str}) — やることはありません！"

    b = _bucket(todos)
    summary_parts = [f"*{today_str}*", f"計 {len(todos)} 件"]
    if b["overdue"]:
        summary_parts.append(f":rotating_light: 期限超過 {len(b['overdue'])}")
    if b["today"]:
        summary_parts.append(f":fire: 今日 {len(b['today'])}")
    if b["this_week"]:
        summary_parts.append(f":calendar: 今週 {len(b['this_week'])}")

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "📝 TODO リマインダー", "emoji": True},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "  ·  ".join(summary_parts)}],
        },
        {"type": "divider"},
    ]

    sections = [
        (":rotating_light:", "期限超過", b["overdue"], True),
        (":fire:", "今日", b["today"], True),
        (":calendar:", "今週中", b["this_week"], True),
        (":date:", "それ以降", b["later"], True),
        (":hourglass:", "期限なし", b["no_deadline"], False),
    ]

    first = True
    for emoji, title, items, show_date in sections:
        block = _section_block(emoji, title, items, show_date=show_date)
        if block is None:
            continue
        if not first:
            blocks.append({"type": "divider"})
        blocks.append(block)
        first = False

    fallback_text = f"TODO ({today_str}) — 計 {len(todos)} 件"
    return blocks, fallback_text


def send_to_slack(blocks: list[dict], fallback_text: str) -> None:
    token = os.environ.get("SLACK_BOT_TOKEN")
    channel = os.environ.get("SLACK_CHANNEL_TODO")
    if not token:
        logger.error("SLACK_BOT_TOKEN is not set")
        sys.exit(1)
    if not channel:
        logger.error("SLACK_CHANNEL_TODO is not set")
        sys.exit(1)
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={"channel": channel, "text": fallback_text, "blocks": blocks},
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        logger.error("Slack API error: %s", body.get("error", "unknown"))
        sys.exit(1)
    logger.info("Posted to Slack channel=%s (%d blocks)", channel, len(blocks))


def _preview_blocks(blocks: list[dict]) -> str:
    """Render Block Kit blocks as plaintext for terminal preview."""
    out: list[str] = []
    for blk in blocks:
        t = blk.get("type")
        if t == "header":
            out.append("\n━━━ " + blk["text"]["text"] + " ━━━")
        elif t == "context":
            out.append("  " + " ".join(e.get("text", "") for e in blk.get("elements", [])))
        elif t == "divider":
            out.append("―" * 40)
        elif t == "section":
            out.append(blk["text"]["text"])
    return "\n".join(out).lstrip("\n")


def cmd_notify(args: argparse.Namespace) -> None:
    markdown = TODOS_PATH.read_text(encoding="utf-8")
    todos = sort_todos(parse_todos(markdown))
    blocks, fallback = format_for_slack_blocks(todos)
    if args.dry_run:
        print(_preview_blocks(blocks))
        return
    send_to_slack(blocks, fallback)


def cmd_list(args: argparse.Namespace) -> None:
    markdown = TODOS_PATH.read_text(encoding="utf-8")
    todos = sort_todos(parse_todos(markdown))
    blocks, _ = format_for_slack_blocks(todos)
    print(_preview_blocks(blocks))


def main() -> None:
    parser = argparse.ArgumentParser(description="Todo notifier")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_notify = sub.add_parser("notify", help="Post pending todos to Slack")
    p_notify.add_argument("--dry-run", action="store_true", help="Print to stdout only")
    p_notify.set_defaults(func=cmd_notify)

    p_list = sub.add_parser("list", help="Print formatted todos to stdout")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
