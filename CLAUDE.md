# AI Workflows

GitHub ActionsとClaudeを活用した自動化ワークフロー集。

## ワークフロー一覧

### 1. 日本株分析 (`stock_analyzer/`)
日本株の短期投資分析を自動化。毎日2回分析してSlack通知。自律改善ループ付き。

```
朝8時: データ取得 → スクリーニング → Claude分析 → Slack通知
夕16時: データ取得 → スクリーニング → Claude分析 → Slack通知 → 戦略レビュー → 学習
```

#### モジュール
- `stock_analyzer/data_fetcher.py` - yfinanceから価格・ファンダメンタルデータ取得
- `stock_analyzer/technical_indicators.py` - テクニカル指標計算 + スクリーニングスコア
- `stock_analyzer/stock_screener.py` - Nikkei225+JPX400のスクリーニング
- `stock_analyzer/market_context.py` - 市場インデックス・レジーム判定
- `stock_analyzer/news_fetcher.py` - kabutan.jpからニュース・信用残取得
- `stock_analyzer/sector_analysis.py` - セクター内相対評価
- `stock_analyzer/ai_analyzer.py` - Claudeのプロンプト構築
- `stock_analyzer/performance_tracker.py` - 予測追跡・検証
- `stock_analyzer/strategy_learner.py` - 戦略メモ・重み管理
- `stock_analyzer/slack_notifier.py` - Slack通知

#### データファイル (data/)
- `predictions_history.json` - 過去の予測履歴（永続・git管理）
- `strategy_notes.json` - 蓄積された戦略メモ（永続・git管理）
- `screening_weights.json` - スクリーニング重み設定（永続・git管理）
- `investment_rules.json` - 投資ルール・制約条件

#### 設定
- `stocks.yml` - 保有銘柄・設定
- `requirements.txt` - Python依存パッケージ

## Claude Code Actionでの分析時の注意

- `data/investment_rules.json` を必ず読み、ルールに従って分析すること
- `data/strategy_notes.json` があれば読み、過去の教訓を考慮すること
- 出力は指定されたJSON形式のみ。余分なテキストは含めない
- 推奨銘柄がなければ正直に「なし」と回答する。無理に選ばない
