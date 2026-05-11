from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

_MAX_BLOCKS_PER_MESSAGE = 50
_PREDICTION_EMOJI = {"UP": ":chart_with_upwards_trend:", "DOWN": ":chart_with_downwards_trend:"}
_CONFIDENCE_EMOJI = {
    "HIGH": ":large_green_circle:",
    "MEDIUM": ":large_yellow_circle:",
    "LOW": ":red_circle:",
}
_SLACK_POST_URL = "https://slack.com/api/chat.postMessage"


def send_analysis_to_slack(
    bot_token: str,
    channel: str,
    holdings_analysis: dict,
    discovery_results: dict,
    timing: str,
    data_quality: dict | None = None,
    portfolio_risk_text: str | None = None,
) -> bool:
    """Format and send the analysis report to Slack.

    Args:
        bot_token: Slack Bot User OAuth Token (xoxb-...)
        channel: Slack channel ID or #name
        holdings_analysis: Claude holdings analysis result
        discovery_results: Claude discovery result
        timing: "morning" or "evening"
        data_quality: Optional dict with success/failure counts
        portfolio_risk_text: Optional Slack-ready text from portfolio_risk
            findings (sector concentration / correlation / total-count
            violations against the live recommendations). When non-empty
            it is appended as its own section so the operator sees it
            adjacent to the picks.

    Returns:
        True if sent successfully
    """
    if holdings_analysis.get("error"):
        return _send_error(bot_token, channel, holdings_analysis.get("message", "不明なエラー"))

    blocks = _build_blocks(holdings_analysis, discovery_results, timing, data_quality)
    if portfolio_risk_text:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": portfolio_risk_text},
            }
        )
    return _send_blocks(bot_token, channel, blocks)


def send_error_to_slack(bot_token: str, channel: str, error_message: str) -> bool:
    """Send error notification to Slack."""
    return _send_error(bot_token, channel, error_message)


def send_market_closed_to_slack(bot_token: str, channel: str, date_str: str) -> bool:
    """Send market closed notification."""
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"本日休場 - {date_str}"}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "本日は東京証券取引所の休場日のため、分析はスキップしました。",
            },
        },
    ]
    return _post_message(bot_token, channel, blocks, fallback_text=f"本日休場 - {date_str}")


