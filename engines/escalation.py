"""Shared escalation-decision logic for all scraping engines."""

from typing import List

BOT_SIGNALS: List[str] = [
    "captcha",
    "access denied",
    "cloudflare",
    "just a moment",
]
THIN_CONTENT_THRESHOLD: int = 100


def needs_tier2(text_preview: str, response_is_none: bool = False) -> bool:
    if response_is_none:
        return True
    if len(text_preview) < THIN_CONTENT_THRESHOLD:
        return True
    return any(sig in text_preview for sig in BOT_SIGNALS)
