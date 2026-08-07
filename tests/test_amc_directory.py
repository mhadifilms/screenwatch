from __future__ import annotations

from screenwatch.identity.work import WorkRef
from screenwatch.providers.amc import AmcProvider
from screenwatch.ranking.spec import SearchSpec
from screenwatch.service.search import SearchService
from screenwatch.service.store import Store
from screenwatch.service.venues import VenueDirectory
from screenwatch.transport import Response


class ReplayTransport:
    def __init__(self, body: str):
        self.body = body

    def get(self, url: str, *, conditional: bool = False):
        return Response(url=url, status_code=200, text=self.body)


def test_amc_provider_discovers_source_backed_national_venues(amc_sitemap_theatres):
    provider = AmcProvider()
    provider._discovery_transport = ReplayTransport(amc_sitemap_theatres)

    venues = provider.discover(SearchSpec(work=WorkRef(query="venue refresh")))

    assert len(venues) > 100
    assert all(venue.chain == "amc" for venue in venues)
    assert all(venue.point is not None for venue in venues)
    assert all(venue.source == "amc:sitemap-theatres" for venue in venues)
    assert all(venue.source_url == venue.url for venue in venues)
    assert any(venue.tz == "America/Los_Angeles" for venue in venues)


def test_full_refresh_persists_the_national_directory(amc_sitemap_theatres):
    provider = AmcProvider()
    provider._discovery_transport = ReplayTransport(amc_sitemap_theatres)
    store = Store.memory()
    service = SearchService(
        [provider], store=store, directory=VenueDirectory(), transport=object()
    )

    result = service.discover_venues(
        SearchSpec(work=WorkRef(query="venue refresh"))
    )

    assert result["provider_stats"][0]["status"] == "ok"
    assert result["scope"] == "national_directory"
    assert result["full_refresh"] is True
    assert result["complete"] is True
    assert result["discovered"] > 100
    assert len(store.directory_venues()) > 100
    assert store.directory_venues()[0]["source"] == "amc:sitemap-theatres"
    assert store.directory_venues()[0]["observed_at"]
    assert store.evidence_overview()["by_kind"][0]["kind"] == "directory"
    store.close()
