"""
Try a single olx.in search from the command line, without touching history or saved listings.

    python -m src.Scraping.probe "iphone 13" --location Bangalore --distance 10
    python -m src.Scraping.probe "activa" --location bangalore_g4058803 --localities "whitefield, hsr"

Useful to check the location lookup and the parsing after olx.in changes its site.
"""
import argparse
import asyncio

import aiohttp
import pandas as pd

from src.Scraping.Scraper import Scraper
from src.Scraping.URLBuilder import URLBuilder


async def probe(args: argparse.Namespace) -> None:
    url_builder = URLBuilder(args.query, location=args.location, distance=args.distance, localities=args.localities)
    scraper = Scraper([url_builder], page_limit=args.pages)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        await scraper.location_resolver.resolve(session, url_builder)
        print(f"Location : {url_builder.location_name or 'All India'} (id={url_builder.location_id}, "
              f"lat={url_builder.location_lat}, lon={url_builder.location_lon})")
        print(f"Web URL  : {url_builder.build_url()}")

        rows = await scraper._fetch_via_api(session, url_builder)
        source = "API"
        if rows is None:
            rows = await scraper._fetch_via_html(session, url_builder)
            source = "HTML page (API response not recognised)"
        print(f"Source   : {source}, {len(rows)} listings before location filters")
        rows = scraper._apply_location_filters(rows, url_builder)
        print(f"Kept     : {len(rows)} listings after location filters\n")

    with pd.option_context("display.max_colwidth", 60, "display.width", 200):
        print(pd.DataFrame(rows).head(args.show).to_string(index=False) if rows else "(no listings)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("query")
    parser.add_argument("--location", default="")
    parser.add_argument("--distance", default="")
    parser.add_argument("--localities", default="")
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--show", type=int, default=10, help="rows to print")
    asyncio.run(probe(parser.parse_args()))


if __name__ == "__main__":
    main()
