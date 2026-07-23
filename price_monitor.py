"""
Price Monitor — independently editable module.

TO ADD OR REMOVE PRODUCTS: edit products_to_track.json (no migration required).

Run directly via GitHub Actions:
    python -c "import asyncio; from price_monitor import run_monitor; asyncio.run(run_monitor())"
"""

import logging
import asyncio
import random
from typing import List, Dict, Any, Optional, Set
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db import AsyncSessionLocal, create_tables
from models import (
    Product,
    PriceHistory,
    PriceAlert,
    AlertType,
    ScrapeRun,
    RunJobType,
    RunStatus,
)
from scrapling.fetchers import StealthyFetcher
from engines.tier3 import extract_price_tier3
from engines.ecommerce_extractors import extract_for_retailer, infer_retailer_from_url
import json
from observability import run_watchdog, RunLogger

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — edit this list to add, remove, or update tracked products.
# Changes take effect on the next GitHub Actions cron run.
#
# Fields:
#   name         Human-readable label (stored in DB, shown in alerts)
#   url          Product page URL to scrape
#   target_price (optional) Fire a "threshold" alert when price drops to/below
#                this value. Set to None to disable threshold alerting.
# ─────────────────────────────────────────────────────────────────────────────


PRODUCTS_TO_TRACK: List[Dict[str, Any]] = json.load(open("products_to_track.json", "r"))


# Minimum % change from the prior reading to fire a "change" alert.
# 1.0 = 1%; protects against floating-point noise between readings.
PRICE_CHANGE_THRESHOLD_PCT: float = 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────


async def _upsert_products(db: AsyncSession) -> None:
    """
    Sync PRODUCTS_TO_TRACK to the products table.

    Step 1 — Deactivate any existing rows whose URL is no longer in the config.
    Step 2 — Upsert the current config list, inferring retailer from the URL domain
             when not explicitly provided in the JSON config.

    Retailer backfill:
      Existing rows whose retailer column still holds the DB default 'Unknown'
      are updated in this same step by the upsert's set_ clause, which always
      writes the freshly inferred retailer value.

    Args:
        db (AsyncSession): The active database session.

    Raises:
        Exception: If the database operation fails, the transaction is rolled back and raised.
    """
    try:
        current_urls: List[str] = [p["url"] for p in PRODUCTS_TO_TRACK]

        # Step 1: deactivate products no longer in config
        if current_urls:
            deactivate_stmt = (
                update(Product)
                .where(Product.url.notin_(current_urls))
                .where(Product.is_active == True)
                .values(is_active=False)
            )
        else:
            deactivate_stmt = (
                update(Product).where(Product.is_active == True).values(is_active=False)
            )

        result = await db.execute(deactivate_stmt)
        logger.info(
            f"[Monitor] Deactivated {result.rowcount} products removed from config."
        )

        # Step 2: upsert current config
        for p in PRODUCTS_TO_TRACK:
            # Retailer: explicit field wins; otherwise infer from URL domain.
            retailer: str = (
                p.get("retailer")
                or infer_retailer_from_url(p["url"])
            )
            stmt = (
                pg_insert(Product)
                .values(
                    name=p["name"],
                    url=p["url"],
                    sku=p.get("sku"),
                    category=p.get("category"),
                    retailer=retailer,
                    target_price=p.get("target_price"),
                    is_active=True,
                )
                .on_conflict_do_update(
                    index_elements=["url"],
                    set_={
                        "name": p["name"],
                        "sku": p.get("sku"),
                        "category": p.get("category"),
                        # Always write the inferred retailer — this also
                        # back-fills the 'Unknown' default on existing rows.
                        "retailer": retailer,
                        "target_price": p.get("target_price"),
                        "is_active": True,
                    },
                )
            )
            await db.execute(stmt)

        await db.commit()
        logger.info(
            f"[Monitor] Upserted {len(PRODUCTS_TO_TRACK)} products from config."
        )
    except Exception as e:
        logger.error(
            "[Monitor] Failed to upsert products config to database.", exc_info=True
        )
        await db.rollback()
        raise


