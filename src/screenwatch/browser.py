"""Browser-backed transport, for the surfaces a plain client cannot reach.

Most of this system runs on `curl_cffi` with a Chrome TLS fingerprint, which
clears edge filtering and Queue-it and even Cloudflare's `__cf_bm` cookie. It
does not clear a Cloudflare *managed challenge*, because that one requires
executing JavaScript. Regal's `experience.regmovies.com/api/GetSeatPlan` is
behind exactly that: warm session, real cookies, still 403.

So: a real engine executes the challenge once, and afterwards the page's own
`fetch()` is used to call the JSON API from inside the trusted context. That
gives JSON ergonomics with browser trust, which was the plan from the outset.

No login is involved anywhere - none of these sites require an account to see
a seat map. The profile is persistent purely so the challenge clearance and
`cf_clearance` cookie survive between runs instead of being re-earned on
every request.

Lazily constructed and shared: launching Chromium costs ~1s and a few hundred
MB, so it happens only when a provider actually needs it, and one instance
serves every source that does.
"""

from __future__ import annotations

import contextlib
import json
import pathlib
import threading
from dataclasses import dataclass

DEFAULT_PROFILE = pathlib.Path.home() / ".screenwatch" / "browser-profile"
DEFAULT_TIMEOUT_MS = 45_000
# A *challenge* resolves itself if you wait and retry - JS runs, a cookie is
# issued, the page loads. A *block* is a firewall rule: the same request will
# fail forever from this IP, and retrying only deepens the hole. They look
# alike (both are Cloudflare 403s) and conflating them meant burning a retry
# budget against a wall.
CHALLENGE_MARKERS = ("Just a moment", "Checking your browser", "cf-challenge")
BLOCK_MARKERS = (
    "Attention Required",
    "Sorry, you have been blocked",
    "You are unable to access",
)


class BrowserUnavailable(RuntimeError):
    """Playwright is not installed, or Chromium could not start."""


@dataclass
class BrowserResponse:
    url: str
    status: int
    text: str

    @property
    def challenged(self) -> bool:
        """A solvable challenge. Worth retrying."""
        return any(m in self.text for m in CHALLENGE_MARKERS)

    @property
    def blocked(self) -> bool:
        """A firewall rule. Retrying will not help and makes it worse."""
        return any(m in self.text for m in BLOCK_MARKERS)

    @property
    def denied(self) -> bool:
        return self.challenged or self.blocked

    def json(self):
        return json.loads(self.text)


class BrowserTransport:
    """One persistent Chromium context, reused for every browser-tier fetch."""

    _lock = threading.Lock()

    def __init__(
        self,
        profile_dir: pathlib.Path | str = DEFAULT_PROFILE,
        *,
        headless: bool = True,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        challenge_wait_ms: int = 12_000,
    ) -> None:
        self.profile_dir = pathlib.Path(profile_dir)
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.challenge_wait_ms = challenge_wait_ms
        self._playwright = None
        self._context = None
        self._page = None

    # ------------------------------------------------------------------
    def _ensure(self):
        if self._page is not None:
            return self._page
        with self._lock:
            if self._page is not None:
                return self._page
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise BrowserUnavailable(
                    "playwright is not installed; `pip install playwright && "
                    "playwright install chromium`"
                ) from exc

            self.profile_dir.mkdir(parents=True, exist_ok=True)
            try:
                self._playwright = sync_playwright().start()
                self._context = self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    headless=self.headless,
                    viewport={"width": 1440, "height": 900},
                    locale="en-US",
                    args=["--disable-blink-features=AutomationControlled"],
                )
                self._context.set_default_timeout(self.timeout_ms)
                self._page = (
                    self._context.pages[0] if self._context.pages
                    else self._context.new_page()
                )
            except Exception as exc:
                raise BrowserUnavailable(f"could not start Chromium: {exc}") from exc
        return self._page

    # ------------------------------------------------------------------
    def visit(self, url: str, *, wait_for_challenge: bool = True) -> BrowserResponse:
        """Navigate, letting a managed challenge resolve itself if one appears."""
        page = self._ensure()
        response = page.goto(url, wait_until="domcontentloaded")
        body = page.content()

        if wait_for_challenge and any(m in body for m in CHALLENGE_MARKERS) \
                and not any(m in body for m in BLOCK_MARKERS):
            # The challenge resolves itself and navigates on; waiting for the
            # network to settle is enough, and cheaper than polling the DOM.
            # Timing out here is fine: the content is read either way, and
            # `challenged` on the response is what decides whether it is
            # usable.
            with contextlib.suppress(Exception):
                page.wait_for_load_state("networkidle",
                                         timeout=self.challenge_wait_ms)
            body = page.content()

        return BrowserResponse(
            url=page.url,
            status=response.status if response else 0,
            text=body,
        )

    def fetch_json(self, url: str, *, origin: str | None = None) -> BrowserResponse:
        """Call a JSON endpoint from *inside* the page.

        This is the point of the whole module: the request inherits the page's
        cookies, TLS fingerprint and challenge clearance, so an endpoint that
        403s a standalone client answers normally.
        """
        page = self._ensure()
        if origin and not page.url.startswith(origin):
            self.visit(origin)

        result = page.evaluate(
            """async (url) => {
                const r = await fetch(url, {
                    headers: {accept: 'application/json, text/plain, */*'},
                    credentials: 'include',
                });
                return {status: r.status, body: await r.text()};
            }""",
            url,
        )
        return BrowserResponse(url=url, status=result["status"], text=result["body"])

    def text(self, url: str) -> str:
        return self.visit(url).text

    # ------------------------------------------------------------------
    def close(self) -> None:
        for closer in (self._context, self._playwright):
            with contextlib.suppress(Exception):
                if closer is not None:
                    closer.close() if hasattr(closer, "close") else closer.stop()
        self._context = self._page = self._playwright = None

    def __enter__(self) -> BrowserTransport:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


_SHARED: BrowserTransport | None = None


def shared_browser(**kw) -> BrowserTransport:
    """Process-wide browser. Starting one per provider would be absurd."""
    global _SHARED
    if _SHARED is None:
        _SHARED = BrowserTransport(**kw)
    return _SHARED
