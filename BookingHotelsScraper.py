"""
Booking.com Armenia Hotels Scraper
===================================
Scrapes all hotels in Armenia (ht_id=204 filter) using Playwright (headless Chromium).
Collects hotel metadata + facilities and all room types + prices for 4 date combos
(peak/nonpeak x weekday/weekend).

Uses async Playwright with parallel workers for speed:
  - N_WORKERS browser tabs scrape hotels concurrently
  - Images, fonts, media blocked on hotel pages
  - domcontentloaded used instead of networkidle

Output:
  hotels.csv      — one row per hotel (metadata + facilities)
  room_prices.csv — one row per room-type x date combo
  failed_hotels.json — hotels that errored out

Usage:
  python3 BookingHotelsScraper.py
"""

import asyncio
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import pandas as pd
import re, random, json, logging
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

SEARCH_URL = (
    "https://www.booking.com/searchresults.html"
    "?ss=Armenia&ssne=Armenia&ssne_untouched=Armenia"
    "&dest_id=11&dest_type=country"
    "&nflt=ht_id%3D204"
    "&selected_currency=AMD"
    "&lang=en-us"
    "&checkin={checkin}&checkout={checkout}"
    "&group_adults=2&no_rooms=1&group_children=0"
)

HOTEL_URL = (
    "https://www.booking.com{slug}"
    "?checkin={checkin}&checkout={checkout}"
    "&group_adults=2&no_rooms=1"
    "&selected_currency=AMD"
)

DATES = {
    ("peak", "weekday"):    ("2026-07-13", "2026-07-14"),
    ("peak", "weekend"):    ("2026-07-17", "2026-07-18"),
    ("nonpeak", "weekday"): ("2026-11-09", "2026-11-10"),
    ("nonpeak", "weekend"): ("2026-11-13", "2026-11-14"),
}

N_WORKERS = 4
SAVE_EVERY = 10
MIN_DELAY = 2.0
MAX_DELAY = 3.5

BLOCKED_RESOURCE_TYPES = {"image", "stylesheet", "font", "media"}

OUTPUT_DIR = Path(".")
HOTELS_CSV = OUTPUT_DIR / "hotels.csv"
ROOMS_CSV  = OUTPUT_DIR / "room_prices.csv"
FAILED_JSON = OUTPUT_DIR / "failed_hotels.json"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("scraper.log")],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

async def async_sleep():
    t = random.uniform(MIN_DELAY, MAX_DELAY)
    await asyncio.sleep(t)


def extract_hotel_id(url: str) -> str:
    m = re.search(r"/hotel/am/([^./?]+)", url)
    return m.group(1) if m else url


def extract_price(text: str) -> int | None:
    m = re.search(r"AMD[\s\xa0]*([\d,]+)", text)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


async def get_soup(page) -> BeautifulSoup:
    return BeautifulSoup(await page.content(), "html.parser")


async def block_resources(route):
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        await route.abort()
    else:
        await route.continue_()


# ---------------------------------------------------------------------------
# Search results scraper (sequential — runs once, already fast via GraphQL)
# ---------------------------------------------------------------------------

async def _capture_graphql_template(page, checkin: str, checkout: str) -> dict | None:
    """Load search page, trigger 'Load more', capture FullSearch GraphQL request."""
    url = SEARCH_URL.format(checkin=checkin, checkout=checkout)
    captured = {}

    def on_request(request):
        if "/dml/graphql" in request.url and request.method == "POST":
            try:
                body = json.loads(request.post_data)
                if body.get("operationName") == "FullSearch":
                    captured["body"] = body
                    captured["url"] = request.url
                    captured["headers"] = dict(request.headers)
            except Exception:
                pass

    page.on("request", on_request)
    await page.goto(url, wait_until="networkidle", timeout=60000)
    await asyncio.sleep(3)

    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(3)
    try:
        lm = page.locator('button:has-text("Load more results")')
        if await lm.count() > 0:
            await lm.first.scroll_into_view_if_needed()
            await asyncio.sleep(0.5)
            if await lm.first.is_visible():
                await lm.first.click()
                await asyncio.sleep(5)
    except Exception:
        pass

    page.remove_listener("request", on_request)

    if "body" not in captured:
        log.warning("  Could not capture GraphQL template")
        return None
    return captured


