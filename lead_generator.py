"""
Lead Generator — independently editable module.

TO ADD OR REMOVE TARGETS: edit the LEAD_TARGETS list below.
No API call, no migration required.

Run directly via GitHub Actions:
    python -c "import asyncio; from lead_generator import run_lead_gen; asyncio.run(run_lead_gen())"
"""

import re
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
import json

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db import AsyncSessionLocal, create_tables
from models import LeadTarget, Lead, ScrapeRun, RunJobType, RunStatus
from scrapling.fetchers import StealthyFetcher
from urllib.parse import urlparse
from engines.homepage_crawler import crawl_homepage
from schemas import ContactsSchema, SocialsSchema

from scrapling.engines.toolbelt.custom import Response
from observability import run_watchdog, RunLogger

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — edit this list to add, remove, or update lead sources.
# Changes take effect on the next GitHub Actions cron run.
#
# Fields:
#   url       Page to scrape for contact information
#   category  Optional tag for filtering in the dashboard (e.g. "saas", "ecom")
# ─────────────────────────────────────────────────────────────────────────────
LEAD_TARGETS: List[Dict[str, Any]] = json.load(open("lead_targets.json", "r"))

# ─────────────────────────────────────────────────────────────────────────────
# Regex patterns
# ─────────────────────────────────────────────────────────────────────────────

# Permissive but practical email regex
EMAIL_PATTERN: re.Pattern = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)
# Matches common US/international phone formats
PHONE_PATTERN: re.Pattern = re.compile(
    r"(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}"
)
# Common email false-positives to filter out
EMAIL_BLOCKLIST: set = {
    "example.",
    "yourcompany.",
    "@sentry.io",
    "noreply@",
    "no-reply@",
}


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────


async def _upsert_targets(db: AsyncSession) -> None:
    """
    Sync LEAD_TARGETS to the lead_targets table.

    Step 1 — Deactivate any existing rows whose URL is no longer in the config.
      Removing a URL from LEAD_TARGETS will set that target's is_active=False,
      stopping it from being scraped on the next run. Historical leads are
      preserved (lead_target row stays in DB, just inactive).

    Step 2 — Upsert the current config list.
      Uses INSERT ... ON CONFLICT DO UPDATE so:
        - New entries are inserted with is_active=True
        - Existing entries (matched by URL) have category refreshed
          and is_active reset to True

    Args:
        db (AsyncSession): The active database session.

    Raises:
        Exception: If the database operation fails, the transaction is rolled back and raised.
    """
    try:
        current_urls: List[str] = [t["url"] for t in LEAD_TARGETS]

        # Step 1: deactivate targets no longer in config
        if current_urls:
            deactivate_stmt = (
                update(LeadTarget)
                .where(LeadTarget.url.notin_(current_urls))
                .where(LeadTarget.is_active == True)
                .values(is_active=False)
            )
        else:
            # If the list is empty, deactivate all active targets
            deactivate_stmt = (
                update(LeadTarget)
                .where(LeadTarget.is_active == True)
                .values(is_active=False)
            )

        result = await db.execute(deactivate_stmt)
        logger.info(
            f"[LeadGen] Deactivated {result.rowcount} targets removed from config."
        )

        # Step 2: upsert current config
        for t in LEAD_TARGETS:
            stmt = (
                pg_insert(LeadTarget)
                .values(
                    url=t["url"],
                    category=t.get("category"),
                    is_active=True,
                )
                .on_conflict_do_update(
                    index_elements=["url"],
                    set_={
                        "category": t.get("category"),
                        "is_active": True,
                    },
                )
            )
            await db.execute(stmt)

        await db.commit()
        logger.info(f"[LeadGen] Upserted {len(LEAD_TARGETS)} targets from config.")
    except Exception as e:
        logger.error(
            "[LeadGen] Failed to upsert targets config to database.", exc_info=True
        )
        await db.rollback()
        raise


