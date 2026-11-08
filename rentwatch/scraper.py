"""Client for immobiliare.it search.

The site is a Next.js app; the listings are embedded in __NEXT_DATA__ on the
search pages. Direct requests to the internal /api-next/ JSON endpoint get
blocked by DataDome (403 captcha), so we fetch the HTML pages with Chrome
impersonation via curl_cffi and extract the dehydrated query state.
"""

import json
import logging
import random
import time
from collections.abc import Iterator

from curl_cffi import requests

log = logging.getLogger(__name__)

BASE_URL = "https://www.immobiliare.it"
DEFAULT_PATH = "/affitto-case/torino/"


class ScrapeError(RuntimeError):
    pass


def _parse_surface(value: str | None) -> float | None:
    # "84 m²" -> 84.0
    if not value:
        return None
    try:
        return float(value.split()[0].replace(".", "").replace(",", "."))
    except ValueError:
        return None


def parse_listing(item: dict) -> dict | None:
    re_ = item.get("realEstate") or {}
    props = re_.get("properties") or [{}]
    p = props[0]
    if not re_.get("id"):
        return None
    loc = p.get("location") or {}
    price = (re_.get("price") or {}).get("value")
    photo = ((p.get("photo") or {}).get("urls") or {}).get("small")
    return {
        "id": re_["id"],
        "source": "immobiliare",
        "url": (item.get("seo") or {}).get("url") or f"https://www.immobiliare.it/annunci/{re_['id']}/",
        "title": re_.get("title"),
        "typology": (re_.get("typology") or {}).get("name"),
        "price": price,
        "surface_m2": _parse_surface(p.get("surface")),
        "rooms": p.get("rooms"),
        "bathrooms": p.get("bathrooms"),
        "floor": (p.get("floor") or {}).get("abbreviation"),
        "elevator": 1 if p.get("elevator") else 0,
        "address": loc.get("address"),
        "macrozone": loc.get("macrozone"),
        "microzone": loc.get("microzone"),
        "latitude": loc.get("latitude"),
        "longitude": loc.get("longitude"),
        "agency": ((re_.get("advertiser") or {}).get("agency") or {}).get("displayName"),
        "photo_url": photo,
        "description": (p.get("description") or "")[:2000],
        "is_new_construction": 1 if re_.get("isNew") else 0,
        "luxury": 1 if re_.get("luxury") else 0,
    }


def extract_next_data(html: str) -> dict:
    marker = '<script id="__NEXT_DATA__"'
    start = html.find(marker)
    if start == -1:
        raise ScrapeError('could not find <script id="__NEXT_DATA__"> in HTML')
    content_start = html.find(">", start) + 1
    end = html.find("</script>", content_start)
    if end == -1:
        raise ScrapeError("could not find closing </script> for __NEXT_DATA__")
    try:
        payload = json.loads(html[content_start:end])
    except Exception as exc:
        raise ScrapeError(f"failed to parse __NEXT_DATA__ JSON: {exc}")

    queries = (payload.get("props") or {}).get("pageProps", {}).get("dehydratedState", {}).get("queries", [])
    for q in queries:
        key = q.get("queryKey")
        if isinstance(key, list) and key and key[0] == "real-estate-list":
            qd = (q.get("state") or {}).get("data")
            if isinstance(qd, dict):
                return qd
    for q in queries:
        qd = (q.get("state") or {}).get("data")
        if isinstance(qd, dict) and "results" in qd and "count" in qd:
            return qd
    raise ScrapeError("could not find real-estate-list in dehydratedState")


# The API hard-caps every search at 80 pages (2000 results) and answers 404/418
# beyond it — maxPages lies. Searches with more results get partitioned into
# price bands that each fit under the cap.
HARD_PAGE_CAP = 80
RESULT_CAP = HARD_PAGE_CAP * 25
PRICE_LADDER = [400, 500, 600, 700, 800, 900, 1000, 1100, 1200, 1400,
                1600, 2000, 2500, 3500, 5000]

PAGES_PER_SESSION = 40          # rotate the session periodically to stay polite
RETRY_BACKOFF = (30, 90)        # seconds, one per retry attempt
BROWSERS = ("chrome", "safari", "safari17_0")

REQUEST_HEADERS = {
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
}


