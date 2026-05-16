# point_sites — multi-site framework

日本のポイ活サイト自動化。Adapter pattern で各サイト (`adapters/<name>/`) を脱着可能にする。
新サイト = `adapters/<name>/` を 1 つ作る + `adapters/__init__.py` の `REGISTRY` に追加 + `.github/workflows/<name>.yml` を作る、これだけ。

## 設計原則 (絶対に変えない)

1. **広告 fraud 禁止** — 第三者広告ネットワーク経由のクリック自動化はやらない (ガチャ・抽選・無料ゲームの大半が該当)
2. **TOS grey は OK / 法律 grey は NG** — クリックメールは OK、アンケート自動回答は data fraud で NG、ガチャは ad-fraud で NG
3. **Playwright OK だが慎重** — IP block 食らったら諦める (chobirich は削除済の前例)
4. **副作用検知の 3 層を必ず備える** — 検知 (balance scrape) → 記録 (`outcomes.jsonl`) → 判断 (degradation alert)

## 検証 3 層 (`OutcomeTracker`)

| 層 | 実装 | fires when |
|---|---|---|
| credit-ratio degradation | `_detect_credit_degradation` | 直近 3 連続 click run で credit ratio < 30% (高 yield サイト用) |
| HTTP-failure fallback | `_detect_click_failure` | 直近 3 連続 click run で全 click が 4xx/5xx (balance 取れない pointincome 等) |
| balance stagnation | `_detect_balance_stagnation` | opt-in。`Adapter.stagnation_window=N` 設定時、N 回連続で balance 増加なし (低 yield amefuri 等) |

## Adapter 追加 flow

1. **user**: ポイ活専用 Gmail (`<your-poikatsu-email>` 系) でサイト登録 → Cookie-Editor で JSON export
2. **user**: GitHub Secret 登録 (`<SITE>_COOKIES` + `SLACK_CHANNEL_<SITE>`) + Slack channel に bot 招待
3. **Claude**:
   - `mkdir adapters/<site>/` + `__init__.py` (`pointincome` をテンプレに `mypage_url` `allowed_hosts` `login_keyword` `source` を埋める)
   - `parser.py` (実 HTML / 実メールから regex を refine)
   - `adapters/__init__.py` の `REGISTRY` に追加
   - `.github/workflows/<site>.yml` (cron 時刻を他サイトと 15 分以上ずらす)
4. `gh workflow run <site>.yml -f discover=true` → URL 構造確認
5. `gh workflow run <site>.yml -f inspect_url=...` → 実 HTML を取って parser を実 URL に narrow
6. `gh workflow run <site>.yml -f extract_links=true` → click せず candidate URL だけ Slack に流して動作確認
7. cron に任せる (default で click mode)

## Source kind の使い分け

| Source | 用途 | 例 |
|---|---|---|
| `GmailSource` | Gmail のクリックメール | moppy / hapitas / pointincome |
| `OnsiteInboxSource` | サイト内受信箱 | pointtown / getmoney |
| `EndpointPollSource` | 単一 GET (login bonus 等、メール不要) | amefuri |

メールが Gmail と on-site 両方に来るサイト (getmoney 等) は **片方でクリックすると他方は加算されない** 仕様が多い → 認証コスト低い `OnsiteInboxSource` 優先。

## Playwright の使い所

| 用途 | 実装 | 例 |
|---|---|---|
| balance 取得 (anti-bot interstitial 突破) | `Adapter.balance_uses_browser=True` | pointincome (突破できなかったので HTTP fallback) |
| 単発の SPA navigation | `Adapter.browser_actions` | amefuri SPA login bonus |
| daily-rotating banner の discover + click | `Adapter.daily_banner_url` + `daily_banner_selector` | hapitas top の click_get banners |
| multi-step button wizard | `Adapter.daily_wizards` | pointtown login bonus modal、hapitas takarakuji 交換 |

## test / lint / mypy

```bash
cd point_sites
uv run ruff check . && uv run ruff format --check .
uv run mypy point_sites    # strict (point_sites のみ全 repo で唯一 strict)
uv run pytest -q
```

CI と同じコマンド。ローカルで通れば CI も通る。
