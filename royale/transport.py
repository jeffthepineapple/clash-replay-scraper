"""How this scraper talks to RoyaleAPI.

Pages  an anonymous headful browser, used for one thing: solving Cloudflare's
       challenge and holding the cf_clearance cookie it earns. Installed Google
       Chrome is preferred on Windows; Playwright Chromium is used elsewhere.
       Headful is not optional, so Linux also needs DISPLAY or WAYLAND_DISPLAY.

Curl   every actual fetch, run in parallel behind a shared rate Limiter. It
       borrows the browser's cf_clearance and UA (the clearance in a browser's
       on-disk jar is usually already rotated and answers 403), and attaches the
       RoyaleAPI session cookie only for /data/replay, which is login-gated.
       Public pages are therefore crawled anonymously.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit

from playwright.sync_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from .cookies import SESSION_COOKIE, Session
from .limiter import Limiter

BASE = "https://royaleapi.com"
SKIP = ("image", "font", "media", "stylesheet")  # nothing we parse needs these
RETRIES = 6  # per request, all of them 429 backoffs handed to the Limiter
LOGIN_WAIT = 300  # seconds a human gets to finish signing in
LANDING = "/me"  # where RoyaleAPI drops you once a login lands
POLL_EVERY = 1.5  # seconds between "are they in yet?" checks


class RateLimited(RuntimeError):
    """429 that survived every backoff."""


class ClearanceExpired(RuntimeError):
    """403: the Cloudflare pass died mid-run and only the browser can renew it."""


class AuthError(RuntimeError):
    """Cloudflare or the login gate turned us away for good."""


def _is_me(url: str) -> bool:
    return urlsplit(url).path.rstrip("/") == LANDING


class Pages:
    """The anonymous browser. Single-threaded: playwright's sync API is."""

    def __init__(self):
        if sys.platform.startswith("linux") and not (
                os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            raise AuthError("no display: the anonymous browser must run headful "
                            "to clear Cloudflare")
        self._pw = sync_playwright().start()
        launch = {
            "headless": False,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if sys.platform == "win32":
            try:
                # Use the user's installed, branded Chrome. Cloudflare can leave
                # Playwright's bundled Chromium on its verification page forever.
                self._browser = self._pw.chromium.launch(channel="chrome", **launch)
            except PlaywrightError:
                self._browser = self._pw.chromium.launch(**launch)
        else:
            self._browser = self._pw.chromium.launch(**launch)
        self._ctx = self._browser.new_context()
        self.page = self._ctx.new_page()
        self._route = lambda r: (r.abort() if r.request.resource_type in SKIP
                                 else r.continue_())
        self.ua = self.page.evaluate("navigator.userAgent")
        self.clearance = ""
        try:
            # Cloudflare's verification must see a complete page. Resource
            # filtering is safe only after the clearance cookie exists.
            self.renew()
        except Exception:
            self.close()
            raise
        self.page.route("**/*", self._route)

    def renew(self) -> str:
        """Load the site and take whatever cf_clearance Cloudflare hands over.

        Must be called from the thread that built this object.
        """
        self.page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=60_000)
        # A fresh context lands on the interstitial; it self-solves in seconds.
        if self.page.title().startswith("Just a moment"):
            try:
                self.page.wait_for_function(
                    "() => !document.title.startsWith('Just a moment')", timeout=60_000)
            except PlaywrightTimeoutError as e:
                raise AuthError(
                    "Cloudflare verification did not finish in 60 seconds in Chrome") from e
        self.clearance = self._cookie("cf_clearance") or self.clearance
        if not self.clearance:
            raise AuthError("browser never got a cf_clearance cookie -- challenge unsolved")
        return self.clearance

    def login(self, timeout: float = LOGIN_WAIT) -> str:
        """Hand this browser to the human and take the session their login earns.

        The fallback for every jar we cannot read -- Chrome and Edge 127+ on
        Windows encrypt theirs app-bound, so no outside process gets in. Logging
        in here also means the session and the Cloudflare pass come from one
        browser, so they always agree.

        Detection cannot lean on any one step of the login: it may finish in an
        OAuth popup, in a new tab, or without ever moving this page. So watch
        for two things -- any tab of ours landing on /me, and /me answering 200
        to the page's own fetch. The session cookie is no signal at all:
        anonymous visitors get one too.
        """
        self.page.unroute("**/*", self._route)  # the human needs css and images
        try:
            self.page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=60_000)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self._landed() or self._authenticated():
                    value = self._cookie(SESSION_COOKIE)
                    if value:
                        # Cloudflare may have rotated the pass while they signed in.
                        self.clearance = self._cookie("cf_clearance") or self.clearance
                        return value
                time.sleep(POLL_EVERY)
            raise AuthError(f"gave up waiting for the login; last page was {self.page.url}")
        finally:
            # A human may well close the tab once signed in; renew() needs one.
            if self.page.is_closed():
                self.page = self._ctx.new_page()
            self.page.route("**/*", self._route)

    def _landed(self) -> bool:
        """Any tab of ours sitting on /me -- where a finished login drops you."""
        return any(_is_me(p.url) for p in self._ctx.pages if not p.is_closed())

    def _authenticated(self) -> bool:
        """Ask /me from inside the page: anonymous callers are sent to /login.

        In-page fetch rather than an API request, so it carries the browser's
        own headers and Cloudflare state and is not challenged.
        """
        try:
            status = self.page.evaluate(
                """async () => {
                    const r = await fetch('/me', {redirect: 'manual'});
                    return r.type === 'opaqueredirect' ? 302 : r.status;
                }""")
        except PlaywrightError:
            return False  # mid-navigation, or the tab is gone
        return status == 200

    def _cookie(self, name: str) -> str:
        for c in self._ctx.cookies():
            if c["name"] == name and c["value"]:
                return c["value"]
        return ""

    def close(self) -> None:
        self._browser.close()
        self._pw.stop()


