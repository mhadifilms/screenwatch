"""The one place the default wiring lives.

Three entry points build a working system - the MCP server, the HTTP API, and
the scheduler daemon - and each used to construct its own provider list. They
drifted: the scheduler was still on four providers after C360 and the
independents were added, so **monitors silently never saw those chains**.
Nothing failed; those venues just never appeared in a watch, which is the
worst kind of bug in a system whose whole job is telling you what exists.

So the list is defined once and imported. A provider added here is available
to every transport by construction.
"""

from __future__ import annotations

from ..identity.resolve import WorkResolver
from ..identity.tmdb import TmdbCatalog
from .search import SearchService
from .store import Store
from .watch import WatchService

DEFAULT_DB = "screenwatch.db"


def default_resolver() -> WorkResolver:
    """Shared across every provider so a film resolved from one chain's
    product id is the same Work when another chain reports it."""
    return WorkResolver(TmdbCatalog.from_env())


def default_providers(resolver: WorkResolver | None = None, *, store=None) -> list:
    resolver = resolver or default_resolver()

    from ..providers.alamo import AlamoProvider
    from ..providers.amc import AmcProvider
    from ..providers.c360 import C360Provider
    from ..providers.cinemark import CinemarkProvider
    from ..providers.independent import IndependentProvider
    from ..providers.regal import RegalProvider

    return [
        AmcProvider(resolver),
        AlamoProvider(resolver),
        RegalProvider(resolver),
        CinemarkProvider(resolver, store=store),
        C360Provider(resolver, store=store),
        IndependentProvider(resolver),
    ]


def default_service(db: str = DEFAULT_DB) -> tuple[SearchService, WatchService, Store]:
    store = Store(db)
    resolver = default_resolver()
    search = SearchService(
        providers=default_providers(resolver, store=store),
        store=store,
        resolver=resolver,
    )
    return search, WatchService(search, store), store
