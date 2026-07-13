"""
Tier 2: Stealthy browser fetch using StealthyFetcher (Patchright/Playwright).
Use when Tier 1 returns a bot-check page or suspiciously thin content.
"""

import logging
from typing import Optional

from scrapling.fetchers import StealthyFetcher
from scrapling.engines.toolbelt.custom import Response

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT: int = 60

async def fetch_tier2(url: str, timeout: int = DEFAULT_TIMEOUT) -> Optional[Response]:
    """
    Attempt a stealthy browser fetch (async).

    StealthyFetcher launches a Chromium browser (via patchright),
    waits for network idle, and returns a fully-rendered Response.

    Uses async_fetch to avoid the "Sync Playwright API inside asyncio loop"
    error that occurs when called from FastAPI/uvicorn or any other async runner.

    Resource Note: Each StealthyFetcher() call launches and tears down
    a real browser. It must not be shared across concurrent calls.

    Args:
        url (str): The URL to fetch.
        timeout (int): Maximum wait time in seconds. Defaults to 60.

    Returns:
        Optional[Response]: The Scrapling Response object if successful, None otherwise.
    """
    try:
        # Instantiate per-call so the browser lifetime is scoped to this function.
        # async_fetch is a proper coroutine (iscoroutinefunction → True) that uses
        # Patchright's async Playwright API — safe inside any asyncio event loop.
        response = await StealthyFetcher().async_fetch(
            url,
            headless=True,
            network_idle=True,
            block_webrtc=True,
            google_search=True,
            timeout=timeout,
        )
        if response and response.status in {200, 201}:
            logger.info(f"[Tier2] OK {response.status} — {url}")
            return response
        status = response.status if response else "N/A"
        logger.warning(f"[Tier2] Bad status {status} — {url}")
        return None
    except Exception as e:
        logger.warning(f"[Tier2] Failed for {url}: {e}", exc_info=True)
        return None
