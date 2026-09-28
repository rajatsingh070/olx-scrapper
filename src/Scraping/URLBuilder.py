import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

OLX_BASE = "https://www.olx.in"
API_SEARCH_URL = f"{OLX_BASE}/api/relevance/v4/search"

# OLX location slugs look like "bangalore_g4058803" (the number is the location id).
_LOCATION_SLUG_RE = re.compile(r"^(?:(?P<name>.*?)_)?g?(?P<id>\d+)$", re.IGNORECASE)


def slugify(text: str) -> str:
    """'Royal Enfield Classic' -> 'royal-enfield-classic'."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def parse_location_slug(text: str) -> Optional[tuple[str, int]]:
    """
    Parse a pinned OLX location such as 'bangalore_g4058803', 'g4058803' or '4058803'.
    :return: (name, id) or None if the text is a plain place name.
    """
    match = _LOCATION_SLUG_RE.match((text or "").strip().replace(" ", "-"))
    if not match:
        return None
    name = (match.group("name") or "").replace("-", " ").strip()
    return name, int(match.group("id"))


def _parse_km(value) -> Optional[float]:
    try:
        km = float(value)
    except (TypeError, ValueError):
        return None
    return km if km > 0 else None


@dataclass
class URLBuilder:
    """
    One olx.in search query.

    :param item_query: What to search for, e.g. "royal enfield classic 350".
    :param location: City / area / state name ("Bangalore", "Whitefield", "Kerala"),
        or a pinned OLX location slug copied from the olx.in URL ("bangalore_g4058803").
        Empty means all of India.
    :param distance: Optional radius in km around the location's centre. Applied locally,
        using each listing's coordinates.
    :param localities: Optional comma-separated area names. Only listings whose location
        mentions one of them are kept, e.g. "Whitefield, Marathahalli".
    :param city: Legacy alias for `location` (old olx.pl configs).
    """
    item_query: str
    location: str = ""
    distance: Optional[str] = ""
    localities: str = ""
    city: str = ""

    # Filled in by LocationResolver before scraping.
    location_id: Optional[int] = field(default=None, repr=False)
    location_name: str = field(default="", repr=False)
    location_lat: Optional[float] = field(default=None, repr=False)
    location_lon: Optional[float] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.item_query = (self.item_query or "").strip()
        self.location = (self.location or self.city or "").strip()
        self.localities = (self.localities or "").strip()
        pinned = parse_location_slug(self.location) if self.location else None
        if pinned:
            self.location_name, self.location_id = pinned

    @property
    def distance_km(self) -> Optional[float]:
        return _parse_km(self.distance)

    @property
    def locality_list(self) -> list[str]:
        return [part.strip().lower() for part in self.localities.split(",") if part.strip()]

    def set_resolved_location(self, location_id: int, name: str,
                              lat: Optional[float] = None, lon: Optional[float] = None) -> None:
        self.location_id = location_id
        self.location_name = name or self.location_name
        self.location_lat = lat
        self.location_lon = lon

    def build_url(self, page: int = 1) -> str:
        """
        Build the olx.in search page URL (what you would see in the browser).
        olx.in paginates with a "Load more" button, so only page 1 is a real page.
        """
        query = quote(slugify(self.item_query))
        if self.location_id:
            place = f"{slugify(self.location_name) or 'location'}_g{self.location_id}"
        else:
            place = "items"
        url = f"{OLX_BASE}/{place}/q-{query}?sorting=desc-creation"
        if page > 1:
            url += f"&page={page}"
        return url

    def build_api_params(self, page: int = 1) -> dict[str, str]:
        """Query parameters for the olx.in search API. The API's pages are 0-based."""
        params = {
            "query": self.item_query,
            "page": str(max(page - 1, 0)),
            "sorting": "desc-creation",
            "platform": "web-desktop",
            "lang": "en-IN",
        }
        if self.location_id:
            params["location"] = str(self.location_id)
        return params

    def generate_data_key(self) -> str:
        """
        Generate key for data dictionary.
        :return: Generated key.
        """
        key = self.item_query.capitalize()
        place = self.location_name or self.location
        if place:
            key += f" - {place.title()}"
        if self.distance_km:
            key += f" - {self.distance_km:g}km"
        if self.locality_list:
            key += f" - {', '.join(loc.title() for loc in self.locality_list)}"
        return key
