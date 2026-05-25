"""Slack chat.postMessage notifier (bot-token based).

Most outputs (summary / dry-run / failure) are heavily redacted: subjects
truncated to 5 chars, URLs reduced to host only. Body HTML is never forwarded.

The exception is ``send_extract_links`` which intentionally posts the full
URLs so the user can click them manually in their browser. Use this only for
the user's own private channel.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Iterable
from typing import TYPE_CHECKING

from .models import ClickResult, RunSummary
from .redaction import host_only

if TYPE_CHECKING:
    from .outcome_tracker import DegradationAlert

logger = logging.getLogger(__name__)

SLACK_POST_URL = "https://slack.com/api/chat.postMessage"

# Slack chat.postMessage rejects payloads with more than 50 blocks. We use a
# margin of 3 chrome blocks (header, context, divider), so 47 sections is the
# theoretical max. We pick 45 to leave a small safety buffer in case the format
# is extended later.
_EXTRACT_MAX_SECTIONS_PER_PAGE = 45


def _slack_escape(text: str) -> str:
    """Escape characters with mrkdwn meaning (`<`, `>`, `&`)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_verification(
    estimated_total_pt: int,
    balance_before: int | None,
    balance_after: int | None,
    prior_balance_after: int | None = None,
) -> str | None:
    """Render the post-click balance verification line.

    Returns ``None`` when there's nothing meaningful to say (e.g. nothing
    was clicked and we never tried to read the balance). The summary
    looks weird if we add a "✓ 加算確認" line on a no-op run.

    ``prior_balance_after`` is the ``balance_after`` from the most recent
    previous click run (read from outcomes.jsonl). When supplied AND it
    differs from this run's ``balance_before``, we render an inter-run
    delta — that's the user's clearest signal that points were credited
    between cron runs (e.g. delayed pointsite crediting or other manual
    activity), since a within-run Δ of 0 looks alarming on its own when
    balance is actually growing day-over-day.
    """
    has_both = balance_before is not None and balance_after is not None
    if not has_both:
        if estimated_total_pt > 0:
            return "⚠ 加算確認: 残高取得失敗（balance.py のパターン更新が必要かも）"
        return None
    assert balance_before is not None and balance_after is not None  # for type checker
    delta = balance_after - balance_before

    inter_suffix = ""
    if prior_balance_after is not None and prior_balance_after != balance_before:
        inter_delta = balance_before - prior_balance_after
        inter_suffix = f" / 前回比 {inter_delta:+}pt ({prior_balance_after}→{balance_before})"

    if estimated_total_pt <= 0:
        return f"✓ 残高: {balance_before}→{balance_after} (Δ{delta:+}pt, 推定なし){inter_suffix}"
    ratio = delta / estimated_total_pt
    flag = " ⚠加算が想定より少ない" if ratio < 0.5 else ""
    return (
        f"✓ 加算確認: {balance_before}→{balance_after} "
        f"(Δ{delta:+}pt / 推定{estimated_total_pt}pt, 比率 {ratio:.0%}){flag}{inter_suffix}"
    )


