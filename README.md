# AI Workflows

GitHub Actions と Claude を活用した個人用自動化ワークフロー集。各プロジェクトは uv による独立仮想環境を持ち、それぞれ独立した cron で動く。

## プロジェクト一覧

| プロジェクト | 概要 | 実行頻度 | 通知先 |
|---|---|---|---|
| [`stock_analyzer/`](./stock_analyzer/) | 日本株の短期投資分析 (テクニカル + ファンダ + ニュース + 信用残)。自律改善ループ (予測記録 → 検証 → 戦略更新) 付き | 平日 8:00 / 16:00 JST、土曜 10:00 JST に週次レビュー | `SLACK_CHANNEL_STOCK` |
| [`tech_catchup/`](./tech_catchup/) | AI 業界のニュース・新リリースを Hacker News / GitHub Trending / arXiv / AI 企業公式ブログ / Reddit から収集して要約 | 毎朝 7:30 JST | `SLACK_CHANNEL_TECH` |
| [`point_sites/`](./point_sites/) | 日本のポイ活サイト + 抽選サイト自動化 (adapter 構造)。本番稼働中: moppy / hapitas / amefuri / pointtown / getmoney / fruitmail / warau / sugutama (Gmail / on-site inbox / endpoint poll の 3 系統) + 抽選 chanceit / fruitmail_lottery / dreammail。pointincome は JP geofence で extract-only (Gmail 抽出 → Slack に click URL 投稿 → user 手動 click)。Cookie rotation 永続化 + ID/PW login fallback + 加算検証 3 層 + Playwright DailyWizard | サイト別 (7:30〜21:45 JST に分散) | `SLACK_CHANNEL_<SITE>` |
| [`verify/`](./verify/) + [`scripts/pending_verify.py`](./scripts/pending_verify.py) | 後日の機械検証 (cron run の log grep / workflow trigger 等) を YAML + GitHub Issue で予約、毎朝自動実行。失敗時は Claude Code Action で auto-fix も試行 | 毎朝 7:30 JST | `SLACK_CHANNEL_VERIFY` |

> 設計・運用方針の詳細は [`CLAUDE.md`](./CLAUDE.md) と [`point_sites/CLAUDE.md`](./point_sites/CLAUDE.md) を参照。

## 共通の前提

- **Claude 認証**: API キーではなく `claude setup-token` で取得した OAuth トークン (`CLAUDE_CODE_OAUTH_TOKEN`) を GitHub Secrets に登録。Claude Pro/Max のサブスクリプション枠で動作
- **Claude GitHub App**: <https://github.com/apps/claude> をリポジトリにインストール済みであること
- **Slack 通知**: 全プロジェクト共通の Bot User OAuth Token (`SLACK_BOT_TOKEN` = `xoxb-...`) と、プロジェクト別 channel (`SLACK_CHANNEL_<PROJECT>`) を Secrets に登録。channel に bot を招待しておくこと。新プロジェクト追加時 Webhook は不要、`SLACK_CHANNEL_<NAME>` を 1 つ足すだけ
- **言語/環境**: Python 3.12+。**全プロジェクト `uv` + `pyproject.toml` + `uv.lock` で 1 仮想環境**。システム Python への直接 `pip install` は禁止
- **Gmail 認証**: 2026-05-17 に IMAP から **Gmail API + OAuth2 (readonly scope)** へ移行済。`GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_REFRESH_TOKEN` の 3 つを全 Gmail 依存ジョブで共有

## 必要な Secrets

| Secret 名 | 用途 |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | 全 Claude Code Action 共通の認証 (Pro/Max sub) |
| `SLACK_BOT_TOKEN` | Slack Bot User OAuth Token。全プロジェクト共有 |
| `SLACK_CHANNEL_<PROJECT>` | プロジェクト別 channel ID または `#name` (`_TECH` / `_STOCK` / `_VERIFY` / `_MOPPY` / `_HAPITAS` / `_POINTINCOME` / `_AMEFURI` / `_POINTTOWN` / `_GETMONEY` / `_FRUITMAIL` / `_WARAU` / `_SUGUTAMA` / `_CHANCEIT` / `_FRUITMAIL_LOTTERY` / `_DREAMMAIL`) |
| `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_REFRESH_TOKEN` | Gmail API OAuth2 (readonly)。`scripts/get_refresh_token.py` で取得 |
| `<SITE>_COOKIES` | point_sites 各サイト用 Cookie JSON (Cookie-Editor export) |
| `<SITE>_USER` / `<SITE>_PASS` | (任意) ID/PW login 自動化用。設定すると Cookie 失効時に Playwright で fresh login し cookie を merge back |

## ローカル実行

各プロジェクトディレクトリで `uv sync` → `uv run`:

```bash
# 株分析
cd stock_analyzer && uv sync
uv run python -m stock_analyzer.main prepare    # データ収集・指標計算
uv run python -m stock_analyzer.main notify     # Slack 通知

# AI ニュースキャッチアップ
cd tech_catchup && uv sync
uv run python -m tech_catchup.main gather       # 収集
uv run python -m tech_catchup.main notify       # 通知

# ポイ活サイト
cd point_sites && uv sync
uv run python -m point_sites.main run --site moppy
uv run python -m point_sites.main gmail_dump --site moppy --query 'from:moppy.jp newer_than:7d'  # debug
```

