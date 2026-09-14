BEGIN;

-- 0047 accidentally double-escaped the PostgreSQL ARE control-character
-- ranges.  The resulting character class matched ordinary text (including
-- Chinese lawyer notes) and made the session-bound 0048 command fail after
-- all authority checks.  Replace only that validation constraint; the note
-- length, byte cap, trimming rule and decision/reason binding remain intact.
ALTER TABLE public.case_agent_ledger_exception_group_decisions
    DROP CONSTRAINT case_agent_ledger_exception_group_decisions_reason_note_check;

ALTER TABLE public.case_agent_ledger_exception_group_decisions
    ADD CONSTRAINT case_agent_ledger_exception_group_decisions_reason_note_check
    CHECK (
        reason_note IS NULL OR (
            reason_note = btrim(reason_note)
            AND length(reason_note) BETWEEN 1 AND 500
            AND octet_length(reason_note) <= 2000
            AND reason_note !~ '[\x00-\x08\x0B\x0C\x0E-\x1F]'
        )
    );

COMMIT;
