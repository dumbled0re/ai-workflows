"""Moppy (https://pc.moppy.jp) adapter.

Wires Moppy-specific values into the shared pipeline: the click-coin
URL regex (in ``parser.py``), mypage URL for login verification + balance
scraping, the Gmail labels used to skip already-clicked / no-coins
mails, and the discover crawl seeds for one-shot recon of the 毎日貯める
section. Everything here is *data*; the actual click/balance/discover
logic lives in ``point_sites.common``.
"""

from ...common.adapter import Adapter
from ...common.balance import DEFAULT_BALANCE_PATTERNS
from ...common.password_login import PasswordLoginConfig
from ...common.sources import GmailSource
from .parser import parse as parse_email

ADAPTER = Adapter(
    name="moppy",
    site_label="Moppy",
    mypage_url="https://pc.moppy.jp/mypage/",
    allowed_hosts=frozenset({"pc.moppy.jp", "ssl.pc.moppy.jp", "mail.moppy.jp", "track.moppy.jp", "moppy.jp"}),
    login_keyword="ログアウト",
    gmail_query="from:moppy.jp -label:moppy-clicked -label:moppy-no-coins newer_than:3d",
    clicked_label="moppy-clicked",
    no_coins_label="moppy-no-coins",
    source=GmailSource(parse_email=parse_email),
    balance_patterns=DEFAULT_BALANCE_PATTERNS,
    discover_seeds=(
        "https://pc.moppy.jp/mypage/",
        "https://pc.moppy.jp/everyday/",
        "https://pc.moppy.jp/coin/",
        "https://pc.moppy.jp/cap/",
        "https://pc.moppy.jp/category/coin/",
    ),
    # 2026-05-16 inspect (--anonymous) で確定。form action=/login/?mode=submit、
    # mail field は name="mail" (placeholder メールアドレス)、pass field は
    # name="pass"。submit は ``button.a-btn__login`` (Yahoo/Google ログインの
    # 別 button も並ぶが本物はこれ)。
    # ssl.pc.moppy.jp を allowed_hosts に追加 (login form host)。
    password_login=PasswordLoginConfig(
        login_url="https://ssl.pc.moppy.jp/login/",
        username_selector='input[name="mail"]',
        password_selector='input[name="pass"]',
        submit_selector="button.a-btn__login",
        success_marker="ログアウト",
    ),
)