async def _graphql_search_page(page, template: dict, offset: int) -> tuple[list[dict], int]:
    body = json.loads(json.dumps(template["body"]))
    body["variables"]["input"]["pagination"] = {"rowsPerPage": 25, "offset": offset}

    resp = await page.evaluate(
        """async ([url, body, hdrs]) => {
            const r = await fetch(url, {
                method: 'POST',
                headers: {'content-type': 'application/json', ...hdrs},
                body: JSON.stringify(body),
                credentials: 'include',
            });
            return await r.json();
        }""",
        [template["url"], body, {"x-booking-context-action-name": "searchresults"}],
    )

    search = resp.get("data", {}).get("searchQueries", {}).get("search", {})
    total = search.get("pagination", {}).get("nbResultsTotal", 0)
    results = search.get("results", [])

    hotels = []
    for r in results:
        bpd = r.get("basicPropertyData", {})
        page_name = bpd.get("pageName", "")
        country_code = bpd.get("location", {}).get("countryCode", "")
        if country_code != "am":
            continue
        name = r.get("displayName", {}).get("text", "Unknown")
        slug = f"/hotel/am/{page_name}.html"
        hotel_id = page_name

        review_score = bpd.get("externalReviewScore")
        rating = None
        if isinstance(review_score, (int, float)):
            rating = float(review_score)
        elif isinstance(review_score, dict):
            rating = float(review_score.get("score", 0)) or None
        elif isinstance(review_score, str):
            try:
                rating = float(review_score)
            except ValueError:
                pass

        hotels.append({"hotel_id": hotel_id, "hotel_name": name, "slug": slug, "rating": rating})

    return hotels, total


async def _paginate_all(page, template: dict, checkin: str, checkout: str, all_hotels: dict[str, dict]):
    """Paginate through all results for one date combo, reusing a captured template."""
    body_override = {"checkin": checkin, "checkout": checkout}

    # Patch dates into the template for this date combo
    patched = json.loads(json.dumps(template))
    patched["body"]["variables"]["input"]["dates"] = body_override

    hotels, total = await _graphql_search_page(page, patched, 0)
    new = sum(1 for h in hotels if h["hotel_id"] not in all_hotels)
    for h in hotels:
        all_hotels.setdefault(h["hotel_id"], h)
    log.info(f"  offset=0: {len(hotels)} hotels, {new} new (total: {total}, unique: {len(all_hotels)})")

    for offset in range(25, total, 25):
        await asyncio.sleep(random.uniform(0.5, 1.5))
        try:
            hotels, _ = await _graphql_search_page(page, patched, offset)
            new = sum(1 for h in hotels if h["hotel_id"] not in all_hotels)
            for h in hotels:
                all_hotels.setdefault(h["hotel_id"], h)
            log.info(f"  offset={offset}: {len(hotels)} hotels, {new} new (unique: {len(all_hotels)})")
        except Exception as e:
            log.warning(f"  offset={offset} failed: {e}")


async def collect_all_hotels(page) -> list[dict]:
    all_hotels: dict[str, dict] = {}
    date_list = list(DATES.items())

    # Capture template ONCE using the first date combo
    (s0, d0), (cin0, cout0) = date_list[0]
    log.info(f"Capturing GraphQL template ({s0}/{d0})...")
    template = await _capture_graphql_template(page, cin0, cout0)
    if not template:
        log.error("Failed to capture GraphQL template — cannot collect hotels")
        return []

    for (season, day_type), (checkin, checkout) in date_list:
        log.info(f"Searching for {season}/{day_type} ({checkin})...")
        await _paginate_all(page, template, checkin, checkout, all_hotels)

    log.info(f"Total unique hotels across all dates: {len(all_hotels)}")
    return list(all_hotels.values())


# ---------------------------------------------------------------------------
# Hotel page scraper — rooms (pure parsing, no async needed)
# ---------------------------------------------------------------------------

def parse_room_table(soup: BeautifulSoup) -> list[dict]:
    rooms = []
    table = soup.find("table", class_="hprt-table")
    if not table:
        return rooms

    current_room = None
    for row in table.select("tbody tr"):
        room_el = row.select_one(".hprt-roomtype-icon-link")
        if room_el:
            current_room = room_el.get_text(strip=True)

        if current_room is None:
            continue

        price_cell = row.select_one(".hprt-table-cell-price")
        if not price_cell:
            continue
        price_amd = extract_price(price_cell.get_text())
        if price_amd is None:
            continue

        cond_cell = row.select_one(".hprt-table-cell-conditions")
        breakfast = "Not Included"
        if cond_cell:
            cond_text = cond_cell.get_text(separator=" ", strip=True).lower()
            if "breakfast" in cond_text and (
                "included" in cond_text or "breakfastincluded" in cond_text
            ):
                breakfast = "Included"

        occ_cell = row.select_one(".hprt-table-cell-occupancy")
        max_people = None
        if occ_cell:
            occ_m = re.search(r"Max\.?\s*people[:\s]*(\d+)", occ_cell.get_text(), re.I)
            if occ_m:
                max_people = int(occ_m.group(1))

        rooms.append({
            "room_type": current_room,
            "price_amd": price_amd,
            "breakfast": breakfast,
            "max_occupancy": max_people,
        })

    return rooms


