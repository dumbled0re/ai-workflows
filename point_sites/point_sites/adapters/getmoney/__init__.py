"""GetMoney! / げっとま (https://dietnavi.com) adapter.

Status: 2026-05-10 wired with on-site Webメール inbox source.
``GETMONEY_COOKIES`` registered, auth verified at ``/pc/mypage/``,
and the click-coin URL pattern was sampled from one real on-site
message (recipientId=945005814 → ``click.php?cid=...&id=...&sec=...``).

Background:
- 旧 ``getmoney.jp`` ドメインは ``dietnavi.com/pc/...`` に統合済 (301)。
- 運営: 株式会社インフォニア (げん玉と同じ親会社 = 規約変更や撤退が
  連動するリスクあり、分散観点では弱い)。
- レート: ``10 pt = 1 円`` — moppy/hapitas (1pt=1円系) と混同しない。
- 180-day inactivity rule の可能性: gendama と同系列なので同じ運用と
  推定。click-point 収集はアクティビティに換算されない可能性が高く、
  半年に1回くらい実購入を挟まないとアカウントが消える。User が
  そのつもりがなければ enable する価値が低い (gendama 同様)。

Why on-site inbox (not Gmail):
  Inbox notice 2026-05-10: ``クリックポイントは重複して獲得できません。
  この一覧からクリックポイントを獲得した場合、メールソフト等で受信した
  メールでのクリックポイントは加算されません。`` Both channels deliver
  the same click-coin items but only one credits — on-site inbox wins
  because (a) no Gmail App Password dependency, (b) login is the same
  cookie that powers balance scraping, (c) pattern matches pointtown
  which is already validated.

Other earning paths surveyed 2026-05-10 (initial pass):
  - ``/pc/daily_click.php``: only ad_jump.php affiliate banners that
    require a real conversion to credit. Out of scope.
  - ``/pc/game/`` / ``/pc/survey/`` / ``/pc/shopping/`` / ``/pc/credit/``
    / ``/pc/monitor/``: human input or real-purchase paths.
  - ログインボーナス widget on mypage: not present (verified via
    inspect 2026-05-10).

2026-05-23 ad-fraud policy 解禁後の追加 audit (issue #30):
  - ``/pc/game/estlier/play.php?id=...`` (6 種、NUMBERS DX 等): visit-only
    wizard 化済。estlier ad-network への redirect + 広告 impression で
    yield 期待。1 週間 balance 観察で credit 確認、無 yield なら multi-step
    進む sequence 実装に escalate。
  - ``/pc/game/game1000/`` (ふるふるパニック clicker): 昆虫クリック型
    interactive game、bot 化困難で skip。
  - ``/pc/game/highandlow/`` / ``/pc/game/ibgame/`` / ``/pc/game/mpgame/``
    / ``/pc/game/typing/``: 同じく interactive、skip (別 follow-up)。

Required Secrets:
  - ``GETMONEY_COOKIES`` — Cookie-Editor JSON export from a logged-in
    dietnavi.com browser session.
  - ``SLACK_CHANNEL_GETMONEY`` — Slack channel ID or ``#name``.
"""

from ...common.adapter import Adapter
from ...common.balance import DEFAULT_BALANCE_PATTERNS
from ...common.sources import OnsiteInboxSource
from ...common.wizard import DailyWizard
from .parser import parse_inbox, parse_message

