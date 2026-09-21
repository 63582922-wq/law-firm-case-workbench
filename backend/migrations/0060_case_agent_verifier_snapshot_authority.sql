-- Give the independent verifier one narrow row-lock authority for the exact
-- current matter snapshot.  The verifier must not receive direct UPDATE on
-- matters merely because PostgreSQL requires UPDATE privilege for SELECT
-- ... FOR UPDATE.  This definer function performs the lock and re-authorizes
-- the transaction-bound verifier principal without exposing a write surface.

BEGIN;

CREATE FUNCTION public.authorize_case_agent_verification_snapshot(
    requested_firm_id uuid,
    requested_matter_id uuid,
    requested_verifier_actor_id uuid,
    expected_matter_version integer
) RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    current_matter_version integer;
BEGIN
    IF requested_firm_id IS NULL
       OR requested_matter_id IS NULL
       OR requested_verifier_actor_id IS NULL
       OR expected_matter_version IS NULL
       OR expected_matter_version <= 0
       OR NULLIF(current_setting('app.firm_id', true), '')::uuid
            IS DISTINCT FROM requested_firm_id
       OR NULLIF(current_setting('app.actor_id', true), '')::uuid
            IS DISTINCT FROM requested_verifier_actor_id THEN
        RAISE EXCEPTION 'case Agent verifier snapshot authority is invalid'
            USING ERRCODE = '42501';
    END IF;

    SELECT matter.version
      INTO current_matter_version
      FROM public.matters matter
     WHERE matter.firm_id = requested_firm_id
       AND matter.matter_id = requested_matter_id
     FOR UPDATE;

    IF current_matter_version IS NULL
       OR NOT EXISTS (
            SELECT 1
              FROM public.matter_actor_roles role
              JOIN public.users principal
                ON principal.user_id = role.user_id
               AND principal.firm_id = role.firm_id
             WHERE role.firm_id = requested_firm_id
               AND role.matter_id = requested_matter_id
               AND role.user_id = requested_verifier_actor_id
               AND role.role = 'SYSTEM_WORKER'
               AND role.revoked_at IS NULL
               AND principal.status = 'ACTIVE'
       )
       OR EXISTS (
            SELECT 1
              FROM public.matter_actor_roles role
             WHERE role.firm_id = requested_firm_id
               AND role.matter_id = requested_matter_id
               AND role.user_id = requested_verifier_actor_id
               AND role.role <> 'SYSTEM_WORKER'
               AND role.revoked_at IS NULL
       ) THEN
        RAISE EXCEPTION 'case Agent verifier snapshot authority is unavailable'
            USING ERRCODE = '42501';
    END IF;

    RETURN current_matter_version;
END;
$$;

REVOKE ALL ON FUNCTION public.authorize_case_agent_verification_snapshot(
    uuid, uuid, uuid, integer
) FROM PUBLIC, lawcase_web_application, lawcase_agent_worker;
GRANT EXECUTE ON FUNCTION public.authorize_case_agent_verification_snapshot(
    uuid, uuid, uuid, integer
) TO lawcase_agent_verifier;

COMMENT ON FUNCTION public.authorize_case_agent_verification_snapshot(
    uuid, uuid, uuid, integer
) IS
    'Locks one current matter snapshot for the transaction-bound independent verifier without granting direct matter UPDATE.';

COMMIT;
