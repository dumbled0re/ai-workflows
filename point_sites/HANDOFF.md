# point_sites 引き継ぎプロンプト

新しい Claude セッションでこのプロジェクトを引き継ぐとき、まずこのファイルを読んでから作業を始めてください。`/Users/ritsushi/.claude/projects/-Users-ritsushi-git-ai-workflows/memory/MEMORY.md` の各 memory ファイルもあわせて読むこと。

最終更新: 2026-05-09 (アメフリ adapter 実環境動作確認 + cron 有効化 — daily login bonus credit 観察モード)

---

## このプロジェクトは何か

`/Users/ritsushi/git/ai-workflows/point_sites/` は **日本のポイ活サイト自動化のための multi-site framework**。各サイトの「クリックでポイント」メールを Gmail IMAP で受信 → URL を抽出 → 認証付き HTTP GET で踏む → Slack 通知。

目的: 個人用、自分のアカウントでクリックポイントを自動収集する。**第三者広告クリック (= 広告 fraud) は絶対やらない**。

実装済み adapter:
- `moppy` (本番運用中、毎日 JST 8:00 cron)
- `pointincome` (scaffold 完了、Cookie 登録 + 実 regex 検証待ち)
- `hapitas` (scaffold、JST 8:30 cron、2FA OFF 前提・Cookie 登録待ち)
- `chobirich` (scaffold、JST 8:45 cron、Cookie 登録待ち)
- `gendama` (scaffold、JST 9:00 cron、180日休眠で account 消滅リスクあり、enable 前に user 判断必要)

未着手 (新 Source 実装が必要、framework 側は ada6d9e で完了済):
- `ポイントタウン` — on-site inbox model (Gmail 不要、サイト内のメールボックスを scrape する path)。`OnsiteInboxSource` を `common/sources/` に追加して adapter から inject すれば動く
- `アメフリ` — daily login bonus endpoint poll (Gmail なし、HTTP GET 1発で完結)。`EndpointPollSource` を同様に追加

その他 (低優先):
- `ECナビ`, `ニフティポイントクラブ`, `ワラウ` 等 — yield 低・blocker あり、agent research で除外推奨

## 重要な設計上の制約 (絶対に変えない)

1. **広告 fraud にしない**: 第三者広告ネットワーク (Google Ads / Yahoo Ads / GMOSSP 等) のクリック自動化は不可。サイト内エンゲージメント (クリックメール / ログインボーナス等) のみ
2. **実プレイ・実インストール不要なものだけ**: ゲーム / アンケート / 動画視聴は対象外
3. **規約 grey は OK、法律 grey は NG**: 各ポイ活サイトの「自動化禁止」TOS 違反は user 承知の上 (個人運用)。広告 fraud / 偽計業務妨害は絶対 NG
4. **server-side HTTP のみ**: GitHub Actions runner 内で完結。Playwright / モバイルエミュレータ等は使わない
5. **副作用検知の3層必須** (`CLAUDE.md` の自律ワークフロー設計原則): 検知 (balance scraping) → 記録 (outcomes.jsonl) → 判断 (degradation alert)

詳細は memory `project_moppy_gacha_blocked.md` 参照 (なぜガチャが NG だったかの実例)。

## 現在の状態 (2026-05-09)

