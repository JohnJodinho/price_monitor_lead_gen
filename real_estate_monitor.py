import asyncio
import json
import logging
import random
import itertools
import re
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from sqlalchemy.dialects.postgresql import insert

from db import AsyncSessionLocal
from config import get_settings
from models import Property, RateHistory, ScrapeRun, RunJobType, RunStatus
from scrapling.fetchers import StealthyFetcher
from engines.real_estate_extractors import extract_metadata_from_json, extract_pricing
from engines.vrbo_extractors import (
    extract_vrbo_property_id,
    extract_vrbo_metadata,
    extract_vrbo_pricing,
)
from observability import run_watchdog, RunLogger
from dlq_storage import upload_dlq_artifacts

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("real_estate_monitor")


def extract_room_id(url: str) -> str:
    match = re.search(r"/rooms/(\d+)", url)
    if not match:
        raise ValueError(f"Could not extract a room ID from: {url}")
    return match.group(1)


def build_scrape_url(
    room_id: str, check_in: str, check_out: str, adults: int = 1
) -> str:
    base = f"https://www.airbnb.com/rooms/{room_id}"
    params = {"check_in": check_in, "check_out": check_out, "adults": adults}
    return f"{base}?{urlencode(params)}"


def build_vrbo_scrape_url(
    property_id: str, check_in: str, check_out: str, adults: int = 2
) -> str:
    base = f"https://www.vrbo.com/{property_id}"
    params = {"adults": adults, "chkin": check_in, "chkout": check_out}
    return f"{base}?{urlencode(params)}"


