"""
E-Commerce Extractors — Amazon, Walmart, Best Buy.

Each public function receives an already-fetched Scrapling Response object and
returns an ExtractionResult dict.  No network requests are made here.

ExtractionResult keys
---------------------
price        : float | None   -- primary sale price; None on OOS / unresolvable
currency     : str            -- ISO-4217, e.g. "USD"
in_stock     : bool           -- False when explicitly OOS or no buybox winner
merchant     : str | None     -- "Sold by AnkerDirect" / "Ships from: Amazon / Sold by: AnkerDirect"
meta_data    : dict           -- condition, coupon, ships_from, sold_by, raw_state, etc.
tier_used    : int            -- 1-4
state        : str            -- "success" | "no_featured_offers" | "variant_required"
                                 "out_of_stock" | "parse_error"
"""

import json
import logging
from typing import Any, Dict, Optional

from scrapling.engines.toolbelt.custom import Response

from engines.tier3 import extract_price_tier3

logger = logging.getLogger(__name__)

ExtractionResult = Dict[str, Any]


def _make_result(
    price: Optional[float],
    currency: str = "USD",
    in_stock: bool = True,
    merchant: Optional[str] = None,
    meta_data: Optional[dict] = None,
    tier_used: int = 1,
    state: str = "success",
) -> ExtractionResult:
    return {
        "price": price,
        "currency": currency,
        "in_stock": in_stock,
        "merchant": merchant,
        "meta_data": meta_data or {},
        "tier_used": tier_used,
        "state": state,
    }


def _parse_price_str(raw: Optional[str]) -> Optional[float]:
    if not raw:
        return None
    cleaned = raw.strip().replace(",", "").strip("$" + chr(163) + chr(8364) + chr(165) + " " + chr(160))
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


# ===========================================================================
# Amazon
# ===========================================================================

_AMAZON_NO_OFFER_PHRASES = [
    "no featured offers available",
    "currently unavailable",
    "item not available",
    "we don't know when or if this item will be back in stock",
]

_AMAZON_VARIANT_PHRASES = [
    "select a configuration",
    "select a size",
    "select a style",
]


