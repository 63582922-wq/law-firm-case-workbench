-- Rollback-only reproduction using two fresh sessions for the known synthetic
-- intake actor. No existing session is changed; no raw cookie is generated.
\set ON_ERROR_STOP on
BEGIN;
SELECT gen_random_uuid() AS target_id, gen_random_uuid() AS other_id \gset
INSERT INTO web_sessions (session_id,firm_id,user_id,issuer,session_token_sha256,csrf_token_sha256,
                         authenticated_at,created_at,expires_at)
SELECT ids.id, prior.firm_id,prior.user_id,prior.issuer,
       digest(ids.id::text || ':session','sha256'),digest(ids.id::text || ':csrf','sha256'),
       now(),now(),now()+interval '5 minutes'
FROM web_sessions prior JOIN web_material_upload_slots slot USING(session_id)
CROSS JOIN (VALUES (:'target_id'::uuid),(:'other_id'::uuid)) ids(id)
WHERE slot.upload_id='8d0c4ce2-6ba2-4d8f-a4e3-8c3f8db3bcc5'
  AND slot.matter_id='767fda38-e3de-5a15-816f-510a686c7600';
SET LOCAL ROLE lawcase_web_session_gateway;
SELECT set_config('app.firm_id','',true),set_config('app.web_session_token_sha256','',true);
SELECT set_config('app.web_session_id', :'target_id', true);
SELECT count(*) AS before_policy_visible FROM web_sessions;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM web_sessions) THEN
        RAISE EXCEPTION 'baseline unexpectedly permits exact-session reads';
    END IF;
END $$;
RESET ROLE;
\ir ../../migrations/0079_web_session_exact_revocation_visibility.sql
SET LOCAL ROLE lawcase_web_session_gateway;
DO $$ BEGIN
    IF (SELECT count(*) FROM web_sessions) <> 1 THEN
        RAISE EXCEPTION 'exact-session visibility is not one';
    END IF;
END $$;
UPDATE web_sessions SET revoked_at=now()
WHERE session_id=:'target_id'::uuid AND revoked_at IS NULL RETURNING session_id;
SELECT set_config('app.web_session_id','',true);
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM web_sessions) THEN
        RAISE EXCEPTION 'no-selector session enumeration allowed';
    END IF;
END $$;
RESET ROLE;
SELECT count(*)=1 AS only_target_revoked FROM web_sessions
WHERE session_id IN (:'target_id'::uuid, :'other_id'::uuid) AND revoked_at IS NOT NULL;
ROLLBACK;
