"""Resolve place names ("Bangalore", "Whitefield") to olx.in location ids and coordinates."""
import json
import math
import os
from typing import Optional

import aiohttp

from src.Scraping.URLBuilder import OLX_BASE, URLBuilder

AUTOCOMPLETE_URL = f"{OLX_BASE}/api/locations/autocomplete"

# When several places share a name, prefer the more specific one users usually mean.
_TYPE_PRIORITY = {"CITY": 0, "NEIGHBOURHOOD": 1, "NEIGHBORHOOD": 1, "DISTRICT": 2, "STATE": 3, "COUNTRY": 4}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in km."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _to_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_suggestions(payload) -> list[dict]:
    """Find location-like dicts ({id, name, ...}) anywhere in the autocomplete response."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            if "id" in node and "name" in node and not isinstance(node.get("name"), (dict, list)):
                found.append(node)
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


def _normalise_suggestion(raw: dict) -> Optional[dict]:
    try:
        location_id = int(raw["id"])
    except (KeyError, TypeError, ValueError):
        return None
    lat = _to_float(raw.get("latitude", raw.get("lat")))
    lon = _to_float(raw.get("longitude", raw.get("lon")))
    return {
        "id": location_id,
        "name": str(raw.get("name", "")).strip(),
        "type": str(raw.get("type", "")).upper(),
        "lat": lat,
        "lon": lon,
    }


def pick_best_suggestion(query: str, suggestions: list[dict], wanted_id: Optional[int] = None) -> Optional[dict]:
    """Pick the suggestion matching `wanted_id`, else an exact name match, else the first one."""
    candidates = [s for s in (_normalise_suggestion(raw) for raw in suggestions) if s]
    if not candidates:
        return None
    if wanted_id is not None:
        return next((s for s in candidates if s["id"] == wanted_id), None)
    q = query.strip().lower()
    exact = [s for s in candidates if s["name"].lower() == q]
    if exact:
        return min(exact, key=lambda s: _TYPE_PRIORITY.get(s["type"], 9))
    return candidates[0]


class LocationResolver:
    """Resolves URLBuilder.location to an OLX location id, caching results on disk."""

    def __init__(self, cache_file: str, headers: Optional[dict] = None) -> None:
        self.cache_file = cache_file
        self.headers = headers or {}
        self._cache = self._load_cache()

    def _load_cache(self) -> dict:
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_cache(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2)
        except OSError:
            pass

    async def _autocomplete(self, session: aiohttp.ClientSession, text: str) -> list[dict]:
        params = {"input": text, "limit": "10", "lang": "en-IN"}
        async with session.get(AUTOCOMPLETE_URL, params=params, headers=self.headers) as response:
            if response.status != 200:
                raise Exception(f"Location lookup failed ({response.status}) for '{text}'")
            return _extract_suggestions(await response.json(content_type=None))

    async def resolve(self, session: aiohttp.ClientSession, url_builder: URLBuilder) -> None:
        """
        Fill url_builder.location_id / name / lat / lon.
        Plain names are looked up through OLX's location autocomplete. Pinned slugs
        ("bangalore_g4058803") already have an id; we only try to fetch their coordinates
        so the distance filter can work.
        """
        if not url_builder.location:
            return

        cache_key = url_builder.location.lower()
        cached = self._cache.get(cache_key)
        if cached:
            url_builder.set_resolved_location(cached["id"], cached["name"], cached.get("lat"), cached.get("lon"))
            return

        pinned_id = url_builder.location_id
        lookup_text = url_builder.location_name if pinned_id else url_builder.location
        best = None
        if lookup_text:
            try:
                suggestions = await self._autocomplete(session, lookup_text)
                best = pick_best_suggestion(lookup_text, suggestions, wanted_id=pinned_id)
            except Exception:
                if not pinned_id:
                    raise
        if best is None:
            if pinned_id:
                return  # keep the pinned id, just without coordinates
            raise Exception(
                f"Could not find OLX location '{url_builder.location}'. "
                f"Try another spelling, or paste the location part of an olx.in URL "
                f"(e.g. 'bangalore_g4058803')."
            )

        url_builder.set_resolved_location(best["id"], best["name"], best["lat"], best["lon"])
        self._cache[cache_key] = best
        self._save_cache()