def _extract_contacts(response: Response, target_domain: str) -> tuple[Dict, Dict]:
    """
    Extract contact methods and social links from the page using HTML/CSS parsing and regex fallback.

    Args:
        response (Response): The Scrapling response object.
        target_domain (str): The domain of the target to match emails against.

    Returns:
        tuple[Dict, Dict]: (contacts_dict, socials_dict) validated by Pydantic.
    """
    tels = response.css('a[href^="tel:"]::attr(href)').getall()
    mailtos = response.css('a[href^="mailto:"]::attr(href)').getall()
    all_links = response.css('a::attr(href)').getall()
    
    # Text fallback for emails without mailto
    all_text = ""
    try:
        all_text = str(response.get_all_text(strip=True, ignore_tags=("script", "style", "noscript")))
    except Exception:
        pass

    phone_list = [t.replace('tel:', '').strip() for t in tels]
    
    email_list = []
    email_with_domain_list = []

    for m in mailtos:
        clean_email = m.replace('mailto:', '').split('?')[0].strip()
        if not clean_email:
            continue
        if target_domain in clean_email.split('@')[-1]:
            email_with_domain_list.append(clean_email)
        else:
            email_list.append(clean_email)

    raw_emails = set(EMAIL_PATTERN.findall(all_text))
    for e in raw_emails:
        if not any(fp in e for fp in EMAIL_BLOCKLIST):
            if target_domain in e.split('@')[-1]:
                email_with_domain_list.append(e)

    socials = {
        "X(twitter)": [],
        "Facebook": [],
        "Whatsapp": [],
        "Instagram": [],
        "linkedIn": []
    }

    for link in all_links:
        l = link.lower()
        if "twitter.com" in l or "x.com" in l:
            socials["X(twitter)"].append(link)
        elif "facebook.com" in l:
            socials["Facebook"].append(link)
        elif "wa.me" in l or "api.whatsapp.com" in l:
            socials["Whatsapp"].append(link)
        elif "instagram.com" in l:
            socials["Instagram"].append(link)
        elif "linkedin.com" in l:
            socials["linkedIn"].append(link)

    c_schema = ContactsSchema(
        phone=list(set(phone_list)),
        email=list(set(email_list)),
        emailWithDomain=list(set(email_with_domain_list))
    )
    
    s_schema = SocialsSchema(
        **{k: list(set(v)) for k, v in socials.items()}
    )

    return c_schema.model_dump(by_alias=True), s_schema.model_dump(by_alias=True)


async def _save_leads(
    db: AsyncSession,
    target: LeadTarget,
    contacts: dict,
    socials: dict,
    source_url: str,
    company_name: Optional[str] = None,
) -> int:
    """
    Upsert leads into the DB using JSONB arrays.
    Uses INSERT ... ON CONFLICT DO UPDATE to merge new JSON arrays for the page.

    Args:
        db (AsyncSession): The active database session.
        target (LeadTarget): The target from which leads were extracted.
        contacts (dict): Validated contacts dictionary.
        socials (dict): Validated socials dictionary.
        source_url (str): The URL where the leads were found.
        company_name (Optional[str]): The extracted company name (e.g. from <title>).

    Returns:
        int: Count of newly inserted rows (0 or 1).
    """
    try:
        stmt = (
            pg_insert(Lead)
            .values(
                target_id=target.id,
                source_url=source_url,
                company_name=company_name,
                contacts=contacts,
                socials=socials,
            )
            .on_conflict_do_update(
                index_elements=["target_id", "source_url"],
                set_={
                    "contacts": contacts,
                    "socials": socials,
                    "company_name": company_name,
                }
            )
        )
        result = await db.execute(stmt)
        await db.commit()
        return result.rowcount
    except Exception as e:
        logger.error(
            f"[LeadGen] Failed to save leads for target {target.url}", exc_info=True
        )
        await db.rollback()
        raise


async def scrape_target(target: LeadTarget) -> bool:
    """
    Run the 2-tier pipeline for a single lead target URL.

    Opens its own isolated AsyncSession for the DB write so that a save
    failure and rollback cannot expire ORM objects held by the caller's
    read session (which would cause MissingGreenlet on the next iteration).

    Args:
        target (LeadTarget): The lead target to scrape. Must be detached
                             (expunged) from any session before being passed
                             here so column attribute access does not trigger
                             a lazy-load on a closed/expired session.
                             
    Returns:
        bool: True if extraction succeeded without fatal error, False otherwise.
    """
    url: str = target.url

    if urlparse(url).path in ("", "/"):
        # ── Homepage path: delegate entirely to Spider engine ──
        res = await crawl_homepage(url=url, target=target)
        return res, {"method": "spider"}

    # ── Existing dedicated-page path: unchanged below ──

    try:
        response = await StealthyFetcher.async_fetch(
            url,
            headless=True,
            network_idle=True,
            block_webrtc=True,
            google_search=True,
            timeout=60_000,
            wait=3000,
        )
    except Exception as e:
        logger.error(f"[LeadGen] Fetch exception for {url}: {e}", exc_info=True)
        response = None

    if response is None:
        logger.error(f"[LeadGen] Browser fetch failed completely for {url}")
        return False, {"error": "All tiers failed"}

    target_domain = urlparse(target.url).netloc.replace("www.", "")
    try:
        contacts, socials = _extract_contacts(response, target_domain=target_domain)
    except Exception as e:
        logger.error(
            f"[LeadGen] Failed to extract contacts/socials from response for {url}",
            exc_info=True,
        )
        return False, {"error": "Failed to extract contacts/socials"}

    if not contacts["email"] and not contacts["emailWithDomain"] and not contacts["phone"]:
        logger.info(f"[LeadGen] No contacts found at {url}")
        return True, {"leads_saved": 0, "status": "no contacts found"}

    # Attempt to pull company name from <title>
    company_name: Optional[str] = None
    try:
        title_el: Optional[str] = response.css("title::text").get()
        if title_el:
            company_name = title_el.strip()[:200]
    except Exception as e:
        logger.warning(f"[LeadGen] Error extracting title for {url}", exc_info=True)

    # Each target gets its own isolated write session.
    # This prevents a rollback here from expiring ORM objects in the caller's
    # read session, which would cause MissingGreenlet on the next iteration.
    try:
        async with AsyncSessionLocal() as save_db:
            count: int = await _save_leads(
                save_db, target, contacts, socials, source_url=url, company_name=company_name
            )
        logger.info(f"[LeadGen] {url} -> {count} new leads saved")
        return True, {"leads_saved": count, "company": company_name}
    except Exception as e:
        # Error is logged inside _save_leads
        return False, {"error": str(e)}


