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
from models import Property, RateHistory, ScrapeRun, RunJobType, RunStatus
from scrapling.fetchers import StealthyFetcher
from engines.real_estate_extractors import extract_metadata_from_json, extract_pricing
from engines.vrbo_extractors import (
    extract_vrbo_property_id,
    extract_vrbo_metadata,
    extract_vrbo_pricing,
)
from observability import run_watchdog, RunLogger

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
    # One request per property per run. check_out = today + 2 to clear
    # any minimum-stay requirements without fetching stale date ranges.
    today = datetime.now(ZoneInfo("America/New_York")).date()
    check_in = today
    check_out = today + timedelta(days=2)
    check_in_str = check_in.strftime("%Y-%m-%d")
    check_out_str = check_out.strftime("%Y-%m-%d")
    logger.info(f"Date window for this run: {check_in_str} → {check_out_str}")
    # ─────────────────────────────────────────────────────────────────────────

    # Initialize ScrapeRun log
    async with AsyncSessionLocal() as session:
        # 1. Watchdog cleanup of any stale runs before we start
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

    file_logger = RunLogger(
        job_type=RunJobType.REAL_ESTATE_MONITOR.value,
        platform="real_estate_all",
        run_id=str(run_record.id),
        started_at=run_record.started_at,
    )

    try:
        for item in properties_input:
            property_label = item.get("name", item["url"])
            logger.info(f"Processing property: {property_label}")
            total_attempted += 1
            try:
                platform = item.get("platform", "airbnb")

                if platform == "vrbo":
                    if vrbo_count >= 15:
                        logger.info(
                            f"Skipping {property_label} (Vrbo test limit of 15 reached)"
                        )
                        continue
                    vrbo_count += 1
                    room_id = extract_vrbo_property_id(item["url"])
                    base_url = f"https://www.vrbo.com/{room_id}"
                else:
                    room_id = extract_room_id(item["url"])
                    base_url = f"https://www.airbnb.com/rooms/{room_id}"

                # Idempotent upsert on url (unique key).
                # On conflict: refresh all seed fields from JSON so corrections
                # in properties_to_track.json are picked up on the next run.
                async with AsyncSessionLocal() as session:
                    stmt = insert(Property).values(
                        name=item.get("name", f"Property {room_id}"),
                        property_key=item.get("property_key"),
                        platform=item.get("platform", "airbnb"),
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

                if platform == "vrbo":
                    scrape_url = build_vrbo_scrape_url(
                        room_id, check_in_str, check_out_str
                    )
                else:
                    scrape_url = build_scrape_url(room_id, check_in_str, check_out_str)
                logger.info(f"Fetching URL: {scrape_url}")

                # Random sleep 5-15s between properties (bot-avoidance)
                sleep_sec = random.randint(5, 15)
                logger.info(f"Sleeping {sleep_sec}s before request...")
                await asyncio.sleep(sleep_sec)

                if platform == "vrbo":
                    response = await StealthyFetcher.async_fetch(
                        scrape_url,
                        headless=True,
                        load_dom=True,  # Telemetry polling prevents network_idle
                        block_webrtc=True,
                        google_search=True,
                        wait=10000,
                        timeout=90_000,
                    )
                else:
                    response = await StealthyFetcher.async_fetch(
                        scrape_url,
                        headless=True,
                        network_idle=True,
                        block_webrtc=True,
                        google_search=True,
                        wait_selector='[data-plugin-in-point-id="BOOK_IT_SIDEBAR"]',
                        wait_selector_state="attached",
                        wait=3000,  # extra 3 s post-render hold for React hydration
                        timeout=90_000,
                    )

                if not response:
                    logger.error(f"Failed to fetch {scrape_url}")
                    total_failed += 1
                    errors.append(f"Network failure: {scrape_url}")
                    continue

                # Coalesce-style metadata update: only write non-None values so a
                # field successfully captured in a previous run is never overwritten
                # by a None from a failed extraction in the current run.
                # Fields that had a good value keep it; new non-None values fill gaps.
                if platform == "vrbo":
                    metadata = extract_vrbo_metadata(response)
                else:
                    metadata = extract_metadata_from_json(response)
                non_null_metadata = {k: v for k, v in metadata.items() if v is not None}
                if non_null_metadata:
                    logger.info(f"Updating metadata for {room_id}: {non_null_metadata}")
                    async with AsyncSessionLocal() as session:
                        upd_stmt = (
                            Property.__table__.update()
                            .where(Property.id == property_id)
                            .values(**non_null_metadata)
                        )
                        await session.execute(upd_stmt)
                        await session.commit()
                else:
                    logger.warning(
                        f"No metadata extracted for {room_id} this run (all fields None)"
                    )

                # Extract pricing and availability
                if platform == "vrbo":
                    pricing_data = extract_vrbo_pricing(response)

                    # Circuit breaker for DataDome blocks
                    if pricing_data["meta_data"].get("extraction_method") == "blocked":
                        logger.error(
                            f"DATADOME BLOCK DETECTED on {scrape_url}. Pausing further Vrbo scraping."
                        )
                        errors.append(f"Vrbo Blocked: {scrape_url}")
                        total_failed += 1
                        blocked_count += 1

                        file_logger.log_item(
                            {
                                "url": scrape_url,
                                "status": "blocked",
                                "error": "DataDome block",
                            }
                        )
                        break  # Stop processing further properties in this run
                else:
                    pricing_data = extract_pricing(response)

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
                            check_in.year,
                            check_in.month,
                            check_in.day,
                            tzinfo=ZoneInfo("UTC"),
                        ),
                        nightly_rate=pricing_data["nightly_rate"],
                        is_available=pricing_data["is_available"],
                        meta_data=pricing_data["meta_data"],
                    )
                    session.add(history)
                    await session.commit()

                if pricing_data["meta_data"].get("extraction_method") == "tier3":
                    tier3_escalation_count += 1

                file_logger.log_item(
                    {
                        "url": scrape_url,
                        "status": "success",
                        "pricing": pricing_data,
                        "metadata": non_null_metadata,
                    }
                )
                total_succeeded += 1

            except Exception as e:
                logger.error(f"Error processing {property_label}: {e}", exc_info=True)
                errors.append(f"{property_label}: {e}")
                total_failed += 1
                file_logger.log_item(
                    {"url": item.get("url"), "status": "failed", "error": str(e)}
                )

    except Exception as e:
        logger.error(f"Fatal error running real estate monitor: {e}", exc_info=True)
        errors.append(f"Fatal run error: {e}")

    finally:
        # Update run record
        file_logger.close()

        async with AsyncSessionLocal() as session:
            final_status = (
                RunStatus.SUCCESS
                if total_failed == 0 and not errors
                else RunStatus.FAILED
            )
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
                    meta_data={
                        "tier3_escalation_count": tier3_escalation_count,
                        "blocked_count": blocked_count,
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