def extract_amazon(response: Response, product_hint: str = "") -> ExtractionResult:
    """
    Multi-tier Amazon price extractor.

    Tier-1a: Buybox hidden form input (amount + currencyCode).
    Tier-1b: Visible apex price span (a-offscreen text).
    Tier-1c: Whole + fraction digit combination.
    Tier-1 merchant: both split and combined DOM formats.
    Location guard: discard non-USD price from hidden input (non-US session artefact).
    Tier-3 classification: twister JSON -> variant/no-offer; page-text terminal states.
    Tier-4: LLM fallback via extract_price_tier3() - only if price still None and
             no terminal state classified.
    """
    price: Optional[float] = None
    currency = "USD"
    meta: dict = {}

    # --- Tier 1a: hidden input ---
    price_input = response.css(
        "input[name*='customerVisiblePrice'][name*='amount']::attr(value)"
    ).get()
    currency_input = response.css(
        "input[name*='customerVisiblePrice'][name*='currencyCode']::attr(value)"
    ).get()

    if price_input:
        try:
            price = float(price_input)
            currency = (currency_input or "USD").strip()
            logger.info(f"[Amazon] Tier-1a hidden-input: {currency} {price}")
        except (ValueError, TypeError):
            price = None

    # Location guard: non-USD from hidden input signals a non-US session.
    if price is not None and currency != "USD":
        logger.warning(
            f"[Amazon] Non-USD currency '{currency}' from hidden input — "
            "discarding (non-US session artefact)."
        )
        meta["location_bug_detected"] = True
        meta["discarded_currency"] = currency
        price = None
        currency = "USD"

    # --- Tier 1b: apex price span ---
    if price is None:
        offscreen = response.css(
            "span.apex-pricetopay-value span.a-offscreen::text"
        ).get() or response.css(
            ".reinventPriceAccordionT2 span.a-offscreen::text"
        ).get()
        
        price = _parse_price_str(offscreen)
        if price is not None:
            logger.info(f"[Amazon] Tier-1b apex/reinvent span: USD {price}")

    # --- Tier 1c: whole + fraction ---
    if price is None:
        whole = response.css(".a-price-whole::text").get()
        frac = response.css(".a-price-fraction::text").get()
        sym = response.css(".a-price-symbol::text").get()
        if whole:
            combined = f"{whole.replace(',', '').rstrip('.')}.{(frac or '00').strip()}"
            price = _parse_price_str(combined)
            if price is not None:
                # Location guard for Tier-1c
                if sym and "$" not in sym:
                    logger.warning(f"[Amazon] Non-USD symbol '{sym}' from DOM — discarding.")
                    meta["location_bug_detected"] = True
                    meta["discarded_currency"] = sym.strip()
                    price = None
                else:
                    logger.info(f"[Amazon] Tier-1c whole+fraction: USD {price}")

    # --- Merchant ---
    ships_from = (
        response.css(
            "#fulfillerInfoFeature_feature_div .offer-display-feature-text-message::text"
        ).get() or ""
    ).strip()
    sold_by = (
        response.css(
            "#merchantInfoFeature_feature_div a.offer-display-feature-text-message::text"
        ).get()
        or response.css(
            "#merchantInfoFeature_feature_div .offer-display-feature-text-message::text"
        ).get()
        or ""
    ).strip()

    merchant: Optional[str] = None
    if ships_from and sold_by and ships_from != sold_by:
        merchant = f"Ships from: {ships_from} / Sold by: {sold_by}"
    elif sold_by:
        merchant = sold_by
    elif ships_from:
        merchant = ships_from

    if ships_from:
        meta["ships_from"] = ships_from
    if sold_by:
        meta["sold_by"] = sold_by

    # --- Tier-3 twister classification (state only, never price) ---
    twister_state = _detect_amazon_twister_state(response)
    if twister_state:
        meta["twister_state"] = twister_state

    # --- Tier-3 page-text terminal states ---
    if price is None:
        page_text = ""
        try:
            page_text = str(
                response.get_all_text(strip=True, ignore_tags=("script", "style", "noscript"))
            ).lower()
        except Exception:
            pass

        for phrase in _AMAZON_NO_OFFER_PHRASES:
            if phrase in page_text:
                logger.info(f"[Amazon] Terminal: no_featured_offers ('{phrase}')")
                return _make_result(
                    price=None, in_stock=False, merchant=merchant,
                    meta_data={**meta, "terminal_phrase": phrase},
                    tier_used=1, state="no_featured_offers",
                )

        for phrase in _AMAZON_VARIANT_PHRASES:
            if phrase in page_text:
                logger.info(f"[Amazon] Terminal: variant_required ('{phrase}')")
                return _make_result(
                    price=None, in_stock=False, merchant=merchant,
                    meta_data={**meta, "terminal_phrase": phrase},
                    tier_used=1, state="variant_required",
                )

    # --- Tier 2: LLM fallback ---
    tier_used = 1
    if price is None:
        if meta.get("location_bug_detected"):
            logger.info("[Amazon] Location bug detected. Skipping Tier 2 LLM fallback.")
        else:
            logger.info("[Amazon] Escalating to Tier-2 LLM")
            tier_used = 2
            
            # Restrict context to buy-box to avoid extracting accessory prices
            buybox_text = None
            buybox = response.css("#desktop_buybox") or response.css("#buybox")
            if buybox:
                try:
                    buybox_text = str(buybox[0].get_all_text(strip=True, ignore_tags=("script", "style", "noscript")))
                except Exception:
                    pass
            
            result = extract_price_tier3(response, product_hint=product_hint, text_snippet=buybox_text)
            if result and result.get("price") is not None:
                try:
                    price = float(result["price"])
                    raw_curr = str(result.get("currency") or "USD")
                    if raw_curr != "USD":
                        logger.warning(f"[Amazon] Tier-2 non-USD: {raw_curr}")
                        meta["llm_currency_raw"] = raw_curr
                except (ValueError, TypeError):
                    price = None

    if price is None:
        logger.warning("[Amazon] All tiers failed")
        return _make_result(
            price=None, in_stock=False, merchant=merchant,
            meta_data=meta, tier_used=tier_used, state="parse_error",
        )

    # Coupon
    coupon_text = (response.css("#couponsInBuybox_feature_div::text").get() or "").strip()
    if coupon_text:
        meta["coupon"] = coupon_text

    return _make_result(
        price=price, currency=currency, in_stock=True,
        merchant=merchant, meta_data=meta,
        tier_used=tier_used, state="success",
    )


def _detect_amazon_twister_state(response: Response) -> Optional[str]:
    """
    Parse twister JSON to detect variation state.
    Prices inside are local-currency; NEVER extracted for use as the product price.
    Returns a descriptive state string or None.
    """
    try:
        scripts = response.css("script[type='a-state']")
        for script in scripts:
            key_attr = script.attrib.get("data-a-state", "")
            if "twister" not in key_attr.lower():
                continue
            raw_json = script.css("::text").get() or ""
            if not raw_json.strip():
                continue
            data = json.loads(raw_json)
            sorted_dims = data.get("sortedDimValuesForAllDims", {})
            for dim_vals in sorted_dims.values():
                for entry in (dim_vals or []):
                    for slot in (entry.get("slots") or []):
                        apex = (slot.get("displayData") or {}).get("apexPriceViewModel") or {}
                        if apex.get("buyingOptionType") == "USED":
                            return "used_only_no_new_offer"
    except Exception:
        pass
    return None