class ImmobiliareScraper:
    def __init__(self, delay: tuple[float, float] = (1.5, 3.0)):
        self.delay = delay
        self._pages_fetched = 0
        self._browser_idx = 0
        self._new_session()

    def _new_session(self, browser: str | None = None):
        if browser is None:
            browser = BROWSERS[self._browser_idx % len(BROWSERS)]
        self.current_browser = browser
        self.session = requests.Session(impersonate=browser)

    def fetch_page(self, params: dict, page: int) -> dict:
        query = dict(params)
        path = query.pop("path", DEFAULT_PATH)
        if not path.startswith("/"):
            path = "/" + path
        query.pop("paramsCount", None)
        query["pag"] = str(page)

        url = f"{BASE_URL}{path}"
        last_err = None
        for attempt, backoff in enumerate((0, *RETRY_BACKOFF)):
            if backoff:
                self._browser_idx += 1
                next_browser = BROWSERS[self._browser_idx % len(BROWSERS)]
                log.warning("%s — backing off %ds and switching to %s (retry %d)",
                            last_err, backoff, next_browser, attempt)
                time.sleep(backoff)
                self._new_session(next_browser)

            r = self.session.get(
                url,
                params=query,
                headers=REQUEST_HEADERS,
                timeout=30,
            )
            if r.status_code == 200:
                self._pages_fetched += 1
                if self._pages_fetched % PAGES_PER_SESSION == 0:
                    pause = random.uniform(20, 40)
                    log.info("fetched %d pages — pausing %.0fs and rotating session",
                             self._pages_fetched, pause)
                    time.sleep(pause)
                    self._new_session()
                try:
                    return extract_next_data(r.text)
                except ScrapeError as exc:
                    last_err = f"Failed to parse page {page}: {exc}"
                    continue
            last_err = f"HTTP {r.status_code} on page {page}: {r.text[:120]}"
            if r.status_code not in (403, 418, 429) and r.status_code < 500:
                break  # 4xx we can't fix by waiting
        raise ScrapeError(last_err)

    def iter_listings(self, search: dict, max_pages: int | None = None) -> Iterator[dict]:
        """Yield parsed listings for one configured search, across all pages."""
        params = {str(k): str(v) for k, v in (search.get("params") or {}).items()}
        # Default to sorting by date so newest listings appear on first pages
        params.setdefault("criterio", "data")
        if "path" in search:
            params["path"] = search["path"]
        first = self.fetch_page(params, 1)
        count = first.get("count") or 0
        log.info("search %r: %s listings", search.get("name"), count)
        if count <= RESULT_CAP or max_pages:
            yield from self._iter_pages(params, first, max_pages)
            return

        log.info("more than %d results — partitioning by price band", RESULT_CAP)
        for lo, hi in self._price_bands(params):
            band = dict(params)
            if lo is not None:
                band["prezzoMinimo"] = str(lo)
            if hi is not None:
                band["prezzoMassimo"] = str(hi)
            bfirst = self.fetch_page(band, 1)
            log.info("price band %s–%s: %s listings", lo or 0, hi or "∞", bfirst.get("count"))
            yield from self._iter_pages(band, bfirst, None)
            time.sleep(random.uniform(*self.delay))

    def _iter_pages(self, params: dict, first: dict, max_pages: int | None) -> Iterator[dict]:
        total = min(first.get("maxPages") or 1, HARD_PAGE_CAP)
        if max_pages:
            total = min(total, max_pages)
        page, data = 1, first
        while True:
            for item in data.get("results", []):
                parsed = parse_listing(item)
                if parsed:
                    yield parsed
            page += 1
            if page > total:
                return
            time.sleep(random.uniform(*self.delay))
            data = self.fetch_page(params, page)

    def _band_count(self, params: dict, lo: int | None, hi: int | None) -> int:
        probe = dict(params)
        if lo is not None:
            probe["prezzoMinimo"] = str(lo)
        if hi is not None:
            probe["prezzoMassimo"] = str(hi)
        time.sleep(random.uniform(*self.delay))
        return self.fetch_page(probe, 1).get("count") or 0

    def _price_bands(self, params: dict) -> list[tuple[int | None, int | None]]:
        """Split the search into price ranges that each fit under RESULT_CAP.

        Walks PRICE_LADDER upward probing result counts: each band's upper cut
        is the largest ladder step still under the cap. Counts only grow with
        the cut, so the ladder index never needs to move backwards.
        """
        bands: list[tuple[int | None, int | None]] = []
        lo: int | None = None
        i = 0
        while i < len(PRICE_LADDER):
            fit = None
            while i < len(PRICE_LADDER):
                cut = PRICE_LADDER[i]
                if self._band_count(params, lo, cut) <= RESULT_CAP:
                    fit = cut
                    i += 1
                else:
                    break
            if fit is None:
                # even the smallest remaining step is over the cap; take it and
                # accept losing whatever sits past page 80 in this band
                fit = PRICE_LADDER[i]
                log.warning("price band up to %s exceeds the result cap — "
                            "some listings may be missed", fit)
                i += 1
            bands.append((lo, fit))
            lo = fit + 1
        bands.append((lo, None))
        return bands