class Notifier:
    def __init__(self, bot_token: str, channel: str) -> None:
        self._token = bot_token
        self._channel = channel

    def _post(self, payload: dict[str, object]) -> bool:
        """POST to Slack chat.postMessage. Returns True on `ok: true`.

        Most callers fire-and-forget (failures are logged), but
        ``send_extract_links`` is the sole channel for that day's links so it
        surfaces the boolean upward and the run exits non-zero on failure.
        """
        body: dict[str, object] = {**payload, "channel": self._channel}
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            SLACK_POST_URL,
            data=data,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self._token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                response = json.loads(resp.read().decode("utf-8"))
                if not response.get("ok"):
                    logger.warning("slack notify failed: %s", response.get("error", "unknown"))
                    return False
                return True
        except (urllib.error.URLError, TimeoutError) as exc:
            logger.warning("slack notify failed: %s", exc)
            return False

    def send_lottery_summary(
        self,
        site_label: str,
        started_at: object,  # datetime, kept loose to avoid import cycles
        finished_at: object,
        wizard_results: list[dict[str, object]],
    ) -> None:
        """Slack 通知: 抽選専用 「応募状況一覧」 format.

        ``wizard_results`` 各 entry: ``{name, url, title, success}``.
        ``success=True`` は **wizard.success_url_pattern / success_text_marker
        による server-side 受理 verify 済み**を意味する (DailyWizard 側で
        verified=True のときだけ立つ)。verify 失敗 (form blocked / PII
        check fail / silent selector miss) は ``success=False`` で
        「未確定」section に振り分ける。

        2026-05-25 user 指摘: wizard 完走を「応募成功」と直結させた false
        positive 通知が大事故。本 method 単独では真偽判定できないので、
        前段の wizard executor が verify した上で success フラグを立てる
        前提。title が空なら URL から prize id (例: detail/710054) を抽出
        して fallback 表示。
        """
        verified = [r for r in wizard_results if r.get("success")]
        unconfirmed = [r for r in wizard_results if not r.get("success")]
        lines: list[str] = []
        # datetime formatting — guarded for type narrowing
        from datetime import datetime as _dt

        if isinstance(started_at, _dt) and isinstance(finished_at, _dt):
            lines.append(f"【{site_label} 抽選応募 run】 {started_at:%Y-%m-%d %H:%M}")
            lines.append(
                f"✅ 応募確認済: {len(verified)} 件 / ⚠️ 未確定: {len(unconfirmed)} 件 "
                f"/ 処理時間 {(finished_at - started_at).total_seconds():.0f}秒"
            )
        else:
            lines.append(f"【{site_label} 抽選応募 run】")
            lines.append(f"✅ 応募確認済: {len(verified)} 件 / ⚠️ 未確定: {len(unconfirmed)} 件")
        if verified:
            lines.append("")
            lines.append("--- 応募確認済 (server-side verify pass) ---")
            for entry in verified:
                url = str(entry.get("url") or "")
                title = str(entry.get("title") or "").strip()
                if not title:
                    # Fallback: extract /detail/<id>/ as identifier
                    import re as _re

                    m = _re.search(r"/detail/(\d+)", url)
                    title = f"(prize id {m.group(1)})" if m else "(タイトル取得失敗)"
                lines.append(f"✅ {title}")
                if url:
                    lines.append(f"   {url}")
        if unconfirmed:
            lines.append("")
            lines.append(
                "--- 未確定 (click は完走、success_url / success_text 不一致 — "
                "実応募が成立したか要 mypage 応募履歴 check) ---"
            )
            for entry in unconfirmed[:10]:
                url = str(entry.get("url") or "")
                title = str(entry.get("title") or "") or "(タイトル不明)"
                lines.append(f"⚠️ {title}")
                if url:
                    lines.append(f"   {url}")
        self._post({"text": "\n".join(lines)})

    def send_summary(
        self,
        summary: RunSummary,
        results: list[ClickResult],
        estimated_total_pt: int,
        *,
        balance_before: int | None = None,
        balance_after: int | None = None,
        prior_balance_after: int | None = None,
        degradation: DegradationAlert | None = None,
    ) -> None:
        failed = [r for r in results if r.final_status != "success"]
        lines = [
            f"[point_sites] {summary.started_at:%Y-%m-%d %H:%M} 完了",
            f"✅ 成功: {summary.success_count}件 / 推定獲得: {estimated_total_pt}pt",
        ]
        verification = _format_verification(estimated_total_pt, balance_before, balance_after, prior_balance_after)
        if verification:
            lines.append(verification)
        lines.extend(
            [
                f"❌ 失敗: {summary.failure_count}件",
                f"⚠ パース失敗: {len(summary.parse_failures)}件",
                f"⚠ 異常メール: {len(summary.anomaly_messages)}件",
                f"処理時間: {(summary.finished_at - summary.started_at).total_seconds():.0f}秒",
            ]
        )
        if failed:
            lines.append("--- 失敗詳細（host のみ） ---")
            for r in failed[:10]:
                host = r.final_host or host_only(str(r.candidate.url))
                lines.append(f"  {host} - {r.final_status} (http={r.http_status})")
        if degradation is not None:
            lines.append("--- 🚨 自動検知: ポイント加算がほぼ止まっています ---")
            lines.append(f"直近 {degradation.runs_inspected} 回の加算比率中央値が {degradation.median_ratio:.0%}")
            lines.append(f"  → {degradation.suggestion}")
        self._post({"text": "\n".join(lines)})

    def send_dry_run(
        self,
        candidates_by_message: list[tuple[str, str, list[str]]],
    ) -> None:
        """``candidates_by_message`` items: (msg_id, redacted_subject, [redacted_urls])."""
        lines = [f"[point_sites] 🧪 dry-run: {len(candidates_by_message)}件のメールから候補抽出"]
        for msg_id, subject, urls in candidates_by_message[:20]:
            lines.append(f"  msg={msg_id[:10]} subject={subject!r} urls={len(urls)}")
            for u in urls[:5]:
                lines.append(f"    - {u}")
        self._post({"text": "\n".join(lines)})

    def build_extract_blocks(
        self,
        candidates_by_message: list[tuple[str, str, list[str]]],
        date_label: str,
    ) -> list[dict[str, object]]:
        """Build Block Kit blocks for ``send_extract_links``.

        Pulled out so it can be unit-tested without hitting Slack.

        ``candidates_by_message`` items: ``(msg_id, full_subject, [full_urls])``.
        URLs are NOT redacted — the user clicks them manually in their browser.
        """
        total_urls = sum(len(urls) for _, _, urls in candidates_by_message)
        header_text = f"📬 モッピー クリックリンク ({total_urls}件)"
        blocks: list[dict[str, object]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": header_text, "emoji": True},
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"*{date_label}*  ·  {len(candidates_by_message)}メール"
                            f"  ·  ⚠ ログイン状態のブラウザでクリックしてください"
                        ),
                    }
                ],
            },
            {"type": "divider"},
        ]
        for _msg_id, subject, urls in candidates_by_message:
            if not urls:
                continue
            lines = [f"*📧 {_slack_escape(subject)}*"]
            for i, u in enumerate(urls, 1):
                lines.append(f"  {i}. <{u}|🔗 click {i}>")
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
        return blocks

    def send_extract_links(
        self,
        candidates_by_message: list[tuple[str, str, list[str]]],
        date_label: str,
    ) -> bool:
        """Post full clickable URLs to Slack so the user can click manually.

        Used during the cookie-gated transition (see DESIGN.md "ログイン（Cookie 注入）").
        Does NOT redact URLs because the recipient channel is private and the
        user needs to click them. Subject is also unredacted to help triage.

        Slack's chat.postMessage rejects payloads with more than 50 blocks
        (`invalid_blocks`). Each ``build_extract_blocks`` call produces 3 chrome
        blocks + one section per email, so we chunk emails into pages of at
        most ``_EXTRACT_MAX_SECTIONS_PER_PAGE`` to stay safely under the limit.

        Returns ``True`` only if every chunked message was delivered. The
        caller (`cmd_run` in extract mode) exits non-zero on ``False`` so that
        a silent Slack failure surfaces as a workflow error rather than losing
        the day's links.
        """
        if not candidates_by_message:
            return self._post({"text": "[point_sites] 📬 抽出対象のメールはありませんでした"})
        # Skip messages with no URLs early — they don't render a section anyway,
        # and counting them toward page size wastes capacity.
        non_empty = [m for m in candidates_by_message if m[2]]
        if not non_empty:
            return self._post({"text": "[point_sites] 📬 抽出対象のメールはありませんでした"})

        total_urls = sum(len(urls) for _, _, urls in non_empty)
        pages = [
            non_empty[i : i + _EXTRACT_MAX_SECTIONS_PER_PAGE]
            for i in range(0, len(non_empty), _EXTRACT_MAX_SECTIONS_PER_PAGE)
        ]
        all_ok = True
        for page_idx, page in enumerate(pages, start=1):
            page_label = date_label if len(pages) == 1 else f"{date_label} ({page_idx}/{len(pages)})"
            blocks = self.build_extract_blocks(page, page_label)
            ok = self._post(
                {
                    "text": f"[point_sites] 📬 {total_urls}件のクリックリンク",
                    "blocks": blocks,
                    # CRITICAL: disable Slack's auto-unfurl. Otherwise Slackbot
                    # would fetch each click URL anonymously to render previews,
                    # which Moppy may treat as a (non-credited) click and could
                    # consume the URL before the user opens it in their
                    # logged-in browser.
                    "unfurl_links": False,
                    "unfurl_media": False,
                }
            )
            if not ok:
                all_ok = False
        return all_ok

    def send_parse_failure(self, message_ids: Iterable[str], reason: str) -> None:
        ids = list(message_ids)
        if not ids:
            return
        lines = [
            f"[point_sites] ⚠ パース失敗 {len(ids)}件 ({reason})",
            "  msg_id (先頭のみ): " + ", ".join(mid[:10] for mid in ids[:5]),
            "  → ローカルで該当 msg_id を fixture 化して parser 修正してください",
        ]
        self._post({"text": "\n".join(lines)})

    def send_auth_error(self, message: str) -> None:
        self._post({"text": f"[point_sites] 🚨 認証エラー: {message}"})

    def send_config_error(self, message: str) -> None:
        self._post({"text": f"[point_sites] 🚨 設定エラー: {message}"})
