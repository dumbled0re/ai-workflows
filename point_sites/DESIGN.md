# point_sites — 設計書（v3: IMAP + App Password）

モッピー（ポイントサイト）配信メールに含まれる「クリックで○pt」リンクを自動でクリック（HTTP GET）し、結果を Slack に通知する自動化ワークフロー。

> **v3 変更点（v2 → v3）**: Gmail API + OAuth から **IMAP + App Password 認証** にスイッチ。Cloud Console セットアップ不要、refresh token 失効リスク無し、Python 標準ライブラリ `imaplib` のみで完結。

> ⚠ **規約上の位置付け**: モッピーの利用規約は「自動化ツールによるアクセス」を禁止しているため、検知された場合はアカウント凍結＋累積ポイント没収の可能性あり。本実装は **検知回避ロジックを含めない**。リスクはユーザー（リポジトリオーナー）が承知の上で運用する。

## ディレクトリ構成

```
point_sites/
├── pyproject.toml
├── uv.lock
├── .venv/                   ← gitignore
├── point_sites/
│   ├── __init__.py
│   ├── main.py              ← CLI entry（fetch / run / click / auth）
│   ├── gmail_client.py      ← Gmail API wrapper（OAuth, 検索, 既読化, ラベル）
│   ├── moppy_parser.py      ← HTML本文 → ClickCandidate 抽出
│   ├── clicker.py           ← URL に対する HTTP GET 実行
│   ├── notifier.py          ← Slack通知
│   ├── state_store.py       ← URL単位の処理履歴管理
│   ├── models.py            ← Pydantic モデル群
│   ├── redaction.py         ← URL/ログから query string・識別子を除去
│   └── config.py            ← 環境変数・定数
├── tests/
│   ├── fixtures/            ← サンプルメールHTML（個人情報マスク済み）
│   ├── test_parser.py       ← golden test
│   ├── test_redaction.py
│   └── test_state_store.py
├── data/                    ← gitignore（state.json）
└── secrets/                 ← gitignore（local 開発用 credentials.json/token.json）
```

## .gitignore 追加項目（実装前に必須）

```
point_sites/.venv/
point_sites/data/
point_sites/secrets/
point_sites/**/token.json
point_sites/**/credentials.json
point_sites/**/__pycache__/
point_sites/.ruff_cache/
point_sites/.pytest_cache/
```

## 処理フロー

```
[GitHub Actions cron 1日1回 / concurrency=moppy-clicker]
   ↓
[1] state_store ロード（実行中の重複・前回失敗URLを把握）
   ↓
[2] Gmail API でメール検索
     query: from:moppy.jp -label:moppy-clicked newer_than:3d
   ↓
[3] 各メッセージから text/html part を抽出
   ↓
[4] moppy_parser → ClickCandidate のリスト（whitelist 厳格適用）
   ↓
[5] state_store で「未処理URLのみ」フィルタ
   ↓
[6] clicker が各URLを GET（直列、間隔 5-15秒、UA固定、redirect上限10）
     → 各URLごとに即座に state_store に状態保存（success/failed/temporary_failure）
   ↓
[7] メール内の全URL成功時のみ Gmail に既読 + label `moppy-clicked` 付与
     失敗が残るメールは未読のまま、次回再試行（最大3回まで、それ以降は label `moppy-failed`）
   ↓
[8] 結果サマリを Slack 通知（URL は host のみ表示）
```

## コンポーネント詳細

### 1. `models.py`（Pydantic）

```python
class ClickCandidate(BaseModel):
    url: HttpUrl
    anchor_text: str
    estimated_points: int | None
    extraction_reason: Literal["whitelist_url_pattern", "whitelist_url_pattern_and_anchor"]

class ClickResult(BaseModel):
    candidate: ClickCandidate
    final_status: Literal["success", "failed_4xx", "failed_5xx", "failed_timeout", "failed_connection"]
    http_status: int | None
    final_host: str | None
    duration_ms: int
    timestamp: datetime

class MessageRun(BaseModel):
    message_id: str
    subject_redacted: str  # 個人ID等は除去
    candidates: list[ClickCandidate]
    results: list[ClickResult]
    attempt_count: int  # 何回目の試行か（再試行管理）

class RunSummary(BaseModel):
    started_at: datetime
    finished_at: datetime
    messages_processed: int
    candidates_total: int
    success_count: int
    failure_count: int
    parse_failures: list[str]  # message_id のみ
```

### 2. `gmail_client.py`

