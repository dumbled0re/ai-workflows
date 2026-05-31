"""Headless-browser companion to ``Clicker`` — runs Playwright Chromium.

Used when a site's interaction shape requires JavaScript:
- balance scraping where ``requests.get`` returns an anti-bot landing
  (``pointincome``)
- daily-bonus / gacha trigger that's wired to client-side JS handlers
  on a SPA shell (``amefri``)
- on-site banner clicks where the credit URL is built by JS at click
  time and not present in the static HTML.

Stays site-agnostic just like ``Clicker``: per-site values come from
the active ``Adapter`` (mypage URL, login keyword, balance regexes).

Cookie format mirrors what ``cookie_store`` already persists for
``Clicker``: ``[{"name", "value", "domain", "path", "secure"}, ...]``.
After a session, ``export_cookies`` returns the rotated jar in the
same shape so the existing ``cookie_store.save`` path works without
branching on session kind.

Lifecycle: use as a context manager. Outside the ``with`` block the
browser is torn down so a workflow step can run a short ``BrowserClicker``
session, exit, then continue with the cheaper ``Clicker`` for any
remaining click-coin emails.
"""

from __future__ import annotations

import logging
import re
from contextlib import suppress
from types import TracebackType
from typing import TYPE_CHECKING, Any, cast

from .balance import DEFAULT_BALANCE_PATTERNS, parse_balance

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page

logger = logging.getLogger(__name__)