async def run_real_estate_monitor():
    logger.info("Starting Real Estate Monitor run")

    settings = get_settings()
    vrbo_proxies = []
    if settings.VRBO_PROXIES:
        vrbo_proxies = [p.strip() for p in settings.VRBO_PROXIES.split(",") if p.strip()]

    with open("properties_to_track.json", "r", encoding="utf-8") as f:
        properties_input = json.load(f)

    # Interleave Airbnb and Vrbo properties so processing alternates
    def interleave(list_a, list_b):
        merged = []
        for a, b in itertools.zip_longest(list_a, list_b):
            if a is not None:
                merged.append(a)
            if b is not None:
                merged.append(b)
        return merged

    airbnb_properties = [
        p for p in properties_input if p.get("platform", "airbnb") == "airbnb"
    ]
    vrbo_properties = [
        p for p in properties_input if p.get("platform", "airbnb") == "vrbo"
    ]
    properties_input = interleave(airbnb_properties, vrbo_properties)

    # ── today-driven date window ──────────────────────────────────────────────
    today = datetime.now(ZoneInfo("America/New_York")).date()
    check_in = today
    check_out = today + timedelta(days=2)
    check_in_str = check_in.strftime("%Y-%m-%d")
    check_out_str = check_out.strftime("%Y-%m-%d")
    logger.info(f"Date window for this run: {check_in_str} → {check_out_str}")
    # ─────────────────────────────────────────────────────────────────────────

    # Initialize ScrapeRun log
    async with AsyncSessionLocal() as session:
        await run_watchdog(
            session, RunJobType.REAL_ESTATE_MONITOR.value, max_duration_hours=8
        )

        run_record = ScrapeRun(
            job_type=RunJobType.REAL_ESTATE_MONITOR,
            status=RunStatus.RUNNING,
            started_at=datetime.now(ZoneInfo("UTC")),
            platform="real_estate_all",
        )
        session.add(run_record)
        await session.commit()
        await session.refresh(run_record)

    total_attempted = 0
    total_succeeded = 0
    total_failed = 0
    errors = []
    vrbo_count = 0
    tier3_escalation_count = 0
    blocked_count = 0
    anomalies_captured = 0

    vrbo_proxy_mode = False
    current_vrbo_proxy = None
    proxy_metrics = {p: {"success": 0, "failed": 0} for p in vrbo_proxies}

    file_logger = RunLogger(
        job_type=RunJobType.REAL_ESTATE_MONITOR.value,
        platform="real_estate_all",
        run_id=str(run_record.id),
        started_at=run_record.started_at,
    )

    try:
        vrbo_blocked = False
        airbnb_blocked = False

        for item in properties_input:
            property_label = item.get("name", item["url"])
            platform = item.get("platform", "airbnb")
            logger.info(f"Processing property: {property_label}")

            if platform == "vrbo" and vrbo_blocked:
                logger.info(f"Skipping {property_label} (Vrbo blocked for this run)")
                continue
            if platform == "airbnb" and airbnb_blocked:
                logger.info(f"Skipping {property_label} (Airbnb blocked for this run)")
                continue

            total_attempted += 1

            if platform == "vrbo":
                vrbo_count += 1
                room_id = extract_vrbo_property_id(item["url"])
                base_url = f"https://www.vrbo.com/{room_id}"
                scrape_url = build_vrbo_scrape_url(room_id, check_in_str, check_out_str)
            else:
                room_id = extract_room_id(item["url"])
                base_url = f"https://www.airbnb.com/rooms/{room_id}"
                scrape_url = build_scrape_url(room_id, check_in_str, check_out_str)

            # Idempotent upsert on url (unique key).
            async with AsyncSessionLocal() as session:
                stmt = insert(Property).values(
                    name=item.get("name", f"Property {room_id}"),
                    property_key=item.get("property_key"),
                    platform=platform,
                    url=base_url,
                    market=item.get("market", "Unknown"),
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["url"],
                    set_={
                        "name": stmt.excluded.name,
                        "platform": stmt.excluded.platform,
                        "property_key": stmt.excluded.property_key,
                        "market": stmt.excluded.market,
                    },
                ).returning(Property.id)
                res = await session.execute(stmt)
                property_id = res.scalar()
                await session.commit()

            max_retries = 1 if platform == "vrbo" else 0
            
            for attempt in range(max_retries + 1):
                try:
                    logger.info(f"Fetching URL (Attempt {attempt+1}): {scrape_url}")

                    # Random sleep 10 - 30s between properties (bot-avoidance)
                    sleep_sec = random.randint(10, 30)
                    logger.info(f"Sleeping {sleep_sec}s before request...")
                    await asyncio.sleep(sleep_sec)

                    html_bytes_ref = [None]
                    screenshot_bytes_ref = [None]
                    
                    async def capture_artifacts(page):
                        try:
                            await page.wait_for_load_state("networkidle", timeout=5000)
                        except:
                            pass
                        try:
                            html_bytes_ref[0] = (await page.content()).encode("utf-8")
                            screenshot_bytes_ref[0] = await page.screenshot(type="jpeg", full_page=True)
                        except Exception as e:
                            logger.error(f"Failed to capture artifacts: {e}")

                    kwargs = {
                        "headless": True,
                        "block_webrtc": True,
                        "google_search": True,
                        "timeout": 90_000,
                        "page_action": capture_artifacts,
                    }

                    if platform == "vrbo":
                        kwargs["load_dom"] = True
                        kwargs["wait"] = 10000
                        kwargs["block_images"] = True
                        if vrbo_proxy_mode and current_vrbo_proxy:
                            kwargs["proxy"] = current_vrbo_proxy
                    else:
                        kwargs["network_idle"] = True
                        kwargs["wait_selector"] = '[data-plugin-in-point-id="BOOK_IT_SIDEBAR"]'
                        kwargs["wait_selector_state"] = "attached"
                        kwargs["wait"] = 3000

                    response = await StealthyFetcher.async_fetch(scrape_url, **kwargs)
                    is_anomaly = False

                    if not response:
                        logger.error(f"Failed to fetch {scrape_url}")
                        is_anomaly = True
                        if attempt < max_retries:
                            continue
                        total_failed += 1
                        errors.append(f"Network failure: {scrape_url}")
                        break

                    if response.status in (429, 403):
                        logger.error(f"BLOCKED ({response.status}) on {scrape_url}")
                        is_anomaly = True
                        
                        if platform == "vrbo" and not vrbo_proxy_mode and vrbo_proxies:
                            logger.warning("First Vrbo block detected. Switching to proxy mode.")
                            vrbo_proxy_mode = True
                            current_vrbo_proxy = random.choice(vrbo_proxies)
                            # Let it retry this property with a proxy
                            continue
                        elif platform == "vrbo" and vrbo_proxy_mode and current_vrbo_proxy:
                            # Proxy failed
                            proxy_metrics[current_vrbo_proxy]["failed"] += 1
                            current_vrbo_proxy = random.choice(vrbo_proxies) # pick another for the next prop
                            vrbo_blocked = True # Blocked even on proxy, skip rest
                        else:
                            airbnb_blocked = True

                        errors.append(f"{platform.capitalize()} Blocked: {scrape_url}")
                        total_failed += 1
                        if platform == "vrbo":
                            blocked_count += 1
                        
                        file_logger.log_item({"url": scrape_url, "status": "blocked", "error": f"HTTP {response.status}"})
                        break

                    if response.status == 404:
                        logger.warning(f"NOT FOUND (404) on {scrape_url}. Skipping.")
                        errors.append(f"Not Found (404): {scrape_url}")
                        total_failed += 1
                        file_logger.log_item({"url": scrape_url, "status": "not_found", "error": "HTTP 404"})

                        async with AsyncSessionLocal() as session:
                            upd = Property.__table__.update().where(Property.id == property_id).values(consecutive_404s=Property.consecutive_404s + 1)
                            await session.execute(upd)
                            await session.commit()
                        break

                    if response.status != 200:
                        logger.warning(f"HTTP {response.status} on {scrape_url}. Skipping.")
                        errors.append(f"HTTP {response.status}: {scrape_url}")
                        total_failed += 1
                        file_logger.log_item({"url": scrape_url, "status": "failed", "error": f"HTTP {response.status}"})
                        break

                    # Successful fetch
                    if platform == "vrbo" and vrbo_proxy_mode and current_vrbo_proxy:
                        proxy_metrics[current_vrbo_proxy]["success"] += 1

                    async with AsyncSessionLocal() as session:
                        upd = Property.__table__.update().where(Property.id == property_id).values(consecutive_404s=0)
                        await session.execute(upd)
                        await session.commit()

                    if platform == "vrbo":
                        metadata = extract_vrbo_metadata(response)
                    else:
                        metadata = extract_metadata_from_json(response)
                    
                    non_null_metadata = {k: v for k, v in metadata.items() if v is not None}
                    if non_null_metadata:
                        logger.info(f"Updating metadata for {room_id}: {non_null_metadata}")
                        async with AsyncSessionLocal() as session:
                            upd_stmt = Property.__table__.update().where(Property.id == property_id).values(**non_null_metadata)
                            await session.execute(upd_stmt)
                            await session.commit()
                    else:
                        logger.warning(f"No metadata extracted for {room_id} this run (all fields None)")

                    if platform == "vrbo":
                        pricing_data = extract_vrbo_pricing(response)

                        if pricing_data["meta_data"].get("extraction_method") == "blocked":
                            is_anomaly = True
                            if not vrbo_proxy_mode and vrbo_proxies:
                                logger.warning("DataDome Block detected. Switching to proxy mode.")
                                vrbo_proxy_mode = True
                                current_vrbo_proxy = random.choice(vrbo_proxies)
                                continue # retry loop
                            elif vrbo_proxy_mode:
                                proxy_metrics[current_vrbo_proxy]["failed"] += 1
                                vrbo_blocked = True
                            
                            logger.error(f"DATADOME BLOCK DETECTED on {scrape_url}")
                            errors.append(f"Vrbo Blocked: {scrape_url}")
                            total_failed += 1
                            blocked_count += 1
                            file_logger.log_item({"url": scrape_url, "status": "blocked", "error": "DataDome block"})
                            break
                    else:
                        pricing_data = extract_pricing(response)

                    if pricing_data["meta_data"].get("extraction_method") == "tier3":
                        is_anomaly = True
                        tier3_escalation_count += 1

                    dlq_html_url, dlq_screenshot_url = None, None
                    if is_anomaly and html_bytes_ref[0] and screenshot_bytes_ref[0]:
                        dlq_html_url, dlq_screenshot_url = await upload_dlq_artifacts(
                            html_bytes_ref[0], screenshot_bytes_ref[0], str(property_id)
                        )
                        anomalies_captured += 1

                    logger.info(
                        f"Result for {check_in_str} | room={room_id} "
                        f"available={pricing_data['is_available']} "
                        f"nightly_rate={pricing_data['nightly_rate']} "
                        f"method={pricing_data['meta_data'].get('extraction_method', 'heuristic')}"
                    )

                    async with AsyncSessionLocal() as session:
                        history = RateHistory(
                            property_id=property_id,
                            stay_date=datetime(
                                check_in.year, check_in.month, check_in.day, tzinfo=ZoneInfo("UTC")
                            ),
                            nightly_rate=pricing_data.get("nightly_rate"),
                            is_available=pricing_data.get("is_available"),
                            meta_data=pricing_data.get("meta_data", {}),
                            dlq_html_url=dlq_html_url,
                            dlq_screenshot_url=dlq_screenshot_url
                        )
                        session.add(history)
                        await session.commit()

                    file_logger.log_item(
                        {
                            "url": scrape_url,
                            "status": "success",
                            "pricing": pricing_data,
                            "metadata": non_null_metadata,
                        }
                    )
                    total_succeeded += 1
                    
                    # Successfully fetched and processed, no need for retry
                    break

                except Exception as e:
                    logger.error(f"Error processing {property_label} on attempt {attempt+1}: {e}", exc_info=True)
                    if attempt < max_retries:
                        continue
                    errors.append(f"{property_label}: {e}")
                    total_failed += 1
                    file_logger.log_item({"url": item.get("url"), "status": "failed", "error": str(e)})

    except Exception as e:
        logger.error(f"Fatal error running real estate monitor: {e}", exc_info=True)
        errors.append(f"Fatal run error: {e}")

    finally:
        file_logger.close()

        async with AsyncSessionLocal() as session:
            is_fatal = any(e.startswith("Fatal run error") for e in errors) if errors else False
            final_status = RunStatus.FAILED if is_fatal else RunStatus.SUCCESS
            upd = (
                ScrapeRun.__table__.update()
                .where(ScrapeRun.id == run_record.id)
                .values(
                    status=final_status,
                    finished_at=datetime.now(ZoneInfo("UTC")),
                    items_attempted=total_attempted,
                    items_succeeded=total_succeeded,
                    items_failed=total_failed,
                    error_summary="; ".join(errors) if errors else None,
                    anomalies_captured=anomalies_captured,
                    meta_data={
                        "tier3_escalation_count": tier3_escalation_count,
                        "blocked_count": blocked_count,
                        "proxy_metrics": proxy_metrics
                    },
                )
            )
            await session.execute(upd)
            await session.commit()

    logger.info(
        f"Real Estate Monitor run completed — "
        f"attempted={total_attempted} succeeded={total_succeeded} failed={total_failed}"
    )


if __name__ == "__main__":
    asyncio.run(run_real_estate_monitor())
