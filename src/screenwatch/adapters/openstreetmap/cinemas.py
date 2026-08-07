"""US cinema discovery through the public OpenStreetMap Overpass API.

This adapter answers a directory question: what cinema places are mapped, and
where are they? It does not claim that every mapped place publishes online
showtimes. The independent provider may attempt a venue website when one is
present, while the venue record keeps the OSM object and freshness visible.

The full refresh is split into geographic tiles so one large US query does not
make the public Overpass service carry an unnecessarily expensive response.
Nearby discovery uses one bounded ``around`` query. Both modes deduplicate on
the stable OSM object identity and reject rows that have no name or geometry.
"""

from __future__ import annotations

import json
import math
import urllib.parse
from dataclasses import dataclass

from ...ranking.spec import EARTH_RADIUS_KM, GeoPoint
from ...transport import Transport

OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
OSM_BASE = "https://www.openstreetmap.org"

# A small, deliberately overlapping tile set. Overlap is harmless because
# object identity is the deduplication key; the overlap avoids gaps on tile
# boundaries and keeps each public query bounded.
US_BBOXES: tuple[tuple[float, float, float, float], ...] = (
    (24.396308, -125.0, 49.384358, -102.0),
    (24.396308, -103.0, 49.384358, -90.0),
    (24.396308, -91.0, 49.384358, -78.0),
    (24.396308, -79.0, 49.384358, -66.885444),
    (51.2, -170.0, 71.6, -129.0),
    (18.8, -160.4, 22.3, -154.5),
)


@dataclass(frozen=True)
class OsmCinema:
    """Normalized cinema metadata from one OSM node, way, or relation."""

    osm_type: str
    osm_id: int
    name: str
    latitude: float
    longitude: float
    website: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    address: str | None = None
    operator: str | None = None
    wikidata: str | None = None

    @property
    def osm_url(self) -> str:
        return f"{OSM_BASE}/{self.osm_type}/{self.osm_id}"

    @property
    def venue_id(self) -> str:
        return f"independent-osm-{self.osm_type}-{self.osm_id}"

    @property
    def point(self) -> GeoPoint:
        return GeoPoint(self.latitude, self.longitude)


def _clean_url(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    if value.startswith("//"):
        return f"https:{value}"
    if not urllib.parse.urlparse(value).scheme:
        return f"https://{value}"
    return value


def _first(tags: dict, *keys: str) -> str | None:
    for key in keys:
        value = tags.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _coordinate(element: dict) -> tuple[float, float] | None:
    if element.get("type") == "node":
        lat, lon = element.get("lat"), element.get("lon")
    else:
        center = element.get("center") or {}
        lat, lon = center.get("lat"), center.get("lon")
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _validate_payload(raw: str | dict) -> dict:
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise ValueError("OpenStreetMap response was not valid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        raise ValueError("OpenStreetMap response has no elements array")
    return payload


class OsmCinemaDirectory:
    """Fetch and parse source-backed cinema directory records."""

    source = "osm:overpass"

    @staticmethod
    def _bbox_query(bbox: tuple[float, float, float, float]) -> str:
        south, west, north, east = bbox
        return (
            "[out:json][timeout:180];"
            "nwr[\"amenity\"=\"cinema\"]"
            f"({south:.6f},{west:.6f},{north:.6f},{east:.6f});"
            "out center tags;"
        )

    @staticmethod
    def _around_query(point: GeoPoint, radius_km: float) -> str:
        radius_m = max(1000, min(int(radius_km * 1000), 200_000))
        return (
            "[out:json][timeout:120];"
            f"nwr[\"amenity\"=\"cinema\"]"
            f"(around:{radius_m},{point.lat:.6f},{point.lon:.6f});"
            "out center tags;"
        )

    @staticmethod
    def request_url(query: str) -> str:
        return f"{OVERPASS_ENDPOINT}?{urllib.parse.urlencode({'data': query})}"

    def fetch_query(self, transport: Transport, query: str) -> dict:
        response = transport.get(self.request_url(query))
        if response.status_code != 200:
            raise RuntimeError(f"Overpass returned HTTP {response.status_code}")
        return _validate_payload(response.text)

    def parse(self, raw: str | dict) -> list[OsmCinema]:
        payload = _validate_payload(raw)
        out: list[OsmCinema] = []
        seen: set[tuple[str, int]] = set()
        for element in payload["elements"]:
            if not isinstance(element, dict):
                continue
            osm_type, osm_id = element.get("type"), element.get("id")
            if osm_type not in {"node", "way", "relation"} or not isinstance(osm_id, int):
                continue
            key = (osm_type, osm_id)
            if key in seen:
                continue
            tags = element.get("tags") or {}
            name = _first(tags, "name", "official_name", "short_name")
            coordinate = _coordinate(element)
            if not name or coordinate is None:
                continue
            website = _clean_url(_first(tags, "contact:website", "website", "url"))
            city = _first(tags, "addr:city", "is_in:city", "addr:town")
            state = _first(tags, "addr:state", "is_in:state")
            postal_code = _first(tags, "addr:postcode", "postal_code")
            street = " ".join(
                value for value in (
                    _first(tags, "addr:housenumber"),
                    _first(tags, "addr:street"),
                ) if value
            ) or None
            seen.add(key)
            out.append(
                OsmCinema(
                    osm_type=osm_type,
                    osm_id=osm_id,
                    name=name,
                    latitude=coordinate[0],
                    longitude=coordinate[1],
                    website=website,
                    city=city,
                    state=state,
                    postal_code=postal_code,
                    address=street,
                    operator=_first(tags, "operator", "brand"),
                    wikidata=_first(tags, "wikidata"),
                )
            )
        return out

    def nearby(self, transport: Transport, point: GeoPoint, radius_km: float) -> list[OsmCinema]:
        query = (
            self._around_query(point, radius_km)
            if radius_km <= 200
            else self._bbox_query(self.bbox_for(point, radius_km))
        )
        return self.parse(self.fetch_query(transport, query))

    def national(self, transport: Transport) -> list[OsmCinema]:
        out: dict[tuple[str, int], OsmCinema] = {}
        for bbox in US_BBOXES:
            for cinema in self.parse(self.fetch_query(transport, self._bbox_query(bbox))):
                out[(cinema.osm_type, cinema.osm_id)] = cinema
        return sorted(out.values(), key=lambda item: (item.name.casefold(), item.osm_id))

    @staticmethod
    def bbox_for(point: GeoPoint, radius_km: float) -> tuple[float, float, float, float]:
        """Return a bounded diagnostic bbox for callers and tests."""
        lat_delta = radius_km / EARTH_RADIUS_KM * 180 / math.pi
        lon_delta = lat_delta / max(math.cos(math.radians(point.lat)), 0.1)
        return (
            max(-90.0, point.lat - lat_delta),
            max(-180.0, point.lon - lon_delta),
            min(90.0, point.lat + lat_delta),
            min(180.0, point.lon + lon_delta),
        )
