import logging
import time
from typing import Any, Dict, List
import pandas as pd
from playwright.sync_api import sync_playwright

# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------

BASE_URL = "https://lamadeleine.com/locations"
API_URL = "https://lamadeleine.com/wp-json/wp/v2/restaurant-locations"

PER_PAGE = 100
REQUEST_TIMEOUT_MS = 30_000
MAX_RETRIES = 3

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


# ------------------------------------------------------------------------------
# Data Fetching
# ------------------------------------------------------------------------------

def fetch_page(page, page_num: int) -> Dict[str, Any]:
    """
    Fetch a single page of location data via browser-executed fetch.
    Uses AbortController for timeout handling.
    """
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

                return {
                    data,
                    totalPages: parseInt(res.headers.get("X-WP-TotalPages") || "1", 10),
                    totalRecords: parseInt(res.headers.get("X-WP-Total") || data.length, 10)
                };

            } finally {
                clearTimeout(timer);
            }
        }
        """,
        {
            "apiUrl": API_URL,
            "pageNum": page_num,
            "perPage": PER_PAGE,
            "timeoutMs": REQUEST_TIMEOUT_MS,
        },
    )


def fetch_page_with_retry(page, page_num: int) -> Dict[str, Any]:
    """
    Retry wrapper to improve robustness against transient failures.
    """
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if attempt > 1:
                logging.info("Retrying page %s (%s/%s)", page_num, attempt, MAX_RETRIES)

            return fetch_page(page, page_num)

        except Exception as e:
            last_error = e
            logging.warning(
                "Page %s failed (%s/%s): %s",
                page_num, attempt, MAX_RETRIES, e
            )

            if attempt < MAX_RETRIES:
                time.sleep(0.5 * attempt)

    raise RuntimeError(f"Failed to fetch page {page_num}") from last_error


def fetch_all_locations() -> List[Dict[str, Any]]:
    """
    Entry point for data extraction.
    Handles pagination dynamically using API response headers.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(BASE_URL, wait_until="networkidle")

            first_page = fetch_page_with_retry(page, 1)

            total_pages = first_page["totalPages"]
            total_records = first_page["totalRecords"]

            logging.info("Detected %s records across %s pages", total_records, total_pages)

            all_data = list(first_page["data"])

            for page_num in range(2, total_pages + 1):
                page_data = fetch_page_with_retry(page, page_num)["data"]
                all_data.extend(page_data)

            logging.info("Fetched %s records total", len(all_data))
            return all_data

        finally:
            browser.close()


# ------------------------------------------------------------------------------
# Data Transformation Layer
# ------------------------------------------------------------------------------

def clean_text(value: Any) -> str:
    """Normalize values to clean string format."""
    if value in (None, "", [], {}):
        return ""
    return str(value).strip()


def get_by_path(obj: Dict, path: tuple) -> str:
    """Safely extract nested value using a path."""
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return ""
    return clean_text(cur)


def find_anywhere(obj: Any, keys: set) -> tuple:
    """
    Recursively search for keys anywhere in nested JSON.
    Returns (value, key_used).
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and v not in (None, "", [], {}):
                return clean_text(v), k
            result = find_anywhere(v, keys)
            if result[0]:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = find_anywhere(item, keys)
            if result[0]:
                return result
    return "", ""


def pick_value(record: Dict, field: str, paths=None, fallback_keys=None) -> str:
    """
    Extract value with priority:
    1. Known stable paths
    2. Fallback key search (schema drift protection)
    """
    paths = paths or []
    fallback_keys = fallback_keys or []

    for path in paths:
        val = get_by_path(record, path)
        if val:
            return val

    val, matched = find_anywhere(record, set(fallback_keys))
    if val:
        logging.warning(
            "Fallback used for %s (record=%s, key=%s)",
            field, record.get("id", ""), matched
        )
    return val


def build_full_address(street1, street2, city, state, postal):
    """Combine structured address fields into one readable string."""
    if street2 and street2 in street1:
        street2 = ""

    parts = [x for x in [street1, street2, city, state, postal] if x]
    return ", ".join(parts)


def normalize_location(record: Dict) -> Dict[str, str]:
    """
    Transform raw API record into standardized schema required. Used primary fieldname/path and fallback fields.
    """

    location_name = pick_value(
        record,
        "locationName",
        paths=[("acf", "locationHero", "storeName"), ("title", "rendered")],
        fallback_keys=["storeName", "locationName", "name"]
    )

    street1 = pick_value(
        record,
        "streetAddress",
        paths=[("acf", "locationHero", "addressLine1")],
        fallback_keys=["addressLine1", "streetAddress", "address"]
    )

    street2 = pick_value(
        record,
        "streetAddress2",
        paths=[("acf", "locationHero", "addressLine2")],
        fallback_keys=["addressLine2", "streetAddress2", "line2"]
    )

    city = pick_value(record, "city", paths=[("acf", "locationHero", "city")])
    state = pick_value(record, "state", paths=[("acf", "locationHero", "state")])
    postal = pick_value(record, "postalCode", paths=[("acf", "locationHero", "zip")])

    store_id = pick_value(
        record,
        "storeID",
        paths=[("id",)],
        fallback_keys=["storeID", "locationId", "slug"]
    )

    full_address = build_full_address(street1, street2, city, state, postal)

    return {
        "locationName": location_name,
        "postalCode": postal,
        "streetAddress": street1,
        "streetAddress2": street2,
        "fullAddress": full_address,
        "city": city,
        "state": state,
        "storeID": store_id,
    }


# ------------------------------------------------------------------------------
# Output Layer
# ------------------------------------------------------------------------------

def export_to_csv(records: List[Dict[str, str]]):
    df = pd.DataFrame(records)

    required_cols = [
        "locationName", "postalCode", "streetAddress",
        "streetAddress2", "fullAddress", "city", "state", "storeID"
    ]

    for col in required_cols:
        if col not in df:
            df[col] = ""

    df = df[required_cols].drop_duplicates().fillna("")
    df.to_csv("lamadeleine_locations.csv", index=False, encoding="utf-8-sig")

    logging.info("CSV exported successfully")


# ------------------------------------------------------------------------------
# Main Execution
# ------------------------------------------------------------------------------

def main():
    logging.info("Starting data extraction...")

    raw_data = fetch_all_locations()
    processed = [normalize_location(r) for r in raw_data]

    export_to_csv(processed)

    logging.info("Pipeline completed successfully")


if __name__ == "__main__":
    main()