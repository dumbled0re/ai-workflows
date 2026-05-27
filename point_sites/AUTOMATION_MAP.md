# point_sites 自動化マップ

各サイトで何を自動化しているか / 何ができないかの完全 inventory (2026-05-25 時点、HANDOFF P1-P3 反映)。

ポイ活 (ポイントサイト) と抽選 (chanceit / fruitmail_lottery / dreammail) の両用 framework。

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
| **chanceit** (2026-05-24 初実装 / 2026-05-25 target-strip + verify + 9 daily missions / 2026-05-27 easy-entry のみに巻き戻し) | wizards-only (source=None) + 9 static + dynamic easy-entry only | **9 static + dynamic ~14 件/日** | — | `/present/list/easy-entry/` (応募形式=応募が簡単 のみ) を毎日 scrape → 各 prize page で `a[href*="/jump.srv?id="]` (`target="_blank"` を `pre_click_evaluate` で剥離) を click。`success_url_pattern=r"^(?!.*/present/detail/)(?!.*error)"` で server 受理確認。加えて `/mypage/tasklist.jsp` の article-view 系 9 件 (visit-only) を毎日訪問してスタンプ加算 |
| **fruitmail_lottery** (NEW 2026-05-25、抽選専用) | wizards-only (source=None) + 5 daily_wizards | **5** verified (everyday / everyweek / everymonth / gorgeous / premium) | — | 既存 fruitmail cookie + ID/PW を流用、4-step click flow: `/prize/<cat>/` → step1 (登録情報確認) → step2 (送付先確認) → step3 (最終確認) → `/prize/end/?page=<cat>` (応募完了)。`success_url_pattern=r"/prize/end/"` で真の完了 verify、`title_selector='input[name="item_name"]'` で hidden 賞品名を Slack 表示 |
| **dreammail** (NEW 2026-05-25、抽選専用 Phase 1 完了) | wizards-only (source=None) + 2 daily_wizards + dynamic precam discovery | **2 verified + dynamic ~7/日** (gacha + mmillion + /presents/precam/<id>) | — | password_login (ID/PW) で fresh login → cookie rotate。gacha: `#btn-submit` → `/game/gacha/lotteried`。precam: `pre_click_evaluate` で target=_blank 剥離 → 外部 ad-network 遷移。mmillion: 50 medals/口 必要 (medals 蓄積後に動く、現状「未確定」) |

**合計**: **94 static wizards** (73 ポイ活 + 21 抽選) + **dynamic (chanceit ~14 + dreammail precam ~7)/日** + 23 daily banners + 1 endpoint poll + 2 browser actions + 7 Gmail/OnsiteInbox click pipelines + 2 password_login fallback (fruitmail / dreammail、chanceit は IP block で不可)

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

### chanceit (2026-05-24 初実装 / 2026-05-25 target-strip + verify 確立 / 2026-05-27 easy-entry のみに巻き戻し + tasklist 9 missions)
- **status**: ✅ 動作確認済 (run 26387753635 で 12/12 が外部 partner サイト遷移)。2026-05-27 巻き戻し: 賞品カテゴリ list (cash-giftcard 等) に X(Twitter)から応募 形式の prize が混入する user 観察 (49 件中の偽陽性源) を受け、easy-entry のみに戻し。9 static daily missions (article visit) は維持
- **dynamic discovery**: 毎日 `/present/list/easy-entry/` のみを scrape (slug-based 形式に移行済 — legacy `?type=N` は redirect が変)
  - 応募形式 = 「応募が簡単」 のみを含む list なので、X 投稿 / Facebook / Instagram / LINE 投稿系 prize は構造的に混入しない
  - `a[href*="/present/detail/"]` で個別 prize URL 抽出 → 最大 20 件 cap (~14 件/日)
  - 各 prize page で `a[href*="/jump.srv?id="]` (「応募する」button) click
