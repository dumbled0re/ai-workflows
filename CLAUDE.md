# AI Workflows

GitHub ActionsとClaudeを活用した自動化ワークフロー集。各プロジェクトは独立した環境を持つ。

## リポジトリ構成

```
ai-workflows/
├── stock_analyzer/                 ← 日本株分析
│   ├── pyproject.toml / uv.lock    ← uv 管理
│   ├── config/stocks.yml           ← ユーザー設定（保有銘柄）
│   ├── data/                       ← 永続データ（git管理対象）
│   │   ├── investment_rules.json
│   │   ├── predictions_history.json
│   │   ├── strategy_notes.json
│   │   └── screening_weights.json
│   └── stock_analyzer/             ← パッケージ本体（main, ai_analyzer, …）
│
├── tech_catchup/                   ← AI技術キャッチアップ
│   ├── pyproject.toml / uv.lock
│   ├── data/                       ← Claudeとのやり取り用 JSON（gitignore）
│   └── tech_catchup/
│       ├── main.py
│       └── sources.py
│
├── moppy_clicker/                  ← モッピー自動クリック
│   ├── pyproject.toml / uv.lock
│   ├── DESIGN.md                   ← 設計詳細
│   ├── tests/                      ← pytest（fixture ベース）
│   └── moppy_clicker/              ← パッケージ本体
│
├── todo/                           ← 個人 TODO リスト
│   ├── pyproject.toml / uv.lock
│   ├── todos.md                    ← TODO 本体（Claude skill が編集）
│   └── todo/main.py
│
├── .github/workflows/              ← cron + workflow_dispatch
│   ├── stock-analysis.yml          ← 株分析（毎日 朝8時/夕16時 JST）
│   ├── weekly-review.yml           ← 戦略レビュー（土曜10時 JST）
│   ├── tech-catchup.yml            ← AIキャッチアップ（毎朝7:30 JST）
│   ├── moppy-clicker.yml           ← モッピークリック（毎日 朝8時 JST）
│   ├── moppy-clicker-ci.yml        ← moppy_clicker の lint/test（PR時）
│   └── todo.yml                    ← TODO リマインダー（毎朝9時 JST）
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
cd stock_analyzer
uv run python -m stock_analyzer.main prepare   # データ収集・指標計算
uv run python -m stock_analyzer.main notify    # Slack通知
uv run python -m stock_analyzer.main review    # 戦略レビュー
uv run python -m stock_analyzer.main apply-review
```

**Slack通知:** `SLACK_BOT_TOKEN` + `SLACK_CHANNEL_STOCK`（株専用チャンネル）

#### Claude Code Actionでの分析時の注意
- `stock_analyzer/data/investment_rules.json` を必ず読み、ルールに従うこと
- 推奨銘柄がなければ正直に「なし」と回答する

### tech_catchup

AI業界の最新動向を毎朝キャッチアップ。
Hacker News、GitHub Trending、arXiv、AI企業公式ブログ（Anthropic/OpenAI/Google/Meta/MS/Vercel）、ツールリリース（Claude Code, Codex, Gemini CLI 等20+リポジトリ）から情報収集してClaudeが要約。

**実行:**
```bash
cd tech_catchup
uv run python -m tech_catchup.main gather   # ニュース収集
uv run python -m tech_catchup.main notify   # Slack通知
```

**Slack通知:** `SLACK_BOT_TOKEN` + `SLACK_CHANNEL_TECH`（AI専用チャンネル）

### todo

個人TODOリスト。`todo/todos.md` をClaude Codeのグローバル `todo` skill で編集（追加/完了/一覧）し、毎朝9:00 JSTにGitHub ActionsがSlackに未完了タスクを投稿する。

**実行:**
```bash
cd todo
uv run python -m todo.main notify --dry-run   # ローカル確認
uv run python -m todo.main notify             # Slack通知
```

**Slack通知:** `SLACK_BOT_TOKEN` + `SLACK_CHANNEL_TODO`（TODO専用チャンネル）

## 環境管理ポリシー

**新規プロジェクトは uv + pyproject.toml で1プロジェクト1仮想環境を必須**とする。システムPython（Homebrewのglobal site-packages）への直接 `pip install` は禁止。理由は依存衝突とAI再現性の確保。

| プロジェクト | 管理方法 | 状態 |
|---|---|---|
| stock_analyzer | uv + pyproject.toml + uv.lock | ✅ |
| tech_catchup | uv + pyproject.toml + uv.lock | ✅ |
| moppy_clicker | uv + pyproject.toml + uv.lock | ✅ |
| todo | uv + pyproject.toml + uv.lock | ✅ |

### uvプロジェクトの標準レイアウト
```
<project>/
├── pyproject.toml         ← 依存とビルド設定
├── uv.lock                ← ロック（コミット必須）
├── .venv/                 ← .gitignore済み
└── <project>/             ← 実コード（packages = ["<project>"]）
    ├── __init__.py
    └── main.py
```

ローカル実行は常に `uv run python -m <project>.main <subcommand>`。`pip install` 直接実行は禁止。新しい依存追加は `uv add <pkg>`。

### データパスの解決パターン
コード内で `data/` や `config/` を参照する場合は **常に `Path(__file__).parent.parent` から絶対パスを組み立てる**。理由は cwd が呼び出し方によって変わるため。