**IMAP + App Password 認証:**
- Gmail の **アプリパスワード**（`https://myaccount.google.com/apppasswords` で発行、要2段階認証）を使う
- 環境変数: `GMAIL_USER`（メアド）+ `GMAIL_APP_PASSWORD`（16文字、空白OK・自動 strip）
- 接続: `imap.gmail.com:993`（SSL）
- Folder: `[Gmail]/All Mail`（INBOX 限定だとアーカイブ済みメールを見落とすため）
- Python 標準ライブラリ `imaplib` のみ使用、追加依存なし

**Gmail 拡張IMAPコマンド使用:**
- 検索: `X-GM-RAW` で Gmail Web UI と同じクエリ構文（`from:moppy.jp -label:moppy-clicked` 等）が使える
- ラベル付与: `X-GM-LABELS` 拡張で IMAP 経由でも Gmail ラベル操作可能

**エラー分類:**
| エラー | 動作 |
|---|---|
| connect 失敗（DNS/network） | `GmailAuthError`、Slack 通知、exit 1 |
| login 失敗（認証エラー） | 「app password 再生成？」と促し exit 1 |
| `GMAIL_USER` 不正（`@` 含まない等） | 起動時 fail-fast |
| `GMAIL_APP_PASSWORD` 16文字でない | 起動時 fail-fast |
| Folder select 失敗 | exit 1 |
| FETCH 個別失敗 | スキップして次のメッセージ |

**API:**
- `search_messages(query: str, max_results: int) -> list[str]`（IMAP UID を返す）
- `get_message(uid: str) -> ParsedMessage`（RFC822 をパースして plaintext / html を抽出）
- `mark_as_read(uid: str)`（`+FLAGS (\Seen)`）
- `add_label(uid: str, label_name: str)`（`+X-GM-LABELS (label)`、ラベル無ければ Gmail 側で自動作成）
- `close()` / context manager サポート

### 3. `moppy_parser.py`

**抽出方針: URL正規表現 + callout 文脈確認（plaintext-first）**

実メール調査の結果、モッピーのクリックコインメールは **plaintext のみ**（HTML body なし）で、定型構造に従う：

```
https://pc.moppy.jp/cc/c?t=<base64-ish token>
▲(N日以内|明日まで)に上記URLアクセスで【Nコイン】GET！
```

候補抽出の手順:

1. `CLICK_COIN_URL_RE = r"https://pc\.moppy\.jp/cc/c\?t=[A-Za-z0-9+/=_\-]+"` で本文から URL を全件取得
2. 各 URL の **直後 200 文字以内** に `CALLOUT_RE = r"上記URLアクセスで\s*【\s*(\d{1,3})\s*コイン\s*】\s*GET"` がマッチするかで confirm
3. 同一 URL の重複は除外（メール末尾で同じ URL が再掲載されるパターンに対応）
4. callout の数値（`【1コイン】` の `1`）を `estimated_points` に格納

**HTML 対応（将来用）:** `is_html=True` で `BeautifulSoup` がタグ除去するが、`<a href="…">` の href を visible text 直前に inject してから regex を適用するので、anchor 経由でも検出可能。

**明示的除外**（regex 段階で）: `unsubscribe`, `optout`, `policy`, `/terms`, `/faq`, `/help`, `/login`, `/contact`, `edit_mail_flg`, `guide/`, `/info/rule`, `/friend/` を含む URL は候補から除く。実際にはこれらは `/cc/c?t=` パターンに該当しないので二重防御。

**異常検知（caller 側で扱う）:**
| 状態 | 動作 |
|---|---|
| 候補 0件 | テンプレ変更の疑い → Slack警告（msg_idのみ）、既読/ラベル付与しない |
| `url_without_callout` anomaly が出た | URL は見つかったが callout がない → テンプレ変更の可能性、警告 |
| 候補数が過去平均の3倍以上 | 同上 |
| body が plaintext / html いずれもない | parse失敗扱い |

**テスト:**
- `tests/fixtures/sample_1coin_ahamo.txt`（1 URL → 1 candidate, dedupe 確認用）
- `tests/fixtures/sample_5coin_recommend.txt`（5 URL → 5 candidates, 異なる token）
- callout バリエーション（5日以内 / 明日まで / 1〜3コイン）も parametrize でカバー
- 除外URLが混在する fixture で false positive 防止を検証

### 4. `clicker.py`

