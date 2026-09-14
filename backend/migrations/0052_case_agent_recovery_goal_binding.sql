-- Immutable, server-derived goal binding for ledger-exception recovery runs.
--
-- 0050 committed the replacement run UUID before RUN_CREATED, but did not
-- bind that UUID to the one fixed recovery goal.  A generic create-run call
-- could therefore occupy the prepared UUID with a different goal before the
-- transfer.  This migration makes both goal identity and canonical goal hash
-- database-verifiable and fail-closed at prepare/resume, run wake, transfer,
-- and both phases of Worker claim.

BEGIN;

LOCK TABLE
    public.case_agent_ledger_exception_recovery_intents,
    public.case_agent_ledger_exception_recovery_intent_heads,
    public.case_agent_goals,
    public.case_agent_runs,
    public.case_agent_run_inbox
    IN SHARE ROW EXCLUSIVE MODE;

CREATE FUNCTION public.case_agent_ledger_exception_recovery_goal_hash(
    input_goal_id uuid,
    input_requested_by uuid
)
RETURNS char(64)
LANGUAGE sql
IMMUTABLE
STRICT
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT pg_catalog.encode(
        public.digest(
            pg_catalog.convert_to(
                '{"constraints":["不得自动确认正式事实、法律口径或对外提交",'
                || '"不得读取其他案件或使用浏览器提供的运行、图谱或对象定位"],'
                || '"goal_id":"' || input_goal_id::text || '",'
                || '"objective":"恢复本案异常材料后续工作并基于当前权威台账继续研判",'
                || '"requested_by":"' || input_requested_by::text || '",'
                || '"schema_version":"lawyer-agent-goal-v1",'
                || '"success_criteria":["接管全部待完成异常分流工作",'
                || '"重新提取任务覆盖原异常组完整受管来源并通过独立校验",'
                || '"全部后续工作完成后基于当前案件版本重新规划"]}',
                'UTF8'
            ),
            'sha256'
        ),
        'hex'
    )::char(64)
$$;

ALTER TABLE public.case_agent_ledger_exception_recovery_intents
    ADD COLUMN recovery_goal_id uuid,
    ADD COLUMN recovery_goal_hash char(64);

-- A non-terminal old intent is safe to carry across the upgrade only when an
-- already-created run has the exact server recovery semantics and the same
-- canonical AgentGoal hash.  Refuse the upgrade instead of blessing a run
-- whose objective may have come from the generic browser create endpoint.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM public.case_agent_ledger_exception_recovery_intents intent
          JOIN public.case_agent_ledger_exception_recovery_intent_heads intent_head
            ON intent_head.recovery_intent_id = intent.recovery_intent_id
           AND intent_head.firm_id = intent.firm_id
           AND intent_head.matter_id = intent.matter_id
          JOIN public.case_agent_runs run
            ON run.run_id = intent.replacement_run_id
           AND run.firm_id = intent.firm_id
           AND run.matter_id = intent.matter_id
          LEFT JOIN public.case_agent_goals goal
            ON goal.goal_id = run.goal_id
           AND goal.firm_id = run.firm_id
           AND goal.matter_id = run.matter_id
         WHERE intent_head.current_outcome IN ('PENDING', 'TRANSFERRED')
           AND (
                goal.goal_id IS NULL
                OR goal.requested_by IS DISTINCT FROM intent.actor_id
                OR goal.objective IS DISTINCT FROM
                    '恢复本案异常材料后续工作并基于当前权威台账继续研判'
                OR goal.success_criteria IS DISTINCT FROM
                    pg_catalog.jsonb_build_array(
                        '接管全部待完成异常分流工作',
                        '重新提取任务覆盖原异常组完整受管来源并通过独立校验',
                        '全部后续工作完成后基于当前案件版本重新规划'
                    )
                OR goal.constraints IS DISTINCT FROM
                    pg_catalog.jsonb_build_array(
                        '不得自动确认正式事实、法律口径或对外提交',
                        '不得读取其他案件或使用浏览器提供的运行、图谱或对象定位'
                    )
                OR goal.goal_hash IS DISTINCT FROM
                    public.case_agent_ledger_exception_recovery_goal_hash(
                        goal.goal_id, intent.actor_id
                    )
           )
    ) THEN
        RAISE EXCEPTION
            '0052 upgrade requires non-canonical recovery runs to be drained';
    END IF;