- **NG 巻き戻し** (2026-05-27): 賞品カテゴリ拡張をやり直す場合は「応募形式 = 応募が簡単 ∩ 賞品カテゴリ」 の交差 list URL が必要、または detail page の「応募形式」 td を post-filter する設計が必要
- **9 static daily missions** (`/mypage/tasklist.jsp` article visits, visit-only):
  - `/article/ranking/?g=6` 芸能人ランキング (10pt)
  - `/article/ranking/?g=5` エンタメランキング (10pt)
  - `/article/ranking/?g=7` ライフスタイルランキング (10pt)
  - `/article/prenew-entertainment/` (3pt)
  - `/article/prenew-lifestyle/` (3pt)
  - `/article/dog/` いぬのきもち (3pt)
  - `/article/cat/` ねこのきもち (3pt)
  - `/article/ichioshi.srv` イチオシ (3pt)
  - `/article/ai/` (3pt)
  - 各 wizard は clicks=() の URL 訪問のみで server-side スタンプ加算を期待。`success_url_pattern=r"chance\.com/article/"` で page load 確認
- **target='_blank' 対応**: apply anchor は `target="_blank"` で新 tab を開く構造のため、`pre_click_evaluate` で全 anchor の target を `_self` に書換える → click 後 same-tab で外部 partner site (`genki-mama.com` / `shopping.yahoo.co.jp` / `mosimo.net` / `nipponham-furusato.jp` 等) に navigation。会員 cookie で server 側 PII auto-fill
- **success_url_pattern**: `r"^(?!.*/present/detail/)(?!.*error)"` で `/present/detail/<id>/` 残留 = silent no-op = 未確定、それ以外 = verified
- **password_login 不可**: GitHub Actions runner IP (US data center) からの login を chanceit server が拒否 (IP geofence + device fingerprint anti-bot)。cookie 失効時は user 手動 Cookie-Editor 再 export が必要 (`CHANCEIT_COOKIES` Secret 更新)
- **2026-05-24 偽陽性事故 (resolved)**: 元実装は target=_blank で全 click silent no-op + success_url_pattern 未設定で「応募確認済 13 件」誤通知。`success_url_pattern` + `pre_click_evaluate` の 2 段構えで対処済
- **source = None, lottery_mode = True**: click-mail pipeline 不要、Gmail API setup 不要
- **Slack channel**: `SLACK_CHANNEL_CHANCEIT` (= #lottery) — 抽選 3 site で共有
- **期待 yield**: 月 ~360-600 件 entry × 当選率 0.5-2% × 賞品平均 1000-5000円 = 1,500-3,000円/月 (保守)
- **TOS 注意**: 利用規約 第 5 条「コンピュータウィルス等」「虚偽情報送信」禁止、自動 click 自体は明示禁止されていないが「不正行為」判定で会員停止/損害賠償リスクあり。大量応募抑制で `dynamic_wizard_max_count=20`

### fruitmail_lottery (NEW 2026-05-25、抽選専用)
- **status**: ✅ **server-confirmed** (run 26381148051 で `/prize/everyweek/` の「応募済み口数: 8」widget で実応募登録を直接確認)
- **5 daily_wizards** (`/prize/<category>/`): everyday / everyweek / everymonth / gorgeous / premium
- **4-step click flow** (2026-05-25 数次の inspect で確定):
  - Step 0: navigate `/prize/<cat>/` → `pre_click_evaluate` で `<select name="selected_apply_number">` を 1 に set → `#applyForm button[type="submit"]` で POST → `/prize/step1/`
  - Step 1 (`/prize/step1/` 登録情報の確認): forward-only submit → `/prize/step2/`
  - Step 2 (`/prize/step2/` 送付先の確認): `:not(.prizeComponent_common__button--secondary)` で back button 除外 → forward submit → `/prize/step3/`
  - Step 3 (`/prize/step3/` 最終確認): forward submit (「応募する」) → **`/prize/end/?page=<cat>` (応募完了)**
- **forward selector**: `button.prizeComponent_common__button[type="submit"]:not(.prizeComponent_common__button--secondary)` で 「戻る」を除外して 「確認して次へ / 応募する」 だけを hit
- **success_url_pattern**: `r"/prize/end/"`
- **title_selector**: `input[name="item_name"]` で hidden input の賞品名 (例: 「ドリームジャンボ宝くじ 10枚」) を抽出 → Slack 「応募した賞品一覧」 に表示
- **source = None, lottery_mode = True**: click-mail は親 `fruitmail` adapter が処理。本 adapter は懸賞応募のみで「応募確認済 / 未確定」 format
- **共有 credentials**: workflow で `FRUITMAIL_COOKIES` / `FRUITMAIL_USER` / `FRUITMAIL_PASS` を流用、user 追加作業ゼロ。cookie 失効時は password_login fallback で自動復旧
- **Slack channel**: `SLACK_CHANNEL_CHANCEIT` (= #lottery) を共有
- **無料**: 「口数」は応募権数 (default 1 件/日)、ポイント消費なし。アンケート回答で口数追加できるが survey 自動回答は CLAUDE.md policy NG なので default 1 件/日で運用
- **multi-口 カテゴリ**: 毎週 (「何度も応募してGET」) / 豪華 (「応募すればするほど」) は 1 日複数口可能。cron daily で累積する設計
- **premium 注意**: Diamond/Platinum/Black ランク限定、未到達アカウントは form 不在で「未確定」になる (fail-soft)
- **期待 yield**: 5 件/日 × 当選率 0.5-2% × 賞品平均 1,000-5,000円 = 100-500円/月 (保守)。豪華は 10 万円現金、毎日はジャンボ宝くじ 10 枚等

### dreammail (NEW 2026-05-25、抽選専用 Phase 1 完了)
- **status**: ✅ password_login + gacha + precam 動作確認済 (run 26380319824 / 26381185075)、mmillion は medals 蓄積待ち
- **2 daily_wizards**:
  - `dreammail_daily_gacha` (`/game/gacha`): `#btn-submit` 1 click → form POST → **`/game/gacha/lotteried`** (lottery 過去形) で完了、結果メール送付。success_url_pattern=`r"/game/gacha/lotteried"`
  - `dreammail_mmillion` (`/mmillion`): `#btnMmillionDailyApply` (50 medals/口、type=button で confirm modal を開く) → `#confirmYes` (modal の「はい」) → form JS submit → `/mmillion/apply`。**残高 0 メダルで silent fail、gacha 蓄積後に動作開始**
- **dynamic_wizard (precam, 0-medal promo)**:
  - `/presents` page から `a[href*="/presents/precam/"]` で URL 抽出 (max 10 件、実 7 件程度)
  - 各 precam page で `pre_click_evaluate` (`document.querySelectorAll('a.gotoLink, a[target="_blank"]').forEach(a => a.target = '_self')`) で target を剥離して same-tab navigation 化
  - click 後 外部 ad-network host (`shopping.yahoo.co.jp` / `cp.manara.jp` / `fasttrack-2hr.com` / `life.bang.co.jp` 等) に遷移、success_url_pattern=`r"^https://(?!.*dreammail\.jp)(?!.*error)"` で verify
  - **注意**: アンケート系 precam (`cp.manara.jp` 等) が混入する → CLAUDE.md policy NG、要 discovery 段階 filter (Phase 2)
- **source = None, lottery_mode = True, Gmail OAuth 不要**
- **password_login fallback**: ID/PW で fresh login (run 26379981201 で動作確認)。cookie 寿命短い chanceit と違い自動復旧する
- **user 作業 (1 回)**: dreammail 会員登録 (PII 必要) + `DREAMMAIL_COOKIES` Secret + `DREAMMAIL_USER` + `DREAMMAIL_PASS` Secret
- **Slack channel**: `SLACK_CHANNEL_CHANCEIT` (= #lottery) を共有
- **期待 yield**: gacha 10-100 medals/日 + precam ~7 件/日 × 当選率 ~1% + (medals 50 蓄積後) mmillion 1 口/日 (月 1 抽選 100 万円)
- **Phase 2 候補 (未着手、HANDOFF.md 参照)**:
  - 他ゲーム (`/game/farm`, `/game/uquiz`, `/game/bingo` 等 20+ 種) — 1-click 系のみ実装方針
  - マンスリーキーワード (16 口蓄積で 0-medal 追加応募)
  - シークレットキーワード (SNS scrape 必要)
  - 1000 万円 (`/tenmillion`) — メルマガクリック型 (lottery Gmail OAuth 必要)
  - 通常懸賞 (`/presents/landing/<id>`) — 10 medals/口、medal 経済の implementation 重い

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
| chanceit password_login (cookie 失効時の自動復旧) | server 側の IP / fingerprint anti-bot | GitHub Actions runner IP (US data center) からの login を server-side 拒否、credentials 正しくても login.srv に form 再表示 (run 26400839026 / 26400937339)。fruitmail / dreammail の password_login は同 runner から動くため、chanceit 固有の挙動。pointincome JP geofence と同種 | JP IP からの login (self-hosted JP runner or VPS)、ただし TOS リスク考慮要 |
| dreammail 1000万円 (`/tenmillion`) | 設計判断で skip | メルマガクリック型 = lottery Gmail OAuth setup が user 作業重い (Google Cloud Console での credential 作成 + `LOTTERY_GMAIL_*` Secrets 登録) | user が OAuth setup OK 出せば実装可、yield は月 1000 万当選確率なので低めだが 1 回 hit すれば桁外れ |
| dreammail 通常懸賞 (`/presents/landing/<id>`) | medal 経済の重さ | 10 medals/口 + 1 口目 PII form。40+ 件/日存在するが medals 蓄積戦略 (gacha + ゲーム + メルマガ) が複雑、yield 対 effort 悪い | medal-economy framework 整備後 |
| dreammail `/game/seven` Amazon ギフト | anti-bot リスク | 3-reel slot game、揃え判定。auto-play で「ツール検出」される懸念 (warau 前例同様) | n/a (out of scope) |
| dreammail シークレットキーワード | discovery 困難 | SNS / メール / サイト内に散在する keyword を発見 → 入力。自動 scrape は不安定 + 範囲外 | manual keyword feed 仕組み |
| fruitmail ポイント懸賞 / ポイントde豪華懸賞 | 設計判断で skip | fruitmail points 消費型 (= 金銭等価)。policy 上 ポイ活 で貯めた point を使う応募は意図的に除外 | n/a (point 消費の方針変更必要) |
| fruitmail ルーレット / 漢字クイズ | 別 mechanism | `/prize/roulette/` `/prize/kanjiquiz/` は別 UI、inspect 未確認。クイズは answer 必要で policy NG 確度高 | mechanism 検出 + policy 検証 |

## 🛠 framework patches (本セッション追加)

- `_content_with_retry`: `Page.content()` の "navigating" exception を 5 回 retry (2s 間隔) で吸収
- `--referer` flag in `cmd_html`: referer-gated page (amefri /game/gacha 等) を referer 付き inspect 可能
- `--inspect-click` repeating flag + navigation-context retry: post-click 構造 recon (moppy /pc_gacha/ 3-step recon に使用)
- `inspect_referer` / `inspect_clicks` input を全 9 site workflow YAML に露出
- **dynamic wizard discovery** (Adapter に 5 fields): `dynamic_wizard_list_url` (legacy 単一 URL) / `dynamic_wizard_list_urls` (2026-05-25 追加、複数 URL を順次 scrape → URL dedup) / `dynamic_wizard_link_selector` / `dynamic_wizard_template` / `dynamic_wizard_max_count`。daily で list page を scrape → 個別 link で wizard を動的生成 (chanceit 抽選専用)。`_list_urls` (plural) を指定すると単一 `_list_url` より優先
- **wizards-only mode** (source=None 対応): chanceit 等の抽選専用 adapter は click-URL source を持たず、wizards のみで動作可能
- **DailyWizard.pre_click_evaluate** (2026-05-25 追加): navigation 後 / clicks 前に走る任意 JS。fruitmail_lottery で `<select>` の値設定、dreammail precam / chanceit で `target="_blank"` 剥離 (same-tab navigation 化) に使用
- **DailyWizard.title_selector** (2026-05-25 追加): static lottery_mode wizards 向け prize title 抽出。INPUT/TEXTAREA は `value` 属性、それ以外は `textContent` を読む。空なら notifier の「(タイトル取得失敗)」fallback へ
- **DailyWizard.success_url_pattern** (2026-05-25 追加): wizard 完走後の `page.url` を regex match → 真の server-side 受理を verify。fruitmail で `/prize/end/`、chanceit で `^(?!.*/present/detail/)(?!.*error)`、dreammail gacha で `/game/gacha/lotteried`、precam で `^https://(?!.*dreammail\.jp)(?!.*error)`。**この field 未設定だと wizard 完走 = verified=True default で false positive (2026-05-25 chanceit / fruitmail / dreammail で 3 連発した偽陽性事故の根本原因)**
- **DailyWizard.success_text_marker** (2026-05-25 追加): body text marker での verify (URL pattern と AND 判定)。lottery_mode で `/prize/end/` 到達後の specific 完了 text を確認したい場合の二重 verify。現状は URL pattern だけで十分動作中
- **PasswordLoginConfig.submit_via_form_selector** (2026-05-25 追加): `page.click()` on `<input type="image">` で submit が発火しない form 向け、JS `form.submit()` bypass。chanceit 用に実装したが server 側 IP block で login 自体不可で fruitmail / dreammail は不要だったため、現状 chanceit では disable
- **Notifier.send_lottery_summary 文言改善** (2026-05-25): 旧「応募成功 N 件 / 失敗 M 件」 → 新「応募確認済 N 件 / 未確定 M 件」。「click は完走したが server-side 受理は不明」を明示する設計、偽陽性通知を構造的に防ぐ
- **password_login fallback** (既存 framework): fruitmail / dreammail で 動作確認 ✅、chanceit は IP block で ❌

## 🚧 未着手 (次セッション候補) — 抽選系

詳細は `point_sites/HANDOFF.md` 参照 (transient、commit されない)。

| Priority | 項目 | 推定工数 | 現実性 |
|---|---|---|---|
| ✅ P1 (2026-05-25) | chanceit 他カテゴリ拡張 → `dynamic_wizard_list_urls` で 4 list URL 対応、cap 20→40 | done | 実装済 (要 cookie 再 export 後 cron で verify) |
| ✅ P2 (2026-05-25) | chanceit tasklist 9 missions (article visits, visit-only) | done | 実装済 (要 cookie 再 export 後 cron で verify) |
| ⏭ P3 (2026-05-25) | dreammail 他ゲーム inspect 完了、追加 wizard なし | done (negative) | 7+ game inspect 結果 1-click + URL-verifiable な game は gacha のみ。 omkj は XHR-only で false positive 不可避、他は canvas/multi-step → 見送り |
| 🔴 P4 | dreammail マンスリー / シークレットキーワード | 不能 (scope 外) | **次セッション内では不可能**: マンスリーは medal 蓄積 + 16 日連続応募が前提、シークレットは SNS scrape (TOS リスク + 複雑度) |

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

最新更新: 2026-05-25 (HANDOFF P1-P3 反映 — chanceit 4-list dynamic + 9 article missions、dreammail 他ゲーム inspect 結果 negative finding + framework に `dynamic_wizard_list_urls` 追加)