def _is_thin_response(response: Any) -> bool:
    """
    Heuristic: decide whether to escalate to Tier 2.
    A response is considered 'thin' if:
      - It is None (fetch failed)
      - Its visible text is very short (< 200 chars)
      - It contains known bot-wall signatures

    Args:
        response: A Scrapling Response object or None.

    Returns:
        bool: True if the response is thin and requires escalation, False otherwise.
    """
    if response is None:
        return True

    try:
        text: str = str(response.get_all_text(strip=True))
        if len(text) < 200:
            return True
        bot_signals: List[str] = [
            "captcha",
            "access denied",
            "robot",
            "cloudflare",
            "just a moment",
        ]
        return any(sig in text.lower() for sig in bot_signals)
    except Exception as e:
        logger.error(
            f"[Monitor] Error evaluating if response is thin: {e}", exc_info=True
        )
        # If we can't parse text, safely assume it's thin and escalate
        return True


async def _save_price(
    db: AsyncSession,
    product: Product,
    price: Optional[float],
    currency: str,
    tier_used: int,
    in_stock: bool = True,
    merchant: Optional[str] = None,
    meta_data: Optional[dict] = None,
) -> None:
    """
    Persist a price/stock reading and fire any applicable alerts.

    All five PriceHistory fields are written atomically:
      price, currency, tier_used, in_stock, merchant, meta_data.

    Alert logic (only fires when price is not None and product is in stock):
      1. Query the most recent prior price_history row BEFORE inserting.
      2. Insert the new PriceHistory record.
      3. Threshold alert: if product.target_price is set and new price <=
         target_price, fire a threshold alert.
      4. Change-detection alert: if a prior price exists and the absolute %
         change >= PRICE_CHANGE_THRESHOLD_PCT, fire a change alert.

    Args:
        db         : Active database session.
        product    : The product being monitored (detached from any session).
        price      : Scraped price (float) or None for OOS / unresolvable.
        currency   : ISO-4217 string, e.g. "USD".
        tier_used  : 1–4, whichever tier produced the result.
        in_stock   : False when the product is confirmed OOS.
        merchant   : Seller string, e.g. "Ships from: Amazon / Sold by: AnkerDirect".
        meta_data  : Arbitrary JSON blob (condition, coupon, ships_from, etc.).

    Raises:
        Exception: Rolls back and re-raises on any DB failure.
    """
    try:
        # 1. Fetch prior price before inserting (for change-detection alert).
        #    Only meaningful when a price was actually found.
        previous_price: Optional[float] = None
        if price is not None:
            prior_result = await db.execute(
                select(PriceHistory.price)
                .where(PriceHistory.product_id == product.id)
                .where(PriceHistory.price.isnot(None))  # ignore prior OOS nulls
                .order_by(desc(PriceHistory.created_at))
                .limit(1)
            )
            _raw_prev = prior_result.scalar_one_or_none()
            previous_price = float(_raw_prev) if _raw_prev is not None else None

        # 2. Insert new price record with full field set.
        record = PriceHistory(
            product_id=product.id,
            price=price,
            currency=currency,
            tier_used=tier_used,
            in_stock=in_stock,
            merchant=merchant,
            meta_data=meta_data or {},
        )
        db.add(record)

        # 3. Threshold alert — only when we have a real price and product is live.
        if price is not None and in_stock and product.target_price is not None:
            target_price_f: float = float(product.target_price)
            if price <= target_price_f:
                threshold_alert = PriceAlert(
                    product_id=product.id,
                    alert_type=AlertType.THRESHOLD,
                    price_at_alert=price,
                    target_price=product.target_price,
                    previous_price=None,
                    pct_change=None,
                )
                db.add(threshold_alert)
                logger.info(
                    f"[Alert/threshold] '{product.name}' at {currency} {price:.2f} "
                    f"(target: {target_price_f})"
                )

        # 4. Change-detection alert — requires both old and new numeric prices.
        if price is not None and previous_price is not None:
            raw_pct: float = (price - previous_price) / previous_price * 100.0
            if abs(raw_pct) >= PRICE_CHANGE_THRESHOLD_PCT:
                change_alert = PriceAlert(
                    product_id=product.id,
                    alert_type=AlertType.CHANGE,
                    price_at_alert=price,
                    target_price=None,
                    previous_price=previous_price,
                    pct_change=round(raw_pct, 2),  # signed: negative = price fell
                )
                db.add(change_alert)
                direction: str = "fell" if raw_pct < 0 else "rose"
                logger.info(
                    f"[Alert/change] '{product.name}' {direction} "
                    f"{abs(raw_pct):.1f}% to {currency} {price:.2f} "
                    f"(was {previous_price:.2f})"
                )

        await db.commit()
    except Exception as e:
        logger.error(
            f"[Monitor] Failed to save price for product {product.url}", exc_info=True
        )
        await db.rollback()
        raise