## GitHub Actions

`.github/workflows/` 配下の主要ファイル:

### 株 / ニュース / 検証

| ファイル | 用途 | スケジュール (JST) |
|---|---|---|
| `stock-analysis.yml` | 株分析 (保有銘柄予測 + 有望株発掘) | 平日 8:00 / 16:00 |
| `weekly-review.yml` | 株戦略の週次レビュー | 土 10:00 |
| `tech-catchup.yml` | AI ニュースキャッチアップ | 毎朝 7:30 |
| `pending-verify.yml` | `verify/**/*.yml` の機械検証を順次実行、結果を Slack + issue に投稿 | 毎朝 7:30 |
| `ci.yml` / `point_sites-ci.yml` | 全体 / point_sites の ruff + mypy + pytest | PR 時 |

### point_sites (ポイ活 / 抽選)

`_site-runner.yml` (reusable workflow) を各サイトの薄いラッパーが呼ぶ構造。`workflow_dispatch` で `extract_links` / `inspect_url` / `discover` / `force_fresh_cookies` / `force_password_login_test` / `gmail_dump_query` 等の debug input を持つ。

| ファイル | 種別 | スケジュール (JST) | 備考 |
|---|---|---|---|
| `moppy.yml` | ポイ活 (Gmail) | 7:30 | |
| `pointincome.yml` | ポイ活 (Gmail extract-only) | 8:15 | JP geofence で auto-click 不可、URL 抽出 → Slack → 手動 click 運用 |
| `chanceit.yml` | 抽選 (Gmail + onsite) | 8:00 | easy-entry 系の自動応募 |
| `dreammail.yml` | ポイ活 + 抽選 | 8:45 | gacha / precam wizard |
| `amefuri.yml` | ポイ活 (endpoint poll) | 9:15 | SPA login bonus は Playwright wizard |
| `pointtown.yml` | ポイ活 (onsite inbox) | 9:30 + 21:30 (keepalive) | |
| `getmoney.yml` | ポイ活 (onsite inbox) | 9:45 + 21:45 (keepalive) | game1000 line=1 のみ |
| `fruitmail_lottery.yml` | 抽選 | 9:30 | 5 prize categories 自動応募 |
| `hapitas.yml` | ポイ活 (Gmail) | 11:30 | 宝くじ交換券 daily wizard |
| `fruitmail.yml` | ポイ活 (Gmail) | 15:00 | スロット / ビンゴ / login bonus / CM 視聴 wizard |
| `warau.yml` | ポイ活 (Gmail) | 18:30 | |
| `sugutama.yml` | ポイ活 (Gmail) | 21:30 | |
| `gendama.yml` | (休止) | — | 180 日休眠条件 + scaffold のみ。cron disabled |

`_site-runner.yml` の `timeout-minutes` は 15 分共通。SPA 待機 / 大量 wizard で超過した場合は wizard 削減か個別 timeout 上げで対応。

## アーキテクチャ共通パターン

### Claude 分析型 (stock_analyzer / tech_catchup)

```
[Phase 1: Python]   データ収集 → JSON 出力
       ↓
[Phase 2: Claude]   JSON 読み込み → AI 分析 → 結果 JSON 出力
                    (GitHub Actions では claude-code-action が実行)
       ↓
[Phase 3: Python]   結果を Slack 通知
```

### 純 Python 自動化型 (point_sites / pending-verify)

Claude を経由せず、Python のみで完結 (クリックメール処理 / Playwright DailyWizard / 検証 cron 等)。

### 自律ワークフローの 3 層 (point_sites の必須前提)

| 層 | 役割 | 実装 |
|---|---|---|
| 検知 (verification) | 副作用が本当に起きたかを観測 | balance scrape / click HTTP status |
| 記録 (telemetry) | 結果を JSONL で時系列に永続化 | `OutcomeTracker` |
| 判断 + 通知 (escalation) | 連続 N 回の閾値割れで具体的アクション付きの Slack 警告 | credit-ratio / HTTP-failure / balance-stagnation の degradation alert |

詳細は [`CLAUDE.md`](./CLAUDE.md) と [`point_sites/CLAUDE.md`](./point_sites/CLAUDE.md)。

## コスト

- **Claude**: Pro/Max サブスクリプション枠内 (API 課金なし)
- **GitHub Actions**: public repo 化済で Linux runner は完全無料 (private 時代の 2,000 min/月 制限解消)
- **API**: yfinance (株) / Gmail API / 各種 RSS / 公式ブログ feed はすべて無料層

## ライセンス・免責

各プロジェクトの実装内容は個人用の参考。投資判断や金銭取引に関わる動作はすべて自己責任。`point_sites` は対象サイト各社の規約により自動アクセスがリスクを伴うため、各サイトの TOS を確認の上で利用してください。
