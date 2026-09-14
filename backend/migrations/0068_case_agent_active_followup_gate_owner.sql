BEGIN;

-- The refresh insert gate reads the 0049 follow-up heads.  Those tables are
-- deliberately hidden from the broad schema owner and exposed only to the
-- isolated ledger confirmation owner.  Run the narrow SECURITY DEFINER gate
-- under that same least-privilege owner.
SET LOCAL ROLE lawcase_schema_owner;

ALTER FUNCTION
    public.gate_case_agent_snapshot_refresh_insert_for_active_followup()
    OWNER TO lawcase_ledger_confirmation_owner;

COMMIT;
