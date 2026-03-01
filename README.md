# JP Stock Analyzer

Claude AIを活用した日本株分析システム。GitHub Actionsで毎日自動実行し、保有銘柄の上昇/下落予測と有望銘柄の発掘結果をSlackに通知します。

## Features

- **保有銘柄分析**: テクニカル指標に基づくUP/DOWN予測（信頼度・理由付き）
- **有望銘柄発掘**: 日経225からAIが有望株をスクリーニング
- **Slack通知**: Block Kitによるリッチフォーマット通知
- **自動実行**: 毎日2回（朝8:00 / 夕16:00 JST）
- **休場日対応**: 日本の祝日・週末は自動スキップ
- **サブスクリプション対応**: Claude Pro/MaxのOAuthトークンで動作（APIキー不要）

## Setup

### 1. Claude OAuthトークンの取得

ローカル環境で以下を実行:

```bash
claude setup-token
```

生成されたトークンをコピーしてください。

### 2. GitHub Secretsの設定

リポジトリの Settings > Secrets and variables > Actions で以下を設定:

| Secret | 説明 |
|--------|------|
| `CLAUDE_CODE_OAUTH_TOKEN` | `claude setup-token` で取得したトークン |
| `SLACK_WEBHOOK_URL` | [Slack App](https://api.slack.com/messaging/webhooks) で取得 |

### 3. Claude GitHub Appのインストール

https://github.com/apps/claude からリポジトリにClaude GitHub Appをインストールしてください。

### 4. 保有銘柄の設定

`stocks.yml` を編集して保有銘柄を設定:

```yaml
holdings:
  - ticker: "7203.T"    # Yahoo Finance形式（.T = 東証）
    name: "トヨタ自動車"
    shares: 100
    avg_cost: 2500       # 省略可

settings:
  history_days: 90       # 分析対象の過去日数
  discovery_top_n: 5     # おすすめ銘柄の数
  screener_pool_size: 50 # スクリーニング候補数
```

### 5. 手動実行

GitHub Actions > Japanese Stock Analysis > Run workflow で手動実行できます。

## Architecture

```
Phase 1 (Python):
  stocks.yml → config_loader → data_fetcher → technical_indicators
                                                      ↓
                                stock_screener (Nikkei 225)
                                                      ↓
                                ai_analyzer.prepare_prompts()
                                                      ↓
                                data/analysis_input.json

Phase 2 (Claude Code Action):
  data/analysis_input.json → Claude AI分析
                                  ↓
  data/holdings_result.json + data/discovery_result.json

Phase 3 (Python):
  data/*_result.json → slack_notifier → Slack通知
```

## Cost

- **Claude**: サブスクリプション内（Pro/Maxプラン）
- **GitHub Actions**: 無料枠内（~5分/実行）

## Local Development

```bash
# 依存パッケージのインストール
pip install -r requirements.txt

# Phase 1: データ準備
export SLACK_WEBHOOK_URL="your-webhook-url"
python -m src.main prepare

# Phase 2: Claude分析（GitHub Actions上ではclaude-code-actionが実行）
# ローカルでは手動でdata/holdings_result.jsonとdata/discovery_result.jsonを作成

# Phase 3: Slack通知
python -m src.main notify
```

## Disclaimer

本ツールはAIによる分析を情報提供目的で提供するものであり、投資助言ではありません。投資判断はご自身の責任で行ってください。
