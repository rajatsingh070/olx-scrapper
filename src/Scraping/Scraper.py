import json
import os
import random
import bs4.element
import pandas as pd
import aiohttp
import asyncio
import re
from datetime import datetime
from typing import Union, Callable, Optional
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from src.Exporting.formatting import format_price, format_listing_date
from src.Scraping.Locations import LocationResolver, haversine_km
from src.Scraping.URLBuilder import URLBuilder, API_SEARCH_URL, OLX_BASE, slugify


# Heuristics for filtering out accessory/parts listings that show up when
# searching for a product. Real product listings rarely contain any of these
# words in their title; accessory listings nearly always do.
_ACCESSORY_BRANDS = {
    "tech-protect", "techprotect", "spigen", "ringke", "baseus", "ugreen",
    "anker", "belkin", "mophie", "otterbox", "nillkin", "rhinoshield",
    "caseology", "puro", "wozinsky", "bizon", "amazingthing",
}

_ACCESSORY_KEYWORDS = {
    # Polish — cases & covers
    "etui", "pokrowiec", "obudowa", "futerał", "futeral",
    # Polish — glass & film
    "szkło", "szklo", "folia", "hartowane", "hartowana",
    # Polish — cables, chargers, adapters
    "ładowarka", "ladowarka", "kabel", "przewód", "przewod",
    # Polish — holders, mounts, straps
    "uchwyt", "stojak", "podstawka", "pasek",
    # Polish — replacement parts & repair
    "klapka", "klapki", "wymiana", "naprawa", "wyświetlacz", "wyswietlacz",
    "części", "czesci", "część", "czesc", "lcd",
    # English equivalents
    "case", "cover", "sleeve", "wallet",
    "glass", "tempered", "protector", "screen", "film",
    "charger", "cable", "adapter", "holder", "mount", "stand",
    "replacement", "repair", "parts", "flap",
    # Common on olx.in
    "guard", "pouch", "skin", "spare", "spares", "accessory", "accessories",
}

_TOKEN_RE = re.compile(r"[\w]+", flags=re.UNICODE)

# olx.in rejects requests that don't look like they come from a browser.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}
_API_HEADERS = {**_BROWSER_HEADERS, "Accept": "application/json, text/plain, */*"}
_HTML_HEADERS = {**_BROWSER_HEADERS, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}

_APP_STATE_RE = re.compile(r"window\.__APP\s*=\s*")

DISTANCE_COLUMN = "Distance (km)"


def _classify_relevance(title: str) -> bool:
    """True if the listing looks like a real product, False if an accessory/part."""
    if not title:
        return False
    t = title.lower()
    if any(brand in t for brand in _ACCESSORY_BRANDS):
        return False
    words = {w.lower() for w in _TOKEN_RE.findall(t)}
    if words & _ACCESSORY_KEYWORDS:
        return False
    return True


def _dig(data, *path):
    """Safely walk nested dicts/lists: _dig(ad, "price", "value", "raw")."""
    for key in path:
        if isinstance(data, dict):
            data = data.get(key)
        elif isinstance(data, list) and isinstance(key, int) and -len(data) <= key < len(data):
            data = data[key]
        else:
            return None
    return data


def _looks_like_ad(node) -> bool:
    return (
        isinstance(node, dict)
        and isinstance(node.get("title"), str)
        and "price" in node
        and ("ad_id" in node or "id" in node)
    )


def _find_ads(payload) -> list[dict]:
    """Collect ad objects from an API response or the page's embedded state, wherever they are."""
    found, seen = [], set()

    def walk(node):
        if _looks_like_ad(node):
            ad_id = str(node.get("ad_id") or node.get("id"))
            if ad_id not in seen:
                seen.add(ad_id)
                found.append(node)
            return
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


