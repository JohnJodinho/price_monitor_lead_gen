# Autonomous Scraping Backend: Price Monitor & Lead Generator

An advanced, stealthy, and highly resilient asynchronous scraping backend built with Python 3.11. This project leverages a cascading multi-tier scraping strategy to autonomously monitor product prices and extract business leads while circumventing modern bot protections.

Designed for zero-maintenance production deployment, the backend runs as ephemeral, serverless cron jobs via **GitHub Actions**, persisting all data to a **PostgreSQL** database (e.g., Supabase, Neon) using SQLAlchemy 2.0 Async.

---

## 🏗 Architecture

This project is strictly a background worker. There are no exposed REST APIs or long-running servers. Execution is triggered via GitHub Actions schedules.

### The Multi-Tier Scraping Cascade
To balance speed, cost, and success rate, the backend employs a dynamic escalation strategy:

* **Tier 1: Fast HTTP (Scrapling / curl_cffi)**
  * Extremely fast and memory-efficient.
  * Impersonates Chrome TLS fingerprints to bypass basic WAFs.
  * Used as the first attempt for all URLs.
* **Tier 2: Headless Browser (Patchright)**
  * Full Chromium instance used to render JavaScript-heavy SPAs or bypass complex captchas.
  * The system automatically escalates to Tier 2 if Tier 1 returns thin content, gets blocked by Cloudflare/Datadome, or triggers a captcha.
* **Tier 3: AI Extraction (Groq / LLaMA 3)** *(Price Monitor Only)*
  * If standard CSS selectors fail to find a price on the page, the raw text is passed to an LLM via the Groq API.
  * The LLM intelligently parses complex page layouts to extract the numerical price and currency without brittle regex.

---

## 📦 Core Modules

### 1. Price Monitor (`price_monitor.py`)
Monitors a predefined list of product URLs for price changes.
* **Smart Alerts:** Detects and logs when a price drops below a specific `target_price`, or when a price shifts by a significant percentage compared to the previous run.
* **Data Integrity:** Stores historical price time-series (`price_history`) and explicit alert records (`price_alerts`).

### 2. Lead Generator (`lead_generator.py`)
Scrapes business contact information from a list of target URLs.
* **Extraction:** Pulls emails, phone numbers, and company names from page text and `mailto:` links.
* **Deduplication:** Ensures leads are not duplicated in the database if the scraper runs across the same page multiple times.

---

## 🚀 Deployment & Usage

### 1. Prerequisites
You need a PostgreSQL database (Neon or Supabase work perfectly) and a Groq API key for Tier 3 extraction.

Create a `.env` file for local development:
```env
DATABASE_URL=postgresql+asyncpg://user:password@host/dbname
GROQ_API_KEY=gsk_your_groq_api_key
```

### 2. Configuration
The system uses "config as code". To add or remove targets, you do not need to interact with a database UI. Simply edit the hardcoded lists at the top of the respective Python files:
* `PRODUCTS_TO_TRACK` in `price_monitor.py`
* `LEAD_TARGETS` in `lead_generator.py`

*Note: Removing an item from these lists automatically marks it as inactive in the database during the next run, preserving its historical data while halting future scraping.*

### 3. Local Development
Install dependencies:
```bash
pip install -r requirements.txt
python -m patchright install chromium --with-deps
```

You can run the modules manually to test them:
```bash
# Run the price monitor
python -c "import asyncio; from price_monitor import run_monitor; asyncio.run(run_monitor())"

# Run the lead generator
python -c "import asyncio; from lead_generator import run_lead_gen; asyncio.run(run_lead_gen())"
```

### 4. Production (GitHub Actions)
The project includes two GitHub Actions workflows:
* `.github/workflows/schedule_monitor.yml` (Runs every 6 hours)
* `.github/workflows/schedule_lead_gen.yml` (Runs daily)

To deploy:
1. Push this repository to GitHub.
2. Go to your repository **Settings > Secrets and variables > Actions**.
3. Add `DATABASE_URL` and `GROQ_API_KEY` as repository secrets.
4. The cron jobs will automatically start running on schedule.

---

## 🗄️ Database Schema & Run Tracking

The backend uses **SQLAlchemy 2.0 Async**. Tables are created automatically on the first run (`create_tables()` is called idempotently).

**Telemetry & Observability:**
Every execution of the Price Monitor or Lead Generator creates a `ScrapeRun` record in the database. This allows you to track:
* Start and end times (duration).
* Overall job status (`running`, `success`, `failed`).
* Granular success rates (`items_attempted`, `items_succeeded`, `items_failed`).
* Fatal error summaries if the job crashed.

---

## 🛡️ Fault Tolerance & Isolation
* **Per-Item Commits:** If processing 10 products and the 5th one crashes the browser, the first 4 are safely committed to the database. The system catches the error, logs it, and continues to the 6th product.
* **Session Isolation:** Each target is processed in its own isolated SQLAlchemy `AsyncSession`. A database rollback on one item will never poison or expire the ORM objects of subsequent items.
