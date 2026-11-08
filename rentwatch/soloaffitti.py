"""Client for soloaffitti.it listings.

Fetches listing search pages for Torino (or other configured regions/cities)
and extracts listing IDs and slugs. Detailed listing data (pricing, rooms,
coordinates, full description, photos) is retrieved directly from the
official detail JSON endpoint (/api/soloaffitti/properties/detail).
"""

import logging
import random
import re
import time
from collections.abc import Iterator

from curl_cffi import requests

log = logging.getLogger(__name__)

BASE_URL = "https://www.soloaffitti.it"
DEFAULT_CITY = "272"  # Torino
SOLOAFFITTI_ID_OFFSET = 900_000_000

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
}


class SoloAffittiScrapeError(RuntimeError):
    pass


def _parse_price(value: str | None) -> int | None:
    if not value:
        return None
    try:
        # "650,00" or "1.200,00" -> 650 or 1200
        clean = str(value).split(",")[0].replace(".", "").strip()
        return int(clean) if clean.isdigit() else None
    except (ValueError, TypeError):
        return None


def _parse_surface(value: str | int | float | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def parse_listing(data: dict, slug: str = "") -> dict | None:
    pid = data.get("id")
    if not pid:
        return None
    try:
        raw_id = int(pid)
    except (ValueError, TypeError):
        return None

    # Namespace IDs to guarantee no collisions with Immobiliare.it primary keys
    listing_id = SOLOAFFITTI_ID_OFFSET + raw_id

    # Coordinates
    lat = float(data["latitude"]) if data.get("latitude") else None
    lng = float(data["longitude"]) if data.get("longitude") else None

    # Location & zone
    city = data.get("city") or "Torino"
    district = data.get("district")
    macrozone = district or city
    microzone = district

    # Agency label
    comp_code = data.get("company_code")
    agency = f"SoloAffitti {comp_code}" if comp_code else "SoloAffitti"

    # Photos
    images = data.get("images") or []
    photo = data.get("main_image_url") or (images[0] if images else None)

    # Clean URL
    url = (
        f"{BASE_URL}/annuncio/{raw_id}/{slug}".rstrip("/")
        if slug
        else f"{BASE_URL}/annuncio/{raw_id}"
    )

    # Year & new construction
    year_val = data.get("building_year")
    try:
        year = int(year_val) if year_val else None
    except (ValueError, TypeError):
        year = None
    is_new = 1 if year and year >= 2022 else 0

    return {
        "id": listing_id,
        "source": "soloaffitti",
        "url": url,
        "title": data.get("full_name") or data.get("name") or f"Immobile {raw_id}",
        "typology": data.get("subtype") or data.get("type"),
        "price": _parse_price(data.get("price")),
        "surface_m2": _parse_surface(data.get("size")),
        "rooms": str(data.get("rooms")) if data.get("rooms") else None,
        "bathrooms": str(data.get("services")) if data.get("services") else None,
        "floor": str(data.get("floor")) if data.get("floor") else None,
        "elevator": 1 if data.get("lift") else 0,
        "address": data.get("address"),
        "macrozone": macrozone,
        "microzone": microzone,
        "latitude": lat,
        "longitude": lng,
        "agency": agency,
        "photo_url": photo,
        "description": (data.get("description") or "")[:2000],
        "is_new_construction": is_new,
        "luxury": 0,
    }


class SoloAffittiScraper:
    def __init__(self, delay: tuple[float, float] = (1.0, 2.0)):
        self.delay = delay
        self.session = requests.Session(impersonate="chrome")

    def fetch_search_page(
        self,
        config: dict,
        page: int = 1,
    ) -> tuple[list[tuple[str, str]], int]:
        """Fetch a search page and return ((listing_id, slug) list, max_pages)."""
        region = config.get("region_slug", "piemonte")
        province = config.get("province_slug", "torino")
        city_slug = config.get("city_slug", "torino")

        url = f"{BASE_URL}/immobili/{region}/{province}/{city_slug}"
        params = {
            "city": config.get("city", DEFAULT_CITY),
            "city_name": config.get("city_name", "Torino"),
            "city_registry_code": config.get("city_registry_code", "L219"),
            "selected_page": str(page),
        }
        district_ids = config.get("district_ids")
        if district_ids:
            params["district_ids"] = str(district_ids)

        try:
            r = self.session.get(
                url,
                params=params,
                headers=REQUEST_HEADERS,
                timeout=30,
            )
        except Exception as exc:
            raise SoloAffittiScrapeError(f"HTTP GET error on search page {page}: {exc}")

        if r.status_code != 200:
            raise SoloAffittiScrapeError(
                f"HTTP {r.status_code} on SoloAffitti search page {page}"
            )

        html = r.text
        # Extract /annuncio/<id>/<slug>
        matches = re.findall(r"/annuncio/(\d+)/([a-zA-Z0-9_\-]+)", html)
        seen: set[str] = set()
        items: list[tuple[str, str]] = []
        for pid, slug in matches:
            if pid not in seen:
                seen.add(pid)
                items.append((pid, slug))

        # Determine max_pages from pagination
        pages = re.findall(r"selected_page=(\d+)", html)
        max_page = max(map(int, pages)) if pages else 1

        return items, max_page

    def fetch_listing_detail(self, property_id: str | int) -> dict | None:
        """Fetch structured JSON for a single property from SoloAffitti API."""
        url = f"{BASE_URL}/api/soloaffitti/properties/detail"
        headers = {
            **REQUEST_HEADERS,
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            r = self.session.post(
                url,
                data={"property_id": str(property_id)},
                headers=headers,
                timeout=30,
            )
            if r.status_code == 200:
                return r.json()
            log.warning("SoloAffitti property %s returned HTTP %s", property_id, r.status_code)
            return None
        except Exception as exc:
            log.warning("SoloAffitti detail error for %s: %exc", property_id, exc)
            return None

    def iter_listings(
        self,
        config: dict,
        max_pages: int | None = None,
    ) -> Iterator[dict]:
        """Yield parsed listings for SoloAffitti across search pages."""
        page = 1
        total_pages = 1
        seen_ids: set[str] = set()

        while page <= total_pages:
            items, detected_max = self.fetch_search_page(config, page)
            if page == 1:
                total_pages = detected_max
                if max_pages:
                    total_pages = min(total_pages, max_pages)
                log.info(
                    "SoloAffitti search %s: %d pages detected",
                    config.get("city_name", "Torino"),
                    detected_max,
                )

            for pid, slug in items:
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)
                detail = self.fetch_listing_detail(pid)
                if detail:
                    parsed = parse_listing(detail, slug=slug)
                    if parsed:
                        yield parsed
                time.sleep(random.uniform(0.15, 0.35))

            page += 1
            if page <= total_pages:
                time.sleep(random.uniform(*self.delay))