# ===========================================================================
# Walmart
# ===========================================================================

def extract_walmart(response: Response, product_hint: str = "") -> ExtractionResult:
    """
    Walmart price extractor via __NEXT_DATA__ embedded JSON.

    Confirmed paths (from walmart_script_tag.txt reference file):
      product.priceInfo.currentPrice.price        -> float
      product.priceInfo.currentPrice.currencyUnit -> "USD"
      product.availabilityStatus                  -> "IN_STOCK" | "OUT_OF_STOCK"
      product.sellerDisplayName                   -> storefront name
      product.sellerName                          -> legal entity name
      product.gradingLabel                        -> "New" | "Pre-Owned: Good" | etc.
      product.discounts.discountMetaData[]        -> coupon data
    """
    meta: dict = {}
    product_data = _extract_walmart_product(response)

    if product_data is None:
        logger.warning("[Walmart] __NEXT_DATA__ absent or unparseable -> Tier-4")
        return _walmart_tier4_fallback(response, product_hint, meta)

    # Price
    price: Optional[float] = None
    currency = "USD"
    try:
        price_info = product_data.get("priceInfo") or {}
        current = price_info.get("currentPrice") or {}
        raw_price = current.get("price")
        if raw_price is not None:
            price = float(raw_price)
        currency = (current.get("currencyUnit") or "USD").strip()
    except (KeyError, TypeError, ValueError) as e:
        logger.warning(f"[Walmart] Price parse error: {e}")

    # Availability
    avail_status = (product_data.get("availabilityStatus") or "").upper()
    in_stock = avail_status == "IN_STOCK"
    meta["availability_status_raw"] = avail_status

    if not in_stock:
        logger.info(f"[Walmart] OOS ({avail_status}) -> recording null price")
        price = None

    # Merchant
    seller_display = (product_data.get("sellerDisplayName") or "").strip()
    seller_legal = (product_data.get("sellerName") or "").strip()
    merchant: Optional[str] = seller_display or seller_legal or None
    if seller_legal and seller_legal != seller_display:
        meta["seller_legal"] = seller_legal

    # Condition
    grading = (product_data.get("gradingLabel") or "").strip()
    if grading:
        meta["condition"] = grading

    # Coupon / promo
    try:
        discounts = product_data.get("discounts") or {}
        for disc in (discounts.get("discountMetaData") or []):
            if disc.get("type") == "CONFIG_PROMO":
                savings = disc.get("savings") or {}
                if savings.get("amount"):
                    meta["coupon"] = {
                        "type": "CONFIG_PROMO",
                        "amount": savings["amount"],
                        "expiry": disc.get("expiry"),
                    }
                break
    except Exception:
        pass

    if price is None and in_stock:
        logger.info("[Walmart] Price absent despite IN_STOCK -> Tier-4")
        return _walmart_tier4_fallback(response, product_hint, meta)

    state = "success" if price is not None else "out_of_stock"
    logger.info(f"[Walmart] Tier-1: {currency} {price}, in_stock={in_stock}")
    return _make_result(
        price=price, currency=currency, in_stock=in_stock,
        merchant=merchant, meta_data=meta, tier_used=1, state=state,
    )


def _extract_walmart_product(response: Response) -> Optional[dict]:
    try:
        raw = response.css("script#__NEXT_DATA__[type='application/json']::text").get()
        if not raw:
            return None
        data = json.loads(raw)
        return data["props"]["pageProps"]["initialData"]["data"]["product"]
    except (KeyError, json.JSONDecodeError, TypeError):
        return None


def _walmart_tier4_fallback(response: Response, product_hint: str, meta: dict) -> ExtractionResult:
    logger.info("[Walmart] Tier-4 LLM fallback")
    result = extract_price_tier3(response, product_hint=product_hint)
    price = None
    if result and result.get("price") is not None:
        try:
            price = float(result["price"])
        except (ValueError, TypeError):
            pass
    state = "success" if price is not None else "parse_error"
    return _make_result(
        price=price, currency="USD", in_stock=price is not None,
        meta_data=meta, tier_used=4, state=state,
    )


# ===========================================================================
# Best Buy
# ===========================================================================

