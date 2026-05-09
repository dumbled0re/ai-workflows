# AI Workflows

GitHub Actions と Claude を活用した個人用自動化ワークフロー集。各プロジェクトは独立した環境を持ち、それぞれ独立した cron で動く。

## プロジェクト一覧

| プロジェクト | 概要 | 実行頻度 | 通知方式 |
|---|---|---|---|
| [`stock_analyzer/`](./stock_analyzer/) | 日本株の短期投資分析（テクニカル + ファンダ + ニュース + 信用残）。自律改善ループ（予測記録 → 検証 → 戦略更新）付き | 毎日 8:00 / 16:00 JST、土曜 10:00 JST にレビュー | Bot Token + `SLACK_CHANNEL_STOCK` |
| [`tech_catchup/`](./tech_catchup/) | AI 業界のニュース・新リリースを Hacker News / GitHub Trending / arXiv / AI 企業公式ブログから収集して要約 | 毎朝 7:30 JST | Bot Token + `SLACK_CHANNEL_TECH` |
| [`point_sites/`](./point_sites/) | 日本のポイ活サイト自動化（adapter 構造）。Moppy の「クリックでポイント」メール自動クリック等。Cookie rotation 永続化 + 加算検証付き | 毎日 8:00 JST | Bot Token + `SLACK_CHANNEL_MOPPY` 等 |
| [`todo/`](./todo/) | 個人 TODO リスト。Claude Code の `todo` skill で `todos.md` を編集し、毎朝 Slack に未完了タスクを通知 | 毎朝 9:00 JST | Bot Token + `SLACK_CHANNEL_TODO` |

> 詳細な設計・運用方針は [`CLAUDE.md`](./CLAUDE.md) と各プロジェクト内の `DESIGN.md` を参照。

## 共通の前提

- **Claude 認証**: API キーではなく `claude setup-token` で取得した OAuth トークン（`CLAUDE_CODE_OAUTH_TOKEN`）を GitHub Secrets に登録。Claude Pro/Max のサブスクリプション枠で動作。
- **Claude GitHub App**: <https://github.com/apps/claude> をリポジトリにインストール済みであること。
- **Slack 通知**: 各プロジェクト専用チャンネルの Incoming Webhook URL を Secrets に登録。
- **言語/環境**: Python 3.12+。新規プロジェクトは `uv` + `pyproject.toml` で **1プロジェクト1仮想環境**。システムPythonへの直接 `pip install` は禁止（`stock_analyzer` / `tech_catchup` は移行待ち）。

## 必要な Secrets

| Secret 名 | 用途 |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | 全 Claude Code Action 共通の認証 |
| `SLACK_BOT_TOKEN` | Slack Bot User OAuth Token (`xoxb-...`)。全プロジェクト共有 |
| `SLACK_CHANNEL_TODO` | TODO 通知先チャンネル |
| `SLACK_CHANNEL_TECH` | AI ニュースの通知先チャンネル |
| `SLACK_CHANNEL_MOPPY` | モッピー自動クリックの通知先チャンネル |
| `SLACK_CHANNEL_STOCK` | 株分析の通知先チャンネル |
| `MOPPY_IMAP_USER` / `MOPPY_IMAP_PASS` | モッピーメール受信用 IMAP 認証（Gmail App Password など） |

## ローカル実行

各プロジェクトに直接入って実行：

```bash
# 株分析（uv 管理）
cd stock_analyzer && uv sync
uv run python -m stock_analyzer.main prepare    # データ収集・指標計算
uv run python -m stock_analyzer.main notify     # Slack 通知

# AI ニュースキャッチアップ（uv 管理）
cd tech_catchup && uv sync
uv run python -m tech_catchup.main gather       # ニュース収集
uv run python -m tech_catchup.main notify       # Slack 通知

# ポイ活サイト自動クリック（uv 管理）
cd point_sites && uv sync
uv run python -m point_sites.main run --site moppy

# TODO リマインダー（uv 管理）
cd todo && uv sync
uv run python -m todo.main notify --dry-run     # ローカル確認
uv run python -m todo.main notify               # Slack 通知
```

## GitHub Actions

`.github/workflows/` 配下：

| ファイル | 用途 | スケジュール |
|---|---|---|
| `stock-analysis.yml` | 株分析（保有銘柄予測 + 有望株発掘） | 毎日 朝/夕 |
| `weekly-review.yml` | 株戦略の週次レビュー | 土曜朝 |
| `tech-catchup.yml` | AI ニュースキャッチアップ | 毎朝 |
| `moppy.yml` | モッピーメール自動クリック (point_sites の moppy adapter) | 毎日 |
| `point_sites-ci.yml` | point_sites の mypy | PR 時 |
| `todo.yml` | TODO リマインダーを Slack に通知 | 毎朝 9:00 JST |

手動実行は GitHub Actions タブから `Run workflow` で。

## アーキテクチャ共通パターン

すべてのプロジェクトが同じ 3 フェーズ構成：

```
[Phase 1: Python]   データ収集 → JSON 出力
       ↓
[Phase 2: Claude]   JSON 読み込み → AI 分析 → 結果 JSON 出力
                    （GitHub Actions 上では claude-code-action が実行）
       ↓
[Phase 3: Python]   結果を Slack 通知
```

Phase 1/3 はプロジェクト固有のロジック、Phase 2 は Claude による分析。Python と Claude の責務を分離することで、Python 側はテスト可能、Claude 側はプロンプトのみ管理可能。

## コスト

- **Claude**: サブスクリプション内（Pro/Max プラン、API 課金なし）
- **GitHub Actions**: 無料枠内（各実行 ~5 分、月数百分のオーダー）
- **API**: yfinance（株）、IMAP（メール）、各種 RSS / API はすべて無料層

## ライセンス・免責

各プロジェクトの実装内容は個人用の参考用。投資判断や金銭的取引に関わる動作はすべて自己責任で運用してください。`point_sites` は対象ポイ活サイト各社の規約により自動アクセスがリスクを伴う点に留意。
