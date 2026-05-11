from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def phase_gather() -> None:
    """Focused gather: Claude Code / OpenAI Codex / Gemini CLI only.

    Picks up only updates from the last ``LOOKBACK_HOURS`` window so the
    cron can run every few hours and surface releases / commits / blog
    posts as they appear, without re-summarising stale material. When
    the window has zero updates, exits silently with status 0 — the
    workflow skips its AI + Slack steps via a sentinel file, and the
    user trusts an absent Slack post to mean "nothing new" rather than
    parsing identical digests repeatedly.

    Wider sources (HN / arXiv / GitHub Trending / generic AI company
    news) remain available in ``sources.py`` but are intentionally not
    called here — the user has narrowed the scope to "stuff that
    directly affects my daily dev tools" for now. Re-enable by adding
    them back to this function.
    """
    from tech_catchup.sources import (
        fetch_focused_tool_updates,
        fetch_github_trending_buzz,
        fetch_hn_ai_buzz,
        fetch_x_ai_buzz,
        format_buzz_layer,
        format_focused_updates,
    )

    # Lookback window. Sized to comfortably overlap the cron interval
    # (every 2-4 hours) so a release published *between* runs is still
    # caught by the next one, without re-surfacing the same item more
    # than twice (the AI dedupes against its own prior digests in
    # spirit, though there's no machine state — overlap is the
    # belt-and-braces.).
    lookback_hours = int(os.environ.get("TECH_CATCHUP_LOOKBACK_HOURS", "6"))

    # Two-layer gather:
    # 1. Focused tools — Claude Code / Codex / Gemini CLI specific
    #    releases / commits / blog posts within the lookback window
    # 2. AI buzz — what's actually getting attention right now via HN
    #    high-engagement filter + GitHub Trending + X (best-effort
    #    RSSHub mirror). Always pulled fresh (no time window — these
    #    are "what's hot now" signals that change throughout the day).
    updates = fetch_focused_tool_updates(lookback_hours=lookback_hours)
    hn_buzz = fetch_hn_ai_buzz(min_score=50, min_comments=30, max_items=10)
    trending = fetch_github_trending_buzz(max_items=10)
    x_buzz = fetch_x_ai_buzz(max_items=8)

    out = Path(__file__).parent.parent / "data"
    out.mkdir(exist_ok=True)
    sentinel_path = out / "skip.flag"
    # Wipe the sentinel from a prior run so a fresh cron doesn't
    # accidentally short-circuit if its phase_gather actually produced
    # updates this time.
    if sentinel_path.exists():
        sentinel_path.unlink()

    has_any = bool(updates) or bool(hn_buzz) or bool(trending) or bool(x_buzz)
    if not has_any:
        logger.info(
            "No focused updates / HN buzz / trending / X buzz in last %dh — skipping AI + Slack steps",
            lookback_hours,
        )
        sentinel_path.write_text("no_updates", encoding="utf-8")
        sys.exit(0)

    focused_text = format_focused_updates(updates)
    buzz_text = format_buzz_layer(hn_buzz, trending, x_buzz)
    sources_text = "\n\n".join(s for s in (focused_text, buzz_text) if s)

    from datetime import datetime, timedelta, timezone

    jst = timezone(timedelta(hours=9))
    now_jst = datetime.now(jst)
    today = now_jst.strftime("%Y-%m-%d")
    window_label = now_jst.strftime("%Y-%m-%d %H:%M JST")

    prompt = f"""\
あなたは Claude Code / OpenAI Codex / Gemini CLI の3ツール + AI 業界のバズに特化した
技術キュレーターです。エンジニアが**今知っておくべき**変更点と話題を簡潔にまとめてください。

実行時刻: {window_label}
データ収集ウィンドウ: 直近 {lookback_hours} 時間

データソースは 2 層構成:
- **focused_updates**: 上記 3 ツールの release / commit / blog (window 内の差分のみ)
- **buzz_layer**: HN 高エンゲージメント (>=50pts or >=30comments) + GitHub Trending + X (Twitter)
  → 「window 関係なく今バズってる AI ネタ」を catch するための直交層

{sources_text}

整理ルール:

1. **重要度 HIGH**: 破壊的変更 / メジャー機能追加 / セキュリティ修正 / 業界を変える発表
2. **重要度 MEDIUM**: 新機能の追加、改善、性能向上、注目を集めている新ツール
3. **重要度 LOW**: バグ修正、軽微な変更、ドキュメント更新、トレンド把握用情報

以下の JSON 形式のみで回答してください:
{{
  "date": "{today}",
  "window_label": "{window_label}",
  "tool_updates": [
    {{
      "tool": "Claude Code / OpenAI Codex / Gemini CLI のいずれか",
      "title": "リリースタグ / コミット要約 / ブログタイトル",
      "kind": "release / commit / blog",
      "importance": "HIGH / MEDIUM / LOW",
      "summary": "2-3 文で要約。具体的なフラグ名・コマンド・破壊的変更を含めること",
      "why_it_matters": "日常の使い方への影響を 1 文で",
      "url": "元のURL",
      "version": "リリースの場合はタグ名、それ以外は空"
    }}
  ],
  "buzz": [
    {{
      "title": "話題のタイトル",
      "source": "HN / GitHub Trending / X / 統合",
      "importance": "HIGH / MEDIUM / LOW",
      "summary": "なぜ話題か + どんな内容か (2-3 文、日本語)",
      "url": "元のURL"
    }}
  ],
  "summary": "この時間帯の更新と話題を 2-3 文で総括（日本語）"
}}

重要:
- **同一リリースの release / commit / blog は統合**して 1 tool_updates エントリにする
- ソースに無い情報を推測しない (バージョン番号、機能名、コマンド等)
- changelog 本文の**具体的な変更点**を summary に必ず引用する。一般論で済ませない
- tool_updates: 最大 8 件
- buzz: 最大 5 件。HN/Trending/X で類似の話題があれば統合 (1 エントリで複数 URL は最も代表的なもの)
- buzz は「Claude Code / Codex / Gemini 本体」の話題でも、上記 tool_updates と被らないなら採用
  (例: ユーザの使用例 blog、3rd-party integration、性能比較記事)
- 重要度: HIGH は本当の breaking change / 大型機能 / 業界転換のみ。version bump だけなら LOW
"""

    with open(out / "tech_catchup_prompt.txt", "w", encoding="utf-8") as f:
        f.write(prompt)

    logger.info("Focused tech catchup prompt saved (%d updates)", len(updates))