def extract_bestbuy(response: Response, product_hint: str = "") -> ExtractionResult:
    """
    Best Buy price extractor via JSON-LD (script#product-schema).

    Confirmed structure (from best_buy_script_tag.txt reference file):
      offers[0].price           -> float
      offers[0].priceCurrency   -> "USD"
      offers[0].availability    -> "https://schema.org/InStock" | ".../OutOfStock"
      offers[0].description     -> "New" | "Open Box" (condition label)
      offers[0].seller.name     -> "Best Buy"

    offers[] has exactly ONE entry — it is condition-scoped to the current URL.
    Open-box / refurb listings are tracked on separate URLs as separate Product rows.
    """
    meta: dict = {}
    ld_data = _extract_bestbuy_jsonld(response)

    if ld_data is None:
        logger.warning("[BestBuy] JSON-LD absent -> Tier-4")
        return _bestbuy_tier4_fallback(response, product_hint, meta)

    offers = ld_data.get("offers") or []
    if not offers:
        logger.warning("[BestBuy] JSON-LD empty offers array -> Tier-4")
        return _bestbuy_tier4_fallback(response, product_hint, meta)

    offer = offers[0]

    # Price
    price: Optional[float] = None
    currency = "USD"
    try:
        raw_price = offer.get("price")
        if raw_price is not None:
            price = float(raw_price)
        currency = (offer.get("priceCurrency") or "USD").strip()
    except (ValueError, TypeError) as e:
        logger.warning(f"[BestBuy] Price parse error: {e}")

    # Availability
    avail_iri = (offer.get("availability") or "").lower()
    in_stock = avail_iri.endswith("/instock")
    meta["availability_iri"] = avail_iri

    if not in_stock:
        logger.info(f"[BestBuy] OOS ({avail_iri}) -> null price")
        price = None

    # Condition
    condition = (offer.get("description") or offer.get("itemCondition") or "").strip()
    if "/" in condition:
        condition = condition.split("/")[-1].replace("Condition", "").strip()
    if condition:
        meta["condition"] = condition

    # Merchant
    seller = offer.get("seller") or {}
    merchant: Optional[str] = (seller.get("name") or "").strip() or None

    if price is None and in_stock:
        logger.info("[BestBuy] Price absent despite InStock -> Tier-4")
        return _bestbuy_tier4_fallback(response, product_hint, meta)

    state = "success" if price is not None else "out_of_stock"
    logger.info(f"[BestBuy] Tier-1: {currency} {price}, in_stock={in_stock}")
    return _make_result(
        price=price, currency=currency, in_stock=in_stock,
        merchant=merchant, meta_data=meta, tier_used=1, state=state,
    )


def _extract_bestbuy_jsonld(response: Response) -> Optional[dict]:
    try:
        raw = response.css(
            "script#product-schema[type='application/ld+json']::text"
        ).get()
        if not raw:
            return None
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _bestbuy_tier4_fallback(response: Response, product_hint: str, meta: dict) -> ExtractionResult:
    logger.info("[BestBuy] Tier-4 LLM fallback")
    result = extract_price_tier3(response, product_hint=product_hint)
    price = None
    if result and result.get("price") is not None:
        try:
            price = float(result["price"])
        except (ValueError, TypeError):
            pass
    state = "success" if price is not None else "parse_error"
    return _make_result(
        price=price, currency="USD", in_stock=price is not None,
        meta_data=meta, tier_used=4, state=state,
    )


# ===========================================================================
# Domain -> retailer inference + routing
# ===========================================================================

_DOMAIN_TO_RETAILER = {
    "amazon.com": "amazon",
    "walmart.com": "walmart",
    "bestbuy.com": "bestbuy",
}


def infer_retailer_from_url(url: str) -> str:
    """
    Return the retailer slug inferred from the URL domain.
    Returns 'unknown' for unrecognised domains.
    """
    url_lower = url.lower()
    for domain, slug in _DOMAIN_TO_RETAILER.items():
        if domain in url_lower:
            return slug
    return "unknown"


def extract_for_retailer(
    retailer: str,
    response: Response,
    product_hint: str = "",
) -> ExtractionResult:
    """Route a fetched Response to the correct retailer extractor."""
    if retailer == "amazon":
        return extract_amazon(response, product_hint=product_hint)
    elif retailer == "walmart":
        return extract_walmart(response, product_hint=product_hint)
    elif retailer == "bestbuy":
        return extract_bestbuy(response, product_hint=product_hint)
    else:
        logger.warning(f"[Ecommerce] Unknown retailer '{retailer}' -> Tier-4 LLM")
        result = extract_price_tier3(response, product_hint=product_hint)
        price = None
        if result and result.get("price") is not None:
            try:
                price = float(result["price"])
            except (ValueError, TypeError):
                pass
        return _make_result(
            price=price, in_stock=price is not None,
            tier_used=4,
            state="success" if price is not None else "parse_error",
        )
