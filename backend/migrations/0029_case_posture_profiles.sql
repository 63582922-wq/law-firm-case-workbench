-- Versioned, lawyer-confirmed case posture inputs for dynamic Agent planning.
--
-- This migration deliberately does not map a party position to a fixed material
-- checklist or work-product template.  It records the stable parties, court
-- proceeding, position in that proceeding and the firm's engagement as exact,
-- auditable inputs.  A separate planner must combine the current profile with
-- the actual record, deadlines and versioned legal authority.

BEGIN;

CREATE TABLE case_parties (
    party_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (party_id, firm_id, matter_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id)
);

CREATE TABLE case_party_versions (
    party_version_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    party_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    party_version bigint NOT NULL CHECK (party_version > 0),
    party_kind text NOT NULL CHECK (party_kind ~ '^[A-Z][A-Z0-9_]{1,63}$'),
    display_label text NOT NULL CHECK (length(trim(display_label)) BETWEEN 1 AND 200),
    basis_hash char(64) NOT NULL CHECK (basis_hash ~ '^[0-9a-f]{64}$'),
    meaning_hash char(64) NOT NULL CHECK (meaning_hash ~ '^[0-9a-f]{64}$'),
    confirmed_matter_version integer NOT NULL CHECK (confirmed_matter_version > 0),
    confirmed_by uuid NOT NULL,
    confirmed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (party_id, party_version),
    UNIQUE (party_version_id, party_id, firm_id, matter_id),
    FOREIGN KEY (party_id, firm_id, matter_id)
        REFERENCES case_parties(party_id, firm_id, matter_id),
    FOREIGN KEY (confirmed_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE case_party_heads (
    party_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    latest_version bigint NOT NULL CHECK (latest_version > 0),
    current_version_id uuid NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (party_id, firm_id, matter_id),
    FOREIGN KEY (party_id, firm_id, matter_id)
        REFERENCES case_parties(party_id, firm_id, matter_id),
    FOREIGN KEY (current_version_id, party_id, firm_id, matter_id)
        REFERENCES case_party_versions(party_version_id, party_id, firm_id, matter_id)
);

ALTER TABLE case_parties ADD CONSTRAINT case_parties_head_same_party
    FOREIGN KEY (party_id, firm_id, matter_id)
    REFERENCES case_party_heads(party_id, firm_id, matter_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE court_proceedings (
    proceeding_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (proceeding_id, firm_id, matter_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id)
);

CREATE TABLE court_proceeding_versions (
    proceeding_version_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    proceeding_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    proceeding_version bigint NOT NULL CHECK (proceeding_version > 0),
    forum_type text NOT NULL CHECK (forum_type ~ '^[A-Z][A-Z0-9_]{1,63}$'),
    case_type_code text NOT NULL CHECK (case_type_code ~ '^[A-Z][A-Z0-9_.]{1,127}$'),
    procedure_stage text NOT NULL CHECK (procedure_stage ~ '^[A-Z][A-Z0-9_]{1,63}$'),
    basis_hash char(64) NOT NULL CHECK (basis_hash ~ '^[0-9a-f]{64}$'),
    meaning_hash char(64) NOT NULL CHECK (meaning_hash ~ '^[0-9a-f]{64}$'),
    confirmed_matter_version integer NOT NULL CHECK (confirmed_matter_version > 0),
    confirmed_by uuid NOT NULL,
    confirmed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (proceeding_id, proceeding_version),
    UNIQUE (proceeding_version_id, proceeding_id, firm_id, matter_id),
    FOREIGN KEY (proceeding_id, firm_id, matter_id)
        REFERENCES court_proceedings(proceeding_id, firm_id, matter_id),
    FOREIGN KEY (confirmed_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE court_proceeding_heads (
    proceeding_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    latest_version bigint NOT NULL CHECK (latest_version > 0),
    current_version_id uuid NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (proceeding_id, firm_id, matter_id),
    FOREIGN KEY (proceeding_id, firm_id, matter_id)
        REFERENCES court_proceedings(proceeding_id, firm_id, matter_id),
    FOREIGN KEY (current_version_id, proceeding_id, firm_id, matter_id)
        REFERENCES court_proceeding_versions(proceeding_version_id, proceeding_id, firm_id, matter_id)
);

ALTER TABLE court_proceedings ADD CONSTRAINT court_proceedings_head_same_proceeding
    FOREIGN KEY (proceeding_id, firm_id, matter_id)
    REFERENCES court_proceeding_heads(proceeding_id, firm_id, matter_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE court_party_positions (
    position_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    proceeding_id uuid NOT NULL,
    party_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (position_id, firm_id, matter_id),
    FOREIGN KEY (proceeding_id, firm_id, matter_id)
        REFERENCES court_proceedings(proceeding_id, firm_id, matter_id),
    FOREIGN KEY (party_id, firm_id, matter_id)
        REFERENCES case_parties(party_id, firm_id, matter_id)
);

CREATE TABLE court_party_position_versions (
    position_version_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    position_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    position_version bigint NOT NULL CHECK (position_version > 0),
    position_code text NOT NULL CHECK (position_code ~ '^[A-Z][A-Z0-9_]{1,63}$'),
    basis_hash char(64) NOT NULL CHECK (basis_hash ~ '^[0-9a-f]{64}$'),
    meaning_hash char(64) NOT NULL CHECK (meaning_hash ~ '^[0-9a-f]{64}$'),
    confirmed_matter_version integer NOT NULL CHECK (confirmed_matter_version > 0),
    confirmed_by uuid NOT NULL,
    confirmed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (position_id, position_version),
    UNIQUE (position_version_id, position_id, firm_id, matter_id),
    FOREIGN KEY (position_id, firm_id, matter_id)
        REFERENCES court_party_positions(position_id, firm_id, matter_id),
    FOREIGN KEY (confirmed_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE court_party_position_heads (
    position_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    latest_version bigint NOT NULL CHECK (latest_version > 0),
    current_version_id uuid NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (position_id, firm_id, matter_id),
    FOREIGN KEY (position_id, firm_id, matter_id)
        REFERENCES court_party_positions(position_id, firm_id, matter_id),
    FOREIGN KEY (current_version_id, position_id, firm_id, matter_id)
        REFERENCES court_party_position_versions(position_version_id, position_id, firm_id, matter_id)
);

ALTER TABLE court_party_positions ADD CONSTRAINT court_party_positions_head_same_position
    FOREIGN KEY (position_id, firm_id, matter_id)
    REFERENCES court_party_position_heads(position_id, firm_id, matter_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE firm_engagements (
    engagement_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    proceeding_id uuid NOT NULL,
    represented_party_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (engagement_id, firm_id, matter_id),
    FOREIGN KEY (proceeding_id, firm_id, matter_id)
        REFERENCES court_proceedings(proceeding_id, firm_id, matter_id),
    FOREIGN KEY (represented_party_id, firm_id, matter_id)
        REFERENCES case_parties(party_id, firm_id, matter_id)
);

CREATE TABLE firm_engagement_versions (
    engagement_version_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    engagement_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    engagement_version bigint NOT NULL CHECK (engagement_version > 0),
    authority_scope_code text NOT NULL CHECK (authority_scope_code ~ '^[A-Z][A-Z0-9_]{1,63}$'),
    engagement_state text NOT NULL CHECK (engagement_state IN ('ACTIVE', 'ENDED')),
    basis_hash char(64) NOT NULL CHECK (basis_hash ~ '^[0-9a-f]{64}$'),
    meaning_hash char(64) NOT NULL CHECK (meaning_hash ~ '^[0-9a-f]{64}$'),
    confirmed_matter_version integer NOT NULL CHECK (confirmed_matter_version > 0),
    confirmed_by uuid NOT NULL,
    confirmed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (engagement_id, engagement_version),
    UNIQUE (engagement_version_id, engagement_id, firm_id, matter_id),
    FOREIGN KEY (engagement_id, firm_id, matter_id)
        REFERENCES firm_engagements(engagement_id, firm_id, matter_id),
    FOREIGN KEY (confirmed_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE firm_engagement_heads (
    engagement_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    latest_version bigint NOT NULL CHECK (latest_version > 0),
    current_version_id uuid NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (engagement_id, firm_id, matter_id),
    FOREIGN KEY (engagement_id, firm_id, matter_id)
        REFERENCES firm_engagements(engagement_id, firm_id, matter_id),
    FOREIGN KEY (current_version_id, engagement_id, firm_id, matter_id)
        REFERENCES firm_engagement_versions(engagement_version_id, engagement_id, firm_id, matter_id)
);

ALTER TABLE firm_engagements ADD CONSTRAINT firm_engagements_head_same_engagement
    FOREIGN KEY (engagement_id, firm_id, matter_id)
    REFERENCES firm_engagement_heads(engagement_id, firm_id, matter_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE case_posture_profiles (
    profile_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    profile_version bigint NOT NULL CHECK (profile_version > 0),
    status text NOT NULL CHECK (status = 'CONFIRMED'),
    represented_party_id uuid NOT NULL,
    represented_party_version_id uuid NOT NULL,
    proceeding_id uuid NOT NULL,
    proceeding_version_id uuid NOT NULL,
    position_id uuid NOT NULL,
    position_version_id uuid NOT NULL,
    engagement_id uuid NOT NULL,
    engagement_version_id uuid NOT NULL,
    case_type_code text NOT NULL CHECK (case_type_code ~ '^[A-Z][A-Z0-9_.]{1,127}$'),
    procedure_stage text NOT NULL CHECK (procedure_stage ~ '^[A-Z][A-Z0-9_]{1,63}$'),
    represented_position text NOT NULL CHECK (represented_position ~ '^[A-Z][A-Z0-9_]{1,63}$'),
    authority_scope_code text NOT NULL CHECK (authority_scope_code ~ '^[A-Z][A-Z0-9_]{1,63}$'),
    engagement_state text NOT NULL CHECK (engagement_state = 'ACTIVE'),
    profile_hash char(64) NOT NULL CHECK (profile_hash ~ '^[0-9a-f]{64}$'),
    confirmed_matter_version integer NOT NULL CHECK (confirmed_matter_version > 0),
    supersedes_profile_id uuid,
    confirmed_by uuid NOT NULL,
    confirmed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (matter_id, profile_version),
    UNIQUE (profile_id, firm_id, matter_id),
    UNIQUE (matter_id, profile_hash),
    FOREIGN KEY (represented_party_version_id, represented_party_id, firm_id, matter_id)
        REFERENCES case_party_versions(party_version_id, party_id, firm_id, matter_id),
    FOREIGN KEY (proceeding_version_id, proceeding_id, firm_id, matter_id)
        REFERENCES court_proceeding_versions(proceeding_version_id, proceeding_id, firm_id, matter_id),
    FOREIGN KEY (position_version_id, position_id, firm_id, matter_id)
        REFERENCES court_party_position_versions(position_version_id, position_id, firm_id, matter_id),
    FOREIGN KEY (engagement_version_id, engagement_id, firm_id, matter_id)
        REFERENCES firm_engagement_versions(engagement_version_id, engagement_id, firm_id, matter_id),
    FOREIGN KEY (supersedes_profile_id, firm_id, matter_id)
        REFERENCES case_posture_profiles(profile_id, firm_id, matter_id),
    FOREIGN KEY (confirmed_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE case_posture_profile_heads (
    matter_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    latest_profile_version bigint NOT NULL CHECK (latest_profile_version > 0),
    current_profile_id uuid,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (matter_id, firm_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (current_profile_id, firm_id, matter_id)
        REFERENCES case_posture_profiles(profile_id, firm_id, matter_id)
);

CREATE TABLE case_posture_profile_events (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    event_sequence integer NOT NULL CHECK (event_sequence > 0),
    effective_status text NOT NULL CHECK (effective_status IN ('CURRENT', 'STALE', 'SUPERSEDED')),
    cause_kind text NOT NULL CHECK (cause_kind IN (
        'PROFILE_CONFIRMED', 'PARTY_VERSION_CHANGED', 'PROCEEDING_VERSION_CHANGED',
        'POSITION_VERSION_CHANGED', 'ENGAGEMENT_VERSION_CHANGED', 'PROFILE_REPLACED'
    )),
    cause_object_id uuid NOT NULL,
    actor_id uuid,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (profile_id, event_sequence),
    UNIQUE (profile_id, effective_status),
    FOREIGN KEY (profile_id, firm_id, matter_id)
        REFERENCES case_posture_profiles(profile_id, firm_id, matter_id),
    FOREIGN KEY (actor_id, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE INDEX case_party_versions_matter_idx
    ON case_party_versions(matter_id, party_id, party_version DESC);
CREATE INDEX court_proceeding_versions_matter_idx
    ON court_proceeding_versions(matter_id, proceeding_id, proceeding_version DESC);
CREATE INDEX court_party_positions_matter_idx
    ON court_party_positions(matter_id, proceeding_id, party_id);
CREATE INDEX firm_engagements_matter_idx
    ON firm_engagements(matter_id, proceeding_id, represented_party_id);
CREATE INDEX case_posture_profiles_matter_idx
    ON case_posture_profiles(matter_id, profile_version DESC);
CREATE INDEX case_posture_profile_events_profile_idx
    ON case_posture_profile_events(profile_id, event_sequence DESC);

CREATE FUNCTION prohibit_case_posture_meaning_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'confirmed case posture meanings and events are append-only';
END;
$$;

CREATE TRIGGER case_parties_append_only BEFORE UPDATE OR DELETE ON case_parties
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_posture_meaning_mutation();
CREATE TRIGGER case_party_versions_append_only BEFORE UPDATE OR DELETE ON case_party_versions
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_posture_meaning_mutation();
CREATE TRIGGER court_proceedings_append_only BEFORE UPDATE OR DELETE ON court_proceedings
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_posture_meaning_mutation();
CREATE TRIGGER court_proceeding_versions_append_only BEFORE UPDATE OR DELETE ON court_proceeding_versions
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_posture_meaning_mutation();
CREATE TRIGGER court_party_positions_append_only BEFORE UPDATE OR DELETE ON court_party_positions
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_posture_meaning_mutation();
CREATE TRIGGER court_party_position_versions_append_only BEFORE UPDATE OR DELETE ON court_party_position_versions
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_posture_meaning_mutation();
CREATE TRIGGER firm_engagements_append_only BEFORE UPDATE OR DELETE ON firm_engagements
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_posture_meaning_mutation();
CREATE TRIGGER firm_engagement_versions_append_only BEFORE UPDATE OR DELETE ON firm_engagement_versions
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_posture_meaning_mutation();
CREATE TRIGGER case_posture_profiles_append_only BEFORE UPDATE OR DELETE ON case_posture_profiles
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_posture_meaning_mutation();
CREATE TRIGGER case_posture_profile_events_append_only BEFORE UPDATE OR DELETE ON case_posture_profile_events
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_posture_meaning_mutation();

CREATE FUNCTION validate_case_posture_version_head() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    actual_version bigint;
    actual_entity_id uuid;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'case posture version head cannot be deleted';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF to_jsonb(NEW) - ARRAY['latest_version','current_version_id','updated_at']
           IS DISTINCT FROM to_jsonb(OLD) - ARRAY['latest_version','current_version_id','updated_at']
           OR NEW.latest_version <> OLD.latest_version + 1 THEN
            RAISE EXCEPTION 'case posture version head transition is invalid';
        END IF;
    END IF;

    IF TG_TABLE_NAME = 'case_party_heads' THEN
        SELECT party_version, party_id INTO actual_version, actual_entity_id
          FROM case_party_versions WHERE party_version_id = NEW.current_version_id;
        IF actual_version <> NEW.latest_version OR actual_entity_id <> NEW.party_id THEN
            RAISE EXCEPTION 'case party head does not reference its exact latest version';
        END IF;
    ELSIF TG_TABLE_NAME = 'court_proceeding_heads' THEN
        SELECT proceeding_version, proceeding_id INTO actual_version, actual_entity_id
          FROM court_proceeding_versions WHERE proceeding_version_id = NEW.current_version_id;
        IF actual_version <> NEW.latest_version OR actual_entity_id <> NEW.proceeding_id THEN
            RAISE EXCEPTION 'court proceeding head does not reference its exact latest version';
        END IF;
    ELSIF TG_TABLE_NAME = 'court_party_position_heads' THEN
        SELECT position_version, position_id INTO actual_version, actual_entity_id
          FROM court_party_position_versions WHERE position_version_id = NEW.current_version_id;
        IF actual_version <> NEW.latest_version OR actual_entity_id <> NEW.position_id THEN
            RAISE EXCEPTION 'court party position head does not reference its exact latest version';
        END IF;
    ELSIF TG_TABLE_NAME = 'firm_engagement_heads' THEN
        SELECT engagement_version, engagement_id INTO actual_version, actual_entity_id
          FROM firm_engagement_versions WHERE engagement_version_id = NEW.current_version_id;
        IF actual_version <> NEW.latest_version OR actual_entity_id <> NEW.engagement_id THEN
            RAISE EXCEPTION 'firm engagement head does not reference its exact latest version';
        END IF;
    ELSE
        RAISE EXCEPTION 'unsupported case posture version head';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_party_heads_guard BEFORE INSERT OR UPDATE OR DELETE ON case_party_heads
    FOR EACH ROW EXECUTE FUNCTION validate_case_posture_version_head();
CREATE TRIGGER court_proceeding_heads_guard BEFORE INSERT OR UPDATE OR DELETE ON court_proceeding_heads
    FOR EACH ROW EXECUTE FUNCTION validate_case_posture_version_head();
CREATE TRIGGER court_party_position_heads_guard BEFORE INSERT OR UPDATE OR DELETE ON court_party_position_heads
    FOR EACH ROW EXECUTE FUNCTION validate_case_posture_version_head();
CREATE TRIGGER firm_engagement_heads_guard BEFORE INSERT OR UPDATE OR DELETE ON firm_engagement_heads
    FOR EACH ROW EXECUTE FUNCTION validate_case_posture_version_head();

CREATE FUNCTION validate_case_posture_profile_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    expected_profile_version bigint;
    expected_supersedes uuid;
    derived_case_type text;
    derived_stage text;
    derived_position text;
    derived_scope text;
    derived_engagement_state text;
    expected_hash text;
BEGIN
    SELECT COALESCE(latest_profile_version, 0) + 1
      INTO expected_profile_version
      FROM case_posture_profile_heads
     WHERE matter_id = NEW.matter_id AND firm_id = NEW.firm_id;
    IF NOT FOUND THEN expected_profile_version := 1; END IF;
    SELECT profile_id INTO expected_supersedes
      FROM case_posture_profiles
     WHERE matter_id = NEW.matter_id AND firm_id = NEW.firm_id
     ORDER BY profile_version DESC LIMIT 1;

    IF NEW.profile_version <> expected_profile_version
       OR NEW.supersedes_profile_id IS DISTINCT FROM expected_supersedes THEN
        RAISE EXCEPTION 'case posture profile version lineage is invalid';
    END IF;

    SELECT cpv.case_type_code, cpv.procedure_stage,
           ppv.position_code, fev.authority_scope_code, fev.engagement_state
      INTO derived_case_type, derived_stage, derived_position, derived_scope, derived_engagement_state
      FROM case_party_heads ph
      JOIN case_party_versions pv
        ON pv.party_version_id = ph.current_version_id
       AND pv.party_id = ph.party_id AND pv.firm_id = ph.firm_id AND pv.matter_id = ph.matter_id
      JOIN court_proceeding_heads cph
        ON cph.proceeding_id = NEW.proceeding_id AND cph.firm_id = NEW.firm_id AND cph.matter_id = NEW.matter_id
      JOIN court_proceeding_versions cpv
        ON cpv.proceeding_version_id = cph.current_version_id
       AND cpv.proceeding_id = cph.proceeding_id AND cpv.firm_id = cph.firm_id AND cpv.matter_id = cph.matter_id
      JOIN court_party_positions pp
        ON pp.position_id = NEW.position_id AND pp.party_id = NEW.represented_party_id
       AND pp.proceeding_id = NEW.proceeding_id AND pp.firm_id = NEW.firm_id AND pp.matter_id = NEW.matter_id
      JOIN court_party_position_heads pph
        ON pph.position_id = pp.position_id AND pph.firm_id = pp.firm_id AND pph.matter_id = pp.matter_id
      JOIN court_party_position_versions ppv
        ON ppv.position_version_id = pph.current_version_id
       AND ppv.position_id = pph.position_id AND ppv.firm_id = pph.firm_id AND ppv.matter_id = pph.matter_id
      JOIN firm_engagements fe
        ON fe.engagement_id = NEW.engagement_id AND fe.represented_party_id = NEW.represented_party_id
       AND fe.proceeding_id = NEW.proceeding_id AND fe.firm_id = NEW.firm_id AND fe.matter_id = NEW.matter_id
      JOIN firm_engagement_heads feh
        ON feh.engagement_id = fe.engagement_id AND feh.firm_id = fe.firm_id AND feh.matter_id = fe.matter_id
      JOIN firm_engagement_versions fev
        ON fev.engagement_version_id = feh.current_version_id
       AND fev.engagement_id = feh.engagement_id AND fev.firm_id = feh.firm_id AND fev.matter_id = feh.matter_id
     WHERE ph.party_id = NEW.represented_party_id
       AND ph.firm_id = NEW.firm_id AND ph.matter_id = NEW.matter_id
       AND ph.current_version_id = NEW.represented_party_version_id
       AND cph.current_version_id = NEW.proceeding_version_id
       AND pph.current_version_id = NEW.position_version_id
       AND feh.current_version_id = NEW.engagement_version_id;
    IF NOT FOUND OR derived_engagement_state <> 'ACTIVE'
       OR NEW.case_type_code <> derived_case_type OR NEW.procedure_stage <> derived_stage
       OR NEW.represented_position <> derived_position OR NEW.authority_scope_code <> derived_scope
       OR NEW.engagement_state <> derived_engagement_state THEN
        RAISE EXCEPTION 'case posture profile must bind the exact current compatible upstream versions';
    END IF;

    expected_hash := encode(digest(concat_ws('|',
        'case-posture-profile-v1', NEW.firm_id::text, NEW.matter_id::text,
        NEW.profile_version::text, NEW.represented_party_id::text,
        NEW.represented_party_version_id::text, NEW.proceeding_id::text,
        NEW.proceeding_version_id::text, NEW.position_id::text,
        NEW.position_version_id::text, NEW.engagement_id::text,
        NEW.engagement_version_id::text, NEW.case_type_code,
        NEW.procedure_stage, NEW.represented_position,
        NEW.authority_scope_code, NEW.engagement_state
    ), 'sha256'), 'hex');
    IF NEW.profile_hash <> expected_hash THEN
        RAISE EXCEPTION 'case posture profile hash does not bind its exact server inputs';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER case_posture_profiles_insert_guard BEFORE INSERT ON case_posture_profiles
    FOR EACH ROW EXECUTE FUNCTION validate_case_posture_profile_insert();

CREATE FUNCTION validate_case_posture_profile_event_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    previous_status text;
    expected_sequence integer;
    event_profile_status text;
    event_firm_id uuid;
    event_matter_id uuid;
BEGIN
    SELECT status, firm_id, matter_id
      INTO event_profile_status, event_firm_id, event_matter_id
      FROM case_posture_profiles WHERE profile_id = NEW.profile_id;
    IF NOT FOUND OR event_profile_status <> 'CONFIRMED'
       OR event_firm_id <> NEW.firm_id OR event_matter_id <> NEW.matter_id THEN
        RAISE EXCEPTION 'case posture profile event does not belong to its exact profile tenant';
    END IF;
    SELECT effective_status, event_sequence + 1
      INTO previous_status, expected_sequence
      FROM case_posture_profile_events
     WHERE profile_id = NEW.profile_id
     ORDER BY event_sequence DESC LIMIT 1;
    IF NOT FOUND THEN
        IF NEW.event_sequence <> 1 OR NEW.effective_status <> 'CURRENT'
           OR NEW.cause_kind <> 'PROFILE_CONFIRMED' THEN
            RAISE EXCEPTION 'first case posture profile event must establish CURRENT';
        END IF;
    ELSIF NEW.event_sequence <> expected_sequence
       OR NOT (
           (previous_status = 'CURRENT' AND NEW.effective_status IN ('STALE', 'SUPERSEDED'))
           OR (previous_status = 'STALE' AND NEW.effective_status = 'SUPERSEDED')
       ) THEN
        RAISE EXCEPTION 'case posture profile event transition is invalid';
    END IF;
    IF (NEW.cause_kind = 'PROFILE_CONFIRMED' AND NEW.cause_object_id <> NEW.profile_id)
       OR (NEW.cause_kind = 'PROFILE_REPLACED' AND NOT EXISTS (
            SELECT 1 FROM case_posture_profiles replacement
             WHERE replacement.profile_id = NEW.cause_object_id
               AND replacement.firm_id = NEW.firm_id
               AND replacement.matter_id = NEW.matter_id
               AND replacement.supersedes_profile_id = NEW.profile_id
       )) THEN
        RAISE EXCEPTION 'case posture profile event cause is not bound to the profile lineage';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER case_posture_profile_events_insert_guard BEFORE INSERT ON case_posture_profile_events
    FOR EACH ROW EXECUTE FUNCTION validate_case_posture_profile_event_insert();

CREATE FUNCTION validate_case_posture_profile_head() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    actual_version bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'case posture profile head cannot be deleted';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.matter_id <> OLD.matter_id OR NEW.firm_id <> OLD.firm_id THEN
            RAISE EXCEPTION 'case posture profile head identity is immutable';
        END IF;
        IF NEW.current_profile_id IS NULL THEN
            IF OLD.current_profile_id IS NULL OR NEW.latest_profile_version <> OLD.latest_profile_version THEN
                RAISE EXCEPTION 'only a current case posture profile can become stale';
            END IF;
            RETURN NEW;
        END IF;
        IF NEW.latest_profile_version <> OLD.latest_profile_version + 1 THEN
            RAISE EXCEPTION 'case posture profile version must increase exactly once';
        END IF;
    END IF;
    IF NEW.current_profile_id IS NULL THEN
        RAISE EXCEPTION 'a newly confirmed case posture profile head requires a current profile';
    END IF;
    SELECT profile_version INTO actual_version
      FROM case_posture_profiles
     WHERE profile_id = NEW.current_profile_id
       AND matter_id = NEW.matter_id AND firm_id = NEW.firm_id;
    IF actual_version IS NULL OR actual_version <> NEW.latest_profile_version THEN
        RAISE EXCEPTION 'case posture profile head does not reference its exact latest profile';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER case_posture_profile_heads_guard BEFORE INSERT OR UPDATE OR DELETE
    ON case_posture_profile_heads FOR EACH ROW EXECUTE FUNCTION validate_case_posture_profile_head();

CREATE FUNCTION stale_current_posture_profile_for_upstream_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    affected_profile_id uuid;
    causing_actor_id uuid;
    cause text;
BEGIN
    IF OLD.current_version_id = NEW.current_version_id THEN RETURN NEW; END IF;
    IF TG_TABLE_NAME = 'case_party_heads' THEN
        cause := 'PARTY_VERSION_CHANGED';
        SELECT p.profile_id, v.confirmed_by INTO affected_profile_id, causing_actor_id
          FROM case_posture_profile_heads h
          JOIN case_posture_profiles p ON p.profile_id = h.current_profile_id
          JOIN case_party_versions v ON v.party_version_id = NEW.current_version_id
         WHERE h.matter_id = NEW.matter_id AND h.firm_id = NEW.firm_id
           AND p.represented_party_id = NEW.party_id
           AND p.represented_party_version_id = OLD.current_version_id;
    ELSIF TG_TABLE_NAME = 'court_proceeding_heads' THEN
        cause := 'PROCEEDING_VERSION_CHANGED';
        SELECT p.profile_id, v.confirmed_by INTO affected_profile_id, causing_actor_id
          FROM case_posture_profile_heads h
          JOIN case_posture_profiles p ON p.profile_id = h.current_profile_id
          JOIN court_proceeding_versions v ON v.proceeding_version_id = NEW.current_version_id
         WHERE h.matter_id = NEW.matter_id AND h.firm_id = NEW.firm_id
           AND p.proceeding_id = NEW.proceeding_id
           AND p.proceeding_version_id = OLD.current_version_id;
    ELSIF TG_TABLE_NAME = 'court_party_position_heads' THEN
        cause := 'POSITION_VERSION_CHANGED';
        SELECT p.profile_id, v.confirmed_by INTO affected_profile_id, causing_actor_id
          FROM case_posture_profile_heads h
          JOIN case_posture_profiles p ON p.profile_id = h.current_profile_id
          JOIN court_party_position_versions v ON v.position_version_id = NEW.current_version_id
         WHERE h.matter_id = NEW.matter_id AND h.firm_id = NEW.firm_id
           AND p.position_id = NEW.position_id
           AND p.position_version_id = OLD.current_version_id;
    ELSIF TG_TABLE_NAME = 'firm_engagement_heads' THEN
        cause := 'ENGAGEMENT_VERSION_CHANGED';
        SELECT p.profile_id, v.confirmed_by INTO affected_profile_id, causing_actor_id
          FROM case_posture_profile_heads h
          JOIN case_posture_profiles p ON p.profile_id = h.current_profile_id
          JOIN firm_engagement_versions v ON v.engagement_version_id = NEW.current_version_id
         WHERE h.matter_id = NEW.matter_id AND h.firm_id = NEW.firm_id
           AND p.engagement_id = NEW.engagement_id
           AND p.engagement_version_id = OLD.current_version_id;
    ELSE
        RAISE EXCEPTION 'unsupported case posture upstream head';
    END IF;

    IF affected_profile_id IS NOT NULL THEN
        INSERT INTO case_posture_profile_events (
            profile_id, firm_id, matter_id, event_sequence, effective_status,
            cause_kind, cause_object_id, actor_id
        )
        SELECT affected_profile_id, NEW.firm_id, NEW.matter_id,
               COALESCE(max(event_sequence), 0) + 1, 'STALE', cause,
               NEW.current_version_id, causing_actor_id
          FROM case_posture_profile_events WHERE profile_id = affected_profile_id;
        UPDATE case_posture_profile_heads
           SET current_profile_id = NULL, updated_at = now()
         WHERE matter_id = NEW.matter_id AND firm_id = NEW.firm_id
           AND current_profile_id = affected_profile_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_party_head_stales_posture AFTER UPDATE ON case_party_heads
    FOR EACH ROW EXECUTE FUNCTION stale_current_posture_profile_for_upstream_change();
CREATE TRIGGER court_proceeding_head_stales_posture AFTER UPDATE ON court_proceeding_heads
    FOR EACH ROW EXECUTE FUNCTION stale_current_posture_profile_for_upstream_change();
CREATE TRIGGER court_party_position_head_stales_posture AFTER UPDATE ON court_party_position_heads
    FOR EACH ROW EXECUTE FUNCTION stale_current_posture_profile_for_upstream_change();
CREATE TRIGGER firm_engagement_head_stales_posture AFTER UPDATE ON firm_engagement_heads
    FOR EACH ROW EXECUTE FUNCTION stale_current_posture_profile_for_upstream_change();

CREATE VIEW current_case_posture_profiles WITH (security_invoker = true) AS
SELECT p.*, latest.effective_status, latest.occurred_at AS status_changed_at
  FROM case_posture_profiles p
  JOIN LATERAL (
      SELECT e.effective_status, e.occurred_at
        FROM case_posture_profile_events e
       WHERE e.profile_id = p.profile_id
       ORDER BY e.event_sequence DESC LIMIT 1
  ) latest ON true;

-- Direct tenant policies remain reviewable on every storage table.  The view
-- above uses security_invoker so these RLS policies still apply to its caller.
ALTER TABLE case_parties ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_parties FORCE ROW LEVEL SECURITY;
ALTER TABLE case_party_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_party_versions FORCE ROW LEVEL SECURITY;
ALTER TABLE case_party_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_party_heads FORCE ROW LEVEL SECURITY;
ALTER TABLE court_proceedings ENABLE ROW LEVEL SECURITY;
ALTER TABLE court_proceedings FORCE ROW LEVEL SECURITY;
ALTER TABLE court_proceeding_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE court_proceeding_versions FORCE ROW LEVEL SECURITY;
ALTER TABLE court_proceeding_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE court_proceeding_heads FORCE ROW LEVEL SECURITY;
ALTER TABLE court_party_positions ENABLE ROW LEVEL SECURITY;
ALTER TABLE court_party_positions FORCE ROW LEVEL SECURITY;
ALTER TABLE court_party_position_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE court_party_position_versions FORCE ROW LEVEL SECURITY;
ALTER TABLE court_party_position_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE court_party_position_heads FORCE ROW LEVEL SECURITY;
ALTER TABLE firm_engagements ENABLE ROW LEVEL SECURITY;
ALTER TABLE firm_engagements FORCE ROW LEVEL SECURITY;
ALTER TABLE firm_engagement_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE firm_engagement_versions FORCE ROW LEVEL SECURITY;
ALTER TABLE firm_engagement_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE firm_engagement_heads FORCE ROW LEVEL SECURITY;
ALTER TABLE case_posture_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_posture_profiles FORCE ROW LEVEL SECURITY;
ALTER TABLE case_posture_profile_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_posture_profile_heads FORCE ROW LEVEL SECURITY;
ALTER TABLE case_posture_profile_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_posture_profile_events FORCE ROW LEVEL SECURITY;

CREATE POLICY case_parties_firm_isolation ON case_parties
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_party_versions_firm_isolation ON case_party_versions
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_party_heads_firm_isolation ON case_party_heads
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY court_proceedings_firm_isolation ON court_proceedings
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY court_proceeding_versions_firm_isolation ON court_proceeding_versions
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY court_proceeding_heads_firm_isolation ON court_proceeding_heads
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY court_party_positions_firm_isolation ON court_party_positions
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY court_party_position_versions_firm_isolation ON court_party_position_versions
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY court_party_position_heads_firm_isolation ON court_party_position_heads
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY firm_engagements_firm_isolation ON firm_engagements
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY firm_engagement_versions_firm_isolation ON firm_engagement_versions
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY firm_engagement_heads_firm_isolation ON firm_engagement_heads
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_posture_profiles_firm_isolation ON case_posture_profiles
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_posture_profile_heads_firm_isolation ON case_posture_profile_heads
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY case_posture_profile_events_firm_isolation ON case_posture_profile_events
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

COMMIT;
