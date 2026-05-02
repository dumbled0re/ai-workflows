# youtube_factory 設計書

## 目的
`tech_catchup` が収集したAIニュースから、YouTube動画を自動生成するモジュール。
最初はMP4ファイル生成までを完全自動化し、最終的にはYouTube投稿まで自動化する。

## ゴール（MVP）
- 週1本、5-7分のAIニュース解説動画を生成
- 完全AI自動生成（人間は最終確認のみ）
- 制作コスト1本あたり0円（API無料枠のみ）
- GitHub Actions Artifactとしてmp4出力

## 非ゴール（最初は含めない）
- YouTube自動アップロード（OAuth設定が複雑なため後回し）
- AI VTuberアバター（重い、品質出すの難しい）
- ショート動画（フォーマットが違うため別モジュール）
- 多言語対応

---

## アーキテクチャ

```
┌─────────────────────────────────┐
│  tech_catchup (既存)             │
│  ニュース収集 → JSON保存         │
└──────────────┬──────────────────┘
               │ tech_catchup_result.json
               ▼
┌─────────────────────────────────┐
│  youtube_factory                 │
│                                  │
│  Phase 1: prepare-script         │
│    Claude用のプロンプトを構築     │
│  Phase 2: Claude Code Action     │
│    動画スクリプトを生成           │
│  Phase 3: render                 │
│    音声合成 → 画像生成 → 動画化  │
│  Phase 4: notify                 │
│    完成通知 + Artifact URL       │
└─────────────────────────────────┘
               │
               ▼ output.mp4 (Artifact)
        手動でダウンロード→YouTube投稿
```

---

## モジュール構成

```
youtube_factory/
  __init__.py
  main.py                    # エントリーポイント
  script_generator.py        # スクリプトプロンプト構築
  voice_synthesizer.py       # 音声合成（Edge TTS）
  image_generator.py         # 画像生成（Pexels + Pillow）
  video_assembler.py         # ffmpegによる動画組立
  requirements.txt           # 依存パッケージ
  data/
    .gitkeep                 # 一時ファイル置き場
```

---

## 技術スタック選定

### 音声合成: **Edge TTS**

| 候補 | コスト | 品質 | CI環境での実行 | 採用 |
|---|---|---|---|---|
| Edge TTS | 無料 | 高（自然） | ◯（APIキー不要） | **採用** |
| VOICEVOX | 無料 | 中（機械的） | △（要サーバー起動） | × |
| OpenAI TTS | $15/1M字 | 最高 | ◯ | 後で検討 |
| ElevenLabs | $5-22/月 | 最高 | ◯ | 後で検討 |

**理由：** Edge TTSはMicrosoftの内部TTSをラップしたOSSライブラリ。APIキー不要、完全無料、品質も実用レベル。CI環境で安定動作する。

### 画像素材: **Pexels API + Pillow**

| 候補 | コスト | 品質 | 採用 |
|---|---|---|---|
| Pexels API | 無料（200req/h） | 高（プロ写真） | **採用** |
| Stable Diffusion | 無料（GPU必要） | 高 | 後で検討 |
| DALL-E 3 | $0.04/枚 | 最高 | 後で検討 |
| 自前テキスト画像 | 無料 | 低 | 補完用 |

**理由：** Pexelsは商用利用可・無料・APIキーは無料登録でもらえる。AIニュース動画にはストック写真+テキスト合成で十分。

### 動画組立: **ffmpeg のみ**

| 候補 | 学習コスト | パフォーマンス | 採用 |
|---|---|---|---|
| ffmpeg直叩き | 高 | 最速 | **採用** |
| MoviePy | 中 | 遅い、メモリ食う | × |
| ffmpeg-python | 低 | ffmpegと同等 | 補助で採用 |

**理由：** GitHub Actionsには標準でffmpegが入っている。MoviePyはmp4書き出しが遅く、6分動画で5-10分かかる。ffmpegなら1-2分。

---

## データフロー詳細

### Phase 1: prepare-script

入力: `tech_catchup/data/tech_catchup_result.json`（前日に収集したニュース）

処理:
1. tech_catchupの結果から「重要度HIGH」のストーリーを最大5個抽出
2. 動画用のClaude向けプロンプトを構築
3. `youtube_factory/data/script_prompt.txt` に保存

出力: `youtube_factory/data/script_prompt.txt`

### Phase 2: Claude Code Action

Claudeが`script_prompt.txt`を読み、以下のJSON形式でスクリプトを生成：

```json
{
  "title": "動画タイトル",
  "description": "概要欄",
  "thumbnail_text": "サムネイル文字",
  "scenes": [
    {
      "id": 1,
      "duration_sec": 15,
      "narration": "ナレーション文",
      "image_query": "Pexelsで検索するキーワード（英語）",
      "text_overlay": "画面に表示するテキスト"
    }
  ],
  "tags": ["タグ1", "タグ2"]
}
```

