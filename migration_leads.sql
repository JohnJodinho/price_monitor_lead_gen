-- 1. Add the new JSONB columns
ALTER TABLE leads ADD COLUMN IF NOT EXISTS contacts JSONB;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS socials JSONB;

-- 2. Aggregate existing phone and email columns into JSONB
-- We assume all old emails are "emailWithDomain" equivalents since they weren't explicitly from mailto: links.
WITH aggregated AS (
    SELECT
        target_id,
        source_url,
        (array_agg(id))[1] AS primary_id,
        jsonb_build_object(
            'phone', COALESCE(jsonb_agg(DISTINCT phone) FILTER (WHERE phone IS NOT NULL), '[]'::jsonb),
            'email', '[]'::jsonb,
            'emailWithDomain', COALESCE(jsonb_agg(DISTINCT email) FILTER (WHERE email IS NOT NULL), '[]'::jsonb)
        ) AS contacts,
        jsonb_build_object(
            'X(twitter)', '[]'::jsonb,
            'Facebook', '[]'::jsonb,
            'Whatsapp', '[]'::jsonb,
            'Instagram', '[]'::jsonb,
            'linkedIn', '[]'::jsonb
        ) AS socials
    FROM leads
    GROUP BY target_id, source_url
)
UPDATE leads
SET
    contacts = a.contacts,
    socials = a.socials
FROM aggregated a
WHERE leads.id = a.primary_id;

-- 3. Delete duplicates (the rows that didn't get the updated JSONB columns)
DELETE FROM leads
WHERE contacts IS NULL;

-- 4. Set NOT NULL on the JSONB columns if desired (optional but good practice)
-- ALTER TABLE leads ALTER COLUMN contacts SET NOT NULL;
-- ALTER TABLE leads ALTER COLUMN socials SET NOT NULL;

-- 5. Drop old constraint and add the new one
ALTER TABLE leads DROP CONSTRAINT IF EXISTS uq_lead_target_email;
ALTER TABLE leads ADD CONSTRAINT uq_lead_target_url UNIQUE (target_id, source_url);

-- 6. Drop the obsolete columns (CASCADE will also drop the dependent view v_recent_leads if it exists)
ALTER TABLE leads DROP COLUMN IF EXISTS email CASCADE;
ALTER TABLE leads DROP COLUMN IF EXISTS phone CASCADE;
ALTER TABLE leads DROP COLUMN IF EXISTS contact_name CASCADE;

-- 7. Recreate the view v_recent_leads to use the new JSONB fields
CREATE OR REPLACE VIEW v_recent_leads AS
SELECT
    l.id,
    lt.url      AS target_url,
    lt.category,
    l.contacts,
    l.socials,
    l.company_name,
    l.source_url,
    l.created_at
FROM leads l
JOIN lead_targets lt ON lt.id = l.target_id
WHERE l.created_at >= NOW() - INTERVAL '7 days'
ORDER BY l.created_at DESC;

-- Ensure read-only access for the anon role on the newly created view
GRANT SELECT ON v_recent_leads TO anon;
