import re
import json
import os
import logging
from urllib.parse import urlparse, parse_qs, urlencode

LISTINGS_PATH = "listings.json"
TRACKED_PATH = "properties_to_track.json"


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def extract_property_key(url, platform):
    """Pull the stable listing id out of a cleaned url."""
    try:
        path = urlparse(url).path
    except Exception as e:
        logger.exception("Failed to parse URL for property key: %s", url)
        return None

    if platform == "vrbo":
        # e.g. /5365580 or /12107955ha -> "5365580" / "12107955ha"
        return path.strip("/") if path else None
    if platform == "airbnb":
        # e.g. /rooms/1499614549034076717 -> "1499614549034076717"
        match = re.search(r"/rooms/(\d+)", path or "")
        return match.group(1) if match else None
    return None


def parse_vrbo(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except Exception as e:
        logger.exception("Failed to read VRBO file %s: %s", path, e)
        return []

    # Only one market in this file
    market = "NYC/NJ Metro"

    logger.debug("VRBO file sample: %r", text[:200])
    urls = re.findall(r"https://www\.vrbo\.com/\S+", text)
    logger.info("VRBO regex found %d matches in %s", len(urls), path)
    if len(urls) > 0:
        logger.debug("VRBO sample matches: %s", urls[:5])
    results = []
    seen = set()
    for u in urls:
        u = u.strip()
        try:
            parsed = urlparse(u)
            clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        except Exception:
            logger.exception("Skipping malformed VRBO URL: %s", u)
            continue
        if clean in seen:
            continue
        seen.add(clean)
        results.append(
            {
                "url": clean,
                "platform": "vrbo",
                "market": market,
                "property_key": extract_property_key(clean, "vrbo"),
            }
        )
    return results


def parse_airbnb(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except Exception as e:
        logger.exception("Failed to read Airbnb file %s: %s", path, e)
        return []

    lines = text.splitlines()

    market_headers = {"NYC/NJ Metro:": "NYC/NJ Metro", "Miami:": "Miami"}
    current_market = None
    results = []
    seen = set()

    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line in market_headers:
            current_market = market_headers[line]
            continue
        if line.startswith("https://www.airbnb.com/rooms/"):
            try:
                parsed = urlparse(line)
                qs = parse_qs(parsed.query)
                sic = qs.get("source_impression_id")
                new_query = urlencode(
                    {"source_impression_id": sic[0]}) if sic else ""
                clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                if new_query:
                    clean = f"{clean}?{new_query}"
            except Exception:
                logger.exception("Skipping malformed Airbnb URL: %s", line)
                continue
            if clean in seen:
                continue
            seen.add(clean)
            results.append(
                {
                    "url": clean,
                    "platform": "airbnb",
                    "market": current_market,
                    "property_key": extract_property_key(clean, "airbnb"),
                }
            )
    return results


# ---------------------------------------------------------------------------
# Tracking / dedupe
# ---------------------------------------------------------------------------


def load_properties(path):
    """Load a list of property objects from disk. Empty list if missing/blank."""
    try:
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return []
        return json.loads(content)
    except json.JSONDecodeError as e:
        logger.exception(
            "JSON decode error loading properties %s: %s", path, e)
        return []
    except Exception as e:
        logger.exception("Error loading properties %s: %s", path, e)
        return []


def save_properties(path, properties):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(properties, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.exception("Failed to save properties to %s: %s", path, e)
        raise


def dedupe_key(item):
    """Identity of a listing: platform + stable property key (falls back to url)."""
    key = item.get("property_key")
    if not key:
        key = extract_property_key(item.get("url", ""), item.get("platform"))
    return (item.get("platform"), key)


def update_tracked_properties(new_listings, tracked_path):
    """
    Compare new_listings against whatever is already stored at tracked_path,
    add any that aren't already tracked, and write the merged result back.

    Returns (updated_list, added_items, duplicate_items).
    """
    tracked = load_properties(tracked_path)

    existing_keys = {dedupe_key(item) for item in tracked}

    added = []
    duplicates = []

    for item in new_listings:
        key = dedupe_key(item)
        if key in existing_keys:
            duplicates.append(item)
            continue
        existing_keys.add(key)
        tracked.append(item)
        added.append(item)

    save_properties(tracked_path, tracked)
    return tracked, added, duplicates


def parse_and_merge(vrbo_path, airbnb_path, listings_path, tracked_path):
    vrbo_results = parse_vrbo(vrbo_path)
    airbnb_results = parse_airbnb(airbnb_path)
    all_results = vrbo_results + airbnb_results

    try:
        save_properties(listings_path, all_results)
    except Exception:
        logger.exception("Failed to save listings to %s", listings_path)

    try:
        updated, added, duplicates = update_tracked_properties(
            all_results, tracked_path)
    except Exception:
        logger.exception(
            "Failed to update tracked properties at %s", tracked_path)
        updated, added, duplicates = all_results, [], []

    print(f"VRBO parsed: {len(vrbo_results)} unique listings")
    print(f"Airbnb parsed: {len(airbnb_results)} unique listings")
    print(f"Total parsed: {len(all_results)}")
    print(f"Already tracked (skipped): {len(duplicates)}")
    print(f"Newly added to tracked list: {len(added)}")
    print(f"Tracked list size now: {len(updated)}")

    return updated, added, duplicates


# ---------------------------------------------------------------------------
# Main: real run + self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # --- 1. Real run against the actual uploaded files -----------------
    real_vrbo = "vrbo.txt"
    real_airbnb = "airbnb.txt"

    if os.path.exists(real_vrbo) and os.path.exists(real_airbnb):
        print("=== Real run ===")
        parse_and_merge(real_vrbo, real_airbnb, LISTINGS_PATH, TRACKED_PATH)
    else:
        print("=== Real run skipped (source txt files not found) ===")

    # # --- 2. Self-test of the dedupe logic, isolated from real files ----
    # print("\n=== Self-test: dedupe logic ===")

    # test_dir = "/tmp/dedupe_test"
    # os.makedirs(test_dir, exist_ok=True)
    # test_tracked_path = os.path.join(test_dir, "properties_to_track.json")

    # # Start with a fake "already tracked" file
    # starting_tracked = [
    #     {
    #         "url": "https://www.vrbo.com/5365580",
    #         "platform": "vrbo",
    #         "market": "NYC/NJ Metro",
    #         "property_key": "5365580",
    #     },
    #     {
    #         "url": "https://www.airbnb.com/rooms/31118155?source_impression_id=OLD_STALE_ID",
    #         "platform": "airbnb",
    #         "market": "NYC/NJ Metro",
    #         "property_key": "31118155",
    #     },
    # ]
    # save_properties(test_tracked_path, starting_tracked)

    # # New batch: one exact duplicate, one same-property-different-session-id
    # # (airbnb source_impression_id changed but it's the same listing),
    # # and two genuinely new listings.
    # new_batch = [
    #     {
    #         "url": "https://www.vrbo.com/5365580",
    #         "platform": "vrbo",
    #         "market": "NYC/NJ Metro",
    #         "property_key": "5365580",
    #     },  # exact duplicate
    #     {
    #         "url": "https://www.airbnb.com/rooms/31118155?source_impression_id=BRAND_NEW_SESSION_ID",
    #         "platform": "airbnb",
    #         "market": "NYC/NJ Metro",
    #         "property_key": "31118155",
    #     },  # same property, different session id -> should still count as duplicate
    #     {
    #         "url": "https://www.vrbo.com/9999999",
    #         "platform": "vrbo",
    #         "market": "NYC/NJ Metro",
    #         "property_key": "9999999",
    #     },  # new
    #     {
    #         "url": "https://www.airbnb.com/rooms/123456789?source_impression_id=ANOTHER_NEW_ID",
    #         "platform": "airbnb",
    #         "market": "Miami",
    #         "property_key": "123456789",
    #     },  # new
    # ]

    # updated, added, duplicates = update_tracked_properties(new_batch, test_tracked_path)

    # assert len(duplicates) == 2, f"Expected 2 duplicates, got {len(duplicates)}"
    # assert len(added) == 2, f"Expected 2 newly added, got {len(added)}"
    # assert len(updated) == 4, f"Expected 4 total tracked, got {len(updated)}"

    # added_keys = {dedupe_key(item) for item in added}
    # assert ("vrbo", "9999999") in added_keys
    # assert ("airbnb", "123456789") in added_keys

    # print(f"Duplicates correctly skipped: {len(duplicates)}")
    # print(f"New listings correctly added: {len(added)}")
    # print(f"Final tracked count: {len(updated)}")
    # print("All self-test assertions passed.")

    # print("\nFinal test tracked file contents:")
    # print(json.dumps(updated, indent=2))
