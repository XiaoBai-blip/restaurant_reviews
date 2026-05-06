import requests
import pandas as pd

from playwright.sync_api import sync_playwright

BASE_URL = "https://lamadeleine.com/locations"
API_URL = "https://lamadeleine.com/wp-json/wp/v2/restaurant-locations?per_page=150"

def fetch_data():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # 先进入页面，拿到 cookie 和浏览器环境
        page.goto(BASE_URL, wait_until="networkidle")

        # 直接在浏览器上下文里请求 API
        data = page.evaluate("""
            async (apiUrl) => {
                const res = await fetch(apiUrl, {
                    method: "GET",
                    headers: {
                        "Accept": "application/json, text/plain, */*",
                        "Referer": "https://lamadeleine.com/locations"
                    }
                });
                return await res.json();
            }
        """, API_URL)

        browser.close()
        return data

def normalize_location(rec):
    acf = rec.get("acf", {})
    hero = acf.get("locationHero", {})

    location_name = hero.get("storeName", "")

    street_address = hero.get("addressLine1", "")
    street_address2 = hero.get("addressLine2", "")
    city = hero.get("city", "")
    state = hero.get("state", "")
    postal_code = hero.get("zip", "")
    store_id = rec.get("id", "")

    full_address = ", ".join(
        [x for x in [street_address, street_address2, city, state, postal_code] if x]
    )

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

    rows = [normalize_location(loc) for loc in data]
    df = pd.DataFrame(rows)

    df = df[
        [
            "locationName",
            "postalCode",
            "streetAddress",
            "streetAddress2",
            "fullAddress",
            "city",
            "state",
            "storeID",
        ]
    ].drop_duplicates().fillna("")


    df.to_csv("lamadeleine_locations.csv", index=False)

    print("Saved lamadeleine_locations.csv")
    print(df.head())

if __name__ == "__main__":
    main()