def phase_notify() -> None:
    """Read Claude's tech summary and send to Slack."""
    logger.info("Sending tech catchup to Slack")

    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_channel = os.environ.get("SLACK_CHANNEL_TECH")
    if not slack_token:
        logger.error("SLACK_BOT_TOKEN not set")
        sys.exit(1)
    if not slack_channel:
        logger.error("SLACK_CHANNEL_TECH not set")
        sys.exit(1)

    result_path = Path(__file__).parent.parent / "data" / "tech_catchup_result.json"
    if not result_path.exists():
        logger.error("Tech catchup result not found")
        sys.exit(1)

    with open(result_path, encoding="utf-8") as f:
        content = f.read().strip()

    # Parse JSON (handle markdown code blocks)
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        if "```json" in content:
            start = content.index("```json") + 7
            end = content.index("```", start)
            result = json.loads(content[start:end].strip())
        elif "```" in content:
            start = content.index("```") + 3
            end = content.index("```", start)
            result = json.loads(content[start:end].strip())
        else:
            logger.error("Could not parse tech catchup result")
            sys.exit(1)

    blocks = _build_slack_blocks(result)
    _send_to_slack(slack_token, slack_channel, blocks, result.get("date", ""))


def _build_slack_blocks(result: dict) -> list[dict]:
    """Build Slack blocks from focused tech catchup result.

    Two sections matching the new prompt schema:
    - tool_updates: Claude Code / Codex / Gemini CLI specific changes
    - buzz: AI industry buzz from HN / GitHub Trending / X
    Each rendered as its own block group with a header so the operator
    can immediately spot whether a run was "tool release" or "industry
    chatter" without reading every item.
    """
    blocks: list[dict] = []

    window = result.get("window_label") or result.get("date", "")
    blocks.append(
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"AI Tech Catchup - {window}"},
        }
    )

    summary = result.get("summary", "") or result.get("daily_insight", "")
    if summary:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*:brain: 今回の動向*\n>{summary}"},
            }
        )
        blocks.append({"type": "divider"})

    importance_emoji = {"HIGH": ":red_circle:", "MEDIUM": ":large_yellow_circle:", "LOW": ":white_circle:"}
    tool_emoji = {
        "Claude Code": ":hammer_and_wrench:",
        "OpenAI Codex": ":computer:",
        "Gemini CLI": ":sparkles:",
    }
    kind_emoji = {"release": ":package:", "commit": ":wrench:", "blog": ":newspaper:"}

    # Tool updates section
    tool_updates = result.get("tool_updates") or result.get("top_stories") or []
    if tool_updates:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*:rocket: Tool Updates (Claude Code / Codex / Gemini CLI)*"},
            }
        )
        for idx, story in enumerate(tool_updates):
            imp = story.get("importance", "LOW")
            imp_e = importance_emoji.get(imp, ":white_circle:")
            tool = story.get("tool", "")
            tool_e = tool_emoji.get(tool, ":bulb:")
            kind = story.get("kind", "")
            kind_e = kind_emoji.get(kind, "")
            version = story.get("version", "")
            ver_text = f" `{version}`" if version else ""

            text = (
                f"{imp_e} {tool_e} {kind_e} *{story.get('title', '')}*{ver_text}\n"
                f"{story.get('summary', '')}\n"
                f"_:point_right: {story.get('why_it_matters', '')}_"
            )
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})

            url = story.get("url", "")
            if url:
                blocks.append(
                    {
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": f":link: <{url}|Source>"}],
                    }
                )
            if idx < len(tool_updates) - 1:
                blocks.append({"type": "divider"})

    # Buzz section
    buzz = result.get("buzz") or []
    if buzz:
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*:fire: AI Buzz (HN / GitHub Trending / X)*"},
            }
        )
        for idx, story in enumerate(buzz):
            imp = story.get("importance", "LOW")
            imp_e = importance_emoji.get(imp, ":white_circle:")
            source = story.get("source", "")
            text = f"{imp_e} *{story.get('title', '')}* _[{source}]_\n{story.get('summary', '')}"
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
            url = story.get("url", "")
            if url:
                blocks.append(
                    {
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": f":link: <{url}|Source>"}],
                    }
                )
            if idx < len(buzz) - 1:
                blocks.append({"type": "divider"})

    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        ":robot_face: Powered by Claude AI | "
                        "Sources: GitHub Releases/Commits (Claude Code, Codex, Gemini CLI), "
                        "Anthropic/OpenAI/Google blogs, Hacker News, GitHub Trending, X via RSSHub"
                    ),
                }
            ],
        }
    )

    return blocks


def _send_to_slack(bot_token: str, channel: str, blocks: list[dict], date_str: str) -> None:
    """Post blocks to Slack via chat.postMessage (bot token)."""
    import requests as req

    resp = req.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={
            "channel": channel,
            "text": f"AI Tech Catchup - {date_str}",
            "blocks": blocks,
        },
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        logger.error("Slack API error: %s", body.get("error", "unknown"))
        # Print result as fallback
        print(json.dumps(blocks, ensure_ascii=False, indent=2))
        sys.exit(1)
    logger.info("Tech catchup sent to Slack channel=%s (%d blocks)", channel, len(blocks))


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Tech Catchup")
    parser.add_argument(
        "phase",
        choices=["gather", "notify"],
        help="Phase: 'gather' or 'notify'",
    )
    args = parser.parse_args()

    if args.phase == "gather":
        phase_gather()
    elif args.phase == "notify":
        phase_notify()


if __name__ == "__main__":
    main()