- **Moppy**: 本番運用中。毎朝 JST 8:00 cron で動作。Cookie persistence + balance verification + outcome tracking 全部稼働
- **アメフリ**: ✅ end-to-end 動作確認済 (commit `35b08e2`)。Cookie 登録 + discover + click 全て成功、balance scrape 動作 (regex `ownedPoint__point` anchor)、`vars.AMEFURI_CRON_MODE=click` 設定済 → JST 9:15 daily cron で credited 自動実行中。**ただし login bonus が即時 credit されない** (balance_before == balance_after == 0)。アメフリの bonus が「session GET trigger」か「SSO 完全 trigger」かは 1〜2日の観察で判定 (前者なら balance が翌朝 1pt に、後者なら 0 のまま)。後者なら adapter 無効化 + 再検討
- **ポイントインカム / ハピタス / ちょびリッチ / げん玉**: コードは入ってる、ただし Cookie 未登録・regex は best-guess。user が `<SITE>_COOKIES` + `SLACK_CHANNEL_<SITE>` Secret を登録 + Gmail 移行 (アメフリと同じ ポイ活専用 Gmail への切替) を済ませてから本番化開始
- **ポイントタウン**: ⏸️ Cookie 未登録。GMO の anti-fraud が業界最強なので、最初は extract-links モードで様子見必須
- **framework 拡張 (Source Protocol)**: ✅ 完了 (commit `ada6d9e` + `e5d7f40`)。`common/sources/` に `ClickUrlSource` Protocol + `GmailSource` + `EndpointPollSource` + `OnsiteInboxSource` 実装。既存 7 adapter は `source=...` 注入に移行。`cmd_run` は source 駆動 loop に refactor 済。挙動不変、95 tests + ruff + mypy strict 全部 green

最新コミット系列 (master):
```
35b08e2 amefuri: point endpoint at /account + add site-specific balance regex
b97406f amefuri: fix mypage URL (/account, not /mypage which 404s) + SSO host
e09a4ad CLAUDE.md: codex collab is for "unsure / want a second opinion"
e5d7f40 point_sites: add amefuri (endpoint-poll) + pointtown (onsite-inbox) adapters
94e0d67 point_sites: HANDOFF.md update for Source Protocol landing
ada6d9e point_sites: introduce ClickUrlSource Protocol (B寄り C-lite)
a6a7396 point_sites: scaffold hapitas/chobirich/gendama adapters + workflows
1df1387 moppy_clicker.yml → moppy.yml + reusable _site-runner (Phase 2)
```

## アーキテクチャ概要

```
point_sites/
├── pyproject.toml                      ← uv プロジェクト
├── point_sites/
│   ├── common/                         ← 全 adapter 共通の基盤
│   │   ├── adapter.py                  ← Adapter dataclass (source: ClickUrlSource を持つ)
│   │   ├── sources/                    ← Click URL の出所を抽象化 (新)
│   │   │   ├── base.py                 ← ClickUrlSource Protocol + ClickBatch
│   │   │   └── gmail.py                ← GmailSource (IMAP-driven)
│   │   ├── clicker.py                  ← HTTP GET + verify_login
│   │   ├── balance.py                  ← mypage 残高 scraping
│   │   ├── cookie_store.py             ← rotated cookie jar の永続化
│   │   ├── outcome_tracker.py          ← 加算検証 + degradation 検知
│   │   ├── notifier.py                 ← Slack
│   │   ├── gmail_client.py             ← IMAP (GmailSource から呼ばれる)
│   │   ├── discover.py                 ← read-only サイト recon
│   │   ├── state_store.py              ← URL重複防止
│   │   ├── redaction.py                ← URL/log redaction
│   │   └── models.py                   ← Pydantic models
│   ├── adapters/
│   │   ├── __init__.py                 ← REGISTRY (--site name → Adapter)
│   │   ├── moppy/                      ← source=GmailSource(parse_email=...)
│   │   ├── pointincome/                ← scaffold + GmailSource、Cookie 未登録
│   │   ├── hapitas/                    ← scaffold + GmailSource、Cookie 未登録
│   │   ├── chobirich/                  ← scaffold + GmailSource、Cookie 未登録
│   │   └── gendama/                    ← scaffold + GmailSource、enable 前 user 判断必要
│   ├── config.py                       ← env → Config
│   └── main.py                         ← CLI: run / click / balance / discover / html / state
├── tests/                              ← pytest 95 件
└── data/                               ← gitignore (per-site: data/moppy/, data/pointincome/)
    └── <site>/
        ├── state.json                  ← URL 重複防止
        ├── cookies.json                ← rotated jar
        └── outcomes.jsonl              ← 加算検証履歴 (degradation 元データ)

.github/workflows/
├── _site-runner.yml                    ← 全サイト共通 reusable workflow_call
├── moppy.yml                           ← Moppy 専用 (cron + dispatch)
├── pointincome.yml                     ← ポイントインカム専用 (cron + dispatch)
├── point_sites-ci.yml                  ← mypy CI (PR時)
└── ci.yml                              ← 全プロジェクト共通 ruff/pytest
```