async def scrape_product(
    product: Product,
    blocked_retailers: Optional[Set[str]] = None,
) -> tuple[bool, dict]:
    """
    Full extraction pipeline for a single product URL.

    Pipeline:
      Tier 0  ->  HTTP status gate (404 -> not_found; 429/403 -> blocked).
                  Blocked / not-found products do NOT get a PriceHistory row.
      Tier 1  ->  curl_cffi HTTP fetch + retailer-specific DOM/JSON extraction.
      Tier 2  ->  StealthyFetcher browser (only if Tier 1 response is thin).
      Tier 3  ->  retailer-specific state classification (no-offers, variant).
      Tier 4  ->  Groq LLM regex fallback (only if price still None and no
                  terminal state was classified).

    Retailer routing:
      product.retailer is used (populated by _upsert_products via URL inference).
      Per-retailer blocking: if a 429/403 is received for a given retailer, the
      caller's blocked_retailers set is updated and subsequent products for that
      retailer are skipped for this run.

    Opens its own isolated AsyncSession for DB writes to prevent MissingGreenlet
    errors (see prior docstring for details).

    Args:
        product          : Detached Product ORM object.
        blocked_retailers: Mutable set of retailer slugs currently blocked this run.

    Returns:
        (success: bool, details: dict)
    """
    if blocked_retailers is None:
        blocked_retailers = set()

    url: str = product.url
    retailer: str = getattr(product, "retailer", None) or infer_retailer_from_url(url)

    # -----------------------------------------------------------------------
    # Per-retailer blocking guard (skip if already blocked this run)
    # -----------------------------------------------------------------------
    if retailer in blocked_retailers:
        logger.info(f"[Monitor] Skipping {url} — retailer '{retailer}' blocked this run")
        return False, {"state": "retailer_blocked", "retailer": retailer}

    # -----------------------------------------------------------------------
    # Network fetch (StealthyFetcher only)
    # -----------------------------------------------------------------------
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
        logger.error(f"[Monitor] Fetch exception for {url}: {e}", exc_info=True)
        response = None

    if response is None:
        logger.error(f"[Monitor] Network failure for {url} — blocking retailer '{retailer}' for this run")
        blocked_retailers.add(retailer)
        return False, {"state": "network_failure", "error": "Browser fetch failed completely"}

    # -----------------------------------------------------------------------
    # Tier 0 — HTTP status gate
    # -----------------------------------------------------------------------
    status_code = getattr(response, "status", None)
    if status_code in (429, 403):
        logger.warning(
            f"[Monitor] Tier-0: {status_code} for {url} — "
            f"blocking retailer '{retailer}' for this run"
        )
        blocked_retailers.add(retailer)
        return False, {
            "state": "blocked",
            "status_code": status_code,
            "retailer": retailer,
        }
    if status_code == 404:
        logger.warning(f"[Monitor] Tier-0: 404 for {url} — not_found, skipping")
        return False, {"state": "not_found", "status_code": 404}

    # -----------------------------------------------------------------------
    # Retailer-specific extraction (Tiers 1-4 delegated to extractor)
    # -----------------------------------------------------------------------
    extraction = extract_for_retailer(retailer, response, product_hint=product.name)

    price: Optional[float] = extraction["price"]
    currency: str = extraction["currency"]
    in_stock: bool = extraction["in_stock"]
    merchant: Optional[str] = extraction["merchant"]
    meta_data: dict = extraction["meta_data"]
    tier_used: int = extraction["tier_used"]
    state: str = extraction["state"]

    # Network tier tracking is removed. tier_used 1 = DOM, 2 = LLM.

    logger.info(
        f"[Monitor] {product.name} | retailer={retailer} | "
        f"price={currency} {price} | in_stock={in_stock} | "
        f"tier={tier_used} | state={state}"
    )

    # Terminal states: no DB write, return failure to caller
    if state in ("no_featured_offers", "variant_required", "parse_error"):
        return False, {"state": state, "tier": tier_used, "retailer": retailer}

    # -----------------------------------------------------------------------
    # Persist — isolated write session (prevents MissingGreenlet on caller)
    # -----------------------------------------------------------------------
    try:
        async with AsyncSessionLocal() as save_db:
            await _save_price(
                save_db,
                product,
                price=price,
                currency=currency,
                tier_used=tier_used,
                in_stock=in_stock,
                merchant=merchant,
                meta_data=meta_data,
            )
        return True, {
            "price": price,
            "currency": currency,
            "in_stock": in_stock,
            "merchant": merchant,
            "tier": tier_used,
            "state": state,
            "retailer": retailer,
        }
    except Exception as e:
        return False, {"state": "db_error", "error": str(e), "retailer": retailer}