# ---------------------------------------------------------------------------
# Hotel page scraper — facilities (pure parsing)
# ---------------------------------------------------------------------------

def _pipe_join(items: list[str]) -> str:
    return " | ".join(i.strip() for i in items if i.strip())


FACILITY_DEFAULTS = {
    "outdoors": "Doesn't have",
    "ski": "Doesn't have",
    "activities": "Doesn't have",
    "food_and_drink": "Doesn't have",
    "internet": "Doesn't have",
    "parking": "Doesn't have",
    "languages_spoken": "Doesn't have",
    "wellness": "Doesn't have",
    "outdoor_swimming_pool": "Doesn't have",
    "spa": "Doesn't have",
}

GROUP_ID_MAP = {
    7:  "food_and_drink",
    11: "internet",
    16: "parking",
    21: "outdoor_swimming_pool",
    2:  "wellness",
}

SLUG_CATEGORY = {
    "ski":       "ski",
    "spa":       "spa",
    "sauna":     "wellness",
    "massage":   "wellness",
    "fitness":   "wellness",
    "hot_tub":   "wellness",
    "swimming":  "outdoor_swimming_pool",
    "terrace":   "outdoors",
    "garden":    "outdoors",
    "bbq":       "outdoors",
    "picnic":    "outdoors",
    "outdoor":   "outdoors",
    "balcony":   "outdoors",
    "patio":     "outdoors",
    "parking":   "parking",
    "restaurant":"food_and_drink",
    "bar":       "food_and_drink",
    "minibar":   "food_and_drink",
}

LANGUAGE_CODE_MAP = {
    "en": "English", "de": "German", "fr": "French", "ru": "Russian",
    "es": "Spanish", "it": "Italian", "hy": "Armenian", "ar": "Arabic",
    "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "pt": "Portuguese",
    "nl": "Dutch", "tr": "Turkish", "pl": "Polish", "el": "Greek",
    "ka": "Georgian", "fa": "Persian", "he": "Hebrew", "hi": "Hindi",
    "uk": "Ukrainian", "cs": "Czech", "sv": "Swedish", "ro": "Romanian",
}


def _extract_apollo_cache(soup: BeautifulSoup) -> dict | None:
    for script in soup.find_all("script", type="application/json"):
        text = script.string or ""
        if "BaseFacility" in text and "ROOT_QUERY" in text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
    return None


def _resolve_ref(cache: dict, ref: dict | str) -> dict:
    if isinstance(ref, dict) and "__ref" in ref:
        return cache.get(ref["__ref"], {})
    return {}


def parse_facilities(soup: BeautifulSoup) -> dict:
    result = dict(FACILITY_DEFAULTS)
    cache = _extract_apollo_cache(soup)
    if not cache:
        log.warning("  Apollo cache not found, facilities will be empty")
        return result

    grouped: dict[str, list[str]] = {}
    for key, val in cache.items():
        if not isinstance(val, dict) or val.get("__typename") != "BaseFacility":
            continue
        slug = val.get("slug", "")
        group_id = val.get("groupId")

        col = GROUP_ID_MAP.get(group_id)
        if not col:
            for pattern, cat in SLUG_CATEGORY.items():
                if pattern in slug:
                    col = cat
                    break
        if not col:
            continue

        for inst_ref in val.get("instances", []):
            inst = _resolve_ref(cache, inst_ref)
            title = inst.get("title")
            if title:
                grouped.setdefault(col, []).append(title)

    for col, items in grouped.items():
        if items:
            result[col] = _pipe_join(items)

    for key, val in cache.items():
        if isinstance(val, dict) and val.get("__typename") == "WifiFacilityHighlight":
            parts = [val.get("title", "WiFi")]
            if val.get("isFree"):
                parts[0] = "Free WiFi"
            for attr in val.get("subtitleAttributes", []) or []:
                if isinstance(attr, dict) and attr.get("title"):
                    parts.append(attr["title"])
            result["internet"] = " | ".join(parts)
            break

    for key, val in cache.items():
        if isinstance(val, dict) and val.get("__typename") == "ParkingFacilityHighlight":
            result["parking"] = val.get("title", "Parking")
            break

    for key, val in cache.items():
        if isinstance(val, dict) and val.get("__typename") == "SwimmingPoolFacilityHighlight":
            result["outdoor_swimming_pool"] = val.get("title", "Swimming pool")
            break

    raw_html = str(soup)
    lang_match = re.search(r'"languageCodes":\[([^\]]*)\]', raw_html)
    if lang_match:
        codes = re.findall(r'"(\w+)"', lang_match.group(1))
        names = [LANGUAGE_CODE_MAP.get(c, c) for c in codes]
        if names:
            result["languages_spoken"] = " | ".join(names)

    return result


