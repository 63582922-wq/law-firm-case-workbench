-- A new matter locks the configured execution and verifier identities before
-- it binds their SYSTEM_WORKER roles. PostgreSQL requires UPDATE privilege for
-- SELECT ... FOR UPDATE, although this Web principal never mutates users.
-- Grant only the immutable primary-key column and rely on FORCE RLS for firm
-- isolation. The local managed post-migration hardening pass restores this
-- exact entitlement after its broad direct-DML revocation.

BEGIN;

GRANT UPDATE (user_id) ON TABLE public.users TO lawcase_web_application;

COMMIT;
