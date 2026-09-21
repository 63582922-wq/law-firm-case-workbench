-- ADR-0103: compiler adds a local legal-research plan for missing law sources.
-- This is not the external search tool or legal-rule approval.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
DO $patch$
DECLARE
    original text;
    tools_before text := '''review_case_context'',''analyze_lawyer_decision_package''';
    skills_before text := '''case_context_review'',''lawyer_decision_package''';
BEGIN
    SELECT pg_get_functiondef('public.case_agent_is_bounded_pending_review_analysis(uuid,uuid,uuid)'::regprocedure) INTO original;
    IF strpos(original,tools_before)=0 OR strpos(original,skills_before)=0 THEN
        RAISE EXCEPTION 'analysis scope differs; migration refused';
    END IF;
    original := replace(original, tools_before, tools_before || ',''plan_authoritative_rule_research''');
    original := replace(original, skills_before, skills_before || ',''legal_rule_research_planning''');
    EXECUTE original;
END
$patch$;
COMMIT;
