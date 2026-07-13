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
from engines.tier1 import fetch_tier1
from engines.tier2 import fetch_tier2

from scrapling.engines.toolbelt.custom import Response

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


def _extract_contacts(text: str) -> Dict[str, List[str]]:
    """
    Extract unique emails and phones from raw text.

    Args:
        text (str): The combined raw text and HTML of the page.

    Returns:
        Dict[str, List[str]]: A dictionary with 'emails' and 'phones' lists.
    """
    emails: List[str] = [
        e
        for e in set(EMAIL_PATTERN.findall(text))
        if not any(fp in e for fp in EMAIL_BLOCKLIST)
    ]
    phones: List[str] = list(set(PHONE_PATTERN.findall(text)))
    return {"emails": emails, "phones": phones}


async def _save_leads(
    db: AsyncSession,
    target: LeadTarget,
    contacts: Dict[str, List[str]],
    source_url: str,
    company_name: Optional[str] = None,
) -> int:
    """
    Upsert leads into the DB.
    Uses INSERT ... ON CONFLICT DO NOTHING to gracefully handle duplicates.

    Note: created_at is set automatically by server_default=func.now() on Base.

    Args:
        db (AsyncSession): The active database session.
        target (LeadTarget): The target from which leads were extracted.
        contacts (Dict[str, List[str]]): Extracted emails and phones.
        source_url (str): The URL where the leads were found.
        company_name (Optional[str]): The extracted company name (e.g. from <title>).

    Returns:
        int: Count of newly inserted rows.

    Raises:
        Exception: If the database operations fail, rolls back the transaction and raises.
    """
    try:
        inserted: int = 0
        first_phone: Optional[str] = (
            contacts["phones"][0] if contacts["phones"] else None
        )

        for email in contacts.get("emails", []):
            stmt = (
                pg_insert(Lead)
                .values(
                    target_id=target.id,
                    email=email,
                    phone=first_phone,
                    company_name=company_name,
                    source_url=source_url,
                )
                .on_conflict_do_nothing(index_elements=["target_id", "email"])
            )
            result = await db.execute(stmt)
            inserted += result.rowcount

        await db.commit()
        return inserted
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

    # Network operations inside Tier 1 handle their own try/excepts and return None on failure
    response: Optional[Response] = fetch_tier1(url)

    # Determine if escalation is needed
    try:
        text_preview: str = (
            str(response.get_all_text(strip=True))[:500].lower() if response else ""
        )
    except Exception as e:
        logger.warning(
            f"[LeadGen] Error parsing text preview for {url}: {e}", exc_info=True
        )
        text_preview = ""

    bot_signals: List[str] = ["captcha", "access denied", "cloudflare", "just a moment"]
    needs_escalation: bool = (
        response is None
        or len(text_preview) < 100
        or any(sig in text_preview for sig in bot_signals)
    )

    if needs_escalation:
        logger.info(f"[LeadGen] Escalating to Tier 2 for {url}")
        # Network operations inside Tier 2 handle their own try/excepts and return None on failure
        response = await fetch_tier2(url)

    if response is None:
        logger.error(f"[LeadGen] All tiers failed for {url}")
        return False

    # Use get_all_text() for visible text + html_content for mailto: links
    try:
        all_text: str = str(
            response.get_all_text(
                strip=True, ignore_tags=("script", "style", "noscript")
            )
        )
        html: str = str(response.html_content)
        combined_text: str = all_text + "\n" + html
        contacts: Dict[str, List[str]] = _extract_contacts(combined_text)
    except Exception as e:
        logger.error(
            f"[LeadGen] Failed to extract text/HTML from response for {url}",
            exc_info=True,
        )
        return False

    if not contacts["emails"] and not contacts["phones"]:
        logger.info(f"[LeadGen] No contacts found at {url}")
        return True

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
                save_db, target, contacts, source_url=url, company_name=company_name
            )
        logger.info(f"[LeadGen] {url} -> {count} new leads saved")
        return True
    except Exception:
        # Error is logged inside _save_leads
        return False


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
        # Create run record
        run_record = ScrapeRun(
            job_type=RunJobType.LEAD_GEN,
            status=RunStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run_record)
        await db.commit()
        await db.refresh(run_record)
        run_id = run_record.id

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

    # Phase 2: scrape each target with its own isolated write session.
    items_attempted = len(targets)
    items_succeeded = 0
    items_failed = 0
    error_summary = None

    for target in targets:
        target_url: str = target.url  # capture before any potential expiry
        try:
            success = await scrape_target(target)
            if success:
                items_succeeded += 1
            else:
                items_failed += 1
        except Exception as e:
            logger.error(
                f"[LeadGen] Unhandled error processing target {target_url}",
                exc_info=True,
            )
            items_failed += 1
            if not error_summary:
                error_summary = str(e)[:500]
                
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
