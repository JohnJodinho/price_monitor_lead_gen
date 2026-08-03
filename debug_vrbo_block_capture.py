"""
Diagnostic script: hit Vrbo properties rapidly with no delay to capture block responses.
NO database persistence. NO modifications to shared engine code.
Now includes Proxy Rotation testing to verify if proxies bypass the block.
"""

import asyncio
import json
import logging
import os
import random
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from scrapling.fetchers import StealthyFetcher
from engines.vrbo_extractors import extract_vrbo_property_id
from config import get_settings

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("debug_vrbo_block")

CAPTURES_DIR = "debug_captures"
MAX_PROPERTIES = 5
BLOCK_STATUSES = {403, 429}


def build_vrbo_scrape_url(
    property_id: str, check_in: str, check_out: str, adults: int = 2
) -> str:
    base = f"https://www.vrbo.com/{property_id}"
    params = urlencode({"adults": adults, "chkin": check_in, "chkout": check_out})
    return f"{base}?{params}"


async def main():
    import shutil
    if os.path.exists(CAPTURES_DIR):
        shutil.rmtree(CAPTURES_DIR)
    os.makedirs(CAPTURES_DIR, exist_ok=True)
    settings = get_settings()

    vrbo_proxies = []
    if settings.VRBO_PROXIES:
        vrbo_proxies = [
            p.strip() for p in settings.VRBO_PROXIES.split(",") if p.strip()
        ]
        logger.info(f"Loaded {len(vrbo_proxies)} proxies from environment.")
    else:
        logger.warning("No VRBO_PROXIES defined in environment.")

    with open("properties_to_track.json", "r", encoding="utf-8") as f:
        all_properties = json.load(f)

    vrbo_properties = [p for p in all_properties if p.get("platform") == "vrbo"]
    logger.info(f"Found {len(vrbo_properties)} Vrbo properties to probe.")

    if not vrbo_properties:
        return

    today = datetime.now(ZoneInfo("America/New_York")).date()
    check_in_str = today.strftime("%Y-%m-%d")
    check_out_str = (today + timedelta(days=2)).strftime("%Y-%m-%d")

    prop_count = 0

    for item in vrbo_properties:
        vrbo_proxy_mode = False
        current_vrbo_proxy = None

        if prop_count >= MAX_PROPERTIES:
            break

        room_id = extract_vrbo_property_id(item["url"])
        scrape_url = build_vrbo_scrape_url(room_id, check_in_str, check_out_str)

        max_retries = 1
        for attempt in range(max_retries + 1):
            logger.info(f"[{room_id}] Fetching (Attempt {attempt + 1}): {scrape_url}")

            screenshot_path = os.path.join(
                CAPTURES_DIR, f"debug_{room_id}_attempt_{attempt + 1}.png"
            )
            html_path = os.path.join(
                CAPTURES_DIR, f"debug_{room_id}_attempt_{attempt + 1}.html"
            )
            ip_path = os.path.join(
                CAPTURES_DIR, f"debug_{room_id}_attempt_{attempt + 1}_ip.txt"
            )
            screenshot_taken = {"done": False}

            async def take_screenshot(page):
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                try:
                    await page.screenshot(path=screenshot_path, full_page=True)
                    screenshot_taken["done"] = True
                except Exception as e:
                    logger.warning(f"Screenshot failed: {e}")

            kwargs = {
                "headless": True,
                "load_dom": True,
                "block_webrtc": True,
                "google_search": True,
                "page_action": take_screenshot,
                "wait": 5000,
                "timeout": 60_000,
                "block_images": True,  # Match real_estate_monitor proxy bandwidth saving
            }

            if vrbo_proxy_mode and current_vrbo_proxy:
                kwargs["proxy"] = current_vrbo_proxy
                logger.info(f"[{room_id}] Using proxy for this request.")

            try:
                response = await StealthyFetcher.async_fetch(scrape_url, **kwargs)
            except Exception as e:
                logger.error(f"[{room_id}] Fetch exception: {e}")
                if attempt < max_retries:
                    continue
                break

            if not response:
                logger.error(f"[{room_id}] No response object returned.")
                break

            status = response.status
            body = ""
            try:
                body = response.body.decode("utf-8", errors="replace")
            except:
                pass

            title = ""
            try:
                title = response.css("title::text").get() or ""
            except:
                pass

            is_block = False
            if (
                response.css("#DATADOME-CHALLENGE")
                or response.css("#cf-challenge-running")
                or response.css(".cf-browser-verification")
            ):
                is_block = True
            elif title.strip() in [
                "Bot or Not?",
                "Just a moment...",
                "Attention Required!",
            ]:
                is_block = True
            elif (
                "datadome" in body.lower()
                or "cloudflare" in body.lower()
                or "perimeterx" in body.lower()
            ):
                is_block = True
            elif status in BLOCK_STATUSES:
                is_block = True

            # Save HTML regardless of block for diagnostic inspection
            try:
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(body)
                logger.info(f"[{room_id}] Saved HTML → {html_path}")
            except Exception as e:
                pass

            if is_block:
                logger.warning(
                    f"[{room_id}] HTTP {status} | BLOCKED | title='{title.strip()}'"
                )

                # Capture the IP without credentials
                ip_address = "Direct Connection"
                if vrbo_proxy_mode and current_vrbo_proxy:
                    import urllib.parse

                    try:
                        parsed = urllib.parse.urlparse(current_vrbo_proxy)
                        ip_address = parsed.hostname or "Unknown Proxy IP"
                    except Exception:
                        ip_address = "Unknown Proxy IP"
                try:
                    with open(ip_path, "w", encoding="utf-8") as f:
                        f.write(f"Blocked IP: {ip_address}\n")
                    logger.info(f"[{room_id}] Saved IP → {ip_path}")
                except Exception:
                    pass

                if not vrbo_proxy_mode and vrbo_proxies:
                    logger.info(f"[{room_id}] Switching to proxy mode and retrying...")
                    vrbo_proxy_mode = True
                    current_vrbo_proxy = random.choice(vrbo_proxies)
                    continue
                elif vrbo_proxy_mode:
                    logger.error(
                        f"[{room_id}] Proxy was also blocked! Proxy rotation failed."
                    )
                    break
            else:
                logger.info(
                    f"[{room_id}] HTTP {status} | SUCCESS | title='{title.strip()}'"
                )
                break  # Success! No retry needed.

        prop_count += 1


if __name__ == "__main__":
    asyncio.run(main())