END;
$$;

-- The table is append-only, so temporarily disable only its mutation guard
-- while this locked migration fills the two new columns.  A safe legacy run
-- keeps its already-canonical goal id; run-less and terminal unsafe intents
-- receive the new deterministic run-id-as-goal-id binding.
ALTER TABLE public.case_agent_ledger_exception_recovery_intents
    DISABLE TRIGGER case_agent_ledger_exception_recovery_intents_append_only;

UPDATE public.case_agent_ledger_exception_recovery_intents intent
   SET recovery_goal_id = intent.replacement_run_id,
       recovery_goal_hash =
            public.case_agent_ledger_exception_recovery_goal_hash(
                intent.replacement_run_id, intent.actor_id
            );

UPDATE public.case_agent_ledger_exception_recovery_intents intent
   SET recovery_goal_id = goal.goal_id,
       recovery_goal_hash = goal.goal_hash
  FROM public.case_agent_runs run
  JOIN public.case_agent_goals goal
    ON goal.goal_id = run.goal_id
   AND goal.firm_id = run.firm_id
   AND goal.matter_id = run.matter_id
 WHERE run.run_id = intent.replacement_run_id
   AND run.firm_id = intent.firm_id
   AND run.matter_id = intent.matter_id
   AND goal.requested_by = intent.actor_id
   AND goal.objective =
        '恢复本案异常材料后续工作并基于当前权威台账继续研判'
   AND goal.success_criteria = pg_catalog.jsonb_build_array(
        '接管全部待完成异常分流工作',
        '重新提取任务覆盖原异常组完整受管来源并通过独立校验',
        '全部后续工作完成后基于当前案件版本重新规划'
   )
   AND goal.constraints = pg_catalog.jsonb_build_array(
        '不得自动确认正式事实、法律口径或对外提交',
        '不得读取其他案件或使用浏览器提供的运行、图谱或对象定位'
   )
   AND goal.goal_hash =
        public.case_agent_ledger_exception_recovery_goal_hash(
            goal.goal_id, intent.actor_id
        );

ALTER TABLE public.case_agent_ledger_exception_recovery_intents
    ENABLE TRIGGER case_agent_ledger_exception_recovery_intents_append_only;

ALTER TABLE public.case_agent_ledger_exception_recovery_intents
    ALTER COLUMN recovery_goal_id SET NOT NULL,
    ALTER COLUMN recovery_goal_hash SET NOT NULL,
    ADD CONSTRAINT case_agent_ledger_exception_recovery_goal_hash_exact
        CHECK (
            recovery_goal_hash =
                public.case_agent_ledger_exception_recovery_goal_hash(
                    recovery_goal_id, actor_id
                )
        );

CREATE FUNCTION public.bind_case_agent_ledger_exception_recovery_goal()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    -- lawcase.case-agent-control-recovery.contract-v3
    -- v2 has already locked the matter before inserting the intent.  Keep the
    -- order matter -> run advisory lock, matching ordinary create_run.
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
        'CASE_AGENT_RECOVERY_GOAL_BINDING|' ||
            NEW.replacement_run_id::text,
        0
    ));
    NEW.recovery_goal_id := NEW.replacement_run_id;
    NEW.recovery_goal_hash :=
        public.case_agent_ledger_exception_recovery_goal_hash(
            NEW.replacement_run_id, NEW.actor_id
        );
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_ledger_exception_recovery_goal_bind
    BEFORE INSERT
    ON public.case_agent_ledger_exception_recovery_intents
    FOR EACH ROW EXECUTE FUNCTION
        public.bind_case_agent_ledger_exception_recovery_goal();

