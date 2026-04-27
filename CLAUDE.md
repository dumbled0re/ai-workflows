# AI Workflows

GitHub ActionsとClaudeを活用した自動化ワークフロー集。各プロジェクトは独立。

## リポジトリ構成

```
stock_analyzer/          ← 日本株分析（独立プロジェクト）
  requirements.txt
  stocks.yml
  data/                  ← 永続データ（git管理）
  main.py
  ...

tech_catchup/            ← AI技術キャッチアップ（独立プロジェクト）
  requirements.txt
  data/                  ← 一時データ
  main.py
  sources.py

.github/workflows/       ← ワークフロー定義
  stock-analysis.yml     ← 株分析（毎日 朝8時/夕16時 JST）
  weekly-review.yml      ← 株：戦略レビュー（土曜 10時 JST）
  tech-catchup.yml       ← AIキャッチアップ（毎朝 7:30 JST）
```

## stock_analyzer

日本株の短期投資分析。テクニカル+ファンダメンタル+ニュース+信用残の多角分析。
自律改善ループ（予測記録→検証→フィードバック→戦略更新）付き。

### Claude Code Actionでの注意
- `stock_analyzer/data/investment_rules.json` を必ず読み、ルールに従うこと
- 推奨銘柄がなければ正直に「なし」と回答する

## tech_catchup

AI業界の最新動向を毎朝キャッチアップ。
Hacker News、GitHub Trending、arXivから情報収集してClaudeが要約。
