-- Complete the verifier's append-only control transaction.  Verification
-- STARTED/PASSED events use the same audited command boundary as every other
-- Agent command, so the verifier needs narrowly column-scoped INSERT on the
-- outbox and idempotency ledgers.  It receives no update/delete authority.

BEGIN;

REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE
    public.outbox_events,
    public.command_idempotency
FROM lawcase_agent_verifier;

GRANT INSERT (
    firm_id, matter_id, aggregate_version, event_type, payload
) ON TABLE public.outbox_events TO lawcase_agent_verifier;

GRANT INSERT (
    firm_id, matter_id, actor_id, command_name, idempotency_key,
    request_hash, response_json
) ON TABLE public.command_idempotency TO lawcase_agent_verifier;

COMMIT;
