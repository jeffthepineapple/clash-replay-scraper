"""How this scraper talks to RoyaleAPI.

Pages  an anonymous headful Chromium, used for one thing: solving the Cloudflare
       challenge and holding the cf_clearance cookie it earns. Headful is not
       optional -- headless Chromium is challenged and never gets through -- so
       on Linux DISPLAY or WAYLAND_DISPLAY must be set.

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
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

from .cookies import SESSION_COOKIE, Session
from .limiter import Limiter

BASE = "https://royaleapi.com"
SKIP = ("image", "font", "media", "stylesheet")  # nothing we parse needs these
RETRIES = 6  # per request, all of them 429 backoffs handed to the Limiter


class RateLimited(RuntimeError):
    """429 that survived every backoff."""


class ClearanceExpired(RuntimeError):
    """403: the Cloudflare pass died mid-run and only the browser can renew it."""


class AuthError(RuntimeError):
    """Cloudflare or the login gate turned us away for good."""


class Pages:
    """The anonymous browser. Single-threaded: playwright's sync API is."""

    def __init__(self):
        if sys.platform.startswith("linux") and not (
                os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            raise AuthError("no display: the anonymous browser must run headful "
                            "to clear Cloudflare")
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=False, args=["--disable-blink-features=AutomationControlled"])
        self._ctx = self._browser.new_context()
        self.page = self._ctx.new_page()
        self.page.route("**/*", lambda r: r.abort() if r.request.resource_type in SKIP
                        else r.continue_())
        self.ua = self.page.evaluate("navigator.userAgent")
        self.clearance = ""
        self.renew()

    def renew(self) -> str:
        """Load the site and take whatever cf_clearance Cloudflare hands over.

        Must be called from the thread that built this object.
        """
        self.page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=60_000)
        # A fresh context lands on the interstitial; it self-solves in seconds.
        if self.page.title().startswith("Just a moment"):
            self.page.wait_for_function(
                "() => !document.title.startsWith('Just a moment')", timeout=60_000)
        for c in self._ctx.cookies():
            if c["name"] == "cf_clearance":
                self.clearance = c["value"]
        if not self.clearance:
            raise AuthError("browser never got a cf_clearance cookie -- challenge unsolved")
        return self.clearance

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


