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
    """Gather AI news from multiple sources and build analysis prompt."""
    from tech_catchup.sources import (
        fetch_arxiv_ai,
        fetch_github_trending_ai,
        fetch_hackernews_ai,
        format_all_sources,
    )

    logger.info("Gathering AI tech news...")

    hn = fetch_hackernews_ai(max_items=15)
    github = fetch_github_trending_ai(max_items=10)
    arxiv = fetch_arxiv_ai(max_items=10)

    logger.info("Sources: HN=%d, GitHub=%d, arXiv=%d", len(hn), len(github), len(arxiv))

    if not hn and not github and not arxiv:
        logger.warning("No AI news found from any source")
        sys.exit(0)

    sources_text = format_all_sources(hn, github, arxiv)

    from datetime import datetime, timedelta, timezone
    jst = timezone(timedelta(hours=9))
    today = datetime.now(jst).strftime("%Y-%m-%d")

    prompt = f"""\
あなたはシニアAIエンジニア向けの技術キュレーターです。
以下の情報源から、エンジニアが今日知っておくべきAI関連の最新動向をまとめてください。

本日: {today}

{sources_text}

以下の基準で情報を整理・優先順位付けしてください:

1. **重要度HIGH**: 業界を変える可能性がある発表、主要なモデル/ツールのリリース、セキュリティ関連
2. **重要度MEDIUM**: 実務で使える新ツール/ライブラリ、興味深い研究成果
3. **重要度LOW**: トレンド把握として知っておくと良い情報

以下のJSON形式で回答してください:
{{
  "date": "{today}",
  "top_stories": [
    {{
      "title": "記事/リポジトリ/論文のタイトル",
      "importance": "HIGH/MEDIUM/LOW",
      "category": "LLM/Agent/Tool/Research/Infrastructure/Other",
      "summary": "エンジニア向けの簡潔な要約（2-3文、日本語）",
      "why_it_matters": "なぜエンジニアが知るべきか（1文）",
      "url": "元のURL",
      "source": "Hacker News/GitHub Trending/arXiv"
    }}
  ],
  "daily_insight": "今日のAI業界の全体的な動向を1段落で要約（日本語）"
}}

重要:
- 重複する情報は統合する
- 既に広く知られている情報より、新しい動きを優先する
- エンジニアが実務で活用できる情報を重視する
- top_storiesは最大10件に絞る
"""

    out = Path("tech_catchup/data")
    out.mkdir(exist_ok=True)
    with open(out / "tech_catchup_prompt.txt", "w", encoding="utf-8") as f:
        f.write(prompt)

    logger.info("Tech catchup prompt saved")


def phase_notify() -> None:
    """Read Claude's tech summary and send to Slack."""
    import requests

    logger.info("Sending tech catchup to Slack")

    slack_webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not slack_webhook:
        logger.error("SLACK_WEBHOOK_URL not set")
        sys.exit(1)

    result_path = Path("tech_catchup/data/tech_catchup_result.json")
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
    _send_to_slack(slack_webhook, blocks)


def _build_slack_blocks(result: dict) -> list[dict]:
    """Build Slack blocks from tech catchup result."""
    blocks: list[dict] = []

    date = result.get("date", "")
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"AI Tech Catchup - {date}"},
    })

    # Daily insight
    insight = result.get("daily_insight", "")
    if insight:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*:brain: 今日のAI動向*\n>{insight}"},
        })
        blocks.append({"type": "divider"})

    # Top stories
    importance_emoji = {"HIGH": ":red_circle:", "MEDIUM": ":large_yellow_circle:", "LOW": ":white_circle:"}
    category_emoji = {
        "LLM": ":robot_face:", "Agent": ":mechanical_arm:", "Tool": ":wrench:",
        "Research": ":microscope:", "Infrastructure": ":building_construction:", "Other": ":bulb:",
    }

    for story in result.get("top_stories", []):
        imp = story.get("importance", "LOW")
        cat = story.get("category", "Other")
        imp_e = importance_emoji.get(imp, ":white_circle:")
        cat_e = category_emoji.get(cat, ":bulb:")

        text = (
            f"{imp_e} {cat_e} *{story.get('title', '')}*\n"
            f"{story.get('summary', '')}\n"
            f"_:point_right: {story.get('why_it_matters', '')}_\n"
            f"<{story.get('url', '')}|{story.get('source', '')}>"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})

    blocks.append({"type": "divider"})
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": ":robot_face: Powered by Claude AI | Sources: Hacker News, GitHub Trending, arXiv"}],
    })

    return blocks


def _send_to_slack(webhook_url: str, blocks: list[dict]) -> None:
    """Send blocks to Slack."""
    import requests as req

    resp = req.post(webhook_url, json={"blocks": blocks}, timeout=10)
    if resp.status_code == 200:
        logger.info("Tech catchup sent to Slack")
    else:
        logger.error("Slack webhook failed: %d %s", resp.status_code, resp.text)
        # Print result as fallback
        print(json.dumps(blocks, ensure_ascii=False, indent=2))


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