**HTTP コード分類:**
| ステータス | 扱い | リトライ |
|---|---|---|
| 2xx (最終到達) | success | - |
| 3xx 連鎖して2xx到達 | success | - |
| 3xx ループ/上限超 | failed_redirect | しない |
| 4xx | failed_4xx | しない |
| 5xx | failed_5xx | **しない**（GETは副作用あり、重複クリック回避） |
| 429 | failed_5xx 同等 | しない |
| timeout | failed_timeout | しない |
| 接続エラー | failed_connection | しない（同上の理由） |

> **GET の副作用に関する明示**: モッピーのクリックポイントURLは「GET 1回 = 1pt 確定」という副作用付き endpoint なので、**サーバ側で何が起きたか不明な失敗（5xx, 429, timeout, 接続エラー）は自動リトライしない**。再試行は state_store 経由で次回 cron 実行時に行うが、その際も「初回試行で2xxが返らなかった = 不明」として attempt_count をインクリメントし、`MAX_ATTEMPTS=3` で諦める。

**仕様:**
- `requests.Session()`（Cookie維持）
- User-Agent: 一般的な Chrome 文字列を固定
- タイムアウト: connect=10s, read=30s
- redirect 最大: 10
- 直列実行、間隔 `random.uniform(MIN, MAX)` 秒（デフォ 5-15）
- レスポンス本文は読まずに即破棄（メモリ削減＋情報残留防止）
- ログには redacted URL（host + path のみ、query なし）

### 5. `state_store.py`

**保存先:** `data/state.json`（GitHub Actions では artifact 経由で世代管理 — ※token は artifact しないが、実害のない state は OK）

> 補足: state artifact も「クリック実績の生データ」なので念のため redaction 済み（query除去済みURLのみ保存）にしておく。

**スキーマ:**
```json
{
  "version": 1,
  "messages": {
    "<message_id>": {
      "first_seen": "ISO8601",
      "last_attempt": "ISO8601",
      "attempt_count": 2,
      "urls": {
        "<url_hash_sha1>": {
          "redacted_url": "https://pc.moppy.jp/redirect/...",
          "status": "success" | "failed_xxx" | "pending",
          "last_status_at": "ISO8601",
          "http_status": 200
        }
      }
    }
  }
}
```

**API:**
- `is_url_done(message_id, url) -> bool`（success または attempt_count>=3 のとき True）
- `record_attempt(message_id, url, ClickResult)`
- `is_message_complete(message_id) -> bool`（全URLが success or 諦めた）
- `prune_old_entries(days=30)`

### 6. `notifier.py`

**通知先:** Slack Bot Token (`SLACK_BOT_TOKEN`) + 専用チャンネル (`SLACK_CHANNEL_MOPPY`)。Bot Token は ai-workflows 全プロジェクト共有、チャンネルだけ分ける。

**通常時:**
```
[point_sites] 2026-05-01 08:00 完了
✅ 成功: 12件 / 推定獲得: 14pt
❌ 失敗: 1件（pc.moppy.jp - HTTP 503）
⚠ パース失敗: 0件
処理時間: 3分42秒
```

**異常時（情報漏洩防止のため最小限）:**
```
[point_sites] ⚠ パース失敗 1件
  msg_id: 1923xxxxabc
  件名: 【モッピー】クリ... (先頭5文字のみ)
  → fixture 化して parser 修正必要
```

**情報漏洩対策:**
- 件名は先頭5文字のみ（個人ID/トラッキングID除去）
- URLは host のみ
- 本文HTMLは絶対に Slack に流さない（codex 指摘の修正点）
- パース失敗時のデバッグは「ローカルで該当 msg_id を再取得して fixture 化」する運用

### 7. `redaction.py`

```python
def redact_url(url: str) -> str:
    """https://pc.moppy.jp/redirect/abc?uid=123&token=xyz → https://pc.moppy.jp/redirect/abc"""

def redact_subject(subject: str, prefix_len: int = 5) -> str:
    """【モッピー】クリックで1pt獲得 → 【モッピ..."""
```

すべての `print` / `logger` / Slack payload 出力前にこれらを通す。

### 8. `config.py`

| 変数 | 必須 | デフォルト | 用途 |
|---|---|---|---|
| `GMAIL_USER` | ◯ | - | Gmail アドレス（フル） |
| `GMAIL_APP_PASSWORD` | ◯ | - | 16文字 app password（空白OK・自動strip） |
| `SLACK_BOT_TOKEN` | ◯ | - | Slack Bot User OAuth Token (`xoxb-...`)。全プロジェクト共有 |
| `SLACK_CHANNEL_MOPPY` | ◯ | - | Slack 通知先チャンネル ID または `#channel-name` |
| `MOPPY_GMAIL_QUERY` | - | `from:moppy.jp -label:moppy-clicked newer_than:3d` | Gmail検索クエリ（X-GM-RAW構文） |
| `MOPPY_DRY_RUN` | - | `0` | `1` でクリック実行せず候補一覧のみ通知 |
| `MOPPY_CLICK_INTERVAL_MIN` | - | `5` | クリック間隔最小秒（≥1） |
| `MOPPY_CLICK_INTERVAL_MAX` | - | `15` | クリック間隔最大秒（≥MIN） |
| `MOPPY_MAX_ATTEMPTS` | - | `3` | 同一URLの最大試行回数 |
| `MOPPY_MAX_MESSAGES` | - | `50` | 1実行あたり最大処理件数 |
| `MOPPY_STATE_PATH` | - | `data/state.json` | 状態ファイルパス |
| `MOPPY_LABEL` | - | `moppy-clicked` | 完了ラベル名 |
| `MOPPY_LOG_LEVEL` | - | `INFO` | ログレベル |
| `MOPPY_COOKIES` | - | （無し）= 匿名運用 | ブラウザでログイン後にエクスポートしたCookieのJSON配列。**未設定だと HTTP 200 が返ってもポイント未加算**（モッピー側で誰のクリックか識別不能）|
| `MOPPY_EXTRACT_LINKS` | - | `0` | `1` でクリック実行せず **ポイント加算リンクを Slack に投稿のみ**（ユーザーが手動クリック前提）。`MOPPY_COOKIES` 不要・state 変更なし・ラベル付与なし |

**起動時 validation:**
- `GMAIL_USER`: `@` を含むこと
- `GMAIL_APP_PASSWORD`: 空白除去後 16文字
- 数値: 型チェック + 範囲（INTERVAL_MIN ≥ 1, MIN ≤ MAX, MAX_ATTEMPTS 1-10, MAX_MESSAGES 1-500）
- `SLACK_BOT_TOKEN`: `xoxb-` で始まること
- `SLACK_CHANNEL_MOPPY`: 必須（チャンネル ID または `#name`）
- `MOPPY_COOKIES`: 未設定なら匿名運用（点未加算で警告ログ）。設定するなら有効な JSON 配列で `name`/`value` 必須
- 失敗時 fail-fast、Slack 通知は出さない（token/channel 自体が無効な可能性）

### 動作モード

| モード | 想定用途 | クリック | Cookie必須 | state/label変更 |
|---|---|---|---|---|
| `click`（デフォルト） | 通常運用、Cookie 注入で得点化 | ✅ 自動 | ✅ | あり |
| `--dry-run` | 開発時の確認、URLは host のみ redact | ✗ | ✗ | なし |
| `--extract-links` / `MOPPY_EXTRACT_LINKS=1` | **手動クリック運用**: 全URLをSlackに投稿、ユーザーがブラウザで踏む | ✗ | ✗ | なし |

`--extract-links` は cookie 取得前の暫定運用や、Cookie 失効時のフォールバックに使う。Slack には件名と全URLがそのまま流れるため、専用 channel（外部からは見えない）であることが前提。

### ログイン（Cookie 注入）

ポイント加算のためにはログイン済みセッションが必須。実装方針:

1. `MOPPY_COOKIES` には Chrome の DevTools (Application → Storage → Cookies) または `Cookie-Editor` 拡張で **`.moppy.jp` ドメインの全 Cookie を JSON エクスポート** したもの (配列) を渡す
2. `Clicker.__init__` でその Cookie を `requests.Session.cookies` に注入
3. クリック開始前に `Clicker.verify_login()` で `https://pc.moppy.jp/mypage/` を GET し、`ログアウト` リンクの存在で判定
4. ログイン未確認なら **クリックを開始せず** Slack に「Cookie 失効」通知 → 早期 abort
5. Cookie 期限切れは ~30日（モッピー側設定次第）。失効時は再エクスポート → GitHub Secret 更新

**Cookie JSON 形式（最小）:**
```json
[
  {"name": "PHPSESSID", "value": "xxx"},
  {"name": "user_token", "value": "yyy"}
]
```

`domain` / `path` は省略時 `.moppy.jp` / `/`。

**運用上の注意:**
- 規約違反リスクは増す（自動クリックがアカウントに紐づくため検知された場合の影響大）。実運用前にポイント変換を済ませること
- パスワードは保存しない（Cookie のみ。盗難時の被害は限定的）
- Cookie 失効時は通知ベースで気付ける設計

## CLI

