-- Persist only deterministic, authenticated LPR observations from an approved
-- CFETS source snapshot.  A browser may choose the exact source locator but
-- cannot provide or alter the rate value used by an LPR-multiple rule.

BEGIN;

CREATE TABLE official_lpr_observations (
    snapshot_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    parsed_output_hash char(64) NOT NULL CHECK (parsed_output_hash ~ '^[0-9a-f]{64}$'),
    publication_date date NOT NULL,
    effective_from date NOT NULL,
    effective_until date,
    one_year_rate numeric(18,12) NOT NULL CHECK (one_year_rate > 0 AND one_year_rate < 1),
    five_year_plus_rate numeric(18,12) NOT NULL CHECK (five_year_plus_rate > 0 AND five_year_plus_rate < 1),
    source_locator text NOT NULL CHECK (length(trim(source_locator)) > 0 AND length(source_locator) <= 1000),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (snapshot_id, publication_date),
    UNIQUE (snapshot_id, source_locator),
    CHECK (publication_date = effective_from),
    CHECK (effective_until IS NULL OR effective_from < effective_until),
    FOREIGN KEY (snapshot_id, firm_id, content_sha256)
        REFERENCES official_legal_source_snapshots(snapshot_id, firm_id, content_sha256)
);

CREATE INDEX official_lpr_observations_lookup_idx
    ON official_lpr_observations (firm_id, snapshot_id, source_locator);

ALTER TABLE official_lpr_observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE official_lpr_observations FORCE ROW LEVEL SECURITY;

CREATE POLICY official_lpr_observations_firm_isolation ON official_lpr_observations
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

CREATE FUNCTION prohibit_official_lpr_observation_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'official LPR observations are append-only';
END;
$$;

CREATE TRIGGER official_lpr_observations_append_only
    BEFORE UPDATE OR DELETE ON official_lpr_observations
    FOR EACH ROW EXECUTE FUNCTION prohibit_official_lpr_observation_mutation();

COMMIT;
