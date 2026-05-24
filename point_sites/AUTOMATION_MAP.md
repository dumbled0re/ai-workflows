# point_sites 自動化マップ

各サイトで何を自動化しているか / 何ができないかの完全 inventory (2026-05-24 時点)。

ポイ活 (ポイントサイト) と抽選 (chanceit 等) の両用 framework。

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
| **chanceit** (NEW 2026-05-24、抽選専用) | wizards-only (source=None) + dynamic_wizard discovery | dynamic (毎日 14 件) | — | 「応募が簡単」list を毎日 scrape → 各 prize で「応募する」button click。会員 cookie 経由で server 側が PII auto-fill |
| **fruitmail_lottery** (NEW 2026-05-25、抽選専用) | wizards-only (source=None) + 5 daily_wizards | **5** (everyday / everyweek / everymonth / gorgeous / premium) | — | 既存 fruitmail cookie を流用、`#applyForm` で submit → `/prize/step1/` → 確認 button → 完了。`title_selector` で hidden `item_name` を抽出して Slack 表示 |
| **dreammail** (NEW 2026-05-25、抽選専用、Phase 1 skeleton) | wizards-only (source=None) + 2 daily_wizards + dynamic precam discovery | **2 + dynamic (~10)** (gacha + mmillion + /presents/precam/<id>) | — | Cookie 取得待ち、selector は blind guess。`/game/gacha` daily medal 獲得、`/mmillion` 50 medals で 100万円 entry、`/presents/precam/<id>` 0-medal promo の動的 discovery |

**合計**: 80 wizards + dynamic (chanceit ~14 + dreammail precam ~10/日) + 23 daily banners + 1 endpoint poll + 2 browser actions + 7 Gmail/OnsiteInbox click pipelines

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

### dreammail (NEW 2026-05-25、抽選専用、Phase 1)
- **status**: cookie 取得待ち、blind selector で initial run、後で inspect-driven refine
- **2 daily_wizards**:
  - `dreammail_daily_gacha` (`/game/gacha`) — 1 日 1 回ガチャ、10-100 medals payout
  - `dreammail_mmillion` (`/mmillion`) — 50 medals で 100万円 entry、月 1 抽選
- **dynamic_wizard**: `/presents` page を scrape → `a[href*="/presents/precam/"]` で 0-medal promo URL を抽出 (max 10 件) → template wizard で各 page を訪問 → 「応募する」/「ゆめキャンで応募」button click
- **source = None, lottery_mode = True, Gmail OAuth 不要**
- **user 作業**: dreammail 会員登録 + cookie export → `DREAMMAIL_COOKIES` Secret
- **Slack channel**: `SLACK_CHANNEL_CHANCEIT` (= #lottery) 共有
- **期待 yield**: medals 蓄積 + monthly 100万円当選確率 + precam promo 抽選数件/月
- **Phase 2 候補** (本実装には含まれない): GmailSource 経由のメルマガクリック型 (1000万円 entry path)、login bonus / 出席 wizard、`/game/seven` Amazon ギフト slot game

### fruitmail_lottery (NEW 2026-05-25、抽選専用)
- **5 daily_wizards** (`/prize/<category>/`): everyday / everyweek / everymonth / gorgeous / premium
  - 各 wizard: navigate → `pre_click_evaluate` で `<select name="selected_apply_number">` を 1 に set → `#applyForm button[type="submit"]` で `/prize/step1/` に POST → 確認 page で `button.prizeComponent_common__button[type="submit"]` を click → `/prize/step2/` で完了
  - `title_selector='input[name="item_name"]'` で hidden input の賞品名 (例: 「ドリームジャンボ宝くじ 10枚」) を抽出 → 「応募した賞品一覧」Slack に表示
- **source = None, lottery_mode = True**: click-mail は親 `fruitmail` adapter が処理。本 adapter は懸賞応募のみで「応募した賞品一覧」format
- **共有 credentials**: workflow で `FRUITMAIL_COOKIES` / `FRUITMAIL_USER` / `FRUITMAIL_PASS` を流用、user 追加作業ゼロ
- **Slack channel**: `SLACK_CHANNEL_CHANCEIT` (= #lottery) を共有
- **無料**: 「口数」は応募権数 (default 1 件/日)、ポイント消費なし。アンケート回答で口数追加できるが survey 自動回答は CLAUDE.md policy NG なので default 1 件/日で運用
- **premium 注意**: Diamond/Platinum/Black ランク限定、未到達アカウントは form 不在で fail-soft (silent no-op)
- **期待 yield**: 5 件/日 × 当選率 0.5-2% × 賞品平均 1,000-5,000円 = 100-500円/月 (保守)。豪華は 10 万円現金、毎日はジャンボ宝くじ 10 枚等

### chanceit (NEW 2026-05-24、抽選専用)
- **dynamic discovery**: 毎日 `https://www.chance.com/present/list.jsp?type=6` を scrape
  - `a[href*="/present/detail/"]` で個別 prize URL 抽出 (最大 20 件 cap)
  - 各 prize page で `a[href*="/jump.srv?id="]` (「応募する」button) click
  - 会員 cookie で server 側が PII (氏名/住所/電話/メール) を auto-fill
  - 公式「30秒足らずで応募」モデル、1 click で entry 完了
- **source = None**: 抽選専用、click-mail pipeline 不要。Gmail API setup 不要
- **期待 yield**: 月 ~30-50 件 entry × 当選率 1-5% × 賞品平均 3000-8000円 = 2,000-4,000円/月 (保守)
- **user 作業**: 新 Gmail で chanceit 会員登録 → cookie export → CHANCEIT_COOKIES Secret 登録
- **TOS 注意**: chanceit 規約 第 5 条「コンピュータウィルス等」「虚偽情報送信」禁止、自動 click 自体は明示禁止されていないが「不正行為」判定で会員停止/損害賠償 リスクあり。大量応募抑制で max_count=20

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
- **dynamic wizard discovery** (Adapter に 4 fields 追加): `dynamic_wizard_list_url` / `dynamic_wizard_link_selector` / `dynamic_wizard_template` / `dynamic_wizard_max_count`。daily で list page を scrape → 個別 link で wizard を動的生成 (chanceit 抽選専用)
- **wizards-only mode** (source=None 対応): chanceit 等の抽選専用 adapter は click-URL source を持たず、wizards のみで動作可能
- **DailyWizard.pre_click_evaluate** (2026-05-25 追加): navigation 後 / clicks 前に走る任意 JS。fruitmail_lottery で `<select>` の値設定に使用 (form `required` を越えるため)
- **DailyWizard.title_selector** (2026-05-25 追加): static lottery_mode wizards 向け prize title 抽出。INPUT/TEXTAREA は `value` 属性、それ以外は `textContent` を読む。空なら notifier の「(タイトル取得失敗)」fallback へ

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
