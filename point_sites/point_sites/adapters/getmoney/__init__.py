"""GetMoney! / げっとま (https://dietnavi.com) adapter.

Status: scaffolded. Awaiting ``GETMONEY_COOKIES`` registration and
discover run to validate URL patterns and balance regex.

Background:
- 旧 ``getmoney.jp`` ドメインは ``dietnavi.com/pc/...`` に統合済 (301)。
  Cookie / mypage / daily click は全て ``dietnavi.com`` 配下。
- 運営: 株式会社インフォニア (げん玉と同じ親会社 = 規約変更や撤退が
  連動するリスクあり、分散観点では弱い)。
- レート: ``10 pt = 1 円`` — moppy/hapitas (1pt=1円系) と混同しない。
- 180-day inactivity rule の可能性: gendama と同系列なので同じ運用と
  推定。click-point 収集はアクティビティに換算されない可能性が高く、
  半年に1回くらい実購入を挟まないとアカウントが消える。User が
  そのつもりがなければ enable する価値が低い (gendama 同様)。

Required Secrets:
  - ``GETMONEY_COOKIES`` — JSON array exported from a logged-in
    dietnavi.com browser session (Cookie-Editor 等).
  - ``SLACK_CHANNEL_GETMONEY`` — Slack channel ID or ``#name``.

Discover-time TODOs (run after Secret 登録):
  1. ``gh workflow run getmoney.yml -f discover=true`` で daily click
     ページの構造を確認、``mypage_url`` と ``discover_seeds`` を実 URL に
     合わせる。
  2. ``inspect_url=https://dietnavi.com/pc/mypage.php`` 等で mypage
     HTML を取り、``DEFAULT_BALANCE_PATTERNS`` でマッチするか確認。
     合わなければ adapter 専用 ``balance_patterns`` を追加。
  3. 最初のクリックメールが届いたら parser.py の URL regex / callout
     regex を実 body に合わせて refine。
"""

from ...common.adapter import Adapter
from ...common.balance import DEFAULT_BALANCE_PATTERNS
from ...common.sources import GmailSource
from .parser import parse as parse_email

ADAPTER = Adapter(
    name="getmoney",
    site_label="GetMoney!",
    # Verified anonymously 2026-05-10: ``/pc/mypage/`` (trailing slash)
    # returns 200; ``/pc/mypage.php`` is 404. Logged-in mypage is the
    # same path with auth — verify_login uses ログアウト keyword.
    mypage_url="https://dietnavi.com/pc/mypage/",
    # ``dietnavi.com`` is the canonical host post-rebrand. Keep
    # ``getmoney.jp`` in the allowlist too in case some click-mail
    # links still point at the legacy domain (which then 301s).
    allowed_hosts=frozenset(
        {
            "dietnavi.com",
            "www.dietnavi.com",
            "getmoney.jp",
            "www.getmoney.jp",
        }
    ),
    login_keyword="ログアウト",
    # Sender domain is best-guess. Real mails may come from
    # ``info@dietnavi.com`` or a sub-domain; widen the filter after
    # the first mail arrives.
    gmail_query=("from:dietnavi.com -label:getmoney-clicked -label:getmoney-no-coins newer_than:3d"),
    clicked_label="getmoney-clicked",
    no_coins_label="getmoney-no-coins",
    source=GmailSource(parse_email=parse_email),
    # Default mypage balance markers (保有ポイント / 保有コイン) cover
    # most Japanese point sites. If GetMoney! uses a different label
    # (e.g. ``現在の所持pt``) the discover step will surface it and we
    # add a site-specific pattern here.
    balance_patterns=DEFAULT_BALANCE_PATTERNS,
    discover_seeds=(
        "https://dietnavi.com/pc/mypage/",
        "https://dietnavi.com/pc/daily_click.php",
    ),
)
