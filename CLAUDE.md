# AI Workflows

GitHub Actions と Claude を活用した個人用自動化 monorepo。各プロジェクトは uv による独立仮想環境を持ち、それぞれ独立した cron で動く。

## プロジェクト

| ディレクトリ | 用途 | cron | Slack channel |
|---|---|---|---|
| `stock_analyzer/` | 日本株分析 (テクニカル + ファンダ + ニュース)、自律改善ループ付き | 毎日 8:00 / 16:00、土 10:00 (review) | `SLACK_CHANNEL_STOCK` |
| `tech_catchup/` | AI 業界ニュース要約 (HN / GitHub Trending / arXiv / 公式ブログ) | 毎朝 7:30 | `SLACK_CHANNEL_TECH` |
| `point_sites/` | ポイ活サイト自動化 (adapter pattern、詳細は `point_sites/CLAUDE.md`) | サイト別 (8:00〜9:45) | `SLACK_CHANNEL_<SITE>` |
| `todo/` | 個人 TODO リマインダー (`todos.md` を `todo` skill で編集) | 毎朝 9:00 | `SLACK_CHANNEL_TODO` |

実行コマンド・workflow 一覧は [`README.md`](./README.md) 参照。

## 環境管理 (必須ルール)

- **全プロジェクトは uv + pyproject.toml + uv.lock** で 1 仮想環境を持つ。システム Python への直接 `pip install` は禁止
- 新規依存追加は `uv add <pkg>`、ローカル実行は `uv run python -m <project>.main <subcommand>`
- データパス参照は **常に `Path(__file__).parent.parent` から絶対パス** で組み立てる (cwd 依存だと GitHub Actions の `working-directory` 切替で壊れる)

```python
# Good
_DATA_DIR = Path(__file__).parent.parent / "data"

# Bad
HISTORY_FILE = Path("stock_analyzer/data/predictions.json")
```

## コード品質 (CI 必須)

| ツール | 適用範囲 |
|---|---|
| `ruff check` / `ruff format --check` | 全プロジェクト |
| `pytest` | `tests/` がある場合 |
| `mypy` (strict) | `point_sites` のみ |

共通の `[tool.ruff]`: `line-length=120`、`target-version=py312`、`select=[E,F,I,B,UP,SIM,RUF]`、`ignore=[RUF001,RUF002,RUF003]` (日本語全角の誤検出回避)。

**コミット前** (対象プロジェクトの dir で):
```bash
uv sync --frozen --group dev
uv run ruff check . && uv run ruff format --check .
uv run pytest                    # tests/ がある場合
uv run mypy point_sites          # point_sites のみ
```

## 自律ワークフロー設計原則

このリポジトリの目的は **「人が介在しなくても回り続ける自動化」**。新しい cron / バッチを追加する時は次の 3 層を必ず備える:

| 層 | 役割 | 失敗例 |
|---|---|---|
| 検知 (verification) | 副作用が本当に起きたかを観測 | クリックが HTTP 200 でもポイント加算とは限らない → 残高で確認 |
| 記録 (telemetry) | 結果を時系列 (JSONL) で永続化 (artifact) | 単発成功でも、過去 N 回比較で degradation が見える |
| 判断 + 通知 (escalation) | 連続 N 回の閾値割れで Slack 警告 + **「ユーザーが何をすべきか」を文中に書く** | 「ポイントが入ってないかも」だけでは行動できない |

**実装パターン** (point_sites を例に詳細は `point_sites/CLAUDE.md`):
- 副作用前後の状態スナップショットで比較
- 単発の異常値はノイズ → 直近 N 回の中央値で判断 (false positive 回避)
- 警告メッセージは具体的アクション (「Cookie 再エクスポート → `<SITE>_COOKIES` Secret 更新」) を含める
- 検証層自体が壊れた場合 (HTML 変更等) も本処理は止めず「検証失敗」として通知
- 自動 fallback はコード変更不要で revert できる経路 (env var / GitHub Variable) を用意

## Git 運用ポリシー

個人リポジトリ。AI は **確認なしで自律実行 OK**:
- `git commit` (lint/test 通過後)
- `git push origin master` (PR 不要、master 直接 push)
- ブランチ作成・切替

**事前確認が必要な破壊的操作:**
- `git push --force` / `--force-with-lease`
- `git reset --hard` / `git checkout -- <file>` / `git clean -f`
- ブランチ・ファイルの削除
- `--no-verify` / `--no-gpg-sign`

**絶対にやらない:**
- secrets / credentials を含むファイルの commit
- `.gitignore` 対象 (`data/`、`.venv/`、`secrets/`、`**/token.json`、`**/credentials.json`) の強制 add

## CLAUDE.md / memory への書き込みルール

- **memory** (`~/.claude/projects/.../memory/`) → Claude が独断で書いて OK (個人観察、commit されない)
- **CLAUDE.md** (このファイル + `point_sites/CLAUDE.md` 等) → repo の正規ルール、commit されて他環境にも伝播 → **必ず user に提案して合意してから commit**

判断に迷ったら memory 側に書いて、user に「次の機会に CLAUDE.md にも書くか相談したい」と伝える。

## 必要な Secrets

| Secret 名 | 用途 |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code Action 認証 (Pro/Max sub) |
| `SLACK_BOT_TOKEN` | 全プロジェクト共有の Bot User OAuth Token (`xoxb-...`) |
| `SLACK_CHANNEL_<PROJECT>` | プロジェクト別 channel (例: `SLACK_CHANNEL_TODO`) |
| `GMAIL_USER` / `GMAIL_APP_PASSWORD` | Gmail 経由のメール取得 (point_sites 各サイト共有) |
| `<SITE>_COOKIES` | point_sites 各サイトの Cookie JSON (Cookie-Editor export) |

Slack は全プロジェクト Bot Token 方式で統一済 (Webhook 不要)。新プロジェクト = `SLACK_CHANNEL_<NAME>` Secret 追加 + bot を該当 channel に招待のみ。

## 新規プロジェクト追加チェックリスト

1. `mkdir <project>/<project>` (ネスト構造、`__init__.py` + `main.py`)
2. `<project>/pyproject.toml` 作成 (`todo/pyproject.toml` をテンプレに)
3. `cd <project> && uv sync` で `.venv` + `uv.lock` 生成
4. データパス参照は `Path(__file__).parent.parent / "data" / "..."` パターン
5. `.github/workflows/<project>.yml` 作成 + `SLACK_CHANNEL_<NAME>` Secret 登録 + bot 招待
6. このファイルのプロジェクト表 + `README.md` のテーブルに追記

## ポリシー (= 自動化しないもの)

| 対象 | 理由 |
|---|---|
| 第三者広告クリック (ガチャ / 抽選 / 無料ゲームの大半) | ad-fraud に該当 |
| アンケート自動回答 | TOS 違反 + survey panel provider に対する data fraud |
| ショッピング / クレジット申込 / モニター | 実購入 / 実申込が必要、自動化適性なし |

このリポジトリの自動化対象は **「サイト内エンゲージメント (クリックメール) のみ」** で一貫させる。
