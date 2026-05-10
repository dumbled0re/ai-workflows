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

Other earning paths surveyed 2026-05-10 (none automated):
  - ``/pc/daily_click.php``: only ad_jump.php affiliate banners that
    require a real conversion to credit. Out of scope.
  - ``/pc/game/`` / ``/pc/survey/`` / ``/pc/shopping/`` / ``/pc/credit/``
    / ``/pc/monitor/``: human input or real-purchase paths, not safely
    automatable per CLAUDE.md (TOS / ad-fraud guardrails).
  - ログインボーナス widget on mypage: not present (verified via
    inspect 2026-05-10).

Required Secrets:
  - ``GETMONEY_COOKIES`` — Cookie-Editor JSON export from a logged-in
    dietnavi.com browser session.
  - ``SLACK_CHANNEL_GETMONEY`` — Slack channel ID or ``#name``.
"""

from ...common.adapter import Adapter
from ...common.balance import DEFAULT_BALANCE_PATTERNS
from ...common.sources import OnsiteInboxSource
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
)
