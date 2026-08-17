-- ==============================================================================
-- SCHEMA DEFINITION (PostgreSQL / Supabase)
-- Generated directly from models.py
-- ==============================================================================

-- 1. Enable Required Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";

-- 2. Clean Existing Tables (Optional / Teardown Order)
-- DROP TABLE IF EXISTS re_knowledge_base CASCADE;
-- DROP TABLE IF EXISTS rate_history CASCADE;
-- DROP TABLE IF EXISTS properties CASCADE;
-- DROP TABLE IF EXISTS scrape_runs CASCADE;
-- DROP TABLE IF EXISTS leads CASCADE;
-- DROP TABLE IF EXISTS lead_targets CASCADE;
-- DROP TABLE IF EXISTS price_alerts CASCADE;
-- DROP TABLE IF EXISTS price_history CASCADE;
-- DROP TABLE IF EXISTS products CASCADE;
-- DROP TABLE IF EXISTS clients CASCADE;

-- ==============================================================================
-- TABLE: clients
-- ==============================================================================
CREATE TABLE IF NOT EXISTS clients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_name VARCHAR(500) NOT NULL,
    contact_email VARCHAR(300),
    vertical VARCHAR(20) NOT NULL, -- 'ecommerce' | 'real_estate'
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_clients_vertical ON clients(vertical);

-- ==============================================================================
-- TABLE: products (E-Commerce Tracking)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID REFERENCES clients(id) ON DELETE SET NULL,
    sku VARCHAR(200),
    name VARCHAR(500) NOT NULL,
    category VARCHAR(200) DEFAULT 'uncategorized',
    url TEXT NOT NULL UNIQUE,
    retailer VARCHAR(100),
    target_price NUMERIC(10, 2),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    consecutive_404s INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_products_sku ON products(sku);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
CREATE INDEX IF NOT EXISTS idx_products_retailer ON products(retailer);
CREATE INDEX IF NOT EXISTS idx_products_client_id ON products(client_id);

-- ==============================================================================
-- TABLE: price_history (E-Commerce Time-Series)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS price_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    price NUMERIC(10, 2),
    currency VARCHAR(10) NOT NULL DEFAULT 'USD',
    in_stock BOOLEAN NOT NULL DEFAULT TRUE,
    merchant VARCHAR(255),
    tier_used INTEGER NOT NULL,
    meta_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_price_history_product_id ON price_history(product_id);
CREATE INDEX IF NOT EXISTS idx_price_history_created_at ON price_history(created_at DESC);

