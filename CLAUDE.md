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
├── point_sites/                    ← 日本のポイ活サイト自動化（Moppy + 将来の adapter）
│   ├── pyproject.toml / uv.lock
│   ├── DESIGN.md                   ← 設計詳細
│   ├── tests/                      ← pytest（fixture ベース）
│   └── point_sites/                ← パッケージ本体（common/ + adapters/<site>/ 構造）
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
│   ├── moppy.yml                   ← モッピークリック（毎日 朝8時 JST）
│   ├── point_sites-ci.yml          ← point_sites の mypy（PR時）
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
| point_sites | uv + pyproject.toml + uv.lock | ✅ |
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
| mypy (strict) | point_sites のみ | ✅ CI で必須 |
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
# point_sites のみ:
uv run mypy point_sites
```

### codex とのコラボ（不安なときだけ）

このリポジトリでは codex を **「不安なとき / 相談したいときの seconding 相手」** として使う。**毎回の reflexive な review は不要** — Claude 側の判断を放棄するだけで、時間と token のコストに見合わない。

> ユーザーフィードバック (2026-05-09): 「毎回 codex にレビューをしなくても、不安とか相談したいことがあるときに使用するようにしてね」

#### codex を呼ぶべきタイミング（= 自分で確信が持てないとき）
- 複数案あって甲乙つけがたいアーキテクチャ判断
- 外部 API / 認証フロー / secrets まわりで落とし穴の有無を独立確認したい
- HTML / フォーム解析など壊れやすい外部依存を入れた直後
- 「これでいいはずだけど、念のため」と思う non-trivial 変更

#### codex を呼ばなくていいタイミング（= 自分で十分判断できる）
- lint / format / mypy / test 全 green な小〜中規模 refactor
- 既存パターンの延長（新 adapter / 新 source など pattern が確立済の追加）
- bug fix / typo / コメント修正 / docs 更新
- 自分で論理的に正しいと納得できる変更

**デフォルトは「呼ばない」**。呼ぶときは「ここが不安だから」と理由を意識する。

#### 使い方
```bash
# 設計相談（対話的に質問する）
codex exec "<相談内容>"

# コード review（不安な変更だけ）
git add -A
codex review --uncommitted

# 既存ファイル・ディレクトリ単位の review
codex review path/to/file.py
```

#### 指摘された場合
- **同意できる指摘** → 直してから commit
- **同意できない指摘** → 理由を user に説明して判断を仰ぐ（codex は時に過度に保守的な提案をするので鵜呑みは禁物）
- **両者の意見が割れた場合** → 最終決定は user

### セッションコンテキスト管理

長時間セッションで context が圧迫してきたら **handoff skill を起動して新セッションに引き継ぐ**。

#### Claude が自発的に handoff を提案する条件
- 1 セッションの token 使用量が体感で重い（複数の長文ファイル read、多数の workflow run を回した、等）
- 1 つのまとまった作業が完了したタイミング（次の作業は別 context で始めた方が clean）
- user が「疲れた」「一旦切る」「セッション分けたい」と言ったとき

#### Claude が提案するときの動き
1. `/handoff` skill を invoke（=「引き継ぎ書いて」と user が言わなくても自発的に）
2. skill が `<project>/HANDOFF.md` を生成（commit しない、`.gitignore` 済）
3. resume prompt を chat に出力
4. user に **「`/clear` してから resume prompt を貼ってください」** と通知

#### 自動化の限界（現状）
Claude 自身は session を再起動できない。`/clear` + resume prompt 貼り付けは user 操作が必要。これを完全自動化するには `~/.claude/settings.json` の hook 設定が必要 (別タスク)。

#### 関連 memory
- `feedback_handoff_transient.md` — HANDOFF.md は commit しない / 引き継ぎ後削除
- `feedback_skill_scope.md` — skill は project-local

### 自律ワークフロー設計原則

このリポジトリの目的は **「人が介在しなくても回り続ける自動化」**。新しい cron / バッチ / クリック系を追加する時は次の3層を必ず備える:

| 層 | 役割 | 失敗例 |
|---|---|---|
| **検知 (verification)** | 副作用が本当に起きたかを観測する | クリックが HTTP 200 でも moppy がポイント加算したとは限らない → マイページ残高で確認 |
| **記録 (telemetry)** | 結果を時系列 (JSONL) で永続化 (artifact) | 単発で見ると「成功 11件」に見えるので、過去 N 回との比較で初めて degradation が分かる |
| **判断 + 通知 (escalation)** | 連続 N 回の閾値割れで自動で警告 + next-action を Slack に出す | 「ポイントが入っていないかも」だけでは行動できない → 「Cookie を再取得してください」まで書く |

**実装パターン:**
- 副作用のあるアクション (HTTP GET でポイント加算、外部 API への書き込み等) は必ず **アクション前後の状態をスナップショット** して比較できるようにする
- 単発の異常値はノイズ。**直近 N 回の中央値** で判断する (false positive を避ける)
- 警告メッセージは **「ユーザーが何をすればよいか」を文中に書く** (「Cookie を再エクスポート → MOPPY_COOKIES Secret 更新」のように具体的に)
- 検証層自体が壊れた場合 (HTML パターン変更等) も、本処理は止めずに「検証失敗」として通知する (検証失敗 ≠ 本処理失敗)
- 自動 fallback (extract モードへの切替等) を入れるなら、**コード変更不要で revert できる経路** (env var / GitHub Variable) を用意する

**実装例:** `point_sites/common/balance.py` (検知) + `common/outcome_tracker.py` (記録 + 判断) + `common/notifier.send_summary` の degradation セクション (通知)。

### Git 運用ポリシー（個人リポジトリ・AI フレンドリー）

このリポジトリは1人運用。AI（Claude 等）は以下を **確認なしで自律実行してよい**:
- `git commit`（lint/test 通過後）
- `git push origin master` を **どんどんやってよい**（master 直接 push を許可、PR 不要）
- ブランチ作成・切替

破壊的操作は **必ず事前にユーザーへ確認**:
- `git push --force` / `--force-with-lease`
- `git reset --hard` / `git checkout -- <file>` / `git clean -f`
- ブランチ・ファイルの削除
- `--no-verify` / `--no-gpg-sign`（hook スキップ）

絶対にやらない:
- secrets / credentials を含むファイルのコミット
- `.gitignore` 対象（`data/`, `.venv/`, `secrets/`, `**/token.json`, `**/credentials.json`）の強制 add

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
