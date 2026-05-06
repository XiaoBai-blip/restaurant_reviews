import logging

import pandas as pd
from playwright.sync_api import sync_playwright

BASE_URL = "https://lamadeleine.com/locations"
API_URL = "https://lamadeleine.com/wp-json/wp/v2/restaurant-locations?per_page=150"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def fetch_data():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(BASE_URL, wait_until="networkidle")

            data = page.evaluate(
                """
                async (apiUrl) => {
                    const res = await fetch(apiUrl, {
                        method: "GET",
                        headers: {
                            "Accept": "application/json, text/plain, */*",
                            "Referer": "https://lamadeleine.com/locations"
                        }
                    });

                    if (!res.ok) {
                        throw new Error(`HTTP ${res.status} ${res.statusText}`);
                    }

                    return await res.json();
                }
                """,
                API_URL,
            )
            return data
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
    """
    Recursively search for a key anywhere in a nested dict/list structure.
    Returns the first non-empty match and the key name that matched.
    """
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
    """
    Try exact nested paths first, then recursively search by fallback keys.
    Returns the extracted value.
    """
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
    # Avoid repeating suite/unit if it is already embedded in street1.
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
    # Current schema (WordPress + ACF):
    # acf.locationHero.storeName
    # acf.locationHero.addressLine1 / addressLine2
    # acf.locationHero.city / state / zip

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