## CLI

```bash
cd point_sites

# 各サイト:
uv run python -m point_sites.main run --site moppy       # クリック実行
uv run python -m point_sites.main balance --site moppy   # 残高だけ取得
uv run python -m point_sites.main discover --site moppy  # 毎日貯める recon
uv run python -m point_sites.main html --site moppy <URL> # 単 URL の HTML dump
```

## 新しいサイトを追加する手順

このフレームワークなら **3ファイル + 2 secrets** で追加可能。所要時間 30〜90 分 (実 regex 検証込み)。

### 1. Adapter コードを書く

`point_sites/adapters/<site>/__init__.py`:

```python
from ...common.adapter import Adapter
from ...common.balance import DEFAULT_BALANCE_PATTERNS
from ...common.sources import GmailSource
from .parser import parse as parse_email

ADAPTER = Adapter(
    name="<site>",                        # 環境変数の prefix になる: <SITE>_COOKIES, SLACK_CHANNEL_<SITE>
    site_label="<日本語名>",
    mypage_url="https://<host>/mypage/",
    allowed_hosts=frozenset({"<host>"}),
    login_keyword="ログアウト",            # logged-in mypage に必ず出る文字列
    gmail_query="from:<host> -label:<site>-clicked -label:<site>-no-coins newer_than:3d",
    clicked_label="<site>-clicked",
    no_coins_label="<site>-no-coins",
    source=GmailSource(parse_email=parse_email),  # Gmail 駆動の場合
    balance_patterns=DEFAULT_BALANCE_PATTERNS,    # 多くの日本ポイ活サイトで通用
    discover_seeds=("https://<host>/mypage/", "https://<host>/<daily-page>/"),
)
```

Gmail 以外の source を使いたい場合 (ポイントタウン on-site inbox / アメフリ endpoint poll):
1. `common/sources/<kind>.py` に `OnsiteInboxSource` / `EndpointPollSource` を実装 (`ClickUrlSource` Protocol を満たす)
2. `common/sources/__init__.py` に export を追加
3. adapter 側は `source=OnsiteInboxSource(...)` 等を inject。`gmail_query` / `clicked_label` / `no_coins_label` は空のままで OK
4. それ以外 (mypage_url / balance_patterns / discover_seeds) は Gmail と同じ

`point_sites/adapters/<site>/parser.py`: `adapters/moppy/parser.py` をコピーして CLICK_COIN_URL_RE / CALLOUT_RE を当該サイト用に変更。最初は best-guess regex で OK、discover 流して実物見てから refine。

### 2. REGISTRY に追加

`point_sites/adapters/__init__.py`:

```python
from .<site> import ADAPTER as <SITE_UPPER>

REGISTRY: dict[str, Adapter] = {
    MOPPY.name: MOPPY,
    POINTINCOME.name: POINTINCOME,
    <SITE_UPPER>.name: <SITE_UPPER>,    # ← 追加
}
```

### 3. workflow yml を書く

`.github/workflows/<site>.yml`: `pointincome.yml` をコピーして名前と Secret 名を置換。cron 時刻は他サイトと **数十分ずらす** (同時刻だと bot 的)。

