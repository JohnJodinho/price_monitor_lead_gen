import logging
from urllib.parse import urlparse

from scrapling.spiders import Spider, Request, LinkExtractor
from scrapling.engines.toolbelt.custom import Response
from scrapling.fetchers import FetcherSession, AsyncStealthySession

from db import AsyncSessionLocal
from config import get_settings


class HomepageCrawlerSpider(Spider):
    name = "homepage_crawler"

    def __init__(self, url, target, max_pages, contact_pattern, t1_timeout, t2_timeout):
        self._t1_timeout = t1_timeout
        self._t2_timeout = t2_timeout
        super().__init__()
        settings = get_settings()

        # ── Crawl behaviour — all from Settings, not hardcoded ───────────────
        self.robots_txt_obey = settings.HOMEPAGE_ROBOTS_TXT_OBEY
        self.concurrent_requests = settings.HOMEPAGE_CONCURRENT_REQUESTS
        self.concurrent_requests_per_domain = (
            settings.HOMEPAGE_CONCURRENT_REQUESTS_PER_DOMAIN
        )
        self.download_delay = settings.HOMEPAGE_DOWNLOAD_DELAY

        # ── Per-crawl state ──────────────────────────────────────────────────
        self.start_urls = [url]
        self.allowed_domains = {urlparse(url).netloc}
        self._target = target
        self._max_pages = max_pages
        self._contact_pattern = contact_pattern  # compiled re.Pattern from Settings

        self._pages_crawled: int = 0
        self._company_name: str = ""
        self._crawl_succeeded: bool = False
        self._link_extractor = LinkExtractor(allow=self._contact_pattern)

    def configure_sessions(self, manager) -> None:
        manager.add(
            "stealth",
            AsyncStealthySession(
                headless=True,
                network_idle=True,
                block_webrtc=True,
                google_search=True,
                timeout=self._t2_timeout * 1000,
            ),
            default=True,
        )

    async def parse(self, response: Response):
        """
        Extract contacts and queue subsequent pages from the StealthyFetcher response.
        """

        self._pages_crawled += 1
        self.logger.info(
            f"[HomepageCrawl] Page {self._pages_crawled}/{self._max_pages}: {response.url}"
        )

        if not self._company_name:
            try:
                title_text = response.css("title::text").get("")
                if title_text:
                    self._company_name = title_text.strip()[:200]
            except Exception:
                pass

        try:
            import lead_generator
            target_domain = urlparse(self._target.url).netloc.replace("www.", "")
            contacts, socials = lead_generator._extract_contacts(response, target_domain=target_domain)
        except Exception:
            self.logger.error(
                f"[HomepageCrawl] Extraction failed for {response.url}", exc_info=True
            )
            return

        if contacts["email"] or contacts["emailWithDomain"] or contacts["phone"] or any(socials.values()):
            yield {
                "contacts": contacts,
                "socials": socials,
                "source_url": response.url,
                "company_name": self._company_name,
            }

        if self._pages_crawled >= self._max_pages:
            self.logger.info(
                f"[HomepageCrawl] Max pages ({self._max_pages}) reached, stopping further link extraction from {response.url}."
            )
            return

        links = self._link_extractor.extract(response)
        if not links:
            self.logger.info(
                f"[HomepageCrawl] No contact-style links found on {response.url} for contact pattern: {self._contact_pattern}."
            )
            return

        # extract() can return strings or Link objects depending on the underlying implementation
        extracted_urls = [getattr(l, "url", l) for l in links]
        self.logger.info(
            f"[HomepageCrawl] Found {len(extracted_urls)} contact-style link(s) on {response.url}: {extracted_urls}"
        )

        for url in extracted_urls:
            yield response.follow(url, callback=self.parse)

    async def on_error(self, request: Request, error: Exception):
        # We need to correctly handle the on_error hook just to prevent silent failures
        self.logger.error(f"Failed request for {request.url}: {error}")


async def crawl_homepage(url: str, target) -> bool:
    settings = get_settings()

    spider = HomepageCrawlerSpider(
        url=url,
        target=target,
        max_pages=settings.HOMEPAGE_MAX_PAGES,
        contact_pattern=settings.HOMEPAGE_CONTACT_PATTERN,
        t1_timeout=settings.HOMEPAGE_T1_TIMEOUT,
        t2_timeout=settings.HOMEPAGE_T2_TIMEOUT,
    )
    leads_saved = 0
    try:
        import lead_generator
        async for item in spider.stream():
            async with AsyncSessionLocal() as save_db:
                leads_saved += await lead_generator._save_leads(
                    save_db,
                    target,
                    item["contacts"],
                    item["socials"],
                    item["source_url"],
                    item["company_name"],
                )
        spider._crawl_succeeded = True
    except Exception as e:
        logging.getLogger(__name__).error(
            f"[HomepageCrawl] Spider crashed for {url}: {e}", exc_info=True
        )
        spider._crawl_succeeded = False

    return spider._crawl_succeeded
