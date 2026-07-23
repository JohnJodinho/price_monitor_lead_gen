# Autonomous Scraping Backend: Price Monitor, Lead Generator & Real Estate Tracker

## Overview
This system is an automated, stealthy scraping backend that performs three distinct business functions: monitoring e-commerce product prices across major retailers, tracking real estate/travel rate fluctuations across booking platforms, and extracting B2B business leads from target websites. Built as a collection of serverless, ephemeral cron jobs triggered via GitHub Actions, the system persists all extracted data into a shared PostgreSQL database (e.g., Supabase, Neon) which powers a separate frontend application.

## Architecture
The core architectural pattern consists of specialized scrapers feeding into a unified Postgres database (via SQLAlchemy 2.0 Async), designed to run entirely in the background without exposing REST APIs. The scraping engines rely on `StealthyFetcher` (leveraging `curl_cffi` and `scrapling` under the hood) to bypass modern bot protections. A core design decision across all modules is a **tiered extraction philosophy**: the system attempts structural and regex-based parsing first (Tier-1) for speed and cost-efficiency. If standard DOM parsing fails to find the target data (or if complex layouts change), it escalates to an LLM-based fallback (via Groq/LLaMA 3) to intelligently extract information from the raw page text without brittle rules.

## Engines

### 1. Price Monitor (`price_monitor.py`)
* **Purpose:** Monitors a predefined list of e-commerce product URLs for price changes, detecting out-of-stock states and generating smart alerts when prices drop below a threshold or shift significantly.
* **Input Format:** Reads from `products_to_track.json`. Example: `{"name": "Beats Solo 4 Wireless Headphones", "url": "...", "retailer": "amazon", "sku": "MUW23LL/A", "category": "electronics_audio"}`
* **Extraction Tiers:** Uses `ecommerce_extractors.py` which attempts structural extraction first (e.g., hidden form inputs, apex price spans, and whole+fraction combinations on Amazon). It includes location guards to discard non-USD prices resulting from VPN routing, and twister JSON classification to detect variants/out-of-stock. If structural extraction fails, it falls back to an LLM extraction restricted to the buy-box.
* **Schedule:** Hourly within the 3-hour gaps between Real Estate runs (`0 3,4,5,9,10,11,15,16,17,21,22,23 * * *`).

### 2. Real Estate Monitor (`real_estate_monitor.py`)
* **Purpose:** Tracks nightly rates, availability, and dynamic pricing for rental properties across platforms like Airbnb and Vrbo.
* **Input Format:** Reads from `properties_to_track.json`. Example: `{"url": "https://www.vrbo.com/5365580", "platform": "vrbo", "market": "NYC/NJ Metro", "property_key": null}`
* **Quirks:** Implements anti-bot evasion by intentionally interleaving requests (e.g., mixing Airbnb and Vrbo requests rather than hitting one platform sequentially). Vrbo extraction detects specific blocked states, while Airbnb uses nested JSON data extraction.
* **Schedule:** Runs every 6 hours (`0 0,6,12,18 * * *`).

### 3. Lead Generator (`lead_generator.py`)
* **Purpose:** Autonomously crawls target URLs to extract B2B contact information (emails, phone numbers, social links) and company names.
* **Input Format:** Reads from `lead_targets.json`. Example: `{"url": "https://compassplumbing.co.nz", "category": "plumbing"}`
* **Extraction:** For homepages, it initiates a recursive spider that streams discovered pages in search of contact details. For deep links, it performs a single fetch. Data is upserted into the DB to avoid duplicates.
* **Schedule:** Runs 4 times a day, positioned safely away from other jobs (`30 4,10,16,22 * * *`).

## Data Model
The database is structured around several core domains, defined in `models.py`:
* **E-commerce:** `Product` stores the core listing, linked to `PriceHistory` (time-series snapshots) and `PriceAlert` (fired events). `sku` is used to group the same item across different retailers. `PriceHistory.price` is nullable to accurately represent out-of-stock states without inserting misleading `0.00` values.
* **Real Estate:** `Property` stores listing metadata, linked to `RateHistory` which tracks the price of a specific stay-date over time. `Property.property_key` allows grouping of the same physical unit across different platforms (Airbnb vs Vrbo). `RateHistory.nightly_rate` is nullable for booked/blocked dates.
* **Lead Generation:** `LeadTarget` represents the seed URL, and `Lead` stores extracted contacts (emails, phones) as JSONB objects.
* **Observability & Multitenancy:** `ScrapeRun` tracks execution metadata, and `Client` handles multitenant segmentation.

## Observability
The system employs a dual-persistence observability pattern:
1. **Summary Persistence:** Every job run creates a `ScrapeRun` row in the database with overall status, start/end times, and counts for `items_attempted`, `items_succeeded`, and `items_failed`. A `run_watchdog` automatically marks orphaned runs as failed if they exceed maximum duration.
2. **Detailed Logs:** A `RunLogger` writes granular, item-level JSONL log files (e.g., `price_monitor_ecommerce_<id>.log`) into the `logs/` directory. These logs are automatically uploaded as GitHub Actions artifacts with a **14-day retention period**.

## Setup & Environment
The backend requires several environment variables for configuration. **Do not commit actual secrets.**
* `DATABASE_URL`: Connection string for the PostgreSQL database (e.g., `postgresql+asyncpg://...`).
* `GROQ_API_KEY`: API key for the Groq LLM fallback extraction.
* `HOMEPAGE_*`: Various configuration variables for the Lead Generator spider (e.g., `HOMEPAGE_MAX_PAGES`, `HOMEPAGE_T1_TIMEOUT`, `HOMEPAGE_DOWNLOAD_DELAY`, `HOMEPAGE_ROBOTS_TXT_OBEY`, `HOMEPAGE_CONTACT_KEYWORDS`).

**One-Time Setup:**
For the `StealthyFetcher` and underlying engines to work in CI/CD, the GitHub Actions workflows explicitly run `python -m patchright install chromium --with-deps` to cache and install the necessary Chromium binaries. 

## Scheduling
To avoid IP bans and resource contention, cron jobs are intentionally staggered in GitHub Actions:

| Workflow | Cron Schedule | Runs | Purpose |
|----------|---------------|------|---------|
| Price Monitor | `0 3,4,5,9,10,11,15,16,17,21,22,23 * * *` | Hourly (in 3hr gaps) | High-frequency price tracking, avoiding real estate windows. |
| Real Estate Monitor | `0 0,6,12,18 * * *` | Every 6 hours | Heavy scraping job occupying the 00, 06, 12, and 18 hours. |
| Lead Gen | `30 4,10,16,22 * * *` | 4 times daily | Mid-block placement offset from the other jobs by 30 mins to 1.5 hrs. |

## Known Limitations & Reliability Notes
* **Target Inputs:** Target lists are managed via JSON files (`products_to_track.json`, `properties_to_track.json`, `lead_targets.json`), which the scripts read to upsert the database configs.
* **Anti-Bot Considerations:** Amazon and Best Buy deploy aggressive location spoofing and HTTP/2 protocol errors. The extractors have specific guards (e.g., checking if the parsed currency symbol is USD) to detect and discard non-US session artefacts.
* **Vrbo / Booking.com:** Booking.com is currently not monitored. Vrbo is heavily rate-limited, hence the deliberate interleaving with Airbnb requests in the Real Estate monitor to dilute request frequency per domain.
* **Amazon ASIN Proliferation:** Due to how Amazon handles variants, ensure that the precise variant URL (often requiring selection of a specific style/size) is provided in `products_to_track.json`. Generic parent URLs may trigger the `variant_required` terminal state.
