"""
Tier 1: Fast HTTP fetcher using curl_cffi via Scrapling's Fetcher.
Use for pages that don't require JS rendering or bot-protection bypass.
"""

import logging
from typing import Optional

from scrapling.fetchers import Fetcher
from scrapling.engines.toolbelt.custom import Response

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT: int = 30
DEFAULT_RETRIES: int = 3
DEFAULT_RETRY_DELAY: int = 1

def fetch_tier1(url: str, timeout: int = DEFAULT_TIMEOUT) -> Optional[Response]:
    """
    Attempt a simple HTTP fetch using curl_cffi.

    Args:
        url (str): The URL to fetch.
        timeout (int): Request timeout in seconds. Defaults to 30.

    Returns:
        Optional[Response]: The Scrapling Response object if successful, None otherwise.
    """
    try:
        # Instantiate Fetcher inside the function
        response = Fetcher().get(
            url,
            impersonate="chrome",
            stealthy_headers=True,
            timeout=timeout,
            retries=DEFAULT_RETRIES,
            retry_delay=DEFAULT_RETRY_DELAY,
            follow_redirects="safe",
        )
        if response.status in {200, 201}:
            logger.info(f"[Tier1] OK {response.status} — {url}")
            return response
        logger.warning(f"[Tier1] Bad status {response.status} — {url}")
        return None
    except Exception as e:
        logger.warning(f"[Tier1] Failed for {url}: {e}", exc_info=True)
        return None
