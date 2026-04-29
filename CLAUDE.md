# AI Workflows

GitHub ActionsとClaudeを活用した自動化ワークフロー集。各プロジェクトは独立した環境を持つ。

## リポジトリ構成

```
ai-workflows/
├── stock_analyzer/         ← 日本株分析（独立プロジェクト・本番稼働中）
│   ├── requirements.txt
│   ├── config/stocks.yml   ← ユーザー設定（保有銘柄）
│   ├── data/               ← 永続データ（git管理）
│   │   ├── investment_rules.json
│   │   ├── predictions_history.json
│   │   ├── strategy_notes.json
│   │   └── screening_weights.json
│   └── *.py (main, ai_analyzer, data_fetcher, etc.)
│
├── tech_catchup/           ← AI技術キャッチアップ（独立プロジェクト・本番稼働中）
│   ├── requirements.txt
│   ├── main.py
│   └── sources.py
│
├── youtube_factory/        ← YouTube動画自動生成（uv管理・実装途中）
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── .venv/              ← uvが管理する仮想環境（gitignore）
│   ├── DESIGN.md
│   ├── IMPROVEMENT_PLAN.md
│   ├── main.py
│   ├── voice_synthesizer.py    (Edge TTS + WordBoundary)
│   ├── audio_processor.py      (master.wav + loudnorm)
│   ├── subtitle_generator.py   (ASS字幕)
│   ├── image_generator.py      (Pillowテキストカード)
│   ├── video_assembler.py      (ffmpeg xfade)
│   ├── script_validator.py     (Pydantic)
│   ├── data/               ← 一時データ（gitignore）
│   └── assets/             ← フォント・BGM置き場
│
├── .github/workflows/      ← ワークフロー定義
│   ├── stock-analysis.yml  ← 株分析（毎日 朝8時/夕16時 JST）
│   ├── weekly-review.yml   ← 戦略レビュー（土曜10時 JST）
│   └── tech-catchup.yml    ← AIキャッチアップ（毎朝7:30 JST）
│   ※ youtube-factory.yml はまだ未作成
│
├── CLAUDE.md
└── README.md
```

## 各プロジェクト

### stock_analyzer

日本株の短期投資分析。テクニカル+ファンダメンタル+ニュース+信用残の多角分析。
自律改善ループ（予測記録→検証→フィードバック→戦略更新）付き。

**実行:**
```bash
python -m stock_analyzer.main prepare   # データ収集・指標計算
python -m stock_analyzer.main notify    # Slack通知
python -m stock_analyzer.main review    # 戦略レビュー
python -m stock_analyzer.main apply-review
```

**Slack通知:** `SLACK_WEBHOOK_URL`（株専用チャンネル）

#### Claude Code Actionでの分析時の注意
- `stock_analyzer/data/investment_rules.json` を必ず読み、ルールに従うこと
- 推奨銘柄がなければ正直に「なし」と回答する

### tech_catchup

AI業界の最新動向を毎朝キャッチアップ。
Hacker News、GitHub Trending、arXiv、AI企業公式ブログ（Anthropic/OpenAI/Google/Meta/MS/Vercel）、ツールリリース（Claude Code, Codex, Gemini CLI 等20+リポジトリ）から情報収集してClaudeが要約。

**実行:**
```bash
python -m tech_catchup.main gather   # ニュース収集
python -m tech_catchup.main notify   # Slack通知
```

**Slack通知:** `SLACK_WEBHOOK_URL_TECH`（AI専用チャンネル）

### youtube_factory

AIニュース動画の完全無料自動生成。**実装途中**。

**特徴:**
- Edge TTS（無料、WordBoundary対応）
- master.wav + loudnorm（-16 LUFS）
- ASS字幕焼き込み
- ken-burnsズーム + xfadeクロスフェード
- Pillowテキストカード（ストーリー別差し色）
- ローカルで2分超のサンプル動画生成成功済み

**実行（uvで隔離環境）:**
```bash
# ローカル開発はuv経由で
cd youtube_factory && uv sync          # 環境構築
# 実行は親ディレクトリから
youtube_factory/.venv/bin/python -m youtube_factory.main demo   # サンプル生成
youtube_factory/.venv/bin/python -m youtube_factory.main render # script.json から生成
```

**前提条件（ローカル）:**
- ffmpeg with libass（`brew install homebrew-ffmpeg/ffmpeg/ffmpeg-full`）
- Hiragino Kaku Gothic フォント（macOS標準）

## 環境管理ポリシー

| プロジェクト | 管理方法 |
|---|---|
| stock_analyzer | requirements.txt（既存・触らない） |
| tech_catchup | requirements.txt（既存・触らない） |
| youtube_factory | uv + pyproject.toml（モダン構成） |

## 必要なSecrets

| Secret名 | 用途 |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code Action認証 |
| `SLACK_WEBHOOK_URL` | 株分析の通知先 |
| `SLACK_WEBHOOK_URL_TECH` | AI Tech Catchupの通知先 |

## 重要な技術的決定（履歴）

1. **Claude認証**: APIキー不可、`/install-github-app` でOAuthトークン管理（個人→Team移行で苦戦した経緯あり）
2. **データソース（株）**: stooq.com → yfinance（API化されたため）
3. **動画パイプライン**: 音声優先（master.wav 1本作成 → 動画は最後にmux）でクリック音回避
4. **動画字幕**: Edge TTS WordBoundaryをそのまま使う（character-count分配より正確）
5. **動画ffmpeg**: `ffmpeg-full`（libass必須） / GitHub Actionsでは `apt install ffmpeg fonts-noto-cjk`
