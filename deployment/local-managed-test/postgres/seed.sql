SET ROLE lawcase_schema_owner;

SELECT set_config('app.firm_id', :'firm_id', false);

SELECT count(*) AS firm_conflict_count
FROM public.firms
WHERE firm_id = :'firm_id'::uuid
  AND display_name <> :'firm_name'
\gset
\if :firm_conflict_count
    \echo 'deterministic firm id is already bound differently'
    \quit 3
\endif

SELECT count(*) AS actor_conflict_count
FROM public.users
WHERE (
    user_id = :'lead_actor_id'::uuid
    AND (
        firm_id <> :'firm_id'::uuid
        OR external_subject <> :'lead_subject'
    )
) OR (
    user_id = :'worker_actor_id'::uuid
    AND (
        firm_id <> :'firm_id'::uuid
        OR external_subject <> 'local-managed:case-agent-execution'
    )
) OR (
    user_id = :'verifier_actor_id'::uuid
    AND (
        firm_id <> :'firm_id'::uuid
        OR external_subject <> 'local-managed:case-agent-verifier'
    )
)
\gset
\if :actor_conflict_count
    \echo 'deterministic actor id is already bound differently'
    \quit 3
\endif

SELECT count(*) AS oidc_conflict_count
FROM public.web_oidc_identities
WHERE issuer = :'oidc_issuer'
  AND subject = :'lead_subject'
  AND (firm_id <> :'firm_id'::uuid OR user_id <> :'lead_actor_id'::uuid)
\gset
\if :oidc_conflict_count
    \echo 'OIDC issuer/subject is already bound to another actor'
    \quit 3
\endif

INSERT INTO public.firms (firm_id, display_name)
VALUES (:'firm_id'::uuid, :'firm_name')
ON CONFLICT (firm_id) DO UPDATE
SET display_name = EXCLUDED.display_name;

INSERT INTO public.users (
    user_id, firm_id, external_subject, display_name, status
) VALUES
    (
        :'lead_actor_id'::uuid, :'firm_id'::uuid, :'lead_subject',
        :'lead_username', 'ACTIVE'
    ),
    (
        :'worker_actor_id'::uuid, :'firm_id'::uuid,
        'local-managed:case-agent-execution', 'Agent execution worker', 'ACTIVE'
    ),
    (
        :'verifier_actor_id'::uuid, :'firm_id'::uuid,
        'local-managed:case-agent-verifier', 'Independent Agent verifier', 'ACTIVE'
    )
ON CONFLICT (user_id) DO UPDATE
SET display_name = EXCLUDED.display_name,
    status = 'ACTIVE';

INSERT INTO public.web_oidc_identities (
    issuer, subject, firm_id, user_id, is_active, active_human_roles
) VALUES (
    :'oidc_issuer', :'lead_subject', :'firm_id'::uuid,
    :'lead_actor_id'::uuid, TRUE, ARRAY['LEAD_LAWYER']::text[]
)
ON CONFLICT (issuer, subject) DO UPDATE
SET is_active = TRUE,
    active_human_roles = ARRAY['LEAD_LAWYER']::text[],
    updated_at = clock_timestamp();