```yaml
on:
  schedule:
    - cron: "30 23 * * *"  # JST 8:30 (moppy 8:00, pointincome 8:15 と分ける)
  workflow_dispatch:
    ...
jobs:
  run:
    uses: ./.github/workflows/_site-runner.yml
    with:
      site: <site>
      cron_mode: ${{ vars.<SITE_UPPER>_CRON_MODE }}
      ...
    secrets:
      slack_bot_token: ${{ secrets.SLACK_BOT_TOKEN }}
      slack_channel: ${{ secrets.SLACK_CHANNEL_<SITE_UPPER> }}
      cookies: ${{ secrets.<SITE_UPPER>_COOKIES }}
      gmail_user: ${{ secrets.GMAIL_USER }}
      gmail_app_password: ${{ secrets.GMAIL_APP_PASSWORD }}
```

### 4. Secrets 登録 (user 作業)

- `<SITE_UPPER>_COOKIES`: ブラウザでログイン後 Cookie-Editor で JSON エクスポート
- `SLACK_CHANNEL_<SITE_UPPER>`: 通知先 Slack チャンネル

### 5. 検証 → 本番化

```bash
# (a) 接続確認: discover 流してログイン通過 + ページ構造確認
gh workflow run <site>.yml --repo dumbled0re/ai-workflows -f discover=true

# (b) ログから実 click email URL pattern を読んで parser.py を refine

# (c) extract モードで Slack に URL 流して手で確認
gh workflow run <site>.yml --repo dumbled0re/ai-workflows -f extract_links=true

# (d) OK なら本番化: GitHub Variables に <SITE_UPPER>_CRON_MODE = click を設定
#     翌朝の cron から credited auto-click 開始
```

## 開発ルール (CLAUDE.md / memory より)

- **commit / push: 自由**: 個人 repo なので確認なしで master push してよい (memory `feedback_git_policy.md`)
- **codex 相談**: 設計判断や非自明な変更は `codex exec "<相談>"` または `codex review --uncommitted` で second opinion (memory `feedback_codex_collab.md`)
- **lint/format/mypy/test 必須**: コミット前に `cd point_sites && uv run ruff check . && uv run ruff format --check . && uv run mypy point_sites && uv run pytest` 全部 green
- **副作用は3層検証**: 検知 (balance) → 記録 (outcomes) → 判断 (degradation) → next-action 付き Slack 通知
- **新規サイト追加は CLAUDE.md の「自律ワークフロー設計原則」を満たすこと**

## 候補のリサーチ結果 (2026-05-09 時点)

別 Claude session のリサーチ結果。優先順:

| 順位 | サイト | yield (pt/day) | 既存コード再利用 | 注意 |
|---|---|---|---|---|
| 1 | **ポイントインカム** | ~6-9 | ~95% (同親会社) | 検知ポリシーも Moppy と同じ可能性 → 分散にならない |
| 2 | **ポイントタウン (GMO)** | 数十円/月 | ~70% (Gmail 不要、サイト内 inbox) | GMO の anti-fraud が業界最強 |
| 3 | **アメフリ** | 10pt (=1円)/日 | ~80% | 純粋ログインボーナスのみ・小額だが純度高 |

避けるべき:
- ハピタス (2024-10 から TOTP 2FA 導入、headless GHA で対応困難)
- ちょびリッチ (2025-11 から banner credit が 1 click/account に減額)
- ECナビ / ニフティ / げん玉 (yield 低 or 仕組み複雑)

業界全体の傾向: 2025 年以降、anti-fraud 強化で yield 縮小傾向。**Moppy 1サイトで概ね天井**、追加は marginal gain。

## 次の Claude が「新しいサイト追加」を頼まれたら

1. user に **どのサイト** か確認
2. memory `project_point_sites_state.md` 読んで現状把握
3. **public 情報で当該サイトを recon** (login URL / mypage URL / 毎日貯める ページ)
4. 「新しいサイトを追加する手順」を上から実行
5. **TOS の自動化禁止 + 広告クリック必須仕様** を必ずチェック (Moppy ガチャ事例のように、技術的に可能でも倫理的に NG なケースを見落とさない)
6. discover 結果を見て regex を refine
7. extract_links → click の段階的本番化