class Curl:
    """Fetches, shelled out to curl. Thread-safe: one process per call, and the
    only shared state is the Limiter and immutable strings off Pages."""

    def __init__(self, pages: Pages, session: Session, limiter: Limiter | None = None):
        self.pages = pages
        self.session = session
        self.limiter = limiter or Limiter()

    def _argv(self, url: str, auth: bool) -> list[str]:
        cookies = f"cf_clearance={self.pages.clearance}"
        if auth:
            cookies += f"; {SESSION_COOKIE}={self.session.value}"
        return [
            "curl", "-sS", "--compressed", "--max-time", "45",
            # No --retry: the Limiter owns backoff, and curl retrying 429s
            # internally would hide them from the rate controller.
            "--retry", "0",
            # cf_clearance is pinned to the UA that solved the challenge.
            "-H", f"User-Agent: {self.pages.ua}",
            "-H", "Accept-Language: en-US,en;q=0.9",
            "-b", cookies, "-w", "\n%{http_code}", url,
        ]

    def get(self, path: str, params: dict | None = None, auth: bool = False) -> str:
        url = path if path.startswith("http") else f"{BASE}{path}"
        if params:
            url += f"?{urlencode(params)}"
        for attempt in range(RETRIES):
            self.limiter.acquire()
            p = subprocess.run(self._argv(url, auth), capture_output=True, text=True)
            if p.returncode != 0:
                self.limiter.blocked()  # network wobble: same treatment, back off
                if attempt == RETRIES - 1:
                    raise RateLimited(f"curl exit {p.returncode} on {path}: {p.stderr.strip()}")
                continue
            body, _, code = p.stdout.rpartition("\n")
            status = int(code)
            if status == 429:
                self.limiter.blocked()
                continue
            if status == 403:
                raise ClearanceExpired(f"403 on {path}")
            self.limiter.ok()
            if status != 200:
                raise AuthError(f"HTTP {status} on {path}")
            return body
        raise RateLimited(f"429 on {path} after {RETRIES} backoffs")

    def json(self, path: str, params: dict | None = None) -> dict:
        return json.loads(self.get(path, params, auth=True))

    def logged_in(self) -> bool:
        """/me answers 200 for a real session and redirects to /login otherwise."""
        try:
            self.get("/me", auth=True)
            return True
        except AuthError:
            return False


