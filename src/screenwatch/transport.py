"""HTTP transport with Queue-it traversal and per-source pacing.

Observed behaviour of amctheatres.com (2026-08):

  * Plain python-requests / stock curl -> 403 at the edge. TLS fingerprint.
  * curl_cffi with a Chrome impersonation profile -> 200, but HTML routes
    return a ~2.6KB Queue-it interstitial rather than content.
  * The interstitial is a JS redirect carrying an `enqueuetoken`. Resolved
    against https://<company>.queue-it.net it returns the real page and sets
    a `QueueITAccepted-*` cookie good for the rest of the session.
  * /sitemap.xml and /sitemaps/*.xml bypass the queue entirely.

So: impersonate at the TLS layer, traverse the queue once per session, and
prefer routes that were never queued in the first place.
"""

from __future__ import annotations

import random
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

from curl_cffi import requests

# The interstitial's sole redirect. Matching this exact shape (rather than any
# document.location assignment) means a redesigned challenge page fails loudly
# instead of being silently mistaken for content.
_QUEUE_REDIRECT = re.compile(
    r"document\.location\.href\s*=\s*decodeURIComponent\('([^']+)'\)"
)
_QUEUE_MARKERS = ("enqueuetoken", "queue-it.net", "QueueITAccepted")

DEFAULT_IMPERSONATE = "chrome131"


class TransportError(RuntimeError):
    pass


class QueueTraversalError(TransportError):
    """Hit the waiting room and could not get through.

    Distinct from a transport failure: it may mean a real queue is active
    (a genuine on-sale surge) rather than a broken adapter.
    """


@dataclass
class Response:
    url: str
    status_code: int
    text: str
    from_cache: bool = False
    queue_traversed: bool = False
    elapsed_ms: int = 0

    @property
    def looks_queued(self) -> bool:
        return len(self.text) < 8000 and any(m in self.text for m in _QUEUE_MARKERS)


@dataclass
class Pacer:
    """Token bucket with jitter. One request in flight per host, always."""

    min_interval_s: float = 2.0
    jitter: float = 0.25
    _last: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def wait(self) -> None:
        with self._lock:
            interval = self.min_interval_s * (1 + random.uniform(-self.jitter, self.jitter))
            delta = self._last + interval - time.monotonic()
            if delta > 0:
                time.sleep(delta)
            self._last = time.monotonic()


class Transport:
    """One long-lived session per source. Keep it warm; don't recreate it.

    Recreating the session throws away the QueueITAccepted cookie and forces
    another traversal, which is both slow and a louder signal than reuse.
    """

    def __init__(
        self,
        impersonate: str = DEFAULT_IMPERSONATE,
        queue_host: str = "https://amctheatres.queue-it.net",
        min_interval_s: float = 2.0,
        timeout: int = 25,
        user_agent: str | None = None,
    ) -> None:
        self._session = requests.Session(impersonate=impersonate)
        self._queue_host = queue_host
        self._timeout = timeout
        self._user_agent = user_agent
        self._pacer = Pacer(min_interval_s=min_interval_s)
        self._etags: dict[str, str] = {}

    def get(self, url: str, *, conditional: bool = False) -> Response:
        started = time.monotonic()
        resp = self._raw_get(url, conditional=conditional)

        if resp.status_code == 304:
            return Response(url, 304, "", from_cache=True,
                            elapsed_ms=int((time.monotonic() - started) * 1000))

        traversed = False
        if resp.looks_queued:
            self._traverse_queue(url, resp.text)
            resp = self._raw_get(url)
            traversed = True
            if resp.looks_queued:
                raise QueueTraversalError(
                    f"still queued after traversal: {url} ({len(resp.text)}b) "
                    "- a real queue may be active"
                )

        resp.queue_traversed = traversed
        resp.elapsed_ms = int((time.monotonic() - started) * 1000)
        return resp

    def _raw_get(self, url: str, *, conditional: bool = False) -> Response:
        self._pacer.wait()
        headers = {"accept-language": "en-US,en;q=0.9"}
        if self._user_agent:
            headers["user-agent"] = self._user_agent
        if conditional and (etag := self._etags.get(url)):
            headers["if-none-match"] = etag
        r = self._session.get(url, headers=headers, timeout=self._timeout)
        if etag := r.headers.get("etag"):
            self._etags[url] = etag
        return Response(url=str(r.url), status_code=r.status_code, text=r.text)

    def _traverse_queue(self, original_url: str, interstitial_html: str) -> None:
        m = _QUEUE_REDIRECT.search(interstitial_html)
        if not m:
            raise QueueTraversalError(
                f"queue markers present but no known redirect in {original_url}; "
                "the challenge page changed - re-capture a fixture"
            )
        target = urllib.parse.urljoin(self._queue_host, urllib.parse.unquote(m.group(1)))
        self._pacer.wait()
        self._session.get(target, timeout=self._timeout, allow_redirects=True)
