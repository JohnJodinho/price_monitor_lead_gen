import re
from typing import Dict, Any, Tuple
from bs4 import BeautifulSoup
from engines.real_estate_extractors import ask_tier3_real_estate

def extract_vrbo_property_id(url: str) -> str:
    match = re.search(r"vrbo\.com/(\d+)", url)
    if not match:
        raise ValueError(f"Could not extract a Vrbo property ID from: {url}")
    return match.group(1)

def extract_vrbo_metadata(response) -> Dict[str, Any]:
    """
    Extract property metadata from the Vrbo DOM.
    Returns a dict with seed fields; fields that fail extraction are omitted or None.
    """
    metadata = {
        "name": None,
        "host_name": None,
        "review_count": None,
        "avg_rating": None,
        "latitude": None,
        "longitude": None,
        "bedrooms": None
    }
    
    # Name from H1
    name_node = response.css('h1.uitk-heading::text').get()
    if name_node:
        metadata['name'] = name_node.strip()

    # Bedrooms from H3 containing "bedroom"
    _BEDROOM_RE = re.compile(r"(\d+)\s+bedroom", re.IGNORECASE)
    bedroom_nodes = response.css('h3:contains("bedroom")::text').getall()
    for _text in bedroom_nodes:
        _m = _BEDROOM_RE.search(_text)
        if _m:
            metadata['bedrooms'] = int(_m.group(1))
            break

    # Host name (H3 that has a sibling or child containing "Host")
    # Actually, html_tags2.txt shows: <h3 class="uitk-heading uitk-heading-6">Robert Grogan Jr.</h3>
    # followed by <div class="uitk-text uitk-type-300 uitk-text-default-theme">Host</div>
    # Using Scrapling CSS/XPath:
    host_xpath = "//h3[contains(@class, 'uitk-heading') and following-sibling::div[contains(text(), 'Host')]]/text()"
    host_node = response.xpath(host_xpath).get()
    if host_node:
        metadata['host_name'] = host_node.strip()

    # Review count: <button aria-label="99 external reviews">
    # We can search for an element with aria-label containing "reviews"
    reviews_xpath = "//*[@aria-label and contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'reviews')]/@aria-label"
    review_label = response.xpath(reviews_xpath).get()
    if review_label:
        # e.g., "99 external reviews"
        _m = re.search(r"(\d+)\s+.*review", review_label, re.IGNORECASE)
        if _m:
            metadata['review_count'] = int(_m.group(1))

    # Rating: "9.8 out of 10" in a visually hidden span next to the badge, or inside a span.
    # HTML: <span class="is-visually-hidden">9.8 out of 10 </span>
    rating_xpath = "//span[contains(text(), 'out of 10')]/text()"
    rating_text = response.xpath(rating_xpath).get()
    if rating_text:
        _m = re.search(r"([\d\.]+)\s+out of", rating_text, re.IGNORECASE)
        if _m:
            try:
                metadata['avg_rating'] = float(_m.group(1))
            except ValueError:
                pass

    # Latitude / Longitude from itemProp tags
    lat = response.css('meta[itemProp="latitude"]::attr(content)').get()
    lng = response.css('meta[itemProp="longitude"]::attr(content)').get()
    
    if lat and lng:
        try:
            metadata['latitude'] = float(lat)
            metadata['longitude'] = float(lng)
        except ValueError:
            pass

    return metadata

def extract_vrbo_pricing(response) -> Dict[str, Any]:
    """
    Classify availability and extract pricing for Vrbo.
    """
    html_content = ""
    try:
        html_content = response.body.decode('utf-8', errors='replace')
    except Exception:
        pass
        
    title = response.css('title::text').get() or ""
    
    # 1. Block Detection
    if title.strip() == "Bot or Not?" or "datadome" in html_content.lower() or response.status in (429, 403):
        return {
            "is_available": None,
            "nightly_rate": None,
            "meta_data": {
                "extraction_method": "blocked",
                "error": "DataDome block detected"
            }
        }

    # 2. Unavailable Detection
    # data-stid="error-messages" or text "Sorry, we aren't taking reservations"
    error_node = response.css('[data-stid="error-messages"]').get()
    unavailable_text = response.css(':contains("Sorry, we aren\'t taking reservations")').get()
    
    if error_node or unavailable_text:
        return {
            "is_available": False,
            "nightly_rate": None,
            "meta_data": {"extraction_method": "heuristic_unavailable"}
        }

    # 3. Available Detection (Price Extraction)
    # The current price is $X (accessible text)
    primary_price_text = response.css(':contains("The current price is")::text').getall()
    primary_rate = None
    for t in primary_price_text:
        m = re.search(r"The current price is \$([\d,]+)", t)
        if m:
            primary_rate = int(m.group(1).replace(",", ""))
            break

    # Secondary cross-check: $Y for N nights
    secondary_text = response.css(':contains("for")::text').getall()
    secondary_audit = None
    for t in secondary_text:
        # e.g., "$550 for 3 nights"
        m = re.search(r"\$([\d,]+)\s+for\s+(\d+)\s+night", t)
        if m:
            total = int(m.group(1).replace(",", ""))
            nights = int(m.group(2))
            if nights > 0:
                secondary_audit = f"${total} / {nights} = ${total/nights:.2f}/night"
            break

    if primary_rate is not None:
        return {
            "is_available": True,
            "nightly_rate": primary_rate,
            "meta_data": {
                "extraction_method": "regex_primary",
                "audit_secondary": secondary_audit
            }
        }

    # 4. Tier 3 Fallback
    # Extract the booking panel to save tokens, or just pass a snippet if possible.
    # In Vrbo, the price panel might be within `[data-stid="property-offers-variant"]`
    panel_html = response.css('[data-stid="property-offers-variant"]').get()
    if not panel_html:
        # If the panel isn't found, try a broader region
        panel_html = response.css('main').get() or html_content

    # To avoid huge context windows, let's clean it up slightly
    soup = BeautifulSoup(panel_html, "lxml")
    for script in soup(["script", "style", "svg", "img"]):
        script.decompose()
    clean_text = soup.get_text(separator=" ", strip=True)
    snippet = clean_text[:3000]

    llm_result = ask_tier3_real_estate(snippet)
    
    is_available = llm_result.get("is_available", True)
    total_price = llm_result.get("total_price")
    nights = llm_result.get("nights")

    nightly_rate = None
    if is_available and total_price is not None and nights is not None and nights > 0:
        nightly_rate = float(total_price) / int(nights)

    return {
        "is_available": is_available,
        "nightly_rate": nightly_rate,
        "meta_data": {
            "raw_total": total_price,
            "parsed_nights": nights,
            "extraction_method": "tier3",
            "audit_secondary": secondary_audit,
            "tier3_snippet": snippet[:500],
        },
    }