class Scraper:
    def __init__(self, url_strings: list[URLBuilder], page_limit: int,
                 request_delay: tuple[float, float] = (1.0, 2.5), max_concurrent_queries: int = 2) -> None:
        """
        Scraper class for scraping data from olx.in.
        :param url_strings: List of URLBuilder objects for scraping data.
        :param page_limit: Limit of pages to scrape for each URL.
        :param request_delay: Random pause range (seconds) between page requests of one query.
        :param max_concurrent_queries: How many queries are scraped at the same time.
        """
        self.url_list = url_strings if url_strings else []
        self.page_limit = page_limit
        self.request_delay = request_delay
        self.max_concurrent_queries = max_concurrent_queries
        self.data_frames = dict()
        self.errors: dict[str, str] = dict()
        self.resources_dir = os.path.join(os.path.dirname(__file__), '../Resources')
        self.scraping_history = self.load_scraping_history()
        self.last_scrape_date = (
            datetime.fromisoformat(self.scraping_history[-1]['scrape_date'])
            if self.scraping_history else None
        )
        self._seen_urls_file = os.path.join(self.resources_dir, 'seen_listings.json')
        self._seen_urls: dict = self._load_seen_urls()
        self.location_resolver = LocationResolver(
            os.path.join(self.resources_dir, 'location_cache.json'), headers=_API_HEADERS
        )

    def add_url(self, url: URLBuilder) -> None:
        """Adds a URLBuilder object to the list of URLs to scrape."""
        self.url_list.append(url)

    async def scrape_data(self, progress_callback: Callable[[int], None] = None) -> dict[str, pd.DataFrame]:
        """Scrapes data from the URLs asynchronously."""
        self.data_frames = dict()
        self.errors = dict()
        num_urls = len(self.url_list)
        if num_urls == 0:
            return self.data_frames

        semaphore = asyncio.Semaphore(self.max_concurrent_queries)
        finished = 0

        async def run_query(session: aiohttp.ClientSession, url_builder: URLBuilder) -> pd.DataFrame:
            nonlocal finished
            async with semaphore:
                try:
                    return await self._fetch_data_from_url(session, url_builder)
                except Exception as e:
                    # One failing query shouldn't throw away the others.
                    self.errors[url_builder.generate_data_key()] = str(e)
                    return pd.DataFrame()
                finally:
                    finished += 1
                    if progress_callback:
                        progress_callback(int(finished / num_urls * 50))

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            data = await asyncio.gather(*(run_query(session, url) for url in self.url_list))

        if len(self.errors) == num_urls:
            raise Exception("; ".join(self.errors.values()))

        current_urls: dict = {}
        for i, result, url_builder in zip(range(len(self.url_list)), data, self.url_list):
            key = url_builder.generate_data_key()
            if not result.empty and "Item URL" in result.columns:
                prev = self._seen_urls.get(key, {})  # {url: price}
                if prev:
                    result["Is New"] = result["Item URL"].apply(
                        lambda url: bool(url) and url not in prev
                    )
                    def _old_price(row, prev=prev):
                        url = row["Item URL"]
                        if not url or url not in prev:
                            return 0
                        old = prev[url]
                        try:
                            cur = int(row["Price"])
                            old = int(old)
                        except (TypeError, ValueError):
                            return 0
                        return old if (old > 0 and cur > 0 and cur < old) else 0
                    result["Old Price"] = result.apply(_old_price, axis=1)
                else:
                    result["Is New"] = False
                    result["Old Price"] = 0
                if "Title" in result.columns:
                    result["Is Match"] = result["Title"].apply(_classify_relevance)
                else:
                    result["Is Match"] = True

                matched_only = result[result["Is Match"] == True]
                filtered_result = self._filter_price_outliers(matched_only)
                result = pd.concat([filtered_result, result[result["Is Match"] == False]])
                
                current_urls[key] = {
                    row["Item URL"]: int(row["Price"])
                    for _, row in result.iterrows()
                    if row.get("Item URL")
                }
            else:
                current_urls[key] = self._seen_urls.get(key, {}) if key in self.errors else {}
            self.data_frames[key] = result
            if progress_callback:
                progress_callback(int((i + 1) / num_urls * 50 + 50))
        self._save_seen_urls(current_urls)
        self._seen_urls = current_urls
        self.last_scrape_date = datetime.now()
        self.save_scrape_date()
        return self.data_frames

    async def _fetch_data_from_url(self, session: aiohttp.ClientSession, url_builder: URLBuilder) -> pd.DataFrame:
        """Fetches, parses and location-filters the listings of one query."""
        await self.location_resolver.resolve(session, url_builder)

        rows = await self._fetch_via_api(session, url_builder)
        if rows is None:
            # The API answered with something we don't recognise; fall back to the web page.
            rows = await self._fetch_via_html(session, url_builder)

        rows = self._apply_location_filters(rows, url_builder)
        return pd.DataFrame(rows) if rows else pd.DataFrame()

    async def _get(self, session: aiohttp.ClientSession, url: str, headers: dict,
                   params: Optional[dict] = None) -> str:
        async with session.get(url, params=params, headers=headers) as response:
            if response.status in (403, 429):
                raise Exception(
                    f"olx.in refused the request ({response.status}) — too many requests or the "
                    f"scraper was blocked. Wait a while, lower the page limit, and try again."
                )
            if response.status != 200:
                raise Exception(f"Error: {response.status} for {response.url}")
            return await response.text()

    async def _fetch_via_api(self, session: aiohttp.ClientSession, url_builder: URLBuilder) -> Optional[list[dict]]:
        """
        Pages through olx.in's search API.
        :return: parsed rows, or None if the response didn't look like search results.
        """
        rows, seen_ids = [], set()
        for page in range(1, self.page_limit + 1):
            if page > 1:
                await asyncio.sleep(random.uniform(*self.request_delay))
            text = await self._get(session, API_SEARCH_URL, _API_HEADERS, url_builder.build_api_params(page))
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                return None if page == 1 else rows

            ads = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(ads, list):
                ads = _find_ads(payload)
                if not ads and page == 1:
                    return None
            ads = [ad for ad in ads if _looks_like_ad(ad)]

            new_ads = [ad for ad in ads if str(ad.get("ad_id") or ad.get("id")) not in seen_ids]
            if not new_ads:
                break
            for ad in new_ads:
                seen_ids.add(str(ad.get("ad_id") or ad.get("id")))
                row = self._parse_ad(ad)
                if row.get("Title"):
                    rows.append(row)
            metadata = _dig(payload, "metadata")
            if isinstance(metadata, dict) and "next_page_url" in metadata and not metadata["next_page_url"]:
                break  # last page
        return rows

    async def _fetch_via_html(self, session: aiohttp.ClientSession, url_builder: URLBuilder) -> list[dict]:
        """Parses the first search results page (olx.in loads later pages via the API)."""
        html = await self._get(session, url_builder.build_url(1), _HTML_HEADERS)
        ads = self._extract_app_state_ads(html)
        if ads:
            rows = [self._parse_ad(ad) for ad in ads]
        else:
            soup = BeautifulSoup(html, "html.parser")
            rows = []
            for card in soup.select('li[data-aut-id="itemBox"]'):
                try:
                    rows.append(self._parse_card(card))
                except Exception:
                    # Defensive: never let one malformed card kill the whole scrape.
                    continue
        return [row for row in rows if row.get("Title")]

    @staticmethod
    def _extract_app_state_ads(html: str) -> list[dict]:
        """olx.in embeds its initial page state as `window.__APP = {...}`; pull the ads out of it."""
        match = _APP_STATE_RE.search(html)
        if not match:
            return []
        try:
            state, _ = json.JSONDecoder().raw_decode(html, match.end())
        except ValueError:
            return []
        return _find_ads(state)

    @staticmethod
    def _parse_ad(ad: dict) -> dict:
        """Turns one olx.in ad object (API or page state) into a table row."""
        title = str(ad.get("title") or "").strip()
        ad_id = ad.get("ad_id") or ad.get("id")

        price = _dig(ad, "price", "value", "raw")
        if not isinstance(price, (int, float)):
            display = _dig(ad, "price", "value", "display")
            if isinstance(display, str):
                price = format_price(display)
            elif isinstance(ad.get("price"), (int, float)):
                price = ad["price"]
            else:
                price = 0

        resolved = ad.get("locations_resolved") or {}
        area = resolved.get("SUBLOCALITY_LEVEL_1_name")
        city = resolved.get("ADMIN_LEVEL_3_name")
        state = resolved.get("ADMIN_LEVEL_1_name")
        parts = [p for p in (area, city) if p] or ([state] if state else [])
        location = ", ".join(dict.fromkeys(parts))
        if not location and isinstance(ad.get("location"), str):
            location = ad["location"]

        lat = _dig(ad, "locations", 0, "lat")
        lon = _dig(ad, "locations", 0, "lon")

        date = ad.get("created_at_first") or ad.get("created_at") or ad.get("display_date") or ""

        url = ad.get("url") if isinstance(ad.get("url"), str) else ""
        if not url and ad_id:
            url = f"/item/{slugify(title) or 'ad'}-iid-{ad_id}"
        url = urljoin(OLX_BASE, url) if url else ""

        photo = ""
        image = _dig(ad, "images", 0)
        if isinstance(image, dict):
            photo = image.get("url") or _dig(image, "big", "url") or _dig(image, "medium", "url") or ""
        elif isinstance(image, str):
            photo = image

        return {
            "Title": title,
            "Price": int(price or 0),
            "Location": location,
            "Date": format_listing_date(str(date)),
            "Item URL": url,
            "Photo": photo,
            "_lat": lat if isinstance(lat, (int, float)) else None,
            "_lon": lon if isinstance(lon, (int, float)) else None,
            "_state": state or "",
        }

    @staticmethod
    def _safe_text(element) -> str:
        """Return stripped text from a bs4 element, or '' if None."""
        if element is None:
            return ""
        try:
            return element.get_text(strip=True)
        except AttributeError:
            return ""

    @staticmethod
    def _parse_card(card: bs4.element.Tag) -> dict:
        """Parses a listing card from the olx.in search page HTML."""
        text = Scraper._safe_text
        title = text(card.select_one('[data-aut-id="itemTitle"]'))
        if not title:
            anchor = card.find("a")
            title = (anchor.get("title") or anchor.get("aria-label") or "") if anchor else ""
        price = format_price(text(card.select_one('[data-aut-id="itemPrice"]')))
        location = text(card.select_one('[data-aut-id="item-location"]'))

        date = ""
        date_el = card.select_one('[data-aut-id="itemDetails"] + span, [data-aut-id="item-location"] ~ span')
        if date_el is not None:
            date = text(date_el)

        anchor = card.find("a", href=True)
        img = card.find("img")
        return {
            "Title": title.strip(),
            "Price": price,
            "Location": location,
            "Date": date,
            "Item URL": urljoin(OLX_BASE, anchor["href"]) if anchor else "",
            "Photo": (img.get("src") or img.get("data-src") or "") if img else "",
            "_lat": None,
            "_lon": None,
            "_state": "",
        }

    @staticmethod
    def _apply_location_filters(rows: list[dict], url_builder: URLBuilder) -> list[dict]:
        """
        Fine-grained location filtering on top of OLX's own location search:
        - localities: keep listings whose location mentions one of the given areas
        - distance: keep listings within N km of the searched location, and add a
          "Distance (km)" column. Listings without coordinates are kept.
        """
        localities = url_builder.locality_list
        if localities:
            rows = [r for r in rows if any(loc in r["Location"].lower() for loc in localities)]

        radius = url_builder.distance_km
        center = (url_builder.location_lat, url_builder.location_lon)
        has_center = None not in center
        if has_center:
            kept = []
            for row in rows:
                if row["_lat"] is not None and row["_lon"] is not None:
                    km = round(haversine_km(center[0], center[1], row["_lat"], row["_lon"]), 1)
                    if radius and km > radius:
                        continue
                    row[DISTANCE_COLUMN] = km
                else:
                    row[DISTANCE_COLUMN] = None
                kept.append(row)
            rows = kept
            if all(row[DISTANCE_COLUMN] is None for row in rows):
                for row in rows:
                    row.pop(DISTANCE_COLUMN)

        for row in rows:
            for internal in ("_lat", "_lon", "_state"):
                row.pop(internal, None)
        return rows

    def save_scrape_date(self) -> None:
        """Saves the date of the last scrape to the scraping history file."""
        history_file_path = os.path.join(self.resources_dir, 'scraping_history.json')
        scraping_entry = {'scrape_date': self.last_scrape_date.isoformat()}

        try:
            with open(history_file_path, 'r+') as file:
                history = json.load(file)
                history.append(scraping_entry)
                file.seek(0)
                json.dump(history, file, indent=4)
        except (FileNotFoundError, json.JSONDecodeError):
            with open(history_file_path, 'w') as file:
                json.dump([scraping_entry], file, indent=4)

    def load_scraping_history(self) -> list[dict[str, Union[str, datetime]]]:
        """Loads the scraping history from the scraping history file."""
        try:
            with open(os.path.join(self.resources_dir, 'scraping_history.json'), 'r') as file:
                return json.load(file)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _load_seen_urls(self) -> dict:
        """Returns {key: {url: price}} — migrates the old [url, ...] list format."""
        try:
            with open(self._seen_urls_file, 'r') as f:
                data = json.load(f)
            migrated = {}
            for k, v in data.items():
                # old format was a plain list of URLs with no prices
                migrated[k] = {url: 0 for url in v} if isinstance(v, list) else v
            return migrated
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_seen_urls(self, current: dict) -> None:
        try:
            with open(self._seen_urls_file, 'w') as f:
                json.dump(current, f)
        except Exception:
            pass

    def update_url_list(self, config: dict) -> None:
        """Updates the URL list with the given configuration."""
        self.url_list = [URLBuilder(**query) for query in config['search_queries']]

    @staticmethod
    def _filter_price_outliers(df: pd.DataFrame) -> pd.DataFrame:
        """
        Uses the Interquartile Range (IQR) to drop extreme outliers (e.g., 35zł pumps 
        or fake 9,999,999zł listings) before they ruin median calculations.
        """
        if df.empty or len(df) < 5 or "Price" not in df.columns: 
            return df
            
        prices = pd.to_numeric(df['Price'], errors='coerce').dropna()
        
        if len(prices) < 5:
            return df

        Q1 = prices.quantile(0.25)
        Q3 = prices.quantile(0.75)
        IQR = Q3 - Q1
        
        lower_bound = Q1 - 1.5 * IQR
        upper_bound = Q3 + 1.5 * IQR
        
        return df[(df['Price'] >= lower_bound) & (df['Price'] <= upper_bound)]