"""
Tier 3: Regex price scan + LLM disambiguation.

IMPORTANT: This does NOT make a new network request.
It receives an existing Response object (from Tier 1 or Tier 2).
"""

import re
import json
import logging
from typing import Optional, List, Dict, Any

from groq import Groq
from scrapling.engines.toolbelt.custom import Response

from config import get_settings

logger = logging.getLogger(__name__)

# Constants
CONTEXT_WINDOW: int = 120
MAX_CANDIDATES_TO_LLM: int = 10
GROQ_MODEL_NAME: str = "llama-3.1-8b-instant"
LLM_TEMPERATURE: float = 0.1
LLM_MAX_TOKENS: int = 64

PRICE_PATTERN = re.compile(
    r"""
    (?:
        [\$\£\€\¥\₹\₩\₽]               # Currency symbol prefix
        |
        (?:USD|EUR|GBP|JPY|CNY|INR|BRL|CAD|AUD)\s*  # ISO code prefix
    )
    \s*
    \d{1,3}(?:[,\.]\d{3})*(?:[\.]\d{1,2})?   # Amount with optional separators
    |
    \d{1,3}(?:[,\.]\d{3})*(?:[\.]\d{1,2})?   # Amount first ...
    \s*
    (?:USD|EUR|GBP|JPY|CNY|INR|BRL|CAD|AUD)  # ... then ISO code
    |
    \d{1,3}(?:[,\.]\d{3})*(?:[\.]\d{1,2})?   # Amount first ...
    [\s\xa0]*
    [\$\£\€\¥\₹\₩\₽]                         # ... then symbol suffix (e.g. 9199 €)
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _extract_price_contexts(text: str) -> List[Dict[str, str]]:
    """
    Find all price-shaped strings and return each with surrounding context.

    Args:
        text (str): The raw text to search.

    Returns:
        List[Dict[str, str]]: A list of dictionaries containing 'match' and 'context'.
    """
    results: List[Dict[str, str]] = []
    for m in PRICE_PATTERN.finditer(text):
        start = max(0, m.start() - CONTEXT_WINDOW)
        end = min(len(text), m.end() + CONTEXT_WINDOW)
        results.append({
            "match": m.group().strip(),
            "context": text[start:end].strip(),
        })
    return results


def _ask_groq(contexts: List[Dict[str, str]], product_hint: str = "") -> Optional[Dict[str, Any]]:
    """
    Send extracted price candidates to Groq LLM.

    Args:
        contexts (List[Dict[str, str]]): List of context dictionaries from regex match.
        product_hint (str): Optional product name to assist LLM disambiguation.

    Returns:
        Optional[Dict[str, Any]]: Dictionary containing parsed price info, or None on failure.
    """
    settings = get_settings()
    # Read GROQ_API_KEY exactly as it is in the updated config.py
    client = Groq(api_key=settings.GROQ_API_KEY.get_secret_value())

    capped_contexts = contexts[:MAX_CANDIDATES_TO_LLM]

    snippets = "\n\n".join(
        f"Candidate {i + 1}: match='{c['match']}'\n  context: ...{c['context']}..."
        for i, c in enumerate(capped_contexts)
    )

    product_line = f"Product hint: {product_hint}\n" if product_hint else ""
    prompt = (
        f"{product_line}"
        "You are a price extraction assistant. Given text snippets from a product "
        "page, identify the PRIMARY sale price (not a crossed-out original price, "
        "not a shipping cost, not a subscription fee).\n\n"
        "WARNING: You must identify the price of the exact product matching the Product hint. "
        "DO NOT extract the price of cheaper accessories (like cases, warranties, ear pads) or "
        "related products shown in 'frequently bought together' sections.\n\n"
        f"Price candidates found on the page:\n{snippets}\n\n"
        "Respond with ONLY valid JSON in this exact format, no explanation:\n"
        '{"price": <float>, "currency": "<ISO 3-letter code>", "confidence": "<high|medium|low>"}\n\n'
        "If no reliable price can be identified, respond with:\n"
        '{"price": null, "currency": null, "confidence": "none"}'
    )

    try:
        completion = client.chat.completions.create(
            model=GROQ_MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=LLM_TEMPERATURE,
            max_tokens=LLM_MAX_TOKENS,
        )
        raw = completion.choices[0].message.content.strip()
        return json.loads(raw)
    except Exception as e:
        logger.error(f"[Tier3] Groq call failed: {e}", exc_info=True)
        return None


def extract_price_tier3(
    response: Response = None,
    product_hint: str = "",
    text_snippet: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Tier 3 price extraction from an existing Response object or text snippet.
    
    This function analyzes the text of the page. It makes no HTTP requests.

    Args:
        response (Response): A Scrapling Response already fetched (optional if text_snippet is provided).
        product_hint (str): Optional product name. Defaults to "".
        text_snippet (str): Optional raw text to analyze instead of the entire response.

    Returns:
        Optional[Dict[str, Any]]: Dictionary containing 'price', 'currency', 
                                  and 'confidence', or None on total failure.
    """
    if text_snippet is not None:
        all_text = text_snippet
    else:
        try:
            all_text = str(
                response.get_all_text(
                    strip=True,
                    ignore_tags=("script", "style", "noscript"),
                )
            )
        except Exception as e:
            logger.warning(f"[Tier3] Failed to extract text from response: {e}", exc_info=True)
            return None

    if not all_text:
        logger.warning("[Tier3] No text content extracted from response")
        return None

    contexts = _extract_price_contexts(all_text)

    if not contexts:
        logger.warning("[Tier3] No price-shaped patterns found in page text")
        return {"price": None, "currency": None, "confidence": "none"}

    logger.info(f"[Tier3] Found {len(contexts)} price candidates — sending to LLM (cap {MAX_CANDIDATES_TO_LLM})")
    result = _ask_groq(contexts, product_hint=product_hint)

    if result is None:
        return {"price": None, "currency": None, "confidence": "none"}

    return result