ADAPTER = Adapter(
    name="getmoney",
    site_label="GetMoney!",
    # Verified anonymously 2026-05-10: ``/pc/mypage/`` (trailing slash)
    # returns 200 + auth-aware content; ``/pc/mypage.php`` is 404.
    mypage_url="https://dietnavi.com/pc/mypage/",
    # Click-coin URLs use the apex ``dietnavi.com`` (not under /pc/).
    # Keep ``getmoney.jp`` in the allowlist too in case any legacy
    # ad/inbox link still emits it (would 301 to dietnavi anyway).
    # Note: ``/pc/game/estlier/play.php?id=...`` redirects through
    # third-party ad-network hosts (heisei-housewarming.work etc.).
    # Those cookies are intentionally NOT in allowed_hosts so the
    # cookie store strips them at persist time — re-sending them on
    # subsequent requests would bloat the cookie header and look like
    # an anomalous session to dietnavi.
    allowed_hosts=frozenset(
        {
            "dietnavi.com",
            "www.dietnavi.com",
            "getmoney.jp",
            "www.getmoney.jp",
        }
    ),
    login_keyword="ログアウト",
    source=OnsiteInboxSource(
        inbox_url="https://dietnavi.com/pc/mypage/mail_notice/index",
        parse_inbox=parse_inbox,
        parse_message=parse_message,
    ),
    # Default mypage balance markers cover ``現在のポイント`` which is
    # what GetMoney! mypage uses (verified 2026-05-10: ``現在のポイント
    # </p><span class="now_point">0Pt</span>``).
    balance_patterns=DEFAULT_BALANCE_PATTERNS,
    discover_seeds=(
        "https://dietnavi.com/pc/mypage/",
        "https://dietnavi.com/pc/mypage/mail_notice/index",
    ),
    # ad-fraud-policy 解禁 (2026-05-23) で /pc/game/estlier/play.php?id=...
    # を visit-only wizard 化。各 id は estlier ad-network 上の別 game
    # (NUMBERS DX / BINGO 等) で、開くだけで広告 impression がカウントされる。
    # クリックポイントとして credit するかは要観察。
    #
    # 各 wizard は clicks=() で「URL を開いて 2s + 2.5s wait → close」
    # するだけ (main.py 689-714 行の click loop が空タプルで skip され、
    # final settle wait のみ走る pattern)。estlier 側の AdBlock 検知は
    # JS で行われるが Playwright で広告 script もロードされるので通る想定。
    # 参考 inspect run 26328321261 (id=38 → http://hair.heisei-housewarming.work/
    # getmoney/index.php?uid=3567521 にリダイレクト)。
    #
    # NUMBERS DX 等は厳密には「数字を選んで submit → 当選日に credit」
    # 系で、visit 単独では credit しない可能性が高い。1 週間 outcome 観察
    # で balance 動かなければ multi-step 進む sequence 実装に escalate。
    # interactive game (game1000 ふるふるパニック / ibgame / typing) は
    # 別途 follow-up issue で対応。
    daily_wizards=(
        DailyWizard(
            name="getmoney_estlier_38",
            url="https://dietnavi.com/pc/game/estlier/play.php?id=38",
            clicks=(),
        ),
        DailyWizard(
            name="getmoney_estlier_71",
            url="https://dietnavi.com/pc/game/estlier/play.php?id=71",
            clicks=(),
        ),
        DailyWizard(
            name="getmoney_estlier_83",
            url="https://dietnavi.com/pc/game/estlier/play.php?id=83",
            clicks=(),
        ),
        DailyWizard(
            name="getmoney_estlier_94",
            url="https://dietnavi.com/pc/game/estlier/play.php?id=94",
            clicks=(),
        ),
        DailyWizard(
            name="getmoney_estlier_102",
            url="https://dietnavi.com/pc/game/estlier/play.php?id=102",
            clicks=(),
        ),
        DailyWizard(
            name="getmoney_estlier_112",
            url="https://dietnavi.com/pc/game/estlier/play.php?id=112",
            clicks=(),
        ),
    ),
    # password_login は 2026-05-21 に試したが reCAPTCHA v2 (data-sitekey
    # ``6LdO2ggT...``) で submit 後 login form のまま (run 26259378305)。
    # 初回 inspect では reCAPTCHA iframe が async load 前で見逃したが、
    # 再 inspect で確認済。hapitas と同類、Playwright stealth では突破不可。
    # Cookie 失効時は従来の Slack auth_error path で user に Cookie 再エクスポート
    # を要求する。
)
