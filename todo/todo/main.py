"""TODO list parser and Slack notifier.

todos.md format:

    ## やること

    - [ ] 2026-05-10 タスクA
    - [ ] - 期限なしのタスク

    ## 完了

    - [x] 2026-05-01 タスクX (完了: 2026-05-02)
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


def format_for_slack(todos: list[Todo]) -> str:
    today = datetime.now(JST).date()
    if not todos:
        return f":white_check_mark: *TODO ({today.isoformat()})*\n やることはありません！"

    overdue: list[Todo] = []
    today_items: list[Todo] = []
    this_week: list[Todo] = []
    later: list[Todo] = []
    no_deadline: list[Todo] = []
    for t in todos:
        if t.deadline is None:
            no_deadline.append(t)
        elif t.deadline < today:
            overdue.append(t)
        elif t.deadline == today:
            today_items.append(t)
        elif t.deadline <= today + timedelta(days=7):
            this_week.append(t)
        else:
            later.append(t)

    parts = [f":memo: *TODO ({today.isoformat()})*"]

    def section(title: str, items: list[Todo], show_date: bool = True) -> None:
        if not items:
            return
        parts.append(f"\n*{title}*")
        for t in items:
            if show_date and t.deadline is not None:
                days = t.days_until
                suffix = ""
                if days is not None:
                    if days < 0:
                        suffix = f"  _({-days}日超過)_"
                    elif days == 0:
                        suffix = "  _(今日)_"
                    else:
                        suffix = f"  _(あと{days}日)_"
                parts.append(f"• `{t.deadline.isoformat()}` {t.text}{suffix}")
            else:
                parts.append(f"• {t.text}")

    section(":rotating_light: 期限超過", overdue)
    section(":fire: 今日", today_items)
    section(":calendar: 今週中", this_week)
    section(":date: それ以降", later)
    section(":hourglass: 期限なし", no_deadline, show_date=False)

    return "\n".join(parts)


def send_to_slack(text: str) -> None:
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
        json={"channel": channel, "text": text, "mrkdwn": True},
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        logger.error("Slack API error: %s", body.get("error", "unknown"))
        sys.exit(1)
    logger.info("Posted to Slack channel=%s (%d chars)", channel, len(text))


def cmd_notify(args: argparse.Namespace) -> None:
    markdown = TODOS_PATH.read_text(encoding="utf-8")
    todos = sort_todos(parse_todos(markdown))
    text = format_for_slack(todos)
    if args.dry_run:
        print(text)
        return
    send_to_slack(text)


def cmd_list(args: argparse.Namespace) -> None:
    markdown = TODOS_PATH.read_text(encoding="utf-8")
    todos = sort_todos(parse_todos(markdown))
    print(format_for_slack(todos))


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
