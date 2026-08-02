"""robots.txt awareness - advisory by default.

robots.txt is a convention for *crawlers*: software that walks a site on its
own initiative to build an index. This tool is not that. It fetches the pages
a specific user asked about, on their behalf, in roughly the volume that user
would generate by clicking. Cinemark disallowing `/TicketSeatMap` is aimed at
indexers, not at a person looking at whether four seats are free.

So enforcement is **off by default** (`enforce=False`): `check_fetch` records
what a crawler-mode client would have skipped and lets the request through.

It is kept, rather than deleted, for the one part of this system that *is*
crawler-shaped: the always-on scheduler polls on a timer with no human in the
loop. Constructing `RobotsCache(enforce=True)` restores blocking for that
path if you want the background poller to be more conservative than the
interactive one.

Fail-open on a missing or unreadable robots.txt.
"""

from __future__ import annotations

import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field

from curl_cffi import requests

USER_AGENT = "*"


class DisallowedByRobots(PermissionError):
    """The site's robots.txt forbids automated retrieval of this path."""

    def __init__(self, url: str, rule_source: str) -> None:
        super().__init__(
            f"{url} is disallowed by {rule_source}. This path is off limits to "
            "automated fetching; surface it as a link for the user instead."
        )
        self.url = url


@dataclass
class RobotsCache:
    """One parsed robots.txt per host, fetched lazily and kept for the process.

    Deliberately not time-expiring: a long-running poller re-reading robots.txt
    on a timer would add traffic to make a decision that essentially never
    changes within a session.
    """

    user_agent: str = USER_AGENT
    enforce: bool = False        # advisory unless a caller opts in
    session: requests.Session | None = None
    _parsers: dict[str, urllib.robotparser.RobotFileParser | None] = field(
        default_factory=dict
    )
    skipped: list[str] = field(default_factory=list)

    def _client(self) -> requests.Session:
        if self.session is None:
            self.session = requests.Session(impersonate="chrome131")
        return self.session

    def _parser(self, url: str):
        parts = urllib.parse.urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host in self._parsers:
            return self._parsers[host]

        parser = urllib.robotparser.RobotFileParser()
        try:
            response = self._client().get(f"{host}/robots.txt", timeout=12)
            if response.status_code == 200:
                parser.parse(response.text.splitlines())
            else:
                parser = None          # no robots.txt: no restrictions
        except Exception:              # noqa: BLE001 - unreachable == unrestricted
            parser = None

        self._parsers[host] = parser
        return parser

    # ------------------------------------------------------------------
    def allowed(self, url: str) -> bool:
        parser = self._parser(url)
        return True if parser is None else parser.can_fetch(self.user_agent, url)

    def check_fetch(self, url: str) -> None:
        """Advisory by default; raises only when `enforce` is set.

        Disallowed URLs are recorded either way so `skipped` shows what a
        crawler-mode client would have avoided.
        """
        if self.allowed(url):
            return
        self.skipped.append(url)
        if self.enforce:
            raise DisallowedByRobots(url, "robots.txt")

    @staticmethod
    def check_link(url: str) -> str:
        """A URL handed to a human. Never blocked.

        Exists so the difference is explicit at every call site: a booking
        deeplink to a disallowed path is fine, because the user clicks it.
        """
        return url


ROBOTS = RobotsCache()
