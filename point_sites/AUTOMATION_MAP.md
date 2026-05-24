# point_sites 自動化マップ

各サイトで何を自動化しているか / 何ができないかの完全 inventory (2026-05-24 時点)。

新規 site 追加時 / 既存 site 改修時 / "もう実装できるものは無いのか" の質問が来た時にこのファイルを更新する。

## ✅ 自動化済 mechanism 表

| サイト | yield 方式 | 既実装 wizards 数 | daily banner | source |
|---|---|---|---|---|
| **moppy** | Gmail click + 自社 banner + 1 wizard | **1** (moppy_gacha) | 8 / day on `/gamecontents/` (selector `a[href*="/cc/c?t=CC"]`) | `GmailSource` |
| **pointincome** | Gmail extract-only | 0 | — | `GmailSource` (JP geofence で auto-click 不可、user 手動 click。`balance_uses_browser`) |
| **hapitas** | Gmail + banner + 5 wizards | **5** (takarakuji_exchange + GMO 4) | 15 / day on `hapitas.jp/` (selector `div.clickget_banner > a[href]`) | `GmailSource` |
| **amefuri** | Endpoint poll + browser_actions + 28 wizards | **28** (gmomedia 3 + panbon + column + stamp hub + 19 stamp sub + farm + gacha) | — | `EndpointPollSource(/account)` + `browser_actions` (login_visit_home, login_visit_account) |
| **fruitmail** | Gmail + 22 wizards | **22** (present_slot + bingo fail-soft + login_bonus + apricot + almond hub + 14 sub-game) | — | `GmailSource` |
| **pointtown** | OnsiteInbox + 8 wizards | **8** (login_bonus + treasure_box + gacha + GMO 4 + pointq) | — | `OnsiteInboxSource` |
| **getmoney** | OnsiteInbox + 6 wizards | **6** (estlier NUMBERS DX 38/71/83/94/102/112) | — | `OnsiteInboxSource` (dietnavi.com mail_notice) |
| **sugutama** | Gmail + 2 visit-only | **2** (game_hub + events、SPA anti-bot で visit-only) | — | `GmailSource`、`balance_uses_browser` |
| **warau** | Gmail + 1 visit-only | **1** (play_hub、SPA anti-bot で visit-only) | — | `GmailSource` |
| **gendama** | scaffolded (非 active) | 0 | — | 180-day inactivity rule で user 判断で disable |

**合計**: 73 wizards + 23 daily banners + 1 endpoint poll + 2 browser actions + 7 Gmail/OnsiteInbox click pipelines

## 📋 wizard 詳細 (site 別)

### amefuri (28 wizards)
- **gmomedia 3** (kantangame deeper、`a.c-n-btn-gameplay--start` → `a.c-n-btn-requid--medium`): easygame / quiz / spotdiff
- **estlier_panbon_slot** (i2ipoint rule.php → game_start.php)
- **estlier_column** (blind 1-step click `a.ui-btn.ui-btn-a` for daily column)
- **ibridge_stamp** hub (visit-only、sub-games は個別 wizard)
- **stamp 19 sub-games** (click_force JS evaluate で href-based selector): nanpre / keisan / eitango / shape_memory / sakana / crossword / jhistory / prefectures / shisokuenzan / dkanji / sanji / kokki / tsume_shogi / proverb / library / elavator / tenshoot / balance / movie
- **ibridge_farm** (`.btn_set.cont_m a.btn_positive` で game start nav + 30s 滞留)
- **gacha** (referer-only visit、server-side で entry tracking 記録)