-- ==============================================================================
-- TABLE: price_alerts (E-Commerce Alerts)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS price_alerts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    alert_type VARCHAR(20) NOT NULL, -- 'THRESHOLD' | 'CHANGE'
    price_at_alert NUMERIC(10, 2) NOT NULL,
    target_price NUMERIC(10, 2),
    previous_price NUMERIC(10, 2),
    pct_change NUMERIC(5, 2),
    acknowledged BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_valid_alert_data CHECK (
        (alert_type = 'THRESHOLD' AND target_price IS NOT NULL AND previous_price IS NULL AND pct_change IS NULL) OR
        (alert_type = 'CHANGE' AND previous_price IS NOT NULL AND pct_change IS NOT NULL AND target_price IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_price_alerts_product_id ON price_alerts(product_id);

-- ==============================================================================
-- TABLE: lead_targets (Lead Gen Seeds)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS lead_targets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    url TEXT NOT NULL UNIQUE,
    category VARCHAR(200),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ==============================================================================
-- TABLE: leads (Business Leads)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS leads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_id UUID NOT NULL REFERENCES lead_targets(id) ON DELETE CASCADE,
    related_alert_id UUID REFERENCES price_alerts(id) ON DELETE SET NULL,
    company_name VARCHAR(500),
    source_url TEXT,
    contacts JSONB NOT NULL DEFAULT '{}'::jsonb,
    socials JSONB NOT NULL DEFAULT '{}'::jsonb,
    pitch_summary TEXT,
    outreach_status VARCHAR(20) NOT NULL DEFAULT 'not_contacted', -- 'not_contacted'|'contacted'|'replied'|'closed'|'dead'
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_lead_target_url UNIQUE (target_id, source_url)
);

CREATE INDEX IF NOT EXISTS idx_leads_target_id ON leads(target_id);

-- ==============================================================================
-- TABLE: scrape_runs (Pipeline Execution Logs)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS scrape_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type VARCHAR(50) NOT NULL, -- 'price_monitor' | 'lead_gen' | 'real_estate_monitor'
    status VARCHAR(20) NOT NULL,   -- 'running' | 'success' | 'failed'
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    items_attempted INTEGER NOT NULL DEFAULT 0,
    items_succeeded INTEGER NOT NULL DEFAULT 0,
    items_failed INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT,
    platform VARCHAR(200),
    anomalies_captured INTEGER NOT NULL DEFAULT 0,
    meta_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scrape_runs_job_type ON scrape_runs(job_type);
CREATE INDEX IF NOT EXISTS idx_scrape_runs_started_at ON scrape_runs(started_at DESC);

-- ==============================================================================
-- TABLE: properties (Real Estate Listings Monitored)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS properties (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID REFERENCES clients(id) ON DELETE SET NULL,
    name VARCHAR(500) NOT NULL,
    property_key VARCHAR(200), -- groups multiple OTA listings of same physical property
    platform VARCHAR(50) NOT NULL, -- 'airbnb' | 'vrbo' | 'booking'
    url TEXT NOT NULL UNIQUE,
    market VARCHAR(200) NOT NULL, -- 'NYC/NJ Metro', 'Miami'
    bedrooms INTEGER,
    host_name VARCHAR(300),
    host_profile_url VARCHAR(500),
    cleaning_fee NUMERIC(10, 2),
    review_count INTEGER,
    avg_rating NUMERIC(3, 2),
    latitude NUMERIC(10, 7),
    longitude NUMERIC(10, 7),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    consecutive_404s INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_properties_property_key ON properties(property_key);
CREATE INDEX IF NOT EXISTS idx_properties_platform ON properties(platform);
CREATE INDEX IF NOT EXISTS idx_properties_market ON properties(market);
CREATE INDEX IF NOT EXISTS idx_properties_client_id ON properties(client_id);

-- ==============================================================================
-- TABLE: rate_history (Real Estate Nightly Rates & Availability Snapshots)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS rate_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    stay_date TIMESTAMPTZ NOT NULL,
    nightly_rate NUMERIC(10, 2), -- NULL when booked / unavailable
    is_available BOOLEAN NOT NULL DEFAULT TRUE,
    currency VARCHAR(10) NOT NULL DEFAULT 'USD',
    meta_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    dlq_html_url TEXT,
    dlq_screenshot_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rate_history_property_id ON rate_history(property_id);
CREATE INDEX IF NOT EXISTS idx_rate_history_stay_date ON rate_history(stay_date);
CREATE INDEX IF NOT EXISTS idx_rate_history_created_at ON rate_history(created_at DESC);

-- ==============================================================================
-- TABLE: re_knowledge_base (RAG Knowledge Base + pgvector)
-- ==============================================================================
CREATE TABLE IF NOT EXISTS re_knowledge_base (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    section_title VARCHAR(255) NOT NULL,
    chunk_content TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding VECTOR(384),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_re_kb_embedding 
ON re_knowledge_base 
USING hnsw (embedding vector_cosine_ops);

-- ==============================================================================
-- CORE ANALYTICAL VIEWS
-- ==============================================================================

-- 1. Real Estate Volatility & Trailing Benchmark View
CREATE OR REPLACE VIEW v_rate_volatility AS
WITH day_ranked AS (
    SELECT 
        rh.id,
        rh.property_id,
        rh.stay_date,
        rh.nightly_rate,
        rh.is_available,
        rh.currency,
        rh.meta_data,
        rh.created_at,
        rh.updated_at,
        dense_rank() OVER (PARTITION BY rh.property_id ORDER BY date(rh.created_at AT TIME ZONE 'America/New_York')) AS day_rank
    FROM rate_history rh
    WHERE rh.nightly_rate IS NOT NULL
), 
daily_avg AS (
    SELECT 
        property_id,
        day_rank,
        avg(nightly_rate) AS day_avg
    FROM day_ranked
    GROUP BY property_id, day_rank
), 
trailing_data AS (
    SELECT 
        property_id,
        day_rank,
        avg(day_avg) OVER (
            PARTITION BY property_id 
            ORDER BY day_rank 
            ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING
        ) AS trailing_avg_rate
    FROM daily_avg
), 
valid_rates AS (
    SELECT 
        dr.id,
        dr.property_id,
        dr.stay_date,
        dr.nightly_rate,
        dr.created_at,
        t.trailing_avg_rate
    FROM day_ranked dr
    JOIN trailing_data t ON t.property_id = dr.property_id AND t.day_rank = dr.day_rank
)
SELECT 
    rh.id,
    p.id AS property_id,
    p.name AS property_name,
    p.url,
    p.market,
    p.platform,
    p.latitude,
    p.longitude,
    p.bedrooms,
    p.avg_rating,
    p.review_count,
    p.is_active,
    rh.stay_date,
    rh.nightly_rate,
    rh.is_available,
    rh.currency,
    rh.created_at AS recorded_at,
    vr.trailing_avg_rate,
    round(((rh.nightly_rate - vr.trailing_avg_rate) / NULLIF(vr.trailing_avg_rate, 0::numeric)) * 100::numeric, 2) AS pct_above_trailing_avg
FROM rate_history rh
JOIN properties p ON p.id = rh.property_id
LEFT JOIN valid_rates vr ON vr.id = rh.id;

-- 2. Scrape Pipeline Health View
CREATE OR REPLACE VIEW v_scrape_health AS
WITH recent AS (
    SELECT 
        sr.*,
        row_number() OVER (PARTITION BY sr.job_type, sr.platform ORDER BY sr.started_at DESC) AS rn
    FROM scrape_runs sr
)
SELECT 
    job_type,
    platform,
    status AS last_status,
    started_at AS last_started_at,
    finished_at AS last_finished_at,
    EXTRACT(epoch FROM finished_at - started_at) AS last_duration_seconds,
    items_attempted,
    items_succeeded,
    items_failed,
    meta_data,
    error_summary,
    (status = 'failed') AS is_failed,
    ((items_failed::float / NULLIF(items_attempted, 0)::float) > 0.20) AS high_failure_rate,
    (COALESCE((meta_data->>'blocked_count')::int, 0) > 0) AS has_blocks
FROM recent
WHERE rn = 1;
