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
from ...common.wizard import DailyWizard
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
    # 2026-05-23 audit で発見: /gamecontents/ に moppy 自身のクリックコイン URL
    # (``/cc/c?t=CC...``) が 8 個程度埋め込まれている。これらは Gmail で送られて
    # くる click-mail と同種の自サイト click reward (= ad-fraud ではない)、
    # /gamecontents/ から直接拾うことで Gmail を待たずに即時 click 可能。
    # hapitas の clickget_banner と同じ pattern。
    daily_banner_url="https://pc.moppy.jp/gamecontents/",
    daily_banner_selector='a[href*="/cc/c?t=CC"]',
    # 2026-05-24 ad-fraud policy 解禁 (CLAUDE.md 2026-05-23) 後の追加。
    # 過去 memory `project_moppy_gacha_blocked` (2026-05-09) は old policy
    # 下での判定で、now policy で 解禁。inspect (run 26360072361 /
    # 26360097456 / 26360123875) で 3-step click chain 確認:
    #   1. /pc_gacha/ → "まわす" (a.a-moppy-gacha__btn) → ball.php?color=N
    #   2. ball.php → "結果を見る" (a.a-moppy-gacha__btn) → result.php?c=N
    #   3. result.php → "バナータップでコイン受取り" (a.a-banner、
    #      href=/pc_gacha/ad_click.php) → credit 加算
    # 注意書きに明記: "ガチャ結果ページにある広告バナーをクリックで
    # ポイント加算対象となります". target="_blank" でも click 自体は
    # server に記録される想定。
    daily_wizards=(
        DailyWizard(
            name="moppy_gacha",
            url="https://pc.moppy.jp/pc_gacha/",
            clicks=(
                ("a.a-moppy-gacha__btn", 1),
                ("a.a-moppy-gacha__btn", 1),
                ("a.a-banner", 1),
            ),
            use_navigation_click=True,
            click_force=True,
            initial_wait_ms=5000,
            inter_step_ms=10000,
            final_wait_ms=15000,
        ),
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