# ---------------------------------------------------------------------------
# Hotel page scraper — metadata (pure parsing)
# ---------------------------------------------------------------------------

def parse_hotel_metadata(soup: BeautifulSoup, hotel_id: str, hotel_name_fallback: str, rating_fallback) -> dict:
    cache = _extract_apollo_cache(soup)

    name = hotel_name_fallback
    if cache:
        for key, val in cache.items():
            if isinstance(val, dict) and val.get("__typename") == "Property" and val.get("name"):
                name = val["name"]
                break
    if name == hotel_name_fallback:
        for sel in ["h2.pp-header__title", "h1.pp-header__title", "h2[data-testid='title']"]:
            el = soup.select_one(sel)
            if el:
                name = el.get_text(strip=True)
                break

    rating = rating_fallback
    rating_el = soup.select_one('[data-testid="review-score-right-component"]')
    if rating_el:
        m = re.search(r"(\d+\.\d+|\d+)", rating_el.get_text())
        if m:
            rating = float(m.group(1))

    location = "Unknown"

    if cache:
        for key, val in cache.items():
            if not isinstance(val, dict):
                continue
            if "breadcrumbs" not in key and "breadcrumbItems" not in str(val):
                continue
            items = val.get("breadcrumbItems", [])
            if not items and isinstance(val, dict):
                for sub_key, sub_val in val.items():
                    if isinstance(sub_val, dict) and "breadcrumbItems" in sub_val:
                        items = sub_val["breadcrumbItems"]
                        break
            city_items = [
                i for i in items
                if isinstance(i, dict) and i.get("type") == "city"
            ]
            if city_items:
                location = city_items[0].get("name", "Unknown")
                break

    if location == "Unknown":
        for sel in [
            "span.hp_address_subtitle",
            '[data-testid="address"]',
            "p.address",
        ]:
            el = soup.select_one(sel)
            if el:
                location = el.get_text(strip=True)
                break

    if location == "Unknown":
        full_text = soup.get_text()
        m = re.search(r"([A-Z][a-zA-Zʼ\s]+),\s*Armenia", full_text)
        if m:
            location = m.group(1).strip()

    return {"hotel_id": hotel_id, "hotel_name": name, "rating": rating, "location": location}


# ---------------------------------------------------------------------------
# Scrape a single hotel page (one date combo)
# ---------------------------------------------------------------------------

async def scrape_hotel_page(page, slug: str, checkin: str, checkout: str) -> tuple[BeautifulSoup, list[dict]]:
    url = HOTEL_URL.format(slug=slug, checkin=checkin, checkout=checkout)
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_selector("table.hprt-table", timeout=10000)
    except Exception:
        await asyncio.sleep(2)
    soup = await get_soup(page)
    rooms = parse_room_table(soup)
    return soup, rooms


# ---------------------------------------------------------------------------
# Scrape one hotel across all date combos
# ---------------------------------------------------------------------------