不明な設計判断は user に確認するか codex に相談。**独走しない**。

## 各 adapter の Cookie 登録手順 (共通)

1. ブラウザで対象サイトにログイン（PC、Cookie-Editor 拡張入り）
2. Cookie-Editor → Export → Export as JSON で対象ドメインの Cookie を全部 JSON 配列として取得
3. GitHub repo Settings → Secrets and variables → Actions → New repository secret
   - 名前: `<SITE_UPPER>_COOKIES` (例: `HAPITAS_COOKIES`)
   - 値: コピーした JSON
4. 通知用 Slack channel を準備、Bot 招待。Secret に `SLACK_CHANNEL_<SITE_UPPER>` 追加
5. `gh workflow run <site>.yml --repo dumbled0re/ai-workflows -f discover=true` で接続確認
6. ログから実 click email URL pattern を読み、`adapters/<site>/parser.py` の `CLICK_COIN_URL_RE` / `CALLOUT_RE` を refine
7. workflow を `extract_links=true` で1日試して Slack に URL が流れること確認
8. OK なら GitHub Variables に `<SITE_UPPER>_CRON_MODE = click` を設定して本番化

## 開発周辺の skill (2026-05-09)

- ✅ `~/.claude/skills/handoff/SKILL.md` 作成済 (2026-05-09)。`/handoff` で HANDOFF.md 更新 + memory refresh + 新セッション resume prompt の生成を1ステップで実行できる。session が長くなって context 圧迫してきた時 / `/clear` 前 / 中断点を残したいときに invoke。複数プロジェクト共通

## 次にやること (優先順)

1. **新規 adapter (Gmail 駆動) の本番化** — user が Cookie Secret 登録してから:
   - `pointincome` / `hapitas` / `chobirich`: `gh workflow run <site>.yml -f discover=true` → ログから実 click email URL pattern を読んで `parser.py` の regex を refine → `extract_links=true` で1日試して URL が想定通り → `vars.<SITE>_CRON_MODE=click` で本番化
   - `gendama`: 180 日休眠ルールで account 消滅リスクあり、enable は user 判断必要

2. **新 Source 実装 (framework 側は完了済)**:
   - **アメフリ** が一番楽 (`EndpointPollSource` で1日1 GET、balance delta で credit 検証)。yield 小さいが実装コスト低
   - **ポイントタウン** は `OnsiteInboxSource` 実装が必要 (inbox HTML scrape + state_key = inbox URL)。実装コスト中
   - 着手前に user に **どっち先か** 確認

3. (低優先) parser 単体テスト追加 — 各 adapter の `parser.py` を fake HTML/text で exercise する pytest fixture。実メールが流れるまで限定的だが、regex typo を CI で catch できる

## 既存 memory 一覧

- `feedback_git_policy.md` — master 直接 push OK
- `feedback_codex_collab.md` — 設計相談に codex を使う
- `project_moppy_cookie_strategy.md` — Cookie は失効時のみ手動更新 (自動ログイン化はリスク見合わず)
- `project_moppy_gacha_blocked.md` — ガチャ自動化は広告 fraud で不可
- `project_point_sites_state.md` — このプロジェクトの状態スナップショット
- `project_handoff_pointer.md` — このファイルへのポインタ

## ユーザーが寝てる/離席中に独走できる範囲

OK:
- コード修正・refactor・push (個人 repo policy)
- workflow_dispatch でテスト走らせる (cookie cycle はちょっと burn する点に留意)
- codex 相談
- memory への state 保存

要 user 確認:
- 新規 Secrets 登録 (Cookie 等のシークレットは user しか取れない)
- アカウント作成 (電話認証等)
- 規約上のリスクが大きい変更 (広告 fraud 寄りの自動化など)
- 強い破壊的操作 (force push, 大量 file 削除等)
