import json
import logging
import re
from typing import Dict, Any, Optional

from groq import Groq
from scrapling.engines.toolbelt.custom import Response

from config import get_settings

logger = logging.getLogger(__name__)

# Matches both plain ("$1,250 for 3 nights") and discounted
# ("$534 for 3 nights, originally $633") accessibility aria-label formats.
# Group 1 = charged total, Group 2 = nights, Group 3 = original total (optional).
PRICE_PATTERN = re.compile(
    r"\$?([0-9,]+(?:\.[0-9]{2})?)\s+for\s+([0-9]+)\s+nights?(?:,\s*originally\s+\$?([0-9,]+(?:\.[0-9]{2})?))?",
    re.IGNORECASE,
)

UNAVAILABLE_PHRASES = [
    "Those dates are not available",
    "Add dates for prices",
    "Something went wrong",
    "no longer available",
    "not available"
]

def clean_script_text(raw_text: str) -> str:
    text = (raw_text or "").strip()
    if text.startswith("<![CDATA[") and text.endswith("]]>"):
        text = text[9:-3]
    for prefix in [
        "window.__INITIAL_STATE__ = ",
        "var initialState = ",
        "const initialState = ",
        "let initialState = ",
    ]:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text.rstrip().rstrip(";")

def parse_json_payload(raw_text: str) -> Any:
    cleaned = clean_script_text(raw_text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        for start_char in ("{", "["):
            start_index = cleaned.find(start_char)
            if start_index == -1:
                continue
            stack: list[str] = []
            in_string = False
            escape = False
            for index in range(start_index, len(cleaned)):
                char = cleaned[index]
                if in_string:
                    if escape:
                        escape = False
                    elif char == "\\":
                        escape = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char in "{[":
                    stack.append(char)
                elif char in "}]":
                    if not stack:
                        break
                    stack.pop()
                    if not stack:
                        try:
                            return json.loads(cleaned[start_index:index + 1])
                        except json.JSONDecodeError:
                            break
            break
        raise ValueError(f"Unable to parse script JSON: {exc}") from exc

def extract_metadata_from_json(response: Response) -> Dict[str, Any]:
    """
    In-memory extraction of Airbnb metadata from the niobeClientData payload.
    """
    script_text = None
    for selector in ["script#data-deferred-state-0", "script[data-state='true']"]:
        node = response.css(f"{selector}::text").get()
        if node and node.strip():
            script_text = node
            break

    if not script_text:
        nodes = response.css("script::text").getall()
        for text in nodes:
            if text and ("niobeClientData" in text or "niobeMinimalClientData" in text):
                script_text = text
                break

    if not script_text:
        return {}

    try:
        data = parse_json_payload(script_text)
        root_key = "niobeClientData" if "niobeClientData" in data else "niobeMinimalClientData"
        if root_key not in data:
            return {}
        
        try:
            root_payload = data[root_key][0][1]["data"]
        except Exception:
            root_payload = None

        sections = None
        node_data = None
        if root_payload:
            try:
                sections = root_payload["presentation"]["stayProductDetailPage"]["sections"]
            except Exception:
                pass
            try:
                node_data = root_payload["node"]
            except Exception:
                pass

        metadata = {}

        # Extract property name from H1 tag (as per html_tags.txt)
        name_node = response.css("h1::text").get()
        if name_node and name_node.strip():
            metadata['name'] = name_node.strip()

        # Extract bedroom count from the property stats list (ol.lgx66tx).
        # html_tags.txt shows the list items contain text like "1 bedroom" or
        # "3 bedrooms" — we scan all <li> text nodes and regex-match the count.
        _BEDROOM_RE = re.compile(r"(\d+)\s+bedroom", re.IGNORECASE)
        li_texts = response.css("ol.lgx66tx li::text").getall()
        for _text in li_texts:
            _m = _BEDROOM_RE.search(_text)
            if _m:
                metadata['bedrooms'] = int(_m.group(1))
                break

        if sections:
            try:
                metadata['latitude'] = sections["metadata"]["loggingContext"]["eventDataLogging"]["listingLat"]
            except Exception:
                try:
                    metadata['latitude'] = sections[1]["section"]["lat"]
                except Exception:
                    pass

            try:
                metadata['longitude'] = sections["metadata"]["loggingContext"]["eventDataLogging"]["listingLng"]
            except Exception:
                try:
                    metadata['longitude'] = sections[1]["section"]["lng"]
                except Exception:
                    pass

            try:
                within_sections = sections["sections"]
                for section in within_sections:
                    try:
                        metadata['host_name'] = section["section"]["cardData"]["name"]
                        break
                    except Exception:
                        continue
            except Exception:
                pass

            try:
                rating = sections["metadata"]["sharingConfig"]["starRating"]
                # starRating of 0.0 means no reviews yet (genuine zero);
                # None means the key was absent (extraction miss).
                # Both cases are handled here — we record None only on KeyError.
                metadata['avg_rating'] = float(rating) if rating is not None else None
            except (KeyError, TypeError):
                pass

            if 'avg_rating' not in metadata:
                try:
                    rating_text = sections["sbuiData"]["sectionConfiguration"]["root"]["sections"][0]["sectionData"]["reviewData"]["ratingText"]
                    # ratingText is a string like "4.87" — convert to float
                    metadata['avg_rating'] = float(rating_text) if rating_text else None
                except (KeyError, TypeError, ValueError):
                    pass

            try:
                count = sections["metadata"]["sharingConfig"]["reviewCount"]
                # 0 means genuinely no reviews; absence raises KeyError → skip
                metadata['review_count'] = int(count) if count is not None else None
            except (KeyError, TypeError):
                pass

        if ('review_count' not in metadata or metadata['review_count'] is None) and node_data:
            try:
                count = node_data["sharingConfig"]["reviewCount"]
                metadata['review_count'] = int(count) if count is not None else None
            except (KeyError, TypeError):
                pass

        # ── DOM fallback: host_name ───────────────────────────────────────────
        # If JSON-path extraction didn't find the host name, use XPath to find the "Hosted by " div.
        if not metadata.get('host_name'):
            host_text = response.xpath('//div[starts-with(normalize-space(text()), "Hosted by ")]/text()').get()
            if host_text:
                metadata['host_name'] = host_text.replace("Hosted by ", "").strip()
                logger.info(f"[Metadata] host_name extracted via XPath: {metadata['host_name']}")

        # Final fallback: Look for profile URL if we still don't have a name
        if not metadata.get('host_name'):
            host_anchor = response.css('a[aria-label="Go to Host full profile"]::attr(href)').get()
            if host_anchor:
                metadata['host_profile_url'] = host_anchor.split('?')[0]  # strip query params
                logger.info(f"[Metadata] host_name not found in JSON; host profile URL captured: {metadata['host_profile_url']}")

        return metadata
    except Exception as e:
        logger.warning(f"Metadata extraction failed: {e}")
        return {}

def ask_tier3_real_estate(text_snippet: str) -> Dict[str, Any]:
    """
    Tier 3 LLM fallback for pricing and availability.
    """
    settings = get_settings()
    client = Groq(api_key=settings.GROQ_API_KEY.get_secret_value())

    prompt = (
        "You are an expert scraping assistant for Airbnb listings.\n"
        "Given the visible text extracted from a real estate listing page, determine:\n"
        "1. Is the listing available for the requested dates or is it unavailable/booked/invalid?\n"
        "2. If available, what is the total price and how many nights does it cover?\n\n"
        f"PAGE TEXT:\n{text_snippet}\n\n"
        "Respond with ONLY valid JSON in this exact format:\n"
        '{"is_available": <true/false>, "total_price": <float or null>, "nights": <int or null>}'
    )

    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=64,
        )
        raw = completion.choices[0].message.content.strip()
        logger.info(f"[Tier3] Sent: {text_snippet[:200]}... Received: {raw}")
        return json.loads(raw)
    except Exception as e:
        logger.error(f"[Tier3] Real estate Groq call failed: {e}", exc_info=True)
        return {"is_available": True, "total_price": None, "nights": None}

