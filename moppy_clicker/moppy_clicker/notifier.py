"""Slack incoming-webhook notifier.

Output is heavily redacted: subjects truncated to 5 chars, URLs reduced to host
only. Body HTML is never forwarded to Slack.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Iterable

from .models import ClickResult, RunSummary
from .redaction import host_only

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, webhook_url: str) -> None:
        self._webhook = webhook_url

    def _post(self, payload: dict[str, str]) -> None:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._webhook,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            logger.warning("slack notify failed: %s", exc)

    def send_summary(
        self,
        summary: RunSummary,
        results: list[ClickResult],
        estimated_total_pt: int,
    ) -> None:
        failed = [r for r in results if r.final_status != "success"]
        lines = [
            f"[moppy_clicker] {summary.started_at:%Y-%m-%d %H:%M} 完了",
            f"✅ 成功: {summary.success_count}件 / 推定獲得: {estimated_total_pt}pt",
            f"❌ 失敗: {summary.failure_count}件",
            f"⚠ パース失敗: {len(summary.parse_failures)}件",
            f"⚠ 異常メール: {len(summary.anomaly_messages)}件",
            f"処理時間: {(summary.finished_at - summary.started_at).total_seconds():.0f}秒",
        ]
        if failed:
            lines.append("--- 失敗詳細（host のみ） ---")
            for r in failed[:10]:
                host = r.final_host or host_only(str(r.candidate.url))
                lines.append(f"  {host} - {r.final_status} (http={r.http_status})")
        self._post({"text": "\n".join(lines)})

    def send_dry_run(
        self,
        candidates_by_message: list[tuple[str, str, list[str]]],
    ) -> None:
        """``candidates_by_message`` items: (msg_id, redacted_subject, [redacted_urls])."""
        lines = [f"[moppy_clicker] 🧪 dry-run: {len(candidates_by_message)}件のメールから候補抽出"]
        for msg_id, subject, urls in candidates_by_message[:20]:
            lines.append(f"  msg={msg_id[:10]} subject={subject!r} urls={len(urls)}")
            for u in urls[:5]:
                lines.append(f"    - {u}")
        self._post({"text": "\n".join(lines)})

    def send_parse_failure(self, message_ids: Iterable[str], reason: str) -> None:
        ids = list(message_ids)
        if not ids:
            return
        lines = [
            f"[moppy_clicker] ⚠ パース失敗 {len(ids)}件 ({reason})",
            "  msg_id (先頭のみ): " + ", ".join(mid[:10] for mid in ids[:5]),
            "  → ローカルで該当 msg_id を fixture 化して parser 修正してください",
        ]
        self._post({"text": "\n".join(lines)})

    def send_auth_error(self, message: str) -> None:
        self._post({"text": f"[moppy_clicker] 🚨 認証エラー: {message}"})

    def send_config_error(self, message: str) -> None:
        self._post({"text": f"[moppy_clicker] 🚨 設定エラー: {message}"})
