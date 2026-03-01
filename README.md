# JP Stock Analyzer

Claude AIを活用した日本株分析システム。GitHub Actionsで毎日自動実行し、保有銘柄の上昇/下落予測と有望銘柄の発掘結果をSlackに通知します。

## Features

- **保有銘柄分析**: テクニカル指標に基づくUP/DOWN予測（信頼度・理由付き）
- **有望銘柄発掘**: 日経225からAIが有望株をスクリーニング
- **Slack通知**: Block Kitによるリッチフォーマット通知
- **自動実行**: 毎日2回（朝8:00 / 夕16:00 JST）
- **休場日対応**: 日本の祝日・週末は自動スキップ

## Setup

### 1. GitHub Secretsの設定

リポジトリの Settings > Secrets and variables > Actions で以下を設定:

| Secret | 説明 |
|--------|------|
| `ANTHROPIC_API_KEY` | [Anthropic Console](https://console.anthropic.com/) で取得 |
| `SLACK_WEBHOOK_URL` | [Slack App](https://api.slack.com/messaging/webhooks) で取得 |

### 2. 保有銘柄の設定

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
  claude_model: "claude-sonnet-4-5-20250929"
```

### 3. 手動実行

GitHub Actions > Japanese Stock Analysis > Run workflow で手動実行できます。

### Local Development

```bash
# 依存パッケージのインストール
pip install -r requirements.txt

# 環境変数の設定
export ANTHROPIC_API_KEY="your-key"
export SLACK_WEBHOOK_URL="your-webhook-url"

# 実行
python -m src.main
```

## Architecture

```
stocks.yml → config_loader → data_fetcher → technical_indicators
                                                    ↓
                              stock_screener (Nikkei 225)
                                                    ↓
                              ai_analyzer (Claude API × 2 calls)
                                                    ↓
                              slack_notifier → Slack
```

## Cost

- **Claude API**: ~$0.08/日（Sonnet × 2回/実行 × 2回/日）
- **GitHub Actions**: 無料枠内（~3分/実行）

## Disclaimer

本ツールはAIによる分析を情報提供目的で提供するものであり、投資助言ではありません。投資判断はご自身の責任で行ってください。
