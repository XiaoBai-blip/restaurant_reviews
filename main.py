import logging
import time

import pandas as pd
from playwright.sync_api import sync_playwright

BASE_URL = "https://lamadeleine.com/locations"
API_URL = "https://lamadeleine.com/wp-json/wp/v2/restaurant-locations"
PER_PAGE = 100
REQUEST_TIMEOUT_MS = 30000
MAX_RETRIES = 3

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def fetch_page(page, page_num, per_page=PER_PAGE, timeout_ms=REQUEST_TIMEOUT_MS):
    return page.evaluate(
        """
        async ({ apiUrl, pageNum, perPage, timeoutMs }) => {
            const url = new URL(apiUrl);
            url.searchParams.set("per_page", String(perPage));
            url.searchParams.set("page", String(pageNum));

            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), timeoutMs);

            try {
                const res = await fetch(url.toString(), {
                    method: "GET",
                    headers: {
                        "Accept": "application/json, text/plain, */*",
                        "Referer": "https://lamadeleine.com/locations"
                    },
                    signal: controller.signal
                });

                if (!res.ok) {
                    throw new Error(`HTTP ${res.status} ${res.statusText}`);
                }

                const data = await res.json();
                const totalPages = parseInt(res.headers.get("X-WP-TotalPages") || "1", 10);
                const totalRecords = parseInt(
                    res.headers.get("X-WP-Total") || String(Array.isArray(data) ? data.length : 0),
                    10
                );

                return {
                    data,
                    totalPages,
                    totalRecords
                };
            } finally {
                clearTimeout(timer);
            }
        }
        """,
        {
            "apiUrl": API_URL,
            "pageNum": page_num,
            "perPage": per_page,
            "timeoutMs": timeout_ms,
        },
    )


def fetch_page_with_retry(page, page_num, per_page=PER_PAGE, max_retries=MAX_RETRIES):
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                logging.info("Retrying page %s (attempt %s/%s)...", page_num, attempt, max_retries)

            return fetch_page(page, page_num, per_page=per_page)

        except Exception as e:
            last_error = e
            logging.warning(
                "Failed to fetch page %s on attempt %s/%s: %s",
                page_num,
                attempt,
                max_retries,
                e,
            )

            if attempt < max_retries:
                time.sleep(0.5 * attempt)

    raise RuntimeError(f"Failed to fetch page {page_num} after {max_retries} attempts") from last_error


def fetch_data():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(REQUEST_TIMEOUT_MS)
            page.set_default_navigation_timeout(REQUEST_TIMEOUT_MS)

            page.goto(BASE_URL, wait_until="networkidle")

            first = fetch_page_with_retry(page, 1)
            first_page_data = first["data"]
            total_pages = first["totalPages"]
            total_records = first["totalRecords"]

            if not isinstance(first_page_data, list):
                raise RuntimeError("Unexpected API response: first page is not a list.")

            logging.info(
                "API reports %s total record(s) across %s page(s).",
                total_records,
                total_pages,
            )

            all_records = list(first_page_data)

            if total_pages > 1:
                logging.info("Pagination detected. Fetching pages 2 to %s...", total_pages)
                for page_num in range(2, total_pages + 1):
                    result = fetch_page_with_retry(page, page_num)
                    page_data = result["data"]

                    if isinstance(page_data, list):
                        all_records.extend(page_data)
                    else:
                        logging.warning(
                            "Skipping unexpected non-list response on page %s.",
                            page_num,
                        )

            logging.info("Fetched %s total record(s).", len(all_records))
            return all_records

        finally:
            browser.close()


def clean_text(value):
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value).strip()
    return str(value).strip()


def get_by_path(obj, path, default=""):
    cur = obj
    try:
        for key in path:
            if isinstance(cur, dict):
                cur = cur.get(key)
            else:
                return default
        return clean_text(cur) if cur not in [None, "", [], {}] else default
    except Exception:
        return default


def find_anywhere(obj, target_keys):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in target_keys and v not in [None, "", [], {}]:
                return clean_text(v), k
            found_val, found_key = find_anywhere(v, target_keys)
            if found_val:
                return found_val, found_key
    elif isinstance(obj, list):
        for item in obj:
            found_val, found_key = find_anywhere(item, target_keys)
            if found_val:
                return found_val, found_key
    return "", ""


