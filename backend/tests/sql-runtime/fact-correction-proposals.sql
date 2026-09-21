-- Run after 0081 DDL inside an outer ROLLBACK-only transaction.
-- The temporary INSERT grant is for this probe, not a deployed write route.
GRANT INSERT, UPDATE, DELETE ON case_agent_fact_correction_proposals TO lawcase_web_application;
SET LOCAL ROLE lawcase_web_application;
SELECT set_config('app.firm_id','11111111-1111-4111-8111-111111111111',true);
SELECT set_config('app.actor_id','22222222-2222-4222-8222-222222222222',true);
DO $$
DECLARE
    c case_agent_ledger_extraction_candidates%ROWTYPE;
    b case_agent_ledger_extraction_batches%ROWTYPE;
    v integer;
    p uuid := gen_random_uuid();
    content bytea;
    denied boolean;
BEGIN
    SELECT * INTO STRICT c FROM case_agent_ledger_extraction_candidates
     WHERE matter_id='767fda38-e3de-5a15-816f-510a686c7600'
       AND candidate_kind='FACT' AND review_lane='EXCEPTION_REVIEW'
     ORDER BY extraction_candidate_id LIMIT 1;
    SELECT * INTO STRICT b FROM case_agent_ledger_extraction_batches
     WHERE extraction_batch_id=c.extraction_batch_id;
    SELECT version INTO STRICT v FROM matters WHERE matter_id=c.matter_id AND firm_id=c.firm_id;
    content := convert_to(jsonb_build_object(
        'schema_version','lawyer-fact-correction-proposal-v1',
        'review_status','NEEDS_LAWYER_REVIEW', 'court_ready',false,
        'original_artifact_hash',b.artifact_content_sha256,
        'source_hash',b.source_hash,'original_candidate',c.candidate_payload,
        'revised_text','合成数据库约束探针：待核对的材料陈述。',
        'reason','只测试存储约束，事务整体回滚。'
    )::text,'UTF8');
    INSERT INTO case_agent_fact_correction_proposals VALUES (
        p,c.firm_id,c.matter_id,c.extraction_candidate_id,v,1,NULL,
        '22222222-2222-4222-8222-222222222222',repeat('a',64),repeat('b',64),content,now()
    );
    denied := false;
    BEGIN
        UPDATE case_agent_fact_correction_proposals SET request_hash=repeat('c',64) WHERE proposal_id=p;
    EXCEPTION WHEN raise_exception THEN denied := true; END;
    IF NOT denied THEN RAISE EXCEPTION 'append-only probe failed'; END IF;
    denied := false;
    BEGIN
        INSERT INTO case_agent_fact_correction_proposals VALUES (
            gen_random_uuid(),c.firm_id,c.matter_id,c.extraction_candidate_id,v-1,2,p,
            '22222222-2222-4222-8222-222222222222',repeat('d',64),repeat('e',64),content,now()
        );
    EXCEPTION WHEN raise_exception THEN denied := true; END;
    IF NOT denied THEN RAISE EXCEPTION 'stale version probe failed'; END IF;
    denied := false;
    BEGIN
        INSERT INTO case_agent_fact_correction_proposals VALUES (
            gen_random_uuid(),c.firm_id,c.matter_id,c.extraction_candidate_id,v,2,p,
            '22222222-2222-4222-8222-222222222222',repeat('d',64),repeat('e',64),
            convert_to((convert_from(content,'UTF8')::jsonb || jsonb_build_object('source_hash',repeat('0',64)))::text,'UTF8'),now()
        );
    EXCEPTION WHEN raise_exception THEN denied := true; END;
    IF NOT denied THEN RAISE EXCEPTION 'source binding probe failed'; END IF;
    RAISE NOTICE 'REAL_WEB_ROLE_CORRECTION_STORAGE_PROBE_PASSED';
END $$;
RESET ROLE;