async def run_lead_gen() -> None:
    """
    Entry point for GitHub Actions.

    Steps:
      1. Ensure DB tables exist (idempotent)
      2. Upsert LEAD_TARGETS config into the lead_targets table
         (also deactivates targets removed from config)
      3. Scrape all active targets sequentially
    """
    logging.basicConfig(level=logging.INFO)
    logger.info("[LeadGen] Starting lead generation run")

    try:
        await create_tables()
    except Exception as e:
        logger.error("[LeadGen] Failed to create tables on startup", exc_info=True)
        return

    # Phase 1: upsert config + read active targets in one short-lived session.
    # expunge_all() detaches the ORM objects so their column attributes remain
    # accessible after the session closes, without any lazy-load risk.
    targets: List[LeadTarget] = []
    run_id = None

    async with AsyncSessionLocal() as db:
        await run_watchdog(db, RunJobType.LEAD_GEN.value, max_duration_hours=4)
        
        # Create run record
        run_record = ScrapeRun(
            job_type=RunJobType.LEAD_GEN,
            status=RunStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
            platform="lead_gen"
        )
        db.add(run_record)
        await db.commit()
        await db.refresh(run_record)
        run_id = run_record.id
        started_at = run_record.started_at

        try:
            await _upsert_targets(db)
        except Exception as e:
            logger.error("[LeadGen] Aborting run due to target upsert failure", exc_info=True)
            run_record.status = RunStatus.FAILED
            run_record.finished_at = datetime.now(timezone.utc)
            run_record.error_summary = str(e)[:500]
            await db.commit()
            return

        try:
            result = await db.execute(
                select(LeadTarget).where(LeadTarget.is_active == True)
            )
            targets = list(result.scalars().all())
            logger.info(f"[LeadGen] {len(targets)} active targets")
            # Detach all objects so they outlive this session safely.
            db.expunge_all()
        except Exception as e:
            logger.error("[LeadGen] Failed to query active targets", exc_info=True)
            run_record.status = RunStatus.FAILED
            run_record.finished_at = datetime.now(timezone.utc)
            run_record.error_summary = str(e)[:500]
            await db.commit()
            return
    # Session is now closed; targets are detached (column values still accessible).

    items_attempted = len(targets)
    items_succeeded = 0
    items_failed = 0
    error_summary = None

    file_logger = RunLogger(
        job_type=RunJobType.LEAD_GEN.value,
        platform="lead_gen",
        run_id=str(run_id),
        started_at=started_at
    )

    try:
        for target in targets:
            target_url: str = target.url
            try:
                success, details = await scrape_target(target)
                if success:
                    items_succeeded += 1
                    file_logger.log_item({"url": target_url, "status": "success", **details})
                else:
                    items_failed += 1
                    file_logger.log_item({"url": target_url, "status": "failed", **details})
            except Exception as e:
                logger.error(
                    f"[LeadGen] Unhandled error processing target {target_url}",
                    exc_info=True,
                )
                items_failed += 1
                if not error_summary:
                    error_summary = str(e)[:500]
                file_logger.log_item({"url": target_url, "status": "error", "error": str(e)})

    except Exception as e:
        logger.error(f"[LeadGen] Fatal loop error: {e}", exc_info=True)
        error_summary = f"Fatal error: {e}"[:500]
        
    finally:
        file_logger.close()
        
        # Phase 3: Update run record status
        async with AsyncSessionLocal() as db:
            status = RunStatus.SUCCESS if items_failed < items_attempted or items_attempted == 0 else RunStatus.FAILED
            if error_summary and status == RunStatus.SUCCESS:
                status = RunStatus.FAILED # If there was a fatal unhandled error, mark failed
                
            await db.execute(
                update(ScrapeRun)
                .where(ScrapeRun.id == run_id)
                .values(
                    status=status,
                    finished_at=datetime.now(timezone.utc),
                    items_attempted=items_attempted,
                    items_succeeded=items_succeeded,
                    items_failed=items_failed,
                    error_summary=error_summary,
                )
            )
            await db.commit()

    logger.info("[LeadGen] Lead generation run complete")