async def scrape_one_hotel(page, hotel: dict, date_combos: list) -> tuple[dict | None, list[dict], dict | None]:
    """Returns (hotel_row, room_rows, failure_or_None)."""
    hotel_id = hotel["hotel_id"]
    slug = hotel["slug"]
    room_rows = []

    try:
        (season0, day0), (cin0, cout0) = date_combos[0]
        soup, rooms0 = await scrape_hotel_page(page, slug, cin0, cout0)

        metadata = parse_hotel_metadata(soup, hotel_id, hotel["hotel_name"], hotel["rating"])
        facilities = parse_facilities(soup)
        hotel_row = {**metadata, **facilities}

        for rm in rooms0:
            room_rows.append({
                "hotel_id":      hotel_id,
                "room_type":     rm["room_type"],
                "price_amd":     rm["price_amd"],
                "breakfast":     rm["breakfast"],
                "max_occupancy": rm.get("max_occupancy"),
                "checkin_date":  cin0,
                "checkout_date": cout0,
                "season":        season0,
                "day_type":      day0,
            })

        log.info(f"    {hotel_id}: date1 -> {len(rooms0)} rooms")

        for (season, day_type), (checkin, checkout) in date_combos[1:]:
            await async_sleep()
            try:
                _, rooms = await scrape_hotel_page(page, slug, checkin, checkout)
                for rm in rooms:
                    room_rows.append({
                        "hotel_id":      hotel_id,
                        "room_type":     rm["room_type"],
                        "price_amd":     rm["price_amd"],
                        "breakfast":     rm["breakfast"],
                        "max_occupancy": rm.get("max_occupancy"),
                        "checkin_date":  checkin,
                        "checkout_date": checkout,
                        "season":        season,
                        "day_type":      day_type,
                    })
                log.info(f"    {hotel_id}: {checkin} -> {len(rooms)} rooms")
            except Exception as e:
                log.warning(f"    {hotel_id}: {checkin} failed: {e}")

        return hotel_row, room_rows, None

    except Exception as e:
        log.error(f"    {hotel_id}: FAILED: {e}")
        return None, [], {"hotel_id": hotel_id, "slug": slug, "error": str(e)}


# ---------------------------------------------------------------------------
# Save helpers
# ---------------------------------------------------------------------------

def save_csv(hotels_rows: list[dict], room_rows: list[dict]):
    pd.DataFrame(hotels_rows).to_csv(HOTELS_CSV, index=False)
    pd.DataFrame(room_rows).to_csv(ROOMS_CSV, index=False)
    log.info(f"Saved {len(hotels_rows)} hotels, {len(room_rows)} room rows.")


def save_failed(failed: list[dict]):
    with open(FAILED_JSON, "w") as f:
        json.dump(failed, f, indent=2)


# ---------------------------------------------------------------------------
# Worker — each runs in its own browser tab
# ---------------------------------------------------------------------------

async def worker(worker_id: int, queue: asyncio.Queue, context, results: dict,
                 lock: asyncio.Lock, total: int, counter: list):
    page = await context.new_page()
    await page.route("**/*", block_resources)

    date_combos = list(DATES.items())

    while True:
        try:
            hotel = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        async with lock:
            counter[0] += 1
            idx = counter[0]

        log.info(f"[W{worker_id}] [{idx}/{total}] {hotel['hotel_id']}")

        hotel_row, room_rows, failure = await scrape_one_hotel(page, hotel, date_combos)

        async with lock:
            if hotel_row:
                results["hotels"].append(hotel_row)
            results["rooms"].extend(room_rows)
            if failure:
                results["failed"].append(failure)

            done = len(results["hotels"]) + len(results["failed"])
            if done > 0 and done % SAVE_EVERY == 0:
                save_csv(results["hotels"], results["rooms"])
                save_failed(results["failed"])

        await async_sleep()

    await page.close()
    log.info(f"[W{worker_id}] finished")


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def main():
    log.info("=== Booking.com Armenia Hotels Scraper ===")
    log.info(f"    Workers: {N_WORKERS}, Delays: {MIN_DELAY}-{MAX_DELAY}s")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT, locale="en-US")

        # --- Step 1: Collect all hotel slugs (sequential, one page) ---
        log.info("--- Step 1: Collecting hotel list ---")
        search_page = await context.new_page()
        hotels = await collect_all_hotels(search_page)
        await search_page.close()
        log.info(f"Total unique hotels found: {len(hotels)}")

        # --- Step 2: Scrape hotel pages (parallel workers) ---
        log.info(f"--- Step 2: Scraping hotel pages ({N_WORKERS} workers) ---")

        queue: asyncio.Queue = asyncio.Queue()
        for h in hotels:
            queue.put_nowait(h)

        results = {"hotels": [], "rooms": [], "failed": []}
        lock = asyncio.Lock()
        counter = [0]

        tasks = [
            asyncio.create_task(worker(i, queue, context, results, lock, len(hotels), counter))
            for i in range(N_WORKERS)
        ]
        await asyncio.gather(*tasks)

        await browser.close()

    save_csv(results["hotels"], results["rooms"])
    save_failed(results["failed"])
    log.info(
        f"Done. {len(results['hotels'])} hotels, "
        f"{len(results['rooms'])} room rows, "
        f"{len(results['failed'])} failures."
    )


if __name__ == "__main__":
    asyncio.run(main())
