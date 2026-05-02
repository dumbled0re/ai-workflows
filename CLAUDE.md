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
├── youtube_factory/        ← YouTube動画自動生成（uv管理・本番稼働間近）
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── .venv/              ← uvが管理する仮想環境（gitignore）
│   ├── main.py             ← CLI entry（render / demo）
│   ├── script.py           ← VideoScript Pydantic モデル
│   ├── audio/
│   │   ├── voice.py        ← VOICEVOX/AivisSpeech HTTP + Edge TTS フォールバック
│   │   └── master.py       ← master.wav 構築 + loudnorm + procedural BGM + ducking
│   ├── visual/
│   │   ├── images.py       ← AI画像 routing（Pollinations/OG/headline card）
│   │   ├── subtitles.py    ← カラオケ風 ASS 字幕（kf wipe）
│   │   └── avatar.py       ← lip-flap PNG sequence renderer（VTuber風）
│   ├── video/
│   │   └── compose.py      ← ken-burns + xfade + burn_subs + mux + avatar overlay
│   ├── docs/
│   │   ├── DESIGN.md       ← パイプライン設計書
│   │   └── IMPROVEMENT_PLAN.md
│   ├── data/               ← 一時データ（gitignore）
│   └── assets/             ← avatar / bgm / fonts
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

AIニュース動画の完全無料自動生成。**ローカル本番動作確認済み**（YouTube投稿のみ未実装）。

**特徴（無料の天井に近い構成）:**
- 音声: VOICEVOX/AivisSpeech HTTP（自動検知）/ Edge TTS フォールバック / WordBoundary 不在時は SentenceBoundary→擬似 word 分割
- 音響: master.wav + 2-pass EBU R128 loudnorm（-16 LUFS）+ procedural ambient drone BGM + サイドチェイン ducking
- 画像: Pollinations.ai (Flux) シネマ生成 → OG → headline card の多段フォールバック、prompt-hash で disk cache、429 リトライ＋バックオフ
- 字幕: ASS カラオケ風 word-by-word wipe（dim white→amber）、`\fad` フェード、libass 焼き込み
- 演出: ken-burnsズーム + xfade クロスフェード + lower-third 出典バナー（全 image source 統一）
- アバター: `assets/avatar/face_*.png` があれば audio amplitude-driven lip-flap を画面右下に overlay（PNG sequence）
- 出力: 1920x1080 H.264 + AAC、120-130秒で 25-35MB
- ローカル demo 動作確認済み

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

**音声を更にアップグレードしたい場合（任意）:**
- VOICEVOX デスクトップ or AivisSpeech を起動して `localhost:50021` で待機させる
- → `voice.py` が自動検知して切替（環境変数 `VOICEVOX_SPEAKER` で voice 切替可、デフォルト 13=青山龍星）
- どちらも未起動の場合は Edge TTS にフォールバック

**環境変数（任意）:**
| 変数 | 用途 |
|---|---|
| `VOICEVOX_URL` | VOICEVOX/AivisSpeech HTTP エンドポイント（デフォルト `http://localhost:50021`） |
| `VOICEVOX_SPEAKER` | speaker ID（デフォルト 13） |
| `YOUTUBE_FACTORY_TTS` | `edge` or `voicevox` を強制 |
| `YOUTUBE_FACTORY_KARAOKE` | `0` でカラオケ字幕無効化 |
| `YOUTUBE_FACTORY_NO_BGM` | `1` で BGM 無効化 |
| `YOUTUBE_FACTORY_NO_AVATAR` | `1` で アバター無効化 |

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
4. **動画字幕**: Edge TTS は日本語で WordBoundary を出さない（SentenceBoundary のみ）→ センテンスを 4 文字単位＋句読点で擬似分割し、duration を文字比で配分
5. **動画ffmpeg**: `ffmpeg-full`（libass必須） / GitHub Actionsでは `apt install ffmpeg fonts-noto-cjk`
6. **AI画像**: Pollinations.ai は無料・key 不要だが匿名 tier は rate limit が厳しい → ThreadPool max_workers=2 + 4回の指数バックオフリトライ + prompt-hash の disk cache で許容範囲に
7. **BGM**: Pixabay 等の API key を要する素材を使わず、ffmpeg `aevalsrc` で A minor triad を合成して低音域に lowpass + tremolo + phaser を掛けた procedural drone を BGM として採用（key 不要、サイドチェイン ducking で声と共存）
8. **アバター**: SadTalker（モデル 1.4GB DL+5min/30s 推論）を選ばず、Pollinations 4 face の amplitude-driven 切替（VTuber 風 lip-flap）を採用 — DL 不要、即時、AI ニュース系では「正攻法」