async def run_monitor() -> None:
    """
    Entry point for GitHub Actions.

    Steps:
      1. Ensure DB tables exist (idempotent).
      2. Watchdog: mark stale RUNNING runs as FAILED.
      3. Create a new ScrapeRun record.
      4. Upsert PRODUCTS_TO_TRACK into the products table (also backfills retailer).
      5. Query all active products.
      6. Scrape sequentially with per-retailer blocking: once a 429/403 is received
         from retailer X, all remaining products from retailer X are skipped for
         this run. Products from other retailers are unaffected.
      7. Finalise the ScrapeRun record.
    """
    logging.basicConfig(level=logging.INFO)
    logger.info("[Monitor] Starting price monitor run")

    try:
        await create_tables()
    except Exception as e:
        logger.error("[Monitor] Failed to create tables on startup", exc_info=True)
        return

    products: List[Product] = []
    run_id = None
    started_at = None

    async with AsyncSessionLocal() as db:
        await run_watchdog(db, RunJobType.PRICE_MONITOR.value, max_duration_hours=4)

        run_record = ScrapeRun(
            job_type=RunJobType.PRICE_MONITOR,
            status=RunStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
            platform="ecommerce",
        )
        db.add(run_record)
        await db.commit()
        await db.refresh(run_record)
        run_id = run_record.id
        started_at = run_record.started_at

        try:
            await _upsert_products(db)
        except Exception as e:
            logger.error(
                "[Monitor] Aborting run due to product upsert failure", exc_info=True
            )
            await db.execute(
                update(ScrapeRun)
                .where(ScrapeRun.id == run_id)
                .values(
                    status=RunStatus.FAILED,
                    finished_at=datetime.now(timezone.utc),
                    error_summary=str(e)[:500],
                )
            )
            await db.commit()
            return

        try:
            result = await db.execute(select(Product).where(Product.is_active == True))
            products = list(result.scalars().all())
            logger.info(f"[Monitor] {len(products)} active products to check")
            db.expunge_all()  # detach so column attributes survive session close
        except Exception as e:
            logger.error("[Monitor] Failed to query active products", exc_info=True)
            await db.execute(
                update(ScrapeRun)
                .where(ScrapeRun.id == run_id)
                .values(
                    status=RunStatus.FAILED,
                    finished_at=datetime.now(timezone.utc),
                    error_summary=str(e)[:500],
                )
            )
            await db.commit()
            return
    # Session closed; ORM objects are detached but safe to read.

    items_attempted = len(products)
    items_succeeded = 0
    items_failed = 0
    error_summary = None

    # Per-retailer blocking set — shared across the product loop.
    # Once a 429/403 is received for a retailer, its slug is added here
    # and all further products from that retailer are skipped this run.
    blocked_retailers: Set[str] = set()

    file_logger = RunLogger(
        job_type=RunJobType.PRICE_MONITOR.value,
        platform="ecommerce",
        run_id=str(run_id),
        started_at=started_at,
    )

    try:
        for product in products:
            product_url: str = product.url
            
            # Bot-avoidance sleep (5 to 15 seconds)
            sleep_sec = random.randint(5, 15)
            logger.info(f"[Monitor] Sleeping {sleep_sec}s before checking {product_url}")
            await asyncio.sleep(sleep_sec)

            try:
                success, details = await scrape_product(
                    product,
                    blocked_retailers=blocked_retailers,
                )
                
                # Handle 404 Observability & Auto-Disable
                if details.get("state") == "not_found":
                    async with AsyncSessionLocal() as session:
                        upd = (
                            update(Product)
                            .where(Product.id == product.id)
                            .values(consecutive_404s=Product.consecutive_404s + 1)
                        )
                        await session.execute(upd)
                        
                        # Check if threshold reached
                        result = await session.execute(
                            select(Product.consecutive_404s).where(Product.id == product.id)
                        )
                        new_404_count = result.scalar()
                        if new_404_count is not None and new_404_count >= 3:
                            logger.warning(
                                f"[Monitor] {product_url} reached {new_404_count} consecutive 404s. Disabling product."
                            )
                            await session.execute(
                                update(Product)
                                .where(Product.id == product.id)
                                .values(is_active=False)
                            )
                        await session.commit()
                elif success or details.get("state") == "success":
                    # Reset 404s on successful finding
                    async with AsyncSessionLocal() as session:
                        await session.execute(
                            update(Product)
                            .where(Product.id == product.id)
                            .values(consecutive_404s=0)
                        )
                        await session.commit()

                if success:
                    items_succeeded += 1
                    file_logger.log_item(
                        {"url": product_url, "status": "success", **details}
                    )
                else:
                    items_failed += 1
                    file_logger.log_item(
                        {"url": product_url, "status": "failed", **details}
                    )
            except Exception as e:
                logger.error(
                    f"[Monitor] Unhandled error processing {product_url}",
                    exc_info=True,
                )
                items_failed += 1
                if not error_summary:
                    error_summary = str(e)[:500]
                file_logger.log_item(
                    {"url": product_url, "status": "error", "error": str(e)}
                )

    except Exception as e:
        logger.error(f"[Monitor] Fatal loop error: {e}", exc_info=True)
        error_summary = f"Fatal error: {e}"[:500]

    finally:
        file_logger.close()
        if blocked_retailers:
            logger.warning(
                f"[Monitor] Retailers blocked this run (429/403): {blocked_retailers}"
            )

        async with AsyncSessionLocal() as db:
            status = (
                RunStatus.SUCCESS
                if items_failed < items_attempted or items_attempted == 0
                else RunStatus.FAILED
            )
            if error_summary and status == RunStatus.SUCCESS:
                status = RunStatus.FAILED

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
                    meta_data={"blocked_retailers": list(blocked_retailers)},
                )
            )
            await db.commit()

        logger.info("[Monitor] Price monitor run complete")