-- Reject the collision transaction itself.  The fixed binding is already
-- committed before RUN_CREATED, and Agent goals are append-only, so this
-- guard makes a generic run with the prepared UUID impossible to persist.
CREATE FUNCTION public.guard_case_agent_recovery_run_goal_binding()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    -- lawcase.case-agent-control-recovery.contract-v3
    PERFORM pg_catalog.set_config('app.firm_id', NEW.firm_id::text, true);
    -- Serialize every attempt to occupy this prepared run UUID with recovery
    -- prepare.  The lock must precede the MVCC visibility check below.
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
        'CASE_AGENT_RECOVERY_GOAL_BINDING|' || NEW.run_id::text,
        0
    ));
    IF EXISTS (
        SELECT 1
          FROM public.case_agent_ledger_exception_recovery_intents intent
         WHERE intent.replacement_run_id = NEW.run_id
           AND intent.firm_id = NEW.firm_id
           AND intent.matter_id = NEW.matter_id
    ) AND NOT EXISTS (
        SELECT 1
          FROM public.case_agent_ledger_exception_recovery_intents intent
          JOIN public.case_agent_goals goal
            ON goal.goal_id = NEW.goal_id
           AND goal.firm_id = NEW.firm_id
           AND goal.matter_id = NEW.matter_id
         WHERE intent.replacement_run_id = NEW.run_id
           AND intent.firm_id = NEW.firm_id
           AND intent.matter_id = NEW.matter_id
           AND intent.recovery_goal_id = NEW.goal_id
           AND intent.recovery_goal_hash = goal.goal_hash
           AND intent.actor_id = NEW.created_by
           AND goal.requested_by = intent.actor_id
           AND goal.objective =
                '恢复本案异常材料后续工作并基于当前权威台账继续研判'
           AND goal.success_criteria = pg_catalog.jsonb_build_array(
                '接管全部待完成异常分流工作',
                '重新提取任务覆盖原异常组完整受管来源并通过独立校验',
                '全部后续工作完成后基于当前案件版本重新规划'
           )
           AND goal.constraints = pg_catalog.jsonb_build_array(
                '不得自动确认正式事实、法律口径或对外提交',
                '不得读取其他案件或使用浏览器提供的运行、图谱或对象定位'
           )
           AND goal.goal_hash =
                public.case_agent_ledger_exception_recovery_goal_hash(
                    goal.goal_id, intent.actor_id
                )
    ) THEN
        RAISE EXCEPTION
            'case Agent recovery run goal differs from its immutable binding';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_runs_recovery_goal_binding_guard
    BEFORE INSERT OR UPDATE
    ON public.case_agent_runs
    FOR EACH ROW EXECUTE FUNCTION
        public.guard_case_agent_recovery_run_goal_binding();

-- Wrap 0050 prepare/transfer rather than weakening their already-reviewed
-- authority, locking and commit-lost behavior.  The legacy implementations
-- remain callable only by the isolated owner; the public application-facing
-- names now add contract-v3 goal checks in the same transaction.
ALTER FUNCTION public.prepare_case_agent_ledger_exception_control_recovery_from_web_session(
    uuid, uuid, uuid, integer, text, text
) RENAME TO case_agent_prepare_recovery_v2_inner;

CREATE FUNCTION public.prepare_case_agent_ledger_exception_control_recovery_from_web_session(
    input_session_id uuid,
    input_matter_id uuid,
    input_replacement_run_id uuid,
    input_expected_version integer,
    input_idempotency_key text,
    input_request_hash text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    receipt jsonb;
    intent_row record;
    run_exists boolean;
    run_goal_is_bound boolean;
BEGIN
    -- lawcase.case-agent-control-recovery.contract-v3
    receipt := public.case_agent_prepare_recovery_v2_inner(
        input_session_id,
        input_matter_id,
        input_replacement_run_id,
        input_expected_version,
        input_idempotency_key,
        input_request_hash
    );

    SELECT intent.recovery_intent_id, intent.firm_id, intent.matter_id,
           intent.replacement_run_id, intent.actor_id,
           intent.recovery_goal_id, intent.recovery_goal_hash,
           intent_head.current_outcome
      INTO intent_row
      FROM public.case_agent_ledger_exception_recovery_intents intent
      JOIN public.case_agent_ledger_exception_recovery_intent_heads intent_head
        ON intent_head.recovery_intent_id = intent.recovery_intent_id
       AND intent_head.firm_id = intent.firm_id
       AND intent_head.matter_id = intent.matter_id
     WHERE intent.recovery_intent_id = (receipt->>'object_id')::uuid
       AND intent.matter_id = input_matter_id
       AND intent.replacement_run_id = (receipt->>'replacement_run_id')::uuid;
    IF NOT FOUND
       OR intent_row.recovery_goal_hash IS DISTINCT FROM
            public.case_agent_ledger_exception_recovery_goal_hash(
                intent_row.recovery_goal_id, intent_row.actor_id
            ) THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ledger exception recovery goal binding is missing',
            ERRCODE = 'P4092';
    END IF;

    SELECT EXISTS (
        SELECT 1
          FROM public.case_agent_runs run
         WHERE run.run_id = intent_row.replacement_run_id
           AND run.firm_id = intent_row.firm_id
           AND run.matter_id = intent_row.matter_id
    ) INTO run_exists;
    IF run_exists THEN
        SELECT EXISTS (
            SELECT 1
              FROM public.case_agent_runs run
              JOIN public.case_agent_goals goal
                ON goal.goal_id = run.goal_id
               AND goal.firm_id = run.firm_id
               AND goal.matter_id = run.matter_id
             WHERE run.run_id = intent_row.replacement_run_id
               AND run.firm_id = intent_row.firm_id
               AND run.matter_id = intent_row.matter_id
               AND run.goal_id = intent_row.recovery_goal_id
               AND run.created_by = intent_row.actor_id
               AND goal.goal_hash = intent_row.recovery_goal_hash
               AND goal.requested_by = intent_row.actor_id
               AND goal.objective =
                    '恢复本案异常材料后续工作并基于当前权威台账继续研判'
               AND goal.success_criteria = pg_catalog.jsonb_build_array(
                    '接管全部待完成异常分流工作',
                    '重新提取任务覆盖原异常组完整受管来源并通过独立校验',
                    '全部后续工作完成后基于当前案件版本重新规划'
               )
               AND goal.constraints = pg_catalog.jsonb_build_array(
                    '不得自动确认正式事实、法律口径或对外提交',
                    '不得读取其他案件或使用浏览器提供的运行、图谱或对象定位'
               )
        ) INTO run_goal_is_bound;
        IF intent_row.current_outcome IN ('PENDING', 'TRANSFERRED')
           AND NOT run_goal_is_bound THEN
            RAISE EXCEPTION USING
                MESSAGE =
                    'ledger exception recovery run goal differs during resume',
                ERRCODE = 'P4092';
        END IF;
    END IF;
    RETURN receipt;
END;
$$;

ALTER FUNCTION public.transfer_case_agent_ledger_exception_control_from_web_session(
    uuid, uuid, uuid, integer, text, text
) RENAME TO case_agent_transfer_recovery_v2_inner;

CREATE FUNCTION public.transfer_case_agent_ledger_exception_control_from_web_session(
    input_session_id uuid,
    input_matter_id uuid,
    input_replacement_run_id uuid,
    input_expected_version integer,
    input_idempotency_key text,
    input_request_hash text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    receipt jsonb;
    run_goal_is_bound boolean;
BEGIN
    -- lawcase.case-agent-control-recovery.contract-v3
    receipt := public.case_agent_transfer_recovery_v2_inner(
        input_session_id,
        input_matter_id,
        input_replacement_run_id,
        input_expected_version,
        input_idempotency_key,
        input_request_hash
    );

    SELECT EXISTS (
        SELECT 1
          FROM public.case_agent_ledger_exception_recovery_intents intent
          JOIN public.case_agent_ledger_exception_recovery_intent_heads intent_head
            ON intent_head.recovery_intent_id = intent.recovery_intent_id
           AND intent_head.firm_id = intent.firm_id
           AND intent_head.matter_id = intent.matter_id
          JOIN public.case_agent_runs run
            ON run.run_id = intent.replacement_run_id
           AND run.firm_id = intent.firm_id
           AND run.matter_id = intent.matter_id
          JOIN public.case_agent_goals goal
            ON goal.goal_id = run.goal_id
           AND goal.firm_id = run.firm_id
           AND goal.matter_id = run.matter_id
         WHERE intent.matter_id = input_matter_id
           AND intent.replacement_run_id = input_replacement_run_id
           AND intent.idempotency_key = input_idempotency_key
           AND intent.request_hash = input_request_hash
           AND intent.expected_matter_version = input_expected_version
           AND intent_head.current_outcome = 'TRANSFERRED'
           AND run.goal_id = intent.recovery_goal_id
           AND run.created_by = intent.actor_id
           AND goal.goal_hash = intent.recovery_goal_hash
           AND goal.requested_by = intent.actor_id
           AND goal.objective =
                '恢复本案异常材料后续工作并基于当前权威台账继续研判'
           AND goal.success_criteria = pg_catalog.jsonb_build_array(
                '接管全部待完成异常分流工作',
                '重新提取任务覆盖原异常组完整受管来源并通过独立校验',
                '全部后续工作完成后基于当前案件版本重新规划'
           )
           AND goal.constraints = pg_catalog.jsonb_build_array(
                '不得自动确认正式事实、法律口径或对外提交',
                '不得读取其他案件或使用浏览器提供的运行、图谱或对象定位'
           )
           AND goal.goal_hash =
                public.case_agent_ledger_exception_recovery_goal_hash(
                    goal.goal_id, intent.actor_id
                )
    ) INTO run_goal_is_bound;
    IF NOT run_goal_is_bound THEN
        RAISE EXCEPTION USING
            MESSAGE =
                'ledger exception recovery run goal differs during transfer',
            ERRCODE = 'P4092';
    END IF;
    RETURN receipt;
END;
$$;

ALTER FUNCTION public.case_agent_ledger_exception_recovery_goal_hash(uuid, uuid)
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.bind_case_agent_ledger_exception_recovery_goal()
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.guard_case_agent_recovery_run_goal_binding()
    OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.prepare_case_agent_ledger_exception_control_recovery_from_web_session(
    uuid, uuid, uuid, integer, text, text
) OWNER TO lawcase_ledger_confirmation_owner;
ALTER FUNCTION public.transfer_case_agent_ledger_exception_control_from_web_session(
    uuid, uuid, uuid, integer, text, text
) OWNER TO lawcase_ledger_confirmation_owner;

REVOKE ALL ON FUNCTION
    public.case_agent_ledger_exception_recovery_goal_hash(uuid, uuid),
    public.bind_case_agent_ledger_exception_recovery_goal(),
    public.guard_case_agent_recovery_run_goal_binding()
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker;
REVOKE ALL ON FUNCTION
    public.case_agent_prepare_recovery_v2_inner(
        uuid, uuid, uuid, integer, text, text
    ),
    public.case_agent_transfer_recovery_v2_inner(
        uuid, uuid, uuid, integer, text, text
    )
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker;
REVOKE ALL ON FUNCTION
    public.prepare_case_agent_ledger_exception_control_recovery_from_web_session(
        uuid, uuid, uuid, integer, text, text
    ),
    public.transfer_case_agent_ledger_exception_control_from_web_session(
        uuid, uuid, uuid, integer, text, text
    )
    FROM PUBLIC, lawcase_agent_worker;
GRANT EXECUTE ON FUNCTION
    public.prepare_case_agent_ledger_exception_control_recovery_from_web_session(
        uuid, uuid, uuid, integer, text, text
    ),
    public.transfer_case_agent_ledger_exception_control_from_web_session(
        uuid, uuid, uuid, integer, text, text
    )
    TO lawcase_web_application;

GRANT SELECT (actor_id, recovery_goal_id, recovery_goal_hash)
    ON TABLE public.case_agent_ledger_exception_recovery_intents
    TO lawcase_agent_worker;

COMMIT;
