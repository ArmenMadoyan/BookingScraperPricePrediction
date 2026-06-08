"""
Booking.com Armenia Hotels Scraper
===================================
Scrapes all hotels in Armenia (ht_id=204 filter) using Playwright (headless Chromium).
Collects hotel metadata + facilities and all room types + prices for 4 date combos
(peak/nonpeak x weekday/weekend).

Output:
  hotels.csv      — one row per hotel (metadata + facilities)
  room_prices.csv — one row per room-type x date combo
  failed_hotels.json — hotels that errored out

Usage:
  python3 BookingHotelsScraper.py
"""

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import pandas as pd
import re, time, random, json, logging
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# dest_id=11 is Armenia (country), ht_id=204 = Hotels only
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

SAVE_EVERY = 10          # save CSV every N hotels
MIN_DELAY = 5.0          # seconds between requests
MAX_DELAY = 10.0

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

def sleep():
    t = random.uniform(MIN_DELAY, MAX_DELAY)
    log.debug(f"Sleeping {t:.1f}s")
    time.sleep(t)


def extract_hotel_id(url: str) -> str:
    """Extract slug from /hotel/am/<slug>.html"""
    m = re.search(r"/hotel/am/([^./?]+)", url)
    return m.group(1) if m else url


def extract_price(text: str) -> int | None:
    """Parse first AMD price from text, e.g. 'AMD 29,720' -> 29720"""
    m = re.search(r"AMD[\s\xa0]*([\d,]+)", text)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def get_soup(page) -> BeautifulSoup:
    return BeautifulSoup(page.content(), "html.parser")


# ---------------------------------------------------------------------------
# Search results scraper
# ---------------------------------------------------------------------------

def _capture_graphql_template(page, checkin: str, checkout: str) -> dict | None:
    """Load the search page, trigger one 'Load more', and capture the FullSearch
    GraphQL request body as a reusable template."""
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
    page.goto(url, wait_until="networkidle", timeout=60000)
    time.sleep(3)

    # Scroll and click Load More to trigger the GraphQL call
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(3)
    try:
        lm = page.locator('button:has-text("Load more results")')
        if lm.count() > 0:
            lm.first.scroll_into_view_if_needed()
            time.sleep(0.5)
            if lm.first.is_visible():
                lm.first.click()
                time.sleep(5)
    except Exception:
        pass

    page.remove_listener("request", on_request)

    if "body" not in captured:
        log.warning("  Could not capture GraphQL template")
        return None
    return captured