```bash
cd point_sites && uv sync

# dry-run: クリックせず、候補一覧を Slack に通知
GMAIL_USER=... GMAIL_APP_PASSWORD=... \
  SLACK_BOT_TOKEN=xoxb-... SLACK_CHANNEL_MOPPY=#moppy \
  uv run python -m point_sites.main run --dry-run

# 本番実行
GMAIL_USER=... GMAIL_APP_PASSWORD=... \
  SLACK_BOT_TOKEN=xoxb-... SLACK_CHANNEL_MOPPY=#moppy \
  uv run python -m point_sites.main run

# 単一URL手動テスト（scheme/host が moppy 配下のみ受理）
uv run python -m point_sites.main click https://pc.moppy.jp/cc/c?t=...

# state 確認
uv run python -m point_sites.main state --message-id <uid>
```

**flag/env 優先順位:** CLI flag > 環境変数 > デフォルト

**dry-run の保証:** `--dry-run` 時は Gmail への既読・ラベル付与・state_store 書き込み・clicker 呼び出し は **すべて発火しない**。候補抽出と Slack notify のみ。

## GitHub Actions

`.github/workflows/moppy-clicker.yml` を参照（cron `0 23 * * *` UTC = JST 朝8時、`workflow_dispatch` で手動 dry-run 可能）。

**必要な GitHub Secrets:**
| Secret 名 | 値 |
|---|---|
| `GMAIL_USER` | Gmail アドレス（フル） |
| `GMAIL_APP_PASSWORD` | アプリパスワード（16文字、空白含んでもOK） |
| `SLACK_BOT_TOKEN` | Slack Bot User OAuth Token (`xoxb-...`)。ai-workflows 全プロジェクト共有 |
| `SLACK_CHANNEL_MOPPY` | Slack 通知先チャンネル（ID または `#name`） |

**安全策:**
- `concurrency` で cron×手動の二重起動を防止
- `continue-on-error` で初回 state なし時にも起動可能
- ファイルベースの secret は無し（環境変数のみ、cleanup 不要）
- state.json のみ artifact（token なし、本物 URL は redaction 済み）

## CI（PR 時）

`.github/workflows/moppy-clicker-ci.yml`:
- ruff check + ruff format --check
- mypy
- pytest（fixture ベースの parser/redaction/state_store テスト）
- 実 API には触らない

## やらないこと（明示・変更なし）

- 検知回避（プロキシ、TLS指紋、residential IP、headless ブラウザ、人間挙動シミュレーション等）
- マウス・スクロール等の人間挙動シミュレーション
- 複数アカウント並行運用
- ポイント以外のキャンペーン参加（アンケート、ガチャ等）

## 運用開始までに必要な手動セットアップ

1. **Gmail で 2段階認証を ON**: https://myaccount.google.com/security
2. **アプリパスワード生成**: https://myaccount.google.com/apppasswords → 16文字パスワードを発行
3. **Slack 通知先チャンネル準備**（モッピー専用、株分析等と兼用しない）。Bot を `/invite` で招待。Bot Token (`SLACK_BOT_TOKEN`) は ai-workflows 全プロジェクト共有のものを利用。
4. **モッピーへブラウザでログイン → Cookie をエクスポート**:
   - Chrome DevTools (F12) → Application タブ → Storage → Cookies → `.moppy.jp`
   - もしくは "Cookie-Editor" 拡張でエクスポート
   - 最低 `PHPSESSID` 等のセッション Cookie を `[{"name": "...", "value": "..."}]` の JSON 配列に整形
5. **GitHub Secrets 登録**（リポジトリ Settings → Secrets and variables → Actions）:
   - `GMAIL_USER` = メアド
   - `GMAIL_APP_PASSWORD` = ステップ2で発行したパスワード
   - `SLACK_BOT_TOKEN` = `xoxb-...`（既存の場合はそのまま）
   - `SLACK_CHANNEL_MOPPY` = ステップ3で決めたチャンネル名（例: `#moppy`）または ID
   - `MOPPY_COOKIES` = ステップ4で書き出した JSON（**未設定だとポイント加算されない**）
6. **`.github/workflows/moppy-clicker.yml` の cron 行のコメントを外す** → 定期実行開始
5. **手動 workflow_dispatch で dry-run** → Slack に候補リンクが届くか確認
6. 問題なければ自動 cron 運用へ
3. **Google Cloud Console** で OAuth client 作成 → Production 公開設定（refresh token 7日失効回避）
4. パース失敗時の運用ポリシー: 「ローカルで該当メールを再取得 → fixture 化 → parser 修正」を OK とするか