# Match the desktop Chrome UA used by Clicker so server-side fingerprints
# stay consistent between sync HTTP and browser sessions.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Stealth init script. Runs in every new page before any site script,
# masking the most reliable Playwright tells. pointincome's
# "コンテンツブロッカー" interstitial detected the headless session even
# with valid cookies; the navigator.webdriver flag is the single
# strongest signal so it gets first priority. ja-JP locale + plugins
# array + chrome runtime stub round out the most common checks. Sites
# with deeper fingerprinting (canvas / WebGL / audio context) would
# need additional scripts; we add those only when a specific site
# proves to need them so the surface area stays minimal.
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP', 'ja', 'en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {
  get: () => [
    {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer'},
    {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
    {name: 'Native Client', filename: 'internal-nacl-plugin'},
  ],
});
window.chrome = {
  runtime: {OnInstalledReason: {INSTALL: 'install'}, PlatformOs: {MAC: 'mac', LINUX: 'linux', WIN: 'win'}},
  app: {isInstalled: false, getDetails: () => null, getIsInstalled: () => false,
    InstallState: {DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed'},
    RunningState: {CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running'}},
  csi: () => ({onloadT: Date.now(), pageT: 0, startE: Date.now(), tran: 15}),
  loadTimes: () => ({
    requestTime: Date.now()/1000, startLoadTime: Date.now()/1000,
    commitLoadTime: Date.now()/1000, finishDocumentLoadTime: Date.now()/1000,
    finishLoadTime: Date.now()/1000, firstPaintTime: Date.now()/1000,
    navigationType: 'Other', wasFetchedViaSpdy: true, wasNpnNegotiated: true,
    npnNegotiatedProtocol: 'h2', wasAlternateProtocolAvailable: false, connectionInfo: 'h2'}),
};
const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {
  window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : originalQuery(parameters)
  );
}
// WebGL vendor/renderer mask (HeadlessChrome detection via UNMASKED_VENDOR_WEBGL)
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
  if (parameter === 37445) return 'Intel Inc.';
  if (parameter === 37446) return 'Intel Iris OpenGL Engine';
  return getParameter.apply(this, arguments);
};
// Canvas fingerprint noise injection (sub-pixel jitter)
const toDataURL = HTMLCanvasElement.prototype.toDataURL;
HTMLCanvasElement.prototype.toDataURL = function(type) {
  const ctx = this.getContext('2d');
  if (ctx) {
    const imageData = ctx.getImageData(0, 0, this.width, this.height);
    for (let i = 0; i < imageData.data.length; i += 4) {
      imageData.data[i] ^= 0x01;
    }
    ctx.putImageData(imageData, 0, 0);
  }
  return toDataURL.apply(this, arguments);
};
// hardwareConcurrency / deviceMemory (Chrome on macOS typical values)
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
"""


def _to_playwright_cookies(
    cookies: list[dict[str, object]],
    default_domain: str,
) -> list[dict[str, Any]]:
    """Convert the project's Cookie-Editor-shaped jar to Playwright's API.

    Cookie-Editor / ``cookie_store.save_jar`` produces:
        {"name", "value", "domain", "path", "secure"}
    Playwright accepts the same keys plus optional ``expires``,
    ``httpOnly``, ``sameSite`` — defaults are fine when missing.
    Cookies without an explicit ``domain`` get the adapter default
    (e.g. ``.amefri.net``) the same way ``Clicker.__init__`` does.
    """
    out: list[dict[str, Any]] = []
    for c in cookies:
        name = str(c.get("name", ""))
        value = str(c.get("value", ""))
        if not name:
            continue
        entry: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": str(c.get("domain") or default_domain),
            "path": str(c.get("path", "/")),
            "secure": bool(c.get("secure", True)),
        }
        # Playwright requires either ``url`` or both ``domain`` and ``path``.
        # We always supply domain+path so ``url`` is omitted.
        out.append(entry)
    return out


class BrowserClicker:
    """Playwright-driven companion to ``Clicker``.

    Mirrors the subset of Clicker's API that adapters consume:
    ``verify_login`` and balance fetching. Extra browser-only helpers
    (``goto``, ``click_selector``) cover SPA daily-bonus / gacha cases
    where pure HTTP returns the SPA shell only.

    The class is sync (``playwright.sync_api``) to match the rest of
    the orchestrator. Sync API spawns its own asyncio loop inside
    ``__enter__``; that's incompatible with running inside an existing
    loop, but ``cmd_run`` is plain sync so this is fine.
    """

    def __init__(
        self,
        cookies: list[dict[str, object]] | None = None,
        default_cookie_domain: str = "",
        user_agent: str = DEFAULT_USER_AGENT,
        headless: bool = True,
        nav_timeout_ms: int = 30_000,
    ) -> None:
        self._cookies_in = cookies or []
        self._default_cookie_domain = default_cookie_domain
        self._user_agent = user_agent
        self._headless = headless
        self._nav_timeout_ms = nav_timeout_ms
        self._pw: Any = None
        self._browser: Any = None
        self._context: BrowserContext | None = None
        self.authenticated: bool = bool(cookies)

    # --- lifecycle -------------------------------------------------------

    def __enter__(self) -> BrowserClicker:
        # Imported lazily so ``import point_sites.common.browser`` stays
        # cheap (and tests can stub the module without forcing a real
        # Playwright install when only the cookie-conversion helper is
        # exercised).
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        # ``--no-sandbox`` is required on the GitHub Actions runner where
        # user namespaces are restricted. Local dev still benefits because
        # we only ever load these sites and don't execute untrusted JS
        # that would justify the sandbox.
        self._browser = self._pw.chromium.launch(
            headless=self._headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        self._context = self._browser.new_context(
            user_agent=self._user_agent,
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={"width": 1280, "height": 800},
        )
        # Apply stealth before any cookie or navigation: scripts added
        # via add_init_script run in every Page and Frame, even those
        # spawned mid-navigation, so anti-bot checks that fire on the
        # very first page load see the masked navigator.webdriver.
        self._context.add_init_script(_STEALTH_INIT_SCRIPT)
        if self._cookies_in:
            # ``add_cookies`` types its argument as the SetCookieParam
            # TypedDict, but that type isn't part of Playwright's public
            # API surface. The shape we build (name/value/domain/path/secure)
            # matches the runtime contract; cast through Any to keep
            # strict mypy happy without invoking an unstable internal type.
            converted = _to_playwright_cookies(self._cookies_in, self._default_cookie_domain)
            self._context.add_cookies(cast("Any", converted))
        # Default timeouts on the context so individual goto/click calls
        # don't each have to specify them.
        self._context.set_default_navigation_timeout(self._nav_timeout_ms)
        self._context.set_default_timeout(self._nav_timeout_ms)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # ``suppress`` so a torn-down browser (e.g. process killed by the
        # GHA timeout) doesn't mask the original exception in ``with``.
        with suppress(Exception):
            if self._context is not None:
                self._context.close()
        with suppress(Exception):
            if self._browser is not None:
                self._browser.close()
        with suppress(Exception):
            if self._pw is not None:
                self._pw.stop()
        self._context = None
        self._browser = None
        self._pw = None

    # --- helpers ---------------------------------------------------------

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("BrowserClicker used outside its context manager")
        return self._context

    def new_page(self) -> Page:
        return self.context.new_page()

    def goto(self, url: str, *, wait_until: str = "networkidle", referer: str | None = None) -> Page:
        """Open a fresh page, navigate, return the loaded page.

        ``networkidle`` is the safest default for SPAs: it waits until the
        client JS has finished its initial XHR burst, which is when login
        bonus / balance fetches typically resolve. Sites with long-poll
        beacons that never quiet down can override to ``domcontentloaded``.

        ``referer`` (optional) sets the Referer header for the goto. Used
        for sites that gate access by referer (e.g. amefri /game/gacha
        returns 302 to home if accessed without /special/freepoint referer).
        """
        page = self.new_page()
        if referer:
            page.goto(url, wait_until=wait_until, referer=referer)  # type: ignore[arg-type]
        else:
            page.goto(url, wait_until=wait_until)  # type: ignore[arg-type]
        return page

    def verify_login(self, mypage_url: str, login_keyword: str = "ログアウト") -> bool:
        """Browser equivalent of ``Clicker.verify_login``.

        Same heuristic — load mypage, check whether ``login_keyword``
        appears anywhere in the DOM after JS render. Avoids strict
        selectors so the check survives template tweaks.
        """
        try:
            page = self.goto(mypage_url)
        except Exception as exc:
            logger.warning("browser login verification navigation failed: %s", exc)
            return False
        try:
            final_url = page.url.lower()
            if "login" in final_url or "/entry/" in final_url:
                return False
            content = page.content()
        finally:
            page.close()
        return login_keyword in content or "logout" in content.lower()

    def fetch_balance(
        self,
        mypage_url: str,
        *,
        patterns: tuple[re.Pattern[str], ...] = DEFAULT_BALANCE_PATTERNS,
        secondary_patterns: tuple[re.Pattern[str], ...] | None = None,
        hydrate_wait_ms: int = 4_000,
    ) -> tuple[int | None, int | None]:
        """Browser equivalent of ``balance.fetch_balance``.

        Loads ``mypage_url`` with a real browser so JS-rendered balance
        widgets (``pointincome`` / sugutama mile counter etc.) are
        present in ``page.content()``. Returns ``(primary, secondary)``;
        ``None`` on any failure / parse miss.

        Uses ``wait_until="domcontentloaded"`` instead of the default
        ``networkidle`` because ad-heavy mypages (sugutama / pointtown:
        Criteo+doubleclick beacons that never quiet down) blow past
        the 30s timeout otherwise. After DOM-ready we add a
        ``hydrate_wait_ms`` buffer (default 4s) so the balance-fetch JS
        XHR can fill the widget before we capture ``page.content()``.
        """
        try:
            page = self.goto(mypage_url, wait_until="domcontentloaded")
        except Exception as exc:
            logger.warning("browser balance fetch navigation failed: %s", exc)
            return None, None
        try:
            page.wait_for_timeout(hydrate_wait_ms)
            html = page.content()
        finally:
            page.close()
        balance = parse_balance(html, patterns)
        secondary = parse_balance(html, secondary_patterns) if secondary_patterns else None
        if balance is None:
            snippet = re.sub(r"[A-Za-z0-9+/=_-]{20,}", "<redacted>", html)
            logger.warning(
                "browser balance parse failed; no pattern matched. snippet head: %s",
                snippet[:200].replace("\n", " "),
            )
        return balance, secondary

    def export_cookies(self) -> list[dict[str, object]]:
        """Return the live jar in the project's persisted shape.

        Same shape as ``cookie_store.save_jar`` writes for ``Clicker``,
        so the persistence pipeline stays single-path: BrowserClicker's
        rotated cookies feed the same ``cookies.json`` that the next
        run's ``Clicker`` will boot from (and vice versa).
        """
        out: list[dict[str, object]] = []
        for c in self.context.cookies():
            out.append(
                {
                    "name": str(c.get("name", "")),
                    "value": str(c.get("value", "")),
                    "domain": str(c.get("domain") or self._default_cookie_domain),
                    "path": str(c.get("path", "/")),
                    "secure": bool(c.get("secure", True)),
                },
            )
        return out