def _graphql_search_page(page, template: dict, offset: int) -> tuple[list[dict], int]:
    """Call the FullSearch GraphQL endpoint with a specific offset.
    Returns (list_of_hotel_dicts, total_results)."""
    body = json.loads(json.dumps(template["body"]))
    body["variables"]["input"]["pagination"] = {"rowsPerPage": 25, "offset": offset}

    resp = page.evaluate(
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


def _collect_for_dates(page, checkin: str, checkout: str, all_hotels: dict[str, dict]):
    """Collect all hotels for one date combo using GraphQL pagination."""
    log.info(f"  Capturing GraphQL template...")
    template = _capture_graphql_template(page, checkin, checkout)
    if not template:
        log.error("  Failed to capture GraphQL template, falling back to HTML scraping")
        return

    # First call to get total count
    hotels, total = _graphql_search_page(page, template, 0)
    new = sum(1 for h in hotels if h["hotel_id"] not in all_hotels)
    for h in hotels:
        all_hotels.setdefault(h["hotel_id"], h)
    log.info(f"  offset=0: {len(hotels)} hotels, {new} new (total results: {total}, unique so far: {len(all_hotels)})")

    # Paginate through remaining results
    for offset in range(25, total, 25):
        time.sleep(random.uniform(1.0, 2.5))
        try:
            hotels, _ = _graphql_search_page(page, template, offset)
            new = sum(1 for h in hotels if h["hotel_id"] not in all_hotels)
            for h in hotels:
                all_hotels.setdefault(h["hotel_id"], h)
            log.info(f"  offset={offset}: {len(hotels)} hotels, {new} new (unique: {len(all_hotels)})")
        except Exception as e:
            log.warning(f"  offset={offset} failed: {e}")


def collect_all_hotels(page) -> list[dict]:
    """Search across all date combos to capture hotels that may only appear in certain seasons."""
    all_hotels: dict[str, dict] = {}

    for (season, day_type), (checkin, checkout) in DATES.items():
        log.info(f"Searching for {season}/{day_type} ({checkin})...")
        _collect_for_dates(page, checkin, checkout, all_hotels)
        sleep()

    log.info(f"Total unique hotels across all dates: {len(all_hotels)}")
    return list(all_hotels.values())


# ---------------------------------------------------------------------------
# Hotel page scraper — rooms
# ---------------------------------------------------------------------------

def parse_room_table(soup: BeautifulSoup) -> list[dict]:
    """Parse hprt-table and return list of room dicts."""
    rooms = []
    table = soup.find("table", class_="hprt-table")
    if not table:
        return rooms

    current_room = None
    for row in table.select("tbody tr"):
        # Check if this row introduces a new room type
        room_el = row.select_one(".hprt-roomtype-icon-link")
        if room_el:
            current_room = room_el.get_text(strip=True)

        if current_room is None:
            continue

        # Price cell
        price_cell = row.select_one(".hprt-table-cell-price")
        if not price_cell:
            continue
        price_amd = extract_price(price_cell.get_text())
        if price_amd is None:
            continue

        # Breakfast
        cond_cell = row.select_one(".hprt-table-cell-conditions")
        breakfast = "Not Included"
        if cond_cell:
            cond_text = cond_cell.get_text(separator=" ", strip=True).lower()
            if "breakfast" in cond_text and (
                "included" in cond_text or "breakfastincluded" in cond_text
            ):
                breakfast = "Included"

        # Occupancy
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
# Hotel page scraper — facilities (via Apollo cache JSON)
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

# Booking.com facility groupId -> our CSV column name.
# Derived from inspecting the Apollo cache JSON structure.
GROUP_ID_MAP = {
    7:  "food_and_drink",    # Restaurant, Bar, Coffee house, Fruit, Wine...
    11: "internet",          # Internet / WiFi
    16: "parking",           # Parking
    21: "outdoor_swimming_pool",  # Swimming pool
    2:  "wellness",          # Hot tub, Spa
}

# Facility slugs we can classify by name pattern when groupId doesn't match
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
    """Find and parse the Apollo cache JSON embedded in a <script type='application/json'> tag."""
    for script in soup.find_all("script", type="application/json"):
        text = script.string or ""
        if "BaseFacility" in text and "ROOT_QUERY" in text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
    return None


def _resolve_ref(cache: dict, ref: dict | str) -> dict:
    """Resolve an Apollo __ref pointer."""
    if isinstance(ref, dict) and "__ref" in ref:
        return cache.get(ref["__ref"], {})
    return {}


def parse_facilities(soup: BeautifulSoup) -> dict:
    """Extract facility data from the Apollo cache JSON embedded in the page."""
    result = dict(FACILITY_DEFAULTS)
    cache = _extract_apollo_cache(soup)
    if not cache:
        log.warning("  Apollo cache not found, facilities will be empty")
        return result

    # --- Collect all BaseFacility items by groupId and slug ---
    grouped: dict[str, list[str]] = {}  # column_name -> [item_titles]
    for key, val in cache.items():
        if not isinstance(val, dict) or val.get("__typename") != "BaseFacility":
            continue
        slug = val.get("slug", "")
        group_id = val.get("groupId")

        # Determine which column this belongs to
        col = GROUP_ID_MAP.get(group_id)
        if not col:
            for pattern, cat in SLUG_CATEGORY.items():
                if pattern in slug:
                    col = cat
                    break
        if not col:
            continue

        # Resolve instance titles
        for inst_ref in val.get("instances", []):
            inst = _resolve_ref(cache, inst_ref)
            title = inst.get("title")
            if title:
                grouped.setdefault(col, []).append(title)

    for col, items in grouped.items():
        if items:
            result[col] = _pipe_join(items)

    # --- WiFi details from WifiFacilityHighlight ---
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

    # --- Parking details from ParkingFacilityHighlight ---
    for key, val in cache.items():
        if isinstance(val, dict) and val.get("__typename") == "ParkingFacilityHighlight":
            result["parking"] = val.get("title", "Parking")
            break

    # --- Swimming pool from SwimmingPoolFacilityHighlight ---
    for key, val in cache.items():
        if isinstance(val, dict) and val.get("__typename") == "SwimmingPoolFacilityHighlight":
            result["outdoor_swimming_pool"] = val.get("title", "Swimming pool")
            break

    # --- Languages from languageCodes in raw HTML ---
    raw_html = str(soup)
    lang_match = re.search(r'"languageCodes":\[([^\]]*)\]', raw_html)
    if lang_match:
        codes = re.findall(r'"(\w+)"', lang_match.group(1))
        names = [LANGUAGE_CODE_MAP.get(c, c) for c in codes]
        if names:
            result["languages_spoken"] = " | ".join(names)

    return result


# ---------------------------------------------------------------------------
# Hotel page scraper — metadata
# ---------------------------------------------------------------------------

def parse_hotel_metadata(soup: BeautifulSoup, hotel_id: str, hotel_name_fallback: str, rating_fallback) -> dict:
    """Extract hotel name, location, rating from the hotel page."""
    cache = _extract_apollo_cache(soup)

    # --- Hotel name ---
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

    # --- Rating ---
    rating = rating_fallback
    rating_el = soup.select_one('[data-testid="review-score-right-component"]')
    if rating_el:
        m = re.search(r"(\d+\.\d+|\d+)", rating_el.get_text())
        if m:
            rating = float(m.group(1))

    # --- Location ---
    location = "Unknown"

    # Strategy 1: Apollo breadcrumbs (most reliable)
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
            # City is typically the second-to-last breadcrumb (before the hotel itself)
            city_items = [
                i for i in items
                if isinstance(i, dict) and i.get("type") == "city"
            ]
            if city_items:
                location = city_items[0].get("name", "Unknown")
                break

    # Strategy 2: HTML selectors
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

    # Strategy 3: regex from page text
    if location == "Unknown":
        full_text = soup.get_text()
        m = re.search(r"([A-Z][a-zA-Zʼ\s]+),\s*Armenia", full_text)
        if m:
            location = m.group(1).strip()

    return {"hotel_id": hotel_id, "hotel_name": name, "rating": rating, "location": location}


# ---------------------------------------------------------------------------
# Scrape a single hotel (one date combo)
# ---------------------------------------------------------------------------

def scrape_hotel_page(page, slug: str, checkin: str, checkout: str) -> tuple[BeautifulSoup, list[dict]]:
    """Navigate to hotel page and return (soup, rooms_list)."""
    url = HOTEL_URL.format(slug=slug, checkin=checkin, checkout=checkout)
    page.goto(url, wait_until="networkidle", timeout=60000)
    time.sleep(3)
    soup = get_soup(page)
    rooms = parse_room_table(soup)
    return soup, rooms


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
# Main orchestrator
# ---------------------------------------------------------------------------

def main():
    log.info("=== Booking.com Armenia Hotels Scraper ===")
    hotels_rows: list[dict]  = []
    room_rows:   list[dict]  = []
    failed:      list[dict]  = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=USER_AGENT, locale="en-US")
        page = ctx.new_page()

        # Step 1: Collect all hotel slugs from search results
        log.info("--- Step 1: Collecting hotel list ---")
        hotels = collect_all_hotels(page)
        log.info(f"Total unique hotels found: {len(hotels)}")

        date_combos = list(DATES.items())

        # Step 2: Scrape each hotel
        log.info("--- Step 2: Scraping hotel pages ---")
        for hotel_idx, hotel in enumerate(hotels):
            hotel_id   = hotel["hotel_id"]
            slug       = hotel["slug"]
            log.info(f"[{hotel_idx+1}/{len(hotels)}] {hotel_id}")

            try:
                # --- First date: get metadata + facilities + rooms ---
                (season0, day0), (cin0, cout0) = date_combos[0]
                log.info(f"  Date 1/{len(date_combos)}: {cin0} ({season0}/{day0})")
                soup, rooms0 = scrape_hotel_page(page, slug, cin0, cout0)

                metadata  = parse_hotel_metadata(soup, hotel_id, hotel["hotel_name"], hotel["rating"])
                facilities = parse_facilities(soup)

                hotel_row = {**metadata, **facilities}
                hotels_rows.append(hotel_row)

                for rm in rooms0:
                    room_rows.append({
                        "hotel_id":     hotel_id,
                        "room_type":    rm["room_type"],
                        "price_amd":    rm["price_amd"],
                        "breakfast":    rm["breakfast"],
                        "max_occupancy": rm.get("max_occupancy"),
                        "checkin_date": cin0,
                        "checkout_date": cout0,
                        "season":       season0,
                        "day_type":     day0,
                    })

                log.info(f"  -> {len(rooms0)} rooms found")

                # --- Remaining date combos: rooms + prices only ---
                for (season, day_type), (checkin, checkout) in date_combos[1:]:
                    sleep()
                    log.info(f"  Date: {checkin} ({season}/{day_type})")
                    try:
                        _, rooms = scrape_hotel_page(page, slug, checkin, checkout)
                        for rm in rooms:
                            room_rows.append({
                                "hotel_id":     hotel_id,
                                "room_type":    rm["room_type"],
                                "price_amd":    rm["price_amd"],
                                "breakfast":    rm["breakfast"],
                                "max_occupancy": rm.get("max_occupancy"),
                                "checkin_date": checkin,
                                "checkout_date": checkout,
                                "season":       season,
                                "day_type":     day_type,
                            })
                        log.info(f"  -> {len(rooms)} rooms found")
                    except Exception as e:
                        log.warning(f"  Date {checkin} failed for {hotel_id}: {e}")

                    sleep()

            except Exception as e:
                log.error(f"Failed hotel {hotel_id}: {e}")
                failed.append({"hotel_id": hotel_id, "slug": slug, "error": str(e)})

            # Intermediate save every SAVE_EVERY hotels
            if (hotel_idx + 1) % SAVE_EVERY == 0:
                save_csv(hotels_rows, room_rows)
                save_failed(failed)

            sleep()

        browser.close()

    # Final save
    save_csv(hotels_rows, room_rows)
    save_failed(failed)
    log.info(f"Done. {len(hotels_rows)} hotels, {len(room_rows)} room-date rows, {len(failed)} failures.")


if __name__ == "__main__":
    main()
