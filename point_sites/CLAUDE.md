# point_sites — multi-site framework

日本のポイ活サイト自動化。Adapter pattern で各サイト (`adapters/<name>/`) を脱着可能にする。
新サイト = `adapters/<name>/` を 1 つ作る + `adapters/__init__.py` の `REGISTRY` に追加 + `.github/workflows/<name>.yml` を作る、これだけ。

## 設計原則 (2026-05-23 update)

1. **広告 fraud は user 判断で OK** (2026-05-23 解禁、root `CLAUDE.md` 詳述) — ガチャ / スロット / ミッション / 第三者広告クリック系を含む。ただし bot 検出回避 + 検出時 fail-soft 必須
2. **TOS grey / 法律 grey 両方 OK / アンケート data fraud は NG** — クリックメール / ガチャ / スロットは OK、アンケート自動回答は依然 NG (attention check で検出されやすく、yield 対 risk 悪い)
3. **Playwright OK だが慎重** — IP block 食らったら諦める (chobirich は削除済の前例)
4. **副作用検知の 3 層を必ず備える** — 検知 (balance scrape) → 記録 (`outcomes.jsonl`) → 判断 (degradation alert)。ad-fraud path は credit reject の可能性が高いので **balance stagnation + credit-ratio degradation 検知が特に重要**
5. **ad-fraud path 実装時の必須要件** — (a) human-like ランダム sleep を click 間に入れる (`<SITE>_CLICK_INTERVAL_MIN/MAX` 既存機構)、(b) 連続失敗時の早期 abort、(c) 連日同一秒の click pattern を避ける (jitter 既設定、`_site-runner.yml`)

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
| balance 取得 (anti-bot interstitial 突破 / JS render 後 read) | `Adapter.balance_uses_browser=True` | sugutama (JS-rendered mile widget)、pointincome (突破できなかったので HTTP fallback) |
| 単発の SPA navigation | `Adapter.browser_actions` | amefuri SPA login bonus |
| daily-rotating banner の discover + click | `Adapter.daily_banner_url` + `daily_banner_selector` | hapitas top の click_get banners |
| multi-step button wizard | `Adapter.daily_wizards` | pointtown login bonus、hapitas takarakuji 交換、fruitmail スロット / ビンゴ / login_bonus |
| Cookie 失効時の自動再ログイン | `Adapter.password_login=PasswordLoginConfig(...)` | fruitmail / moppy (実装済)。pointtown は GMO SSO anti-fraud で無効化 |

### ID/PW login fallback (password_login)

Cookie 失効頻繁な site 向けに、`<SITE>_USER` / `<SITE>_PASS` Secret から Playwright で fresh login する仕組み。

```python
# adapter __init__.py 抜粋
password_login=PasswordLoginConfig(
    login_url="https://example.jp/login",
    username_selector='input[name="email"]',  # CSS selector (page.fill で使う)
    password_selector='input[name="pass"]',
    submit_selector="button.login-btn",        # page.click() (native) で発火
    success_marker="ログアウト",                # post-submit page 内に含まれてれば成功
)
```

`_verify_login` の挙動:
1. cookie で verify
2. 失敗時、`adapter.password_login is not None` かつ env var が set されていれば BrowserClicker (cookies=None) で fresh login → 成功なら cookie merge back
3. 再 verify、失敗なら従来の Slack auth_error path

**Debug flag**: `force_password_login_test=true` workflow input で cookie verify を skip して fallback を強制発火 (実装新規時の selector 確認に使う)。

**注意**:
- `selector` に `name="LoginForm[email]"` のような `[]` を含む name attr は Playwright で escape 問題が起きる場合あり → id-based / type-based selector を優先
- SSO 経由 site (GMO SSO 等) は redirect chain 完了に 5s 程度かかる → password_login は post-submit に 5s wait + content() check 内蔵
- anti-fraud (US runner IP が「不審なログイン」判定される site = GMO 系) は突破不可と確定。framework としては fail-soft で従来 path に fallback

## inspect (cmd_html) の debug flag

`gh workflow run <site>.yml -f inspect_url=<URL>` で実 HTML を取得して analyse する debug 機構。追加 flag:

| Flag | 用途 |
|---|---|
| `inspect_browser=true` | Playwright Chromium で render してから content() (SPA / JS-driven page) |
| `inspect_cap=<N>` | 出力 byte 数上限 (default 80000、SPA で大きい page は 200000-500000) |
| `inspect_wait_selector=<CSS>` | browser モードで指定 selector を wait してから content() (SPA hydration 待ち) |
| `inspect_anonymous=true` | cookie load と login verify を skip。**login form 等の logged-out HTML を見る用** (logged-in session は redirect で見えない) |
| `inspect_capture_network=true` | browser モードで全 network request を log 出力。**SPA の API endpoint 探索用** |

## test / lint / mypy

```bash
cd point_sites
uv run ruff check . && uv run ruff format --check .
uv run mypy point_sites    # strict (point_sites のみ全 repo で唯一 strict)
uv run pytest -q
```

CI と同じコマンド。ローカルで通れば CI も通る。
