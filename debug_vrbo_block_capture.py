"""
Diagnostic script: hit Vrbo properties rapidly with no delay to capture block responses.
NO database persistence. NO modifications to shared engine code.
Saves up to 2 blocked response HTML + screenshot pairs, then exits.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from scrapling.fetchers import StealthyFetcher
from engines.vrbo_extractors import extract_vrbo_property_id

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("debug_vrbo_block")

CAPTURES_DIR = "debug_captures"
MAX_CAPTURES = 2
BLOCK_STATUSES = {403, 429}


def build_vrbo_scrape_url(
    property_id: str, check_in: str, check_out: str, adults: int = 2
) -> str:
    """Inlined from real_estate_monitor.py to avoid its transitive DB imports."""
    base = f"https://www.vrbo.com/{property_id}"
    params = urlencode({"adults": adults, "chkin": check_in, "chkout": check_out})
    return f"{base}?{params}"


async def main():
    os.makedirs(CAPTURES_DIR, exist_ok=True)

    with open("properties_to_track.json", "r", encoding="utf-8") as f:
        all_properties = json.load(f)

    vrbo_properties = [p for p in all_properties if p.get("platform") == "vrbo"]
    logger.info(f"Found {len(vrbo_properties)} Vrbo properties to probe.")

    if not vrbo_properties:
        logger.warning("No Vrbo properties in properties_to_track.json. Exiting.")
        return

    today = datetime.now(ZoneInfo("America/New_York")).date()
    check_in_str = today.strftime("%Y-%m-%d")
    check_out_str = (today + timedelta(days=2)).strftime("%Y-%m-%d")
    logger.info(f"Date window: {check_in_str} → {check_out_str}")

    capture_count = 0

    for item in vrbo_properties:
        if capture_count >= MAX_CAPTURES:
            logger.info(f"Captured {MAX_CAPTURES} blocks. Stopping.")
            break

        room_id = extract_vrbo_property_id(item["url"])
        scrape_url = build_vrbo_scrape_url(room_id, check_in_str, check_out_str)

        # Prepare a page_action callback that will take a screenshot while the
        # browser page is still open (before Scrapling closes the context).
        screenshot_path = os.path.join(
            CAPTURES_DIR, f"debug_vrbo_block_{capture_count + 1}.png"
        )
        # We use a mutable container so the inner closure can signal whether it ran.
        screenshot_taken = {"done": False, "path": screenshot_path}

        async def take_screenshot(page, _path=screenshot_path, _flag=screenshot_taken):
            try:
                await page.screenshot(path=_path, full_page=True)
                _flag["done"] = True
            except Exception as e:
                logger.warning(f"Screenshot failed: {e}")

        logger.info(f"[{room_id}] Fetching: {scrape_url}")

        try:
            response = await StealthyFetcher.async_fetch(
                scrape_url,
                headless=True,
                load_dom=True,
                block_webrtc=True,
                google_search=True,
                page_action=take_screenshot,
                wait=5000,
                timeout=60_000,
            )
        except Exception as e:
            logger.error(f"[{room_id}] Fetch exception: {e}")
            continue

        if not response:
            logger.error(f"[{room_id}] No response object returned.")
            continue

        status = response.status
        is_block = status in BLOCK_STATUSES

        # Also check for DataDome "Bot or Not?" title as a soft block
        title = ""
        try:
            title = response.css("title::text").get() or ""
        except Exception:
            pass
        if not is_block and title.strip() == "Bot or Not?":
            is_block = True

        # Also check for datadome in body
        if not is_block:
            try:
                body = response.body.decode("utf-8", errors="replace").lower()
                if "datadome" in body:
                    is_block = True
            except Exception:
                pass

        if is_block:
            capture_count += 1
            idx = capture_count
            html_path = os.path.join(CAPTURES_DIR, f"debug_vrbo_block_{idx}.html")

            # Save raw HTML
            try:
                raw_html = response.body.decode("utf-8", errors="replace")
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(raw_html)
                logger.info(f"[{room_id}] ✓ Saved HTML capture → {html_path}")
            except Exception as e:
                logger.error(f"[{room_id}] Failed to save HTML: {e}")

            # Check if screenshot was taken by page_action
            if screenshot_taken["done"]:
                logger.info(
                    f"[{room_id}] ✓ Saved screenshot → {screenshot_taken['path']}"
                )
            else:
                logger.warning(
                    f"[{room_id}] ✗ Screenshot was NOT captured by page_action."
                )

            logger.info(
                f"[{room_id}] HTTP {status} | title='{title.strip()}' | "
                f"CAPTURED as pair #{idx} (HTML + screenshot)"
            )
        else:
            logger.info(
                f"[{room_id}] HTTP {status} | title='{title.strip()}' | "
                f"NOT a block — skipping capture."
            )

    logger.info(
        f"Debug run complete. Captured {capture_count}/{MAX_CAPTURES} block pairs."
    )

    # List what we actually saved
    if os.path.isdir(CAPTURES_DIR):
        files = os.listdir(CAPTURES_DIR)
        if files:
            logger.info(f"Files in {CAPTURES_DIR}/: {files}")
        else:
            logger.info(f"No files saved in {CAPTURES_DIR}/.")


if __name__ == "__main__":
    asyncio.run(main())