```python
# Good
_DATA_DIR = Path(__file__).parent.parent / "data"
HISTORY_FILE = _DATA_DIR / "predictions.json"

# Bad (cwd 依存・GitHub Actions の working-directory 切り替えで壊れる)
HISTORY_FILE = Path("stock_analyzer/data/predictions.json")
```

## コード品質 / Lint・Format・Type・Test

| ツール | 適用範囲 | 強制 |
|---|---|---|
| ruff check | 全プロジェクト | ✅ CI で必須 |
| ruff format --check | 全プロジェクト | ✅ CI で必須 |
| mypy (strict) | moppy_clicker のみ | ✅ CI で必須 |
| mypy (lenient) | 他3プロジェクト | ⏳ 将来 strict 化目標 |
| pytest | tests/ がある場合のみ | ✅ CI で必須 |

全プロジェクト共通の ruff 設定（`[tool.ruff]` / `[tool.ruff.lint]`）:
- `line-length = 120`
- `target-version = "py312"`
- `select = ["E", "F", "I", "B", "UP", "SIM", "RUF"]`
- `ignore = ["RUF001", "RUF002", "RUF003"]`（日本語の全角記号誤検出を回避）

### エージェント向けコミット前チェックリスト
コミット前に **対象プロジェクトのディレクトリで** 以下を実行する。CI と同じコマンドなので、ローカルで通れば CI も通る:

```bash
cd <project>                       # 例: cd stock_analyzer
uv sync --frozen --group dev       # 依存とdev tools同期
uv run ruff check .                # lint
uv run ruff format --check .       # フォーマット確認（修正は format . で）
uv run pytest                      # tests/ がある場合のみ
# moppy_clicker のみ:
uv run mypy moppy_clicker
```

設計や非自明な変更は **codex review でセカンドオピニオンを取る**。コミット前に:
```bash
git add -A
codex review --uncommitted        # 設計・整合性・落とし穴を指摘してもらう
```

迷ったら codex に投げる方がコスト安い。特に:
- 共通ライブラリ・パターンの導入
- ファイル/ディレクトリ構造の変更
- セキュリティ関連（secrets、外部API、認証フロー）
- 失敗時の挙動・エラーパス

## 新規プロジェクト追加チェックリスト
1. `mkdir <project>/<project>` でネスト構造を作る（`<project>/__init__.py`, `<project>/main.py`）
2. `<project>/pyproject.toml` を作成（既存の `todo/pyproject.toml` をテンプレに `name`/`description`/`dependencies` だけ書き換え）
3. `cd <project> && uv sync` で `.venv` と `uv.lock` 生成
4. データパス参照は `Path(__file__).parent.parent / "data" / "..."` パターン
5. `.github/workflows/<project>.yml` を作成（既存ワークフローをテンプレに、`working-directory: <project>` と `SLACK_BOT_TOKEN` / `SLACK_CHANNEL_<NAME>` を設定）
6. Slack 用に `SLACK_CHANNEL_<NAME>` Secret を GitHub に追加し、bot を招待
7. `CLAUDE.md` と `README.md` のテーブルに新プロジェクトを追記
8. `git add -A` → `codex review --uncommitted` → 指摘あれば修正 → commit

## 必要なSecrets

| Secret名 | 用途 |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code Action認証 |
| `SLACK_BOT_TOKEN` | Slack Bot User OAuth Token (`xoxb-...`)。全プロジェクト共有 |
| `SLACK_CHANNEL_TODO` | TODO通知先チャンネル（例: `#todo` または ID） |
| `SLACK_CHANNEL_TECH` | AI Tech Catchupの通知先チャンネル |
| `SLACK_CHANNEL_MOPPY` | モッピー自動クリックの通知先チャンネル |
| `SLACK_CHANNEL_STOCK` | 株分析の通知先チャンネル |
| `GMAIL_USER` / `GMAIL_APP_PASSWORD` | モッピーメール受信用 |
| `MOPPY_COOKIES` | モッピーログイン済み Cookie の JSON 配列。未設定時は匿名でアクセスし **ポイントが加算されない** |
| `vars.MOPPY_CRON_MODE` | （Variable）`click` で cron を credited auto-click モードに切替。未設定 or `extract` で extract-links モード（暫定） |

### Slack通知の方針
- 全プロジェクトで **Bot Token方式**（`SLACK_BOT_TOKEN` + `SLACK_CHANNEL_<PROJECT>`）に統一済み。1個のBot Tokenを共有し、チャンネルだけ分ける。
- Bot Token取得手順: https://api.slack.com/apps → Create New App → OAuth & Permissions で `chat:write` と `chat:write.public`（公開チャンネルに招待なしで投稿する場合）を付与 → Install to Workspace → Bot User OAuth Token をコピー。
- 新規プロジェクトを追加する際は `SLACK_CHANNEL_<NAME>` Secret を追加し、bot を該当チャンネルに招待するだけでよい（Webhook 発行は不要）。

## 重要な技術的決定（履歴）

1. **Claude認証**: APIキー不可、`/install-github-app` でOAuthトークン管理（個人→Team移行で苦戦した経緯あり）
2. **データソース（株）**: stooq.com → yfinance（API化されたため）
3. **youtube_factory**: 2026-05-02 に開発一時停止（自動生成 AI ニュース動画は 2026 年時点では参入時期として遅く、稼ぐには niche + 長尺 + 人力 polish が必要と判断）。コードは `git checkout 08674a5 -- youtube_factory` で復元可能
