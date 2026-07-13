"""
Price Monitor — independently editable module.

TO ADD OR REMOVE PRODUCTS: edit the PRODUCTS_TO_TRACK list below.
No API call, no migration required.

Run directly via GitHub Actions:
    python -c "import asyncio; from price_monitor import run_monitor; asyncio.run(run_monitor())"
"""

import logging
from typing import List, Dict, Any, Optional
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
from engines.tier1 import fetch_tier1
from engines.tier2 import fetch_tier2
from engines.tier3 import extract_price_tier3
import json

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
      Removing a URL from PRODUCTS_TO_TRACK will set that product's is_active=False,
      stopping it from being scraped on the next run. Historical price_history rows
      are preserved (product row stays in DB, just inactive).

    Step 2 — Upsert the current config list.
      Uses INSERT ... ON CONFLICT DO UPDATE so:
        - New entries are inserted with is_active=True
        - Existing entries (matched by URL) have name/target_price refreshed
          and is_active reset to True

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
            # If the list is empty, deactivate all active products
            deactivate_stmt = (
                update(Product).where(Product.is_active == True).values(is_active=False)
            )

        result = await db.execute(deactivate_stmt)
        logger.info(
            f"[Monitor] Deactivated {result.rowcount} products removed from config."
        )

        # Step 2: upsert current config
        for p in PRODUCTS_TO_TRACK:
            stmt = (
                pg_insert(Product)
                .values(
                    name=p["name"],
                    url=p["url"],
                    target_price=p.get("target_price"),
                    is_active=True,
                )
                .on_conflict_do_update(
                    index_elements=["url"],
                    set_={
                        "name": p["name"],
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
    price: float,
    currency: str,
    tier_used: int,
) -> None:
    """
    Persist a price reading and fire any applicable alerts.

    Alert logic:
      1. Query the most recent prior price_history row BEFORE inserting.
      2. Insert the new PriceHistory record.
      3. "threshold" alert: if product.target_price is set and new price <=
         target_price, fire a threshold alert.
      4. "change" alert: if a prior price exists and the absolute % change
         from that prior price >= PRICE_CHANGE_THRESHOLD_PCT, fire a change
         alert. Direction is recorded in pct_change's sign.

    Note: created_at is set automatically by server_default=func.now() on Base.

    Args:
        db (AsyncSession): The active database session.
        product (Product): The product being monitored.
        price (float): The newly scraped price.
        currency (str): The currency of the price.
        tier_used (int): The scraping tier that successfully obtained the price.

    Raises:
        Exception: If the database operations fail, rolls back the transaction and raises.
    """
    try:
        # 1. Fetch prior price before inserting new record.
        #    scalar_one_or_none() returns Decimal for Numeric columns; cast to float
        #    immediately so all downstream arithmetic uses a uniform type.
        prior_result = await db.execute(
            select(PriceHistory.price)
            .where(PriceHistory.product_id == product.id)
            .order_by(desc(PriceHistory.created_at))
            .limit(1)
        )
        _raw_prev = prior_result.scalar_one_or_none()
        previous_price: Optional[float] = (
            float(_raw_prev) if _raw_prev is not None else None
        )

        # 2. Insert new price record
        record = PriceHistory(
            product_id=product.id,
            price=price,
            currency=currency,
            tier_used=tier_used,
        )
        db.add(record)

        # 3. Threshold alert.
        #    product.target_price is Decimal (from DB); cast to float before comparing
        #    with the scraped price (float) to avoid TypeError on mixed arithmetic.
        if product.target_price is not None:
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

        # 4. Change-detection alert
        if previous_price is not None:
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


async def scrape_product(product: Product) -> bool:
    """
    Run the 3-tier pipeline for a single product URL.

    Tier 1  ->  fast curl_cffi HTTP request
    Tier 2  ->  stealthy Playwright browser (escalate if Tier 1 is thin)
    Tier 3  ->  regex + Groq LLM (if CSS selector extraction fails)

    Opens its own isolated AsyncSession for the DB write so that a save
    failure and rollback cannot expire ORM objects held by the caller's
    read session (which would cause MissingGreenlet on the next iteration).

    Args:
        product (Product): The product to scrape. Must be detached (expunged)
                           from any session before being passed here so that
                           accessing its column attributes does not trigger a
                           lazy-load on a closed/expired session.

    Returns:
        bool: True if extraction and saving was successful, False otherwise.
    """
    url: str = product.url
    tier_used: int = 1
    price: Optional[float] = None
    currency: str = "USD"

    # --- Tier 1 ---
    # Network operations inside Tier 1 handle their own try/excepts and return None on failure
    response = fetch_tier1(url)

    if _is_thin_response(response):
        logger.info(f"[Monitor] Escalating to Tier 2 for {url}")
        # Network operations inside Tier 2 handle their own try/excepts and return None on failure
        response = await fetch_tier2(url)
        tier_used = 2

    if response is None:
        logger.error(f"[Monitor] All network tiers failed for {url}")
        return False

    # --- CSS extraction (fast, zero LLM cost) ---
    # Try a cascade of common price selectors before calling the LLM.
    try:
        price_raw: Optional[str] = (
            response.css(".price::text").get()
            or response.css("[data-price]::attr(data-price)").get()
            or response.css(".a-price-whole::text").get()  # Amazon
            or response.css(".priceToPay::text").get()  # Amazon alt
            or response.css("span[itemprop='price']::attr(content)").get()
            or response.css(
                "meta[property='product:price:amount']::attr(content)"
            ).get()
            or response.css(
                "#__next > main > div > div > div > div.product-content-wrapper.css-36ak8o.e1pl6npa12 > div.css-1f150rr.e15c0rei0 > div.css-kqe5aa.emlf3670 > div.product-info-wrapper.css-m2w3q2.emlf3670 > div.price.css-o7uf8d.e1pl6npa6::text"
            ).get()
            or response.css(
                "body > div.wrapper > main > div.container.test-sites-cars.item-page > div.mb-5 > div.product-wrapper > div > div:nth-child(2) > div.title-section.mb-3 > h3 > span.amount::text"
            ).get()
            or response.css(
                "#content_inner > article > div.row > div.col-sm-6.product_main > p.price_color::text"
            ).get()
        )

        if price_raw:
            # Strip whitespace, thousands-separator commas, then currency symbols
            # from BOTH ends — handles both prefix (€9199) and suffix (9199 €) formats.
            # Also strips non-breaking spaces (\xa0) common in European price strings.
            cleaned: str = price_raw.strip().replace(",", "").strip("$£€¥₹₩₽ \xa0")
            price = float(cleaned)
    except Exception as e:
        logger.warning(
            f"[Monitor] Exception during CSS extraction for {url}: {e}", exc_info=True
        )
        price = None

    # --- Tier 3 fallback if CSS failed ---
    if price is None:
        logger.info(f"[Monitor] CSS extraction failed — escalating to Tier 3 for {url}")
        tier_used = 3
        # Network operations inside Tier 3 (LLM call) handle their own try/excepts
        result: Optional[dict] = extract_price_tier3(
            response, product_hint=product.name
        )
        if result and result.get("price") is not None:
            try:
                price = float(result["price"])
                currency = str(result.get("currency") or "USD")
            except (ValueError, TypeError) as e:
                logger.warning(
                    f"[Monitor] Exception parsing Tier 3 result for {url}: {e}",
                    exc_info=True,
                )
                price = None

    if price is None:
        logger.warning(f"[Monitor] Could not extract price for {url}")
        return False

    logger.info(f"[Monitor] {product.name}: {currency} {price:.2f} (Tier {tier_used})")

    # Each product gets its own isolated write session.
    # This prevents a rollback here from expiring ORM objects in the caller's
    # read session, which would cause MissingGreenlet on the next iteration.
    try:
        async with AsyncSessionLocal() as save_db:
            await _save_price(save_db, product, price, currency, tier_used)
        return True
    except Exception:
        # Error is logged inside _save_price
        return False


async def run_monitor() -> None:
    """
    Entry point for GitHub Actions.

    Steps:
      1. Ensure DB tables exist (idempotent)
      2. Upsert PRODUCTS_TO_TRACK config into the products table
         (also deactivates products removed from config)
      3. Scrape all active products sequentially
    """
    logging.basicConfig(level=logging.INFO)
    logger.info("[Monitor] Starting price monitor run")

    try:
        await create_tables()
    except Exception as e:
        logger.error("[Monitor] Failed to create tables on startup", exc_info=True)
        return

    # Phase 1: upsert config + read active products in one short-lived session.
    # expunge_all() detaches the ORM objects so their column attributes remain
    # accessible after the session closes, without any lazy-load risk.
    products: List[Product] = []
    run_id = None

    async with AsyncSessionLocal() as db:
        # Create run record
        run_record = ScrapeRun(
            job_type=RunJobType.PRICE_MONITOR,
            status=RunStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run_record)
        await db.commit()
        await db.refresh(run_record)
        run_id = run_record.id

        try:
            await _upsert_products(db)
        except Exception as e:
            logger.error(
                "[Monitor] Aborting run due to product upsert failure", exc_info=True
            )
            run_record.status = RunStatus.FAILED
            run_record.finished_at = datetime.now(timezone.utc)
            run_record.error_summary = str(e)[:500]
            await db.commit()
            return

        try:
            result = await db.execute(select(Product).where(Product.is_active == True))
            products = list(result.scalars().all())
            logger.info(f"[Monitor] {len(products)} active products to check")
            # Detach all objects so they outlive this session safely.
            db.expunge_all()
        except Exception as e:
            logger.error("[Monitor] Failed to query active products", exc_info=True)
            run_record.status = RunStatus.FAILED
            run_record.finished_at = datetime.now(timezone.utc)
            run_record.error_summary = str(e)[:500]
            await db.commit()
            return
    # Session is now closed; products are detached (column values still accessible).

    # Phase 2: scrape each product with its own isolated write session.
    # scrape_product() opens AsyncSessionLocal internally for _save_price, so a
    # rollback in one product cannot expire objects used by the next.
    items_attempted = len(products)
    items_succeeded = 0
    items_failed = 0
    error_summary = None

    for product in products:
        product_url: str = product.url  # capture before any potential expiry
        try:
            success = await scrape_product(product)
            if success:
                items_succeeded += 1
            else:
                items_failed += 1
        except Exception as e:
            logger.error(
                f"[Monitor] Unhandled error processing product {product_url}",
                exc_info=True,
            )
            items_failed += 1
            if not error_summary:
                error_summary = str(e)[:500]

    # Phase 3: Update run record status
    async with AsyncSessionLocal() as db:
        status = (
            RunStatus.SUCCESS
            if items_failed < items_attempted or items_attempted == 0
            else RunStatus.FAILED
        )
        if error_summary and status == RunStatus.SUCCESS:
            status = (
                RunStatus.FAILED
            )  # If there was a fatal unhandled error, mark failed

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

    logger.info("[Monitor] Price monitor run complete")