def send_save_failure_to_slack(bot_token: str, channel: str) -> bool:
    """Notify Slack when prediction-tracking data fails to commit/push."""
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": ":rotating_light: データ保存失敗"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "予測追跡データのgit pushが失敗しました。\n"
                    "改善ループが停止しています。\n\n"
                    "GitHub Actionsのログを確認してください。"
                ),
            },
        },
    ]
    return _post_message(bot_token, channel, blocks, fallback_text="データ保存失敗")


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
    blocks.append(
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"日本株AI分析レポート - {date_str} {timing_label}",
            },
        }
    )

    # Data quality info (Codex review feedback: make partial failures visible)
    if data_quality:
        quality_text = (
            f":bar_chart: データ品質: 成功 {data_quality.get('success', 0)} / 失敗 {data_quality.get('failed', 0)} 銘柄"
        )
        if data_quality.get("failed", 0) > 0:
            quality_text += " :warning:"
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": quality_text}],
            }
        )

    blocks.append({"type": "divider"})

    # Market overview
    market_overview = holdings_analysis.get("market_overview", "")
    if market_overview:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*:earth_asia: マーケット概況*\n>{market_overview}",
                },
            }
        )
        blocks.append({"type": "divider"})

    # Holdings analysis
    holdings = holdings_analysis.get("holdings_analysis", [])
    if holdings:
        blocks.append(
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "保有銘柄分析"},
            }
        )
        for h in holdings:
            blocks.append(_format_holding_block(h))

    blocks.append({"type": "divider"})

    # Short-term picks
    short_term = discovery_results.get("short_term_picks", [])
    # Fallback: support old format
    if not short_term:
        short_term = discovery_results.get("recommended_stocks", [])
    if short_term:
        blocks.append(
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "短期トレード候補（1-4週間）"},
            }
        )
        for r in short_term:
            blocks.append(_format_discovery_block(r))

    blocks.append({"type": "divider"})

    # Long-term picks
    long_term = discovery_results.get("long_term_picks", [])
    if long_term:
        blocks.append(
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "長期投資候補（3-12ヶ月）"},
            }
        )
        for r in long_term:
            blocks.append(_format_long_term_block(r))

    # Market condition
    market_cond = discovery_results.get("market_condition", "")
    if market_cond:
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*:crystal_ball: 市場環境評価*\n>{market_cond}",
                },
            }
        )

    # Footer
    blocks.append({"type": "divider"})
    blocks.append(
        {
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
        }
    )

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
    action = h.get("action", "")
    stop_loss = h.get("stop_loss", "")

    text = (
        f"*{h.get('name', '')} ({h.get('ticker', '')})* "
        f"{pred_emoji} *{pred}* | {conf_emoji} 信頼度: *{conf}*\n"
        f"{summary}\n"
    )
    if action:
        text += f"  :arrow_right: アクション: *{action}*\n"
    text += f"{reasons}\n"
    if stop_loss:
        text += f"  :octagonal_sign: 損切りライン: {stop_loss}\n"
    text += f"  :warning: リスク: {risk}"
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _format_discovery_block(r: dict) -> dict:
    """Format a single discovery recommendation into a Slack block."""
    pred_emoji = _PREDICTION_EMOJI.get(r.get("prediction", ""), ":question:")
    conf_emoji = _CONFIDENCE_EMOJI.get(r.get("confidence", ""), ":white_circle:")

    reasons = "\n".join(f"  - {r_}" for r_ in r.get("reasons", []))
    risk = r.get("risk_factor", "")
    entry = r.get("entry_strategy", "")
    expected = r.get("expected_move", "")
    entry_price = r.get("entry_price", "")
    stop_loss = r.get("stop_loss", "")
    target_price = r.get("target_price", "")

    text = (
        f"*#{r.get('rank', '?')} - {r.get('name', '')} ({r.get('ticker', '')})* "
        f"{pred_emoji} | {conf_emoji} 信頼度: *{r.get('confidence', '?')}*\n"
        f"予想: {expected}\n"
        f"{reasons}\n"
    )
    if entry_price:
        text += f"  :moneybag: エントリー価格: {entry_price}\n"
    if target_price:
        text += f"  :dart: 利確目標: {target_price}\n"
    if stop_loss:
        text += f"  :octagonal_sign: 損切り: {stop_loss}\n"
    if entry:
        text += f"  :bulb: 戦略: {entry}\n"
    text += f"  :warning: リスク: {risk}"
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _format_long_term_block(r: dict) -> dict:
    """Format a single long-term investment recommendation into a Slack block."""
    conf_emoji = _CONFIDENCE_EMOJI.get(r.get("confidence", ""), ":white_circle:")

    reasons = "\n".join(f"  - {r_}" for r_ in r.get("reasons", []))
    risk = r.get("risk_factor", "")
    thesis = r.get("investment_thesis", "")
    expected = r.get("expected_return", "")
    entry_zone = r.get("ideal_entry_zone", "")
    dividend = r.get("dividend_info", "")

    text = (
        f"*#{r.get('rank', '?')} - {r.get('name', '')} ({r.get('ticker', '')})*"
        f" {conf_emoji} 信頼度: *{r.get('confidence', '?')}*\n"
    )
    if thesis:
        text += f"{thesis}\n"
    if expected:
        text += f"  :chart_with_upwards_trend: 想定リターン: {expected}\n"
    text += f"{reasons}\n"
    if entry_zone:
        text += f"  :moneybag: 理想的な買い場: {entry_zone}\n"
    if dividend:
        text += f"  :money_with_wings: 配当: {dividend}\n"
    text += f"  :warning: リスク: {risk}"
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _send_blocks(bot_token: str, channel: str, blocks: list[dict]) -> bool:
    """Send blocks to Slack, splitting into multiple messages if needed."""
    if len(blocks) <= _MAX_BLOCKS_PER_MESSAGE:
        return _post_message(bot_token, channel, blocks, fallback_text="日本株AI分析レポート")

    # Split into chunks
    success = True
    for i in range(0, len(blocks), _MAX_BLOCKS_PER_MESSAGE):
        chunk = blocks[i : i + _MAX_BLOCKS_PER_MESSAGE]
        if not _post_message(bot_token, channel, chunk, fallback_text="日本株AI分析レポート"):
            success = False
    return success


def _send_error(bot_token: str, channel: str, message: str) -> bool:
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
    return _post_message(bot_token, channel, blocks, fallback_text="株分析エラー")


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


def _post_message(bot_token: str, channel: str, blocks: list[dict], *, fallback_text: str) -> bool:
    """POST to Slack chat.postMessage with bot token."""
    try:
        resp = requests.post(
            _SLACK_POST_URL,
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={"channel": channel, "text": fallback_text, "blocks": blocks},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.error("Slack HTTP failed: %d %s", resp.status_code, resp.text)
            return False
        body = resp.json()
        if not body.get("ok"):
            logger.error("Slack API error: %s", body.get("error", "unknown"))
            return False
        return True
    except requests.RequestException:
        logger.error("Slack request failed", exc_info=True)
        return False
