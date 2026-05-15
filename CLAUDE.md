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

## 後日に持ち越す task は GitHub issue に積む

session 内で完結しない / 翌日以降に持ち越す task は **必ず GitHub issue に登録する**。口頭・memory だけだと session 跨ぎで存在を忘れる。`/todo` skill は廃止予定なので使わない。memory は補助として書いて OK だが、**user-facing reminder としては必ず issue を主にする**。

### 該当する task タイプと運用

| タイプ | 例 | 仕組み | label |
|---|---|---|---|
| **機械検証** | cron 1 ラウンド後の log grep、N 日後の状態確認、外部 event 待ち | pending-verify system (下記) で毎朝 07:30 JST 自動実行 | `pending-verify` |
| **改善 / 後回し実装** | codex review 指摘の段階実装、観察期間後に着手する refactor | 人手 (= 次 session の Claude) で着手。issue body に依存 / 期限 / 参照 commit を書く | `enhancement` |
| **user に確認依頼** | 「user に X を確認してもらって結果を Claude に伝える」など人手介在 task | issue で予約 + body に「user がやること / 結果をどう伝えるか」を明記。気付き手段は **gh issue list (open) を session 開始時に Claude が確認** または開いた issue の Slack 通知 | 適宜 (`question` 等) |
| **観察 follow-up** | 効果測定中の経過確認、長期 metric 評価 | issue body に期限を書く + 必要なら別途 pending-verify で機械確認を併用 | `enhancement` 等 |

**ルール**: 「あとでやる」「明日確認」「来週着手」が出たら **即 `gh issue create`**。memory に書いて満足しない。issue body には: 関連 commit hash / 関連 memory file / 依存する他 issue 番号 / 期限 (あれば) を残し、次 session の Claude が context 引ける状態にする。

### Deferred verification (pending-verify) 仕組み

機械的に確認できる検証 task は pending-verify system に積む。**monorepo 全 project 横断の共通基盤**で、新しく追加した任意の project でそのまま使える。

新しい deferred check を登録する手順:

1. `verify/<project>/<date>-<slug>.yml` を schema に沿って作成
2. GitHub Issue (`pending-verify` ラベル) を YAML front-matter で `verify_id` 紐付けて作成
3. push、以降 cron が自動で見にいく

利用可能な `kind` (検証種別):
- `workflow_run_grep` / `workflow_run_no_grep` — 任意の workflow を trigger → log を grep
- `recent_run_log_grep` — 既存の直近 cron run の log を passive 観察 (外部 event 待ち向け)
- `manual` — 自動化困難な確認 (canary heartbeat 等)

ファイル:
- `scripts/pending_verify.py` — runner (kind の registry 含む)
- `.github/workflows/pending-verify.yml` — daily cron + Stage 2 Claude auto-fix
- `verify/**/*.yml` — 検証 schema 群
- 詳細は `scripts/pending_verify.py` の module docstring 参照

通知: `SLACK_CHANNEL_VERIFY` Secret 指定 channel に全イベント (success / inconclusive / failure) を流す。issue にもコメントが残るので Slack ロスト時の冗長性あり。pending-verify の `kind` 登録は機械検証用なので、`enhancement` 等の人手 task issue には `pending-verify` label を **付けないこと** (cron が拾って混乱の元になる)。

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
- **`~/.claude/CLAUDE.md` (user-global) は触らない** — Claude が独断で作成 / 編集禁止。「個人ポリシーだから global に置こう」という発想は NG。repo のルールは複数 repo で重複していても各 repo の CLAUDE.md に書く

判断に迷ったら memory 側に書いて、user に「次の機会に CLAUDE.md にも書くか相談したい」と伝える。

## codex とのコラボ (= 不安なときだけ呼ぶ)

codex は **「不安なとき / 相談したいときの seconding 相手」**。**毎回の reflexive な review は不要** — Claude 側の判断を放棄するだけで時間と token のコストに見合わない。

**呼ぶ:** 複数案あって甲乙つけがたいアーキテクチャ判断 / 外部 API・認証フロー・secrets まわりの落とし穴の独立確認 / HTML・フォーム解析など壊れやすい外部依存導入直後 / 「これでいいはずだけど念のため」な non-trivial 変更。

**呼ばない:** lint/format/mypy/test 全 green な小〜中規模 refactor / 既存パターンの延長 (新 adapter 等) / bug fix・typo・docs 更新 / 自分で論理的に納得できる変更。

```bash
codex exec "<相談内容>"             # 設計相談
codex review --uncommitted          # コード review (git add 後)
codex review path/to/file.py        # ファイル単位 review
```

指摘されたら: 同意 → 直して commit / 不同意 → 理由を user に説明、最終決定は user。

## handoff の自動起動

長時間セッションで context が圧迫してきたら **Claude は許可を求めず handoff skill を即実行する**。

**自動 handoff トリガー (どれか1つで即実行):**
1. **Context budget 警告** — system reminder で context 残量警告が出た時
2. **大きな作業の完結** — implementation / debugging / migration が commit + push まで完了したタイミング
3. **user の区切り発言** — 「疲れた」「一旦切る」「ここまで」「お疲れ」「明日続ける」等
4. **大きな作業の入口で context 残量が乏しい時** (Claude 自己判断) — 新サイト追加・大規模 refactor・複数 phase 跨ぎ実装で現セッションで完走できないと判断したら、user 発言を待たず先に handoff してから着手

**実行手順:** (1) handoff skill を invoke (許可確認しない) → (2) HANDOFF.md 作成 + memory 更新 → (3) resume prompt を chat に出力 → (4) user に「`/clear` してから resume prompt を貼って」と通知。User の手動操作は最終的に **`/clear` + paste の 1 ステップ**だけ。

詳細は `.claude/skills/handoff/SKILL.md` 参照。

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