def pick_value(rec, field_name, paths=None, fallback_keys=None):
    paths = paths or []
    fallback_keys = fallback_keys or []

    for path in paths:
        val = get_by_path(rec, path)
        if val:
            return val

    if fallback_keys:
        val, matched_key = find_anywhere(rec, set(fallback_keys))
        if val:
            logging.warning(
                "Using fallback for %s (record id=%s, key=%s)",
                field_name,
                rec.get("id", ""),
                matched_key,
            )
            return val

    return ""


def build_full_address(street1, street2, city, state, postal):
    if street1 and street2 and street2 in street1:
        street2 = ""

    parts = []
    street = " ".join([x for x in [street1, street2] if x]).strip()
    if street:
        parts.append(street)
    if city:
        parts.append(city)
    if state:
        parts.append(state)
    if postal:
        parts.append(postal)
    return ", ".join(parts)


def normalize_location(rec):
    location_name = pick_value(
        rec,
        field_name="locationName",
        paths=[
            ("acf", "locationHero", "storeName"),
            ("title", "rendered"),
        ],
        fallback_keys=["storeName", "locationName", "name", "title"],
    )

    street_address = pick_value(
        rec,
        field_name="streetAddress",
        paths=[
            ("acf", "locationHero", "addressLine1"),
            ("addressLine1",),
        ],
        fallback_keys=["addressLine1", "streetAddress", "address", "line1", "addr1"],
    )

    street_address2 = pick_value(
        rec,
        field_name="streetAddress2",
        paths=[
            ("acf", "locationHero", "addressLine2"),
            ("addressLine2",),
        ],
        fallback_keys=["addressLine2", "streetAddress2", "line2", "addr2", "suite", "unit"],
    )

    city = pick_value(
        rec,
        field_name="city",
        paths=[
            ("acf", "locationHero", "city"),
            ("city",),
        ],
        fallback_keys=["city", "town", "locality", "municipality"],
    )

    state = pick_value(
        rec,
        field_name="state",
        paths=[
            ("acf", "locationHero", "state"),
            ("state",),
        ],
        fallback_keys=["state", "stateCode", "region", "province"],
    )

    postal_code = pick_value(
        rec,
        field_name="postalCode",
        paths=[
            ("acf", "locationHero", "zip"),
            ("zip",),
            ("postalCode",),
        ],
        fallback_keys=["zip", "postalCode", "zipcode", "zipCode", "postal_code"],
    )

    store_id = pick_value(
        rec,
        field_name="storeID",
        paths=[
            ("id",),
            ("acf", "locationHero", "id"),
        ],
        fallback_keys=["id", "storeID", "storeId", "locationId", "locationID", "uuid", "slug"],
    )

    full_address = build_full_address(
        street_address,
        street_address2,
        city,
        state,
        postal_code,
    )

    if not location_name:
        logging.warning("Missing locationName for record id=%s", rec.get("id", ""))
    if not street_address:
        logging.warning("Missing streetAddress for record id=%s", rec.get("id", ""))

    return {
        "locationName": location_name,
        "postalCode": postal_code,
        "streetAddress": street_address,
        "streetAddress2": street_address2,
        "fullAddress": full_address,
        "city": city,
        "state": state,
        "storeID": store_id,
    }


def main():
    print("Fetching data...")
    data = fetch_data()
    print(f"Total records: {len(data)}")

    rows = []
    for rec in data:
        try:
            rows.append(normalize_location(rec))
        except Exception as e:
            logging.exception("Failed to normalize record id=%s: %s", rec.get("id", ""), e)

    df = pd.DataFrame(rows)

    required_cols = [
        "locationName",
        "postalCode",
        "streetAddress",
        "streetAddress2",
        "fullAddress",
        "city",
        "state",
        "storeID",
    ]

    for col in required_cols:
        if col not in df.columns:
            df[col] = ""

    df = df[required_cols].drop_duplicates().fillna("")
    df.to_csv("lamadeleine_locations.csv", index=False, encoding="utf-8-sig")

    print("Saved lamadeleine_locations.csv")
    print(df.head())


if __name__ == "__main__":
    main()