出力: `youtube_factory/data/script.json`

### Phase 3: render

入力: `script.json`

処理:
1. **音声合成**: 各シーンのナレーションをEdge TTSで音声化 → `audio_{scene_id}.mp3`
2. **画像取得**: 各シーンの`image_query`でPexels APIから画像取得 → `image_{scene_id}.jpg`
3. **テキスト合成**: Pillowで画像にtext_overlayを合成 → `slide_{scene_id}.jpg`
4. **動画組立**: ffmpegでスライド+音声を結合
   - 各シーンを1つのmp4セグメントに（ken-burns効果でズーム）
   - 全セグメントを連結
   - BGMを薄く重ねる（フリー素材）
5. 最終mp4を出力 → `output.mp4`

出力: `youtube_factory/data/output.mp4`（Artifactとしてアップロード）

### Phase 4: notify

Slackに完成通知 + ダウンロードURL + メタ情報（タイトル、概要欄、タグ）を投稿

---

## GitHub Actions ワークフロー

```yaml
name: YouTube Video Factory

on:
  schedule:
    - cron: '0 23 * * 0'  # 月曜 8:00 JST
  workflow_dispatch: {}

permissions:
  contents: read
  id-token: write

jobs:
  generate:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@...
      - uses: actions/setup-python@...
      - run: sudo apt-get install -y ffmpeg
      - run: pip install -r youtube_factory/requirements.txt
      
      - name: Prepare script prompt
        run: python -m youtube_factory.main prepare-script
      
      - name: Generate script with Claude
        uses: anthropics/claude-code-action@v1
        with:
          claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
          prompt: |
            youtube_factory/data/script_prompt.txt を読み、
            指定されたJSON形式で動画スクリプトを youtube_factory/data/script.json に出力してください。
      
      - name: Render video
        env:
          PEXELS_API_KEY: ${{ secrets.PEXELS_API_KEY }}
        run: python -m youtube_factory.main render
      
      - name: Upload artifact
        uses: actions/upload-artifact@v4
        with:
          name: video-output
          path: youtube_factory/data/output.mp4
          retention-days: 30
      
      - name: Notify Slack
        env:
          SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL_TECH }}
        run: python -m youtube_factory.main notify
```

---

## 必要な追加 Secrets

| Secret名 | 用途 | 取得方法 |
|---|---|---|
| `PEXELS_API_KEY` | 画像取得 | https://www.pexels.com/api/ で無料登録 |

既存の Secret（`CLAUDE_CODE_OAUTH_TOKEN`, `SLACK_WEBHOOK_URL_TECH`）はそのまま流用。

---

## requirements.txt（候補）

```
edge-tts==7.0.0           # 音声合成
requests==2.32.3          # Pexels API（既存）
pillow==11.0.0            # 画像合成
ffmpeg-python==0.2.0      # ffmpeg補助（subprocess呼び出しで十分なら不要）
```

ffmpeg本体はGitHub Actionsランナーに標準搭載（apt-get install ffmpegで確実に入る）。

---

## データ容量・実行時間の見積

| 工程 | ファイルサイズ | 実行時間 |
|---|---|---|
| 音声合成（5シーン） | 各1-2MB | 30秒 |
| 画像取得（5枚） | 各500KB-1MB | 10秒 |
| 画像合成 | 各1MB | 5秒 |
| 動画組立 | 50-150MB | 60-120秒 |
| **合計** | mp4: 100MB前後 | **3-5分** |

GitHub Actions Artifactは10GB/月の無料枠。動画100MB×4本/月=400MBで余裕。

---

## 失敗パターンと対策

| 失敗パターン | 検出方法 | 対策 |
|---|---|---|
| Pexels APIで画像が見つからない | レスポンスチェック | デフォルト画像（テキストのみのスライド）でフォールバック |
| Edge TTSが音声生成失敗 | 例外処理 | リトライ3回 → ダメならSlackエラー通知 |
| Claudeのスクリプトが規定JSON形式でない | parse例外 | エラー詳細をSlackに通知 |
| ffmpegが動画組立失敗 | exit code確認 | ログをSlackに通知 |

---

## Phase 5（将来）: YouTube自動投稿

OAuth2のrefresh tokenを使って自動投稿。
今回のMVPには含めない（最初は手動レビュー後にアップロードする運用）。

---

## 設計上の懸念点（要レビュー）

1. **動画品質：** 静止画+ken-burns効果のみで視聴維持率が出るか？最初は良くても飽きられる可能性
2. **シーン分割：** Claudeに「duration_sec」を任せるが、実際の音声長と合わない可能性。実音声長で再計算が必要かも
3. **BGM：** フリーBGMを動画に使うとライセンス問題のリスク。最初はBGM無しで様子見が安全？
4. **テスト戦略：** 動画生成は実行が重い。各モジュール単体テストの方法
5. **データの永続化：** scriptとoutput.mp4をgit管理するか、Artifactのみか
