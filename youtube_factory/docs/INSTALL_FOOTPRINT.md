# youtube_factory ローカルインストール記録

ローカル PC へインストールしたパッケージ・モデル・データの **物理的な合計使用容量** をここに記録する。
新しいライブラリやモデルを追加するたびに更新する。

## 現在の合計フットプリント（2026-05-01 時点）

| 区分 | パス | 容量 |
|---|---|---|
| Python venv | `youtube_factory/.venv/` | **319 MB** |
| Whisper モデル等の HF キャッシュ | `~/.cache/huggingface/` | **464 MB** |
| **VOICEVOX core 一式（lib + 音声モデル + dict）** | `youtube_factory/assets/voicevox_core/` | **1.6 GB** |
| Pollinations 画像キャッシュ | `youtube_factory/data/cache/ai_images/` | ~1 MB |
| **合計** | | **約 2.4 GB** |

> 「合計」は重複計算なし、純粋にディスク使用量。
> VOICEVOX 音声モデル 26 個（各 ~55 MB）が大半。1 個（ずんだもん入りの 0.vvm）以外は削除可能で 1.5GB 削減できる。

## venv 内訳（主要パッケージ）

| パッケージ | 容量 | 用途 |
|---|---|---|
| `pyopenjtalk` | 105 MB | 日本語形態素解析・音素抽出（VOICEVOX 内部と同じ） |
| `onnxruntime` | 65 MB | faster-whisper の依存 |
| `numpy` | 23 MB | 一般 |
| `PIL` | 14 MB | Pillow（画像処理） |
| `tokenizers` | 8 MB | faster-whisper の依存 |
| `ctranslate2` | 5 MB | faster-whisper の推論バックエンド |
| `pydantic` + `pydantic_core` | 8 MB | スクリプト validate |
| `aiohttp` | 3 MB | edge-tts 通信 |
| `huggingface-hub` | 3 MB | モデル DL |
| `faster_whisper` | 1 MB | Whisper 本体 |
| その他細々 | ~84 MB | requests, urllib3, attrs, edge_tts 等 |

## HF キャッシュ内訳

| モデル | 容量 | 用途 |
|---|---|---|
| `Systran/faster-whisper-small` | ~244 MB | 日本語 STT による word-level タイミング抽出 |
| その他メタファイル | ~220 MB | tokenizers config, vocab 等 |

## 履歴

| 日付 | 追加 | 増分 | 累計 |
|---|---|---|---|
| 初期構築 | edge-tts, Pillow, requests, pydantic | ~70 MB | 70 MB |
| 2026-05-01 | pyopenjtalk + numpy（音素 lip sync） | ~130 MB | 200 MB |
| 2026-05-01 | faster-whisper + onnxruntime + tokenizers + huggingface-hub（STT 整列） | ~120 MB | 320 MB |
| 2026-05-01 | faster-whisper-small モデル（HF キャッシュ） | ~464 MB | 784 MB |
| 2026-05-01 | voicevox_core wheel + onnxruntime + dict + 26 音声モデル | ~1.6 GB | **2.4 GB** |

## 削除する場合

```bash
# venv まるごと削除（pip 依存を再 install したい場合）
rm -rf youtube_factory/.venv
cd youtube_factory && uv sync

# Whisper モデルキャッシュだけ削除
rm -rf ~/.cache/huggingface/

# Pollinations 画像キャッシュ
rm -rf youtube_factory/data/cache/ai_images/

# VOICEVOX 音声モデル（ずんだもん以外を削除して 1.5GB 節約）
cd youtube_factory/assets/voicevox_core/models/vvms/
ls *.vvm | grep -v '^0\.vvm$' | xargs rm -f

# VOICEVOX 全削除（lib + dict + onnxruntime + models）
rm -rf youtube_factory/assets/voicevox_core/
youtube_factory/.venv/bin/pip uninstall voicevox-core
```

## 環境変数で機能を OFF にすれば DL 不要

| 変数 | 効果 |
|---|---|
| `YOUTUBE_FACTORY_NO_WHISPER=1` | Whisper を使わず pyopenjtalk 推定タイミングにフォールバック（HF モデル DL 不要） |
| `YOUTUBE_FACTORY_NO_AVATAR=1` | アバター未生成（軽量化） |
| `YOUTUBE_FACTORY_NO_BGM=1` | BGM ミックスなし |

## 参考: 検討したが導入しなかった重量級

| ツール | 想定容量 | 不採用理由 |
|---|---|---|
| SadTalker | 1.4 GB（モデル）| ユーザ判断で lip-flap で十分 |
| audiocraft (MusicGen) | 1.5 GB（モデル + torch）| pydantic 依存が衝突 |
| Style-Bert-VITS2 | 300-500 MB（モデル）| pip 化されておらず手動 setup 必要 |
| MuseTalk | 2-3 GB | 重い |
| LivePortrait | 500 MB | 別タイプ（face-to-face、audio→不可） |
| VOICEVOX engine（GUI アプリ） | 1 GB | ユーザがアプリ使用拒否 |