def extract_pricing(response: Response) -> Dict[str, Any]:
    """
    Extract pricing via heuristics, regex, or Tier 3 fallback.

    Extraction priority:
      1. Try the Airbnb booking sidebar element first
         ([data-plugin-in-point-id="BOOK_IT_SIDEBAR"]).
      2. If the sidebar isn't in the DOM yet, fall back to full page text.

    All three tiers (heuristic / regex / LLM) operate on the same focused
    text so Tier 3 never receives 40K chars of nav/footer noise.
    """
    # ── Region extraction ────────────────────────────────────────────────────
    # Extract both text nodes AND aria-label attributes to ensure discounted prices
    # (which are often stored in aria-labels for screen readers) are captured.
    sidebar_texts = response.css(
        '[data-plugin-in-point-id="BOOK_IT_SIDEBAR"] *::text'
    ).getall()
    
    sidebar_arias = response.css(
        '[data-plugin-in-point-id="BOOK_IT_SIDEBAR"] [aria-label]::attr(aria-label)'
    ).getall()

    if sidebar_texts or sidebar_arias:
        # Prepend arias so they match regex first
        combined = sidebar_arias + sidebar_texts
        region_text = " ".join(t.strip() for t in combined if t.strip())
        logger.info(
            f"[Pricing] Sidebar element found. "
            f"Region text length={len(region_text)} chars."
        )
    else:
        logger.warning(
            "[Pricing] Sidebar element NOT found — wait_selector may have timed out. "
            "Falling back to full page text."
        )
        try:
            region_text = str(response.get_all_text(strip=True, ignore_tags=("script", "style", "noscript")))
        except Exception:
            region_text = response.text

    text_content = str(region_text)
    # ─────────────────────────────────────────────────────────────────────────

    # 1. Heuristic: Check for unavailable states
    lower_text = text_content.lower()
    for phrase in UNAVAILABLE_PHRASES:
        if phrase.lower() in lower_text:
            logger.info(f"[Pricing] Matched unavailable phrase: '{phrase}'")
            return {
                "is_available": False,
                "nightly_rate": None,
                "meta_data": {"state": "unavailable", "matched_phrase": phrase},
            }

    # 2. Regex: Accessible "$X for Y nights" (plain or discounted) pattern.
    # PRICE_PATTERN group layout:
    #   Group 1 = charged total (discounted price when a discount is applied)
    #   Group 2 = number of nights
    #   Group 3 = original total (present only when a discount is active)
    match = PRICE_PATTERN.search(text_content)
    if match:
        charged_str, nights_str, original_str = match.groups()
        charged_total = float(charged_str.replace(",", ""))
        parsed_nights = int(nights_str)
        nightly_rate = charged_total / parsed_nights if parsed_nights > 0 else None

        meta: Dict[str, Any] = {
            "raw_total": charged_total,
            "parsed_nights": parsed_nights,
            "extraction_method": "regex",
        }

        if original_str:
            # A discount is in effect — record full breakdown in meta_data
            original_total = float(original_str.replace(",", ""))
            original_nightly = original_total / parsed_nights if parsed_nights > 0 else None
            discount_amount = original_total - charged_total
            meta["is_discounted"] = True
            meta["original_total"] = original_total
            meta["original_nightly_rate"] = original_nightly
            meta["discount_amount"] = discount_amount
            logger.info(
                f"[Pricing] Discounted regex match: charged={charged_total}, "
                f"original={original_total}, nights={parsed_nights}"
            )
        else:
            meta["is_discounted"] = False
            logger.info(f"[Pricing] Regex match: total={charged_total}, nights={parsed_nights}")

        return {
            "is_available": True,
            "nightly_rate": nightly_rate,
            "meta_data": meta,
        }

    # 3. Tier 3: LLM fallback — send only the sidebar region (compact, focused)
    # Log the snippet so we can confirm the sidebar rendered correctly.
    dom_snippet = text_content[:300].replace("\n", " ")
    logger.info(
        f"[Tier3] DOM region length={len(text_content)} chars. "
        f"First 300: {dom_snippet!r}"
    )
    logger.info("[Tier3] Falling back to LLM for price extraction")
    # Sidebar text is already compact; cap at 2000 chars to stay within token budget.
    snippet = text_content[:2000]
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
            "tier3_snippet": snippet[:500],
        },
    }
