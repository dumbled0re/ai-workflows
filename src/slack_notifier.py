from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

_MAX_BLOCKS_PER_MESSAGE = 50
_PREDICTION_EMOJI = {"UP": ":chart_with_upwards_trend:", "DOWN": ":chart_with_downwards_trend:"}
_CONFIDENCE_EMOJI = {"HIGH": ":large_green_circle:", "MEDIUM": ":large_yellow_circle:", "LOW": ":red_circle:"}


def send_analysis_to_slack(
    webhook_url: str,
    holdings_analysis: dict,
    discovery_results: dict,
    timing: str,
    data_quality: dict | None = None,
) -> bool:
    """Format and send the analysis report to Slack.

    Args:
        webhook_url: Slack Incoming Webhook URL
        holdings_analysis: Claude holdings analysis result
        discovery_results: Claude discovery result
        timing: "morning" or "evening"
        data_quality: Optional dict with success/failure counts

    Returns:
        True if sent successfully
    """
    if holdings_analysis.get("error"):
        return _send_error(webhook_url, holdings_analysis.get("message", "不明なエラー"))

    blocks = _build_blocks(holdings_analysis, discovery_results, timing, data_quality)
    return _send_blocks(webhook_url, blocks)


def send_error_to_slack(webhook_url: str, error_message: str) -> bool:
    """Send error notification to Slack."""
    return _send_error(webhook_url, error_message)


def send_market_closed_to_slack(webhook_url: str, date_str: str) -> bool:
    """Send market closed notification."""
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"本日休場 - {date_str}"}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "本日は東京証券取引所の休場日のため、分析はスキップしました。"},
        },
    ]
    return _post_webhook(webhook_url, {"blocks": blocks})


def _build_blocks(
    holdings_analysis: dict,
    discovery_results: dict,
    timing: str,
    data_quality: dict | None,
) -> list[dict]:
    """Build Slack Block Kit blocks for the full report."""
    from datetime import datetime, timedelta, timezone

    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    timing_label = "朝" if timing == "morning" else "夕"
    date_str = now.strftime("%Y-%m-%d")

    blocks: list[dict] = []

    # Header
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"日本株AI分析レポート - {date_str} {timing_label}"},
    })

    # Data quality info (Codex review feedback: make partial failures visible)
    if data_quality:
        quality_text = (
            f":bar_chart: データ品質: "
            f"成功 {data_quality.get('success', 0)} / "
            f"失敗 {data_quality.get('failed', 0)} 銘柄"
        )
        if data_quality.get("failed", 0) > 0:
            quality_text += " :warning:"
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": quality_text}],
        })

    blocks.append({"type": "divider"})

    # Market overview
    market_overview = holdings_analysis.get("market_overview", "")
    if market_overview:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*:earth_asia: マーケット概況*\n>{market_overview}"},
        })
        blocks.append({"type": "divider"})

    # Holdings analysis
    holdings = holdings_analysis.get("holdings_analysis", [])
    if holdings:
        blocks.append({
            "type": "header",
            "text": {"type": "plain_text", "text": "保有銘柄分析"},
        })
        for h in holdings:
            blocks.append(_format_holding_block(h))

    blocks.append({"type": "divider"})

    # Discovery
    recommended = discovery_results.get("recommended_stocks", [])
    if recommended:
        blocks.append({
            "type": "header",
            "text": {"type": "plain_text", "text": "おすすめ銘柄"},
        })
        for r in recommended:
            blocks.append(_format_discovery_block(r))

    # Footer
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": (
                    ":robot_face: Claude AI分析 | Yahoo Financeデータ | "
                    "*投資助言ではありません。投資判断はご自身の責任で行ってください。*"
                ),
            }
        ],
    })

    return blocks


def _format_holding_block(h: dict) -> dict:
    """Format a single holding analysis into a Slack block."""
    pred = h.get("prediction", "?")
    conf = h.get("confidence", "?")
    pred_emoji = _PREDICTION_EMOJI.get(pred, ":question:")
    conf_emoji = _CONFIDENCE_EMOJI.get(conf, ":white_circle:")

    reasons = "\n".join(f"  - {r}" for r in h.get("reasons", []))
    risk = h.get("risk_factor", "")
    summary = h.get("short_summary", "")

    text = (
        f"*{h.get('name', '')} ({h.get('ticker', '')})* "
        f"{pred_emoji} *{pred}* | {conf_emoji} 信頼度: *{conf}*\n"
        f"{summary}\n"
        f"{reasons}\n"
        f"  :warning: リスク: {risk}"
    )
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _format_discovery_block(r: dict) -> dict:
    """Format a single discovery recommendation into a Slack block."""
    pred_emoji = _PREDICTION_EMOJI.get(r.get("prediction", ""), ":question:")
    conf_emoji = _CONFIDENCE_EMOJI.get(r.get("confidence", ""), ":white_circle:")

    reasons = "\n".join(f"  - {r_}" for r_ in r.get("reasons", []))
    risk = r.get("risk_factor", "")
    entry = r.get("entry_strategy", "")
    expected = r.get("expected_move", "")

    text = (
        f"*#{r.get('rank', '?')} - {r.get('name', '')} ({r.get('ticker', '')})* "
        f"{pred_emoji} | {conf_emoji} 信頼度: *{r.get('confidence', '?')}*\n"
        f"予想: {expected}\n"
        f"{reasons}\n"
        f"  :dart: エントリー: {entry}\n"
        f"  :warning: リスク: {risk}"
    )
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _send_blocks(webhook_url: str, blocks: list[dict]) -> bool:
    """Send blocks to Slack, splitting into multiple messages if needed."""
    if len(blocks) <= _MAX_BLOCKS_PER_MESSAGE:
        return _post_webhook(webhook_url, {"blocks": blocks})

    # Split into chunks
    success = True
    for i in range(0, len(blocks), _MAX_BLOCKS_PER_MESSAGE):
        chunk = blocks[i : i + _MAX_BLOCKS_PER_MESSAGE]
        if not _post_webhook(webhook_url, {"blocks": chunk}):
            success = False
    return success


def _send_error(webhook_url: str, message: str) -> bool:
    """Send an error notification to Slack."""
    # Sanitize error message to avoid leaking secrets
    sanitized = _sanitize_error(message)
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": ":x: 株分析エラー"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{sanitized}```"}},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "GitHub Actionsのログを確認してください。"}],
        },
    ]
    return _post_webhook(webhook_url, {"blocks": blocks})


def _sanitize_error(message: str) -> str:
    """Remove potential secrets from error messages."""
    sensitive_patterns = ["sk-ant-", "xoxb-", "xoxp-", "hooks.slack.com"]
    sanitized = message
    for pattern in sensitive_patterns:
        if pattern in sanitized:
            idx = sanitized.index(pattern)
            end = min(idx + len(pattern) + 10, len(sanitized))
            sanitized = sanitized[:idx] + "[REDACTED]" + sanitized[end:]
    return sanitized[:500]  # Limit length


def _post_webhook(webhook_url: str, payload: dict) -> bool:
    """POST to Slack webhook."""
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code == 200:
            return True
        logger.error("Slack webhook failed: %d %s", resp.status_code, resp.text)
        return False
    except requests.RequestException:
        logger.error("Slack webhook request failed", exc_info=True)
        return False