### fruitmail (22 wizards)
- **present_slot** (#start / #stop)
- **bingo** (fail-soft、fruitmail 側 page 構造変更で form 不在)
- **login_bonus** (`.global_loginBonus__confirmButton`)
- **apricot_michannel** (`/cm/cmplay/<id>` 35s 視聴)
- **almond_estlier** hub (visit-only、sub-games は個別 wizard)
- **almond sub-games 14 種** (i2ipoint rule.php → game_start.php、woodcut.work platform 含む):
  - panbon_slot / panbon_roulette / kokuhaku / highlow / fashion / cook / dog (rule.php 系 4 + index.php 系 3)
  - tarzan (sv0401.contents-group.work 別 platform) — blind 2nd click
  - sarasara (`/sarasara/select.php`) — blind 2nd click
  - otsukai / yuusha / scratch / uranai (index.php 系) — blind 2nd click
  - train / egg / teruteru (rule.php 系) — 確実 2nd click

### hapitas (5 wizards)
- **takarakuji_exchange** (minitakarakuji 交換券 spend、4-step click)
- **gmo_easygame** + kantangame deeper
- **gmo_gesoten** + kantangame deeper
- **gmo_quiz** + kantangame deeper
- **gmo_spotdiff** + kantangame deeper

### pointtown (8 wizards)
- **login_bonus** (`.global_loginBonus__confirmButton`)
- **treasure_box** (3-step: login bonus modal → 宝箱 → claim、35s video)
- **gacha** (`a[href="/gacha/play"]` 1-step nav)
- **easygame/gesoten/brain_quiz/nazotore** (GMO 4、kantangame deeper)
- **pointq** (`a.btn-default[href="/pointq/input"]` 1-step nav)

### getmoney (6 wizards)
- **estlier 38/71/83/94/102/112** (NUMBERS DX 系、`.next_bt a × 2` で rule.php まで)

### moppy (1 wizard)
- **moppy_gacha** (3-step: `/pc_gacha/` → まわす (`a.a-moppy-gacha__btn`) → 結果を見る (同 selector) → 広告バナー (`a.a-banner` → `/pc_gacha/ad_click.php`))

### sugutama (2 wizards)
- **game_hub** (`/sugutama/game` visit-only)
- **events** (`/sugutama/events` visit-only)

### warau (1 wizard)
- **play_hub** (`/play/` visit-only)

## ❌ 実装できないもの (理由付き)

| 項目 | 区分 | 理由 | 復活条件 |
|---|---|---|---|
| warau / sugutama SPA games (個別 bingo/janken/ガラポン) | 真に難しい (anti-bot 実証) | stealth plugin / residential proxy / TLS fingerprint なしでは突破不可 (memory `project_warau_fortune_blocked`) | stealth plugin 進化 or VPS residential proxy |
| HTML5 canvas game の実プレイ | 真に難しい (非現実的) | canvas 自動化は OCR / computer vision + game-specific logic 必要 | n/a (out of scope) |
| pointtown gacha「回す」(動画 ad trigger) | 真に難しい (anti-bot リスク) | `#js-ad-mov-trigger-btn-txt` は video ad 視聴 trigger、warau 前例で detection 確実 | bot detection 緩和の実証 |
| amefri /game/gacha 深い click | server design 制約 | referer 付与でも 302→home、visit のみで entry tracking 記録される設計 | site UI 変更 |
| getmoney 数字選択 + submit | 真に難しい (anti-cheat 実証) | `rule.php` の `input.next_bt` (image submit) を JS click → `err_b.php?x=0&y=0` で「ツール等を利用してゲームを有利にすすめる行為」anti-cheat 警告 (run 26357587465) | 多重 framework 拡張 (image submit coords + multi-element random click) + bot pattern detection risk 受容 |
| fruitmail_bingo 実 click 復活 | vendor-side 問題 | `/bingo/index.php` に `#bingo_start` form 不在 (ad scaffolding のみ)、fruitmail 側 page 構造変更 | fruitmail 側で bingo 復活、もしくは新 selector recon |
| pointq クイズ自動回答 | CLAUDE.md policy NG (data fraud-adjacent) | AI-quiz random pick 25% は yield-to-risk 悪い + bot pattern detection 強い | policy 変更 (見込みなし) |
| 先着ボーナス (pointtown 等) | out of scope (CLAUDE.md) | 広告主商品の購入/申込必須 | n/a (実購入を伴うため自動化対象外) |
| moppy /gamecontents/{quiz,game,gesoten}_box_jump.php | 真に難しい | 3rd-party game jump → HTML5 canvas 実プレイ | computer vision 統合 |
| moppy /everyday/ | サイト側終了 | page 自体が「無効です」と表示 (2026-05-24 inspect) | site UI 復活 |
| moppy /gamecontents/mission/ direct | session required | 直接 visit で 403 Forbidden | session 経由 navigation 経路探索 |
| amefri estlier_column 個別 article click 深い | survey/column mix リスク | コラム/アンケート同 selector で混在、survey 自動入力 NG。1-step nav で impression 取得は実装済 | column vs survey selector 分離 |
| amefri ibridge_stamp hub | 設計通り visit-only | hub 自体は visit-only、sub-games (19 個) が個別 interactive wizard | n/a (最深到達) |
| amefri ibridge_farm | 最深到達済 | 既に「ゲームスタート」nav + 30s simulation | n/a |
| moppy daily_banner 以外の direct credit | 設計通り | Gmail / daily_banner で網羅、wizard 不要 | n/a |
| gendama | 非 active | 180-day inactivity rule、user 判断で disable | user が活性化決定すれば実装 |

## 🛠 framework patches (本セッション追加)

- `_content_with_retry`: `Page.content()` の "navigating" exception を 5 回 retry (2s 間隔) で吸収
- `--referer` flag in `cmd_html`: referer-gated page (amefri /game/gacha 等) を referer 付き inspect 可能
- `--inspect-click` repeating flag + navigation-context retry: post-click 構造 recon (moppy /pc_gacha/ 3-step recon に使用)
- `inspect_referer` / `inspect_clicks` input を全 9 site workflow YAML に露出

## 📊 ポイント増加期待値

各 wizard の credit registration depends on:
- 広告 NW (microad / google ads / kantangame 等) の view duration / engagement metric 判定
- moppy 側の click_id tracking
- 各 site の credit timing (即時 / 翌日 / 翌月)

**現実的見積もり** (sites 合計):
- 楽観 (理想 credit 全 register): 1500-2500 円/月
- 中庸 (50% silent no-op + 半数 credit register): 800-1500 円/月
- 保守 (impression のみで credit register せず): 300-500 円/月

実 yield 確認は **pending-verify (#32〜#42)** の 1 週間 monitor で観察。

## 🔄 更新 protocol

新 wizard 追加 / 既存 wizard 改修 / 「もう実装できるもの無い？」確認時に本ファイル更新。最終 commit hash を git log で追える状態にする。

最新更新: 2026-05-24 (moppy_gacha 追加、ultrathink audit 完了)
