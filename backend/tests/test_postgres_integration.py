"""Opt-in destructive integration tests for an explicitly dedicated PostgreSQL test database."""

from __future__ import annotations

import os
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import unittest
from uuid import uuid4
from unittest.mock import patch

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from case_api.web_case_agent_recovery_goal import (
    build_web_case_agent_recovery_goal,
)

from case_kernel.case_ledger_postgres import PostgresCaseLedgerStore, _payload_hash
from case_kernel.case_agent_runtime_identity import case_agent_worker_id
from case_kernel.case_agent_runtime_postgres import PostgresCaseAgentRunInbox
from case_kernel.case_agent_postgres import PostgresCaseAgentStore
from case_kernel.case_agent_supervisor import (
    AgentDeliverableKind, AgentGoal, AgentSupervisorEvent, AgentEventType,
    RunCreatedPayload, RunResourceBudget, CaseSnapshotRef,
)
from case_kernel.case_agent_planning_snapshot_postgres import PostgresCasePlanningProjectionRepository
from case_kernel.case_agent_planner import CasePlanningSnapshot, PlanningInputRef, PlanningInputKind
from case_kernel.case_agent_ledger_exception_followup import (
    LedgerExceptionControlHealth,
    LedgerExceptionFollowupAction,
    ManagedEvidenceSourceRef,
    ManagedEvidenceSourceType,
    control_transfer_request_hash,
)
from case_kernel.case_agent_ledger_exception_followup_postgres import (
    PostgresCaseLedgerExceptionFollowupStore,
    preflight_case_agent_ledger_exception_followup_schema,
)
from case_kernel.case_agent_ledger_exception_review import (
    LedgerExceptionDecision,
    LedgerExceptionReason,
    exception_decision_request_hash,
)
from case_kernel.errors import IdempotencyConflict
from case_kernel.evidence_refs import EvidenceLink
from case_kernel.fact_claim_ledger import AssertionOrigin, ClaimResponsePosition, FactStatus
from case_kernel.models import Actor, Matter, MatterStage, Role
from case_kernel.case_posture_postgres import (
    PostgresCasePostureStore, CasePartyConfirmation, CourtProceedingConfirmation,
    CourtPartyPositionConfirmation, FirmEngagementConfirmation,
)
from case_kernel.postgres_store import (
    CaseAgentMatterProvisioningBlocked,
    PostgresMatterStore,
    preflight_case_agent_matter_provisioning_contract,
)
from case_kernel.workflow import MatterWorkflow
from case_kernel.transaction_ledger import (
    ClassificationOrigin,
    DatePrecision,
    ObligationAllocation,
    PaymentNature,
    TransactionChannel,
    TransactionDirection,
)


TEST_DSN = os.environ.get("CASE_WORKBENCH_TEST_DATABASE_URL", "")
WEB_APPLICATION_TEST_DSN = (
    make_conninfo(TEST_DSN, options="-c role=lawcase_web_application")
    if TEST_DSN
    else ""
)
ALLOW_DESTRUCTIVE = os.environ.get("CASE_WORKBENCH_ALLOW_DESTRUCTIVE_TEST_DB") == "YES"
NATIVE_PREPARED_SOCKET = os.environ.get("CASE_WORKBENCH_NATIVE_PREPARED_SOCKET", "")
BUSINESS_TEST_DSN = WEB_APPLICATION_TEST_DSN if NATIVE_PREPARED_SOCKET else TEST_DSN
WORKER_TEST_DSN = (
    make_conninfo(TEST_DSN, options="-c role=lawcase_agent_worker")
    if TEST_DSN and NATIVE_PREPARED_SOCKET else TEST_DSN
)
MIGRATIONS = tuple(sorted((Path(__file__).resolve().parents[1] / "migrations").glob("*.sql")))
RLS_TEST_ROLE = "lawcase_integration_rls_reader"
MIGRATION_PREREQUISITE_ROLES = (
    "lawcase_agent_worker",
    "lawcase_identity_directory",
    "lawcase_ledger_confirmation_owner",
    "lawcase_web_application",
    "lawcase_web_session_gateway",
)


def _configured_test_database() -> bool:
    if TEST_DSN and NATIVE_PREPARED_SOCKET:
        return True  # The isolated-server checks below precede every write.
    if not TEST_DSN or not ALLOW_DESTRUCTIVE:
        return False
    database_name = conninfo_to_dict(TEST_DSN).get("dbname", "")
    return database_name.endswith("_test")


def _validate_native_connection_target() -> None:
    """Reject network targets before connecting, not merely before writing."""
    project = Path(__file__).resolve().parents[2]
    socket = Path(NATIVE_PREPARED_SOCKET).resolve(strict=True)
    parameters = conninfo_to_dict(TEST_DSN)
    if (not socket.is_relative_to(project / "artifacts")
            or socket.stat().st_mode & 0o077
            or parameters.get("host") != str(socket)
            or any(parameters.get(key) for key in ("hostaddr", "service"))):
        raise RuntimeError("native business test requires a private project socket")


def _validate_native_prepared_database(connection) -> None:
    """Opt-in reuse of a freshly migrated project cluster; never reset a schema."""
    project = Path(__file__).resolve().parents[2]
    row = connection.execute("""
        SELECT current_setting('data_directory') AS data_directory,
               current_setting('listen_addresses') AS listen,
               current_setting('server_version_num')::int AS version,
               EXISTS (SELECT 1 FROM public.firms) AS populated,
               to_regclass('public.case_agent_document_revision_current_results') IS NOT NULL AS migrated
    """).fetchone()
    if (row["listen"] or not 160000 <= row["version"] < 170000
            or not Path(row["data_directory"]).resolve().is_relative_to(project / "artifacts/native-pg16")
            or row["populated"] or not row["migrated"]):
        raise RuntimeError("native business test refuses nonempty or unprepared cluster")


def _validate_native_business_roles() -> None:
    for dsn, expected in ((BUSINESS_TEST_DSN, "lawcase_web_application"),
                          (WORKER_TEST_DSN, "lawcase_agent_worker")):
        with psycopg.connect(dsn, row_factory=dict_row) as connection:
            row = connection.execute("""
                SELECT current_user AS role, rolsuper, rolbypassrls
                FROM pg_roles WHERE rolname = current_user
            """).fetchone()
            if (row is None or row["role"] != expected
                    or row["rolsuper"] or row["rolbypassrls"]):
                raise RuntimeError("business tests require non-bypass runtime roles")


@unittest.skipUnless(
    _configured_test_database(),
    "requires CASE_WORKBENCH_TEST_DATABASE_URL ending in _test and CASE_WORKBENCH_ALLOW_DESTRUCTIVE_TEST_DB=YES",
)
class PostgresMatterStoreIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if NATIVE_PREPARED_SOCKET:
            _validate_native_connection_target()
        with psycopg.connect(
            TEST_DSN, autocommit=True, row_factory=dict_row
        ) as connection:
            if NATIVE_PREPARED_SOCKET:
                _validate_native_prepared_database(connection)
                _validate_native_business_roles()
            for role_name in MIGRATION_PREREQUISITE_ROLES:
                role = connection.execute(
                    """
                    SELECT rolcanlogin, rolinherit, rolsuper, rolbypassrls
                    FROM pg_catalog.pg_roles WHERE rolname = %s
                    """,
                    (role_name,),
                ).fetchone()
                if role is None:
                    options = (
                        "NOLOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS"
                        if role_name == "lawcase_ledger_confirmation_owner"
                        else "NOLOGIN NOSUPERUSER NOBYPASSRLS"
                    )
                    connection.execute(
                        sql.SQL("CREATE ROLE {} " + options).format(
                            sql.Identifier(role_name)
                        )
                    )
                elif role_name == "lawcase_ledger_confirmation_owner" and (
                    role["rolcanlogin"]
                    or role["rolinherit"]
                    or role["rolsuper"]
                    or role["rolbypassrls"]
                ):
                    raise RuntimeError(
                        "dedicated test cluster has an unsafe ledger confirmation owner"
                    )
            if not NATIVE_PREPARED_SOCKET:
                connection.execute("DROP SCHEMA public CASCADE")
                connection.execute("CREATE SCHEMA public")
                for migration in MIGRATIONS:
                    connection.execute(migration.read_text(encoding="utf-8"))
            connection.execute(
                """
                DO $role$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_catalog.pg_roles
                        WHERE rolname = 'lawcase_integration_rls_reader'
                    ) THEN
                        CREATE ROLE lawcase_integration_rls_reader
                            NOLOGIN NOSUPERUSER NOBYPASSRLS;
                    END IF;
                END
                $role$;
                """
            )
            connection.execute(
                f"GRANT USAGE ON SCHEMA public TO {RLS_TEST_ROLE}"
            )
            connection.execute(
                f"GRANT SELECT ON TABLE matters TO {RLS_TEST_ROLE}"
            )
            connection.execute(
                f"""
                GRANT SELECT ON TABLE
                    case_agent_ledger_exception_followups,
                    case_agent_ledger_exception_control_assignments,
                    case_agent_ledger_exception_control_heads,
                    case_agent_ledger_exception_managed_evidence_requests,
                    case_agent_ledger_exception_evidence_source_bindings,
                    case_agent_ledger_exception_followup_events,
                    case_agent_ledger_exception_followup_heads
                TO {RLS_TEST_ROLE}
                """
            )

    def setUp(self) -> None:
        self.firm_id = str(uuid4())
        self.actor_id = str(uuid4())
        self.worker_id = str(uuid4())
        self.verifier_id = str(uuid4())
        self.actor = Actor(self.actor_id, self.firm_id, frozenset({Role.LEAD_LAWYER}))
        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute("INSERT INTO firms (firm_id, display_name) VALUES (%s, %s)", (self.firm_id, "Synthetic Test Firm"))
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (self.firm_id,))
            connection.execute(
                "INSERT INTO users (user_id, firm_id, external_subject, display_name, status) VALUES (%s, %s, %s, %s, 'ACTIVE')",
                (self.actor_id, self.firm_id, f"subject-{self.actor_id}", "Synthetic Lead"),
            )
            connection.execute(
                """
                INSERT INTO users (
                    user_id, firm_id, external_subject, display_name, status
                ) VALUES
                    (%s, %s, %s, %s, 'ACTIVE'),
                    (%s, %s, %s, %s, 'ACTIVE')
                """,
                (
                    self.worker_id,
                    self.firm_id,
                    f"system-worker-{self.worker_id}",
                    "Synthetic Agent Worker",
                    self.verifier_id,
                    self.firm_id,
                    f"system-verifier-{self.verifier_id}",
                    "Synthetic Agent Verifier",
                ),
            )
        self.store = PostgresMatterStore(
            BUSINESS_TEST_DSN,
            system_worker_ids_by_firm={self.firm_id: self.worker_id},
            system_verifier_ids_by_firm={self.firm_id: self.verifier_id},
        )
        self.case_ledger_store = PostgresCaseLedgerStore(BUSINESS_TEST_DSN)
        self.exception_followup_store = (
            PostgresCaseLedgerExceptionFollowupStore(WEB_APPLICATION_TEST_DSN)
        )
        self.workflow = MatterWorkflow(self.store)

    def test_active_plan_execution_guard_rejects_cross_tenant_row_before_binding_lookup(self) -> None:
        other_firm_id = str(uuid4())
        with psycopg.connect(
            WEB_APPLICATION_TEST_DSN,
            autocommit=True,
            row_factory=dict_row,
        ) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, false)",
                (self.firm_id,),
            )
            with self.assertRaises(psycopg.errors.InsufficientPrivilege) as caught:
                connection.execute(
                    """
                    INSERT INTO case_agent_active_plan_execution_runs (
                        execution_id, firm_id, matter_id, plan_id, plan_hash,
                        activated_matter_version, source_run_id, run_id, requested_by
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(uuid4()),
                        other_firm_id,
                        str(uuid4()),
                        str(uuid4()),
                        "0" * 64,
                        1,
                        str(uuid4()),
                        str(uuid4()),
                        str(uuid4()),
                    ),
                )
            self.assertEqual(caught.exception.sqlstate, "42501")
            self.assertIn(
                "active-plan execution tenant context differs from row",
                str(caught.exception),
            )
            current = connection.execute(
                "SELECT current_setting('app.firm_id', true) AS firm_id"
            ).fetchone()
            self.assertEqual(str(current["firm_id"]), self.firm_id)

    def _create_confirmed_document_matter(self):
        """Real command chain for document fixtures; no disabled constraints."""
        matter_id = str(uuid4())
        self.workflow.create_matter(self.actor, matter_id=matter_id,
            title="[合成] 文书修订事务案件", idempotency_key="document-matter-create")
        store = PostgresCasePostureStore(BUSINESS_TEST_DSN)
        scope = dict(matter_id=matter_id, actor=self.actor)
        party = store.confirm_party(**scope, expected_version=1,
            idempotency_key="document-party", confirmation=CasePartyConfirmation(
                "NATURAL_PERSON", "[合成] 委托人甲", sha256(b"synthetic-party").hexdigest()))
        proceeding = store.confirm_proceeding(**scope, expected_version=party.matter_version,
            idempotency_key="document-proceeding", confirmation=CourtProceedingConfirmation(
                "COURT", "CIVIL.PRIVATE_LENDING", "FIRST_INSTANCE", sha256(b"synthetic-proceeding").hexdigest()))
        position = store.confirm_position(**scope, expected_version=proceeding.matter_version,
            idempotency_key="document-position", confirmation=CourtPartyPositionConfirmation(
                proceeding.object_id, party.object_id, "DEFENDANT", sha256(b"synthetic-position").hexdigest()))
        engagement = store.confirm_engagement(**scope, expected_version=position.matter_version,
            idempotency_key="document-engagement", confirmation=FirmEngagementConfirmation(
                proceeding.object_id, party.object_id, "GENERAL", "ACTIVE", sha256(b"synthetic-engagement").hexdigest()))
        profile_args = dict(**scope, expected_version=engagement.matter_version,
            idempotency_key="document-profile", represented_party_id=party.object_id,
            proceeding_id=proceeding.object_id, position_id=position.object_id, engagement_id=engagement.object_id)
        confirmed = store.confirm_current_profile(**profile_args)
        self.assertEqual(store.confirm_current_profile(**profile_args), confirmed)
        profile = store.get_current_profile(**scope)
        self.assertIsNotNone(profile)
        self.assertEqual(profile.profile_id, confirmed.object_id)
        self.assertEqual(profile.represented_position, "DEFENDANT")
        # Profile binds the reviewed input version; the command then advances it.
        self.assertEqual(profile.confirmed_matter_version, engagement.matter_version)
        self.assertEqual(confirmed.matter_version, engagement.matter_version + 1)
        self.assertEqual(profile.effective_status, "CURRENT")
        return matter_id, profile

    def test_document_fixture_confirms_posture_through_web_commands(self) -> None:
        matter_id, profile = self._create_confirmed_document_matter()
        self.assertEqual(self.workflow.get_matter(self.actor, matter_id=matter_id).version,
                         profile.confirmed_matter_version + 1)
        events = self.store.audit_events(matter_id, firm_id=self.firm_id)
        self.assertEqual(len(events), 6)
        self.assertEqual(events[-1].event_type, "CASE_POSTURE_PROFILE_CONFIRMED")

    def _create_document_planning_run(self, *, allow_one_external_call=False):
        matter_id, profile = self._create_confirmed_document_matter()
        snapshot = self.case_ledger_store.get_case_snapshot(matter_id=matter_id, actor=self.actor)
        snapshot_ref = CaseSnapshotRef(matter_id=matter_id, matter_version=snapshot.version,
            snapshot_hash=snapshot.snapshot_hash, schema_version="case-ledger-snapshot-v1")
        goal = AgentGoal.build(goal_id=str(uuid4()),
            objective="[合成] 形成可追溯的应诉文书审阅稿，列出尚缺的证据。",
            success_criteria=("文书段落绑定来源", "明确资料缺口"),
            constraints=("不得编造证据或自动提交",), requested_by=self.actor_id)
        event = AgentSupervisorEvent(event_id=str(uuid4()), run_id=str(uuid4()),
            firm_id=self.firm_id, matter_id=matter_id, sequence=1,
            event_type=AgentEventType.RUN_CREATED, occurred_at=datetime.now(timezone.utc),
            actor_id=self.actor_id, payload=RunCreatedPayload(goal=goal, snapshot=snapshot_ref,
                budget=RunResourceBudget(20, 40, int(allow_one_external_call), 1800,
                                         100 if allow_one_external_call else 0, 10000000)))
        store = PostgresCaseAgentStore(BUSINESS_TEST_DSN)
        args = dict(matter_id=matter_id, actor=self.actor, expected_matter_version=snapshot.version,
                    idempotency_key="document-planning-run", event=event)
        receipt = store.create_run(**args)
        self.assertEqual(store.create_run(**args), receipt)
        worker = Actor(self.worker_id, self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        projection = PostgresCasePlanningProjectionRepository(WORKER_TEST_DSN).read_atomic_projection(
            firm_id=self.firm_id, matter_id=matter_id, actor=worker, expected_case_snapshot=snapshot_ref)
        return event, profile, projection

    def test_document_planning_run_reads_actual_confirmed_context(self) -> None:
        event, profile, projection = self._create_document_planning_run()
        self.assertEqual(projection.posture.profile_id, profile.profile_id)
        self.assertEqual(projection.posture.profile_hash, profile.profile_hash)
        self.assertEqual(projection.opening_case_snapshot, event.payload.snapshot)
        self.assertEqual(projection.closing_case_snapshot, event.payload.snapshot)

    def test_document_planning_claim_is_durable_without_provider_submission(self) -> None:
        event, profile, projection = self._create_document_planning_run()
        posture_object = next(item for item in projection.objects if item.object_id == profile.profile_id)
        planning = CasePlanningSnapshot.build(case_snapshot=event.payload.snapshot,
            authorized_inputs=(PlanningInputRef(ref_id=posture_object.ref_id,
                kind=PlanningInputKind.PROCEDURAL_EVENT, object_version=posture_object.object_version,
                content_hash=posture_object.content_hash, status=posture_object.status,
                allowed_skill_ids=("case_context_review",)),), signals=())
        actor = Actor(self.worker_id, self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        store = PostgresCaseAgentStore(WORKER_TEST_DSN)
        args = dict(matter_id=event.matter_id, actor=actor, run_id=event.run_id,
            expected_event_version=1, planning_hash=planning.planning_hash,
            planning_kind="PLAN", idempotency_key="document-plan-claim",
            lease_owner=case_agent_worker_id(self.firm_id), lease_seconds=120)
        claim = store.claim_planning_attempt(**args)
        current = store.current_planning_attempt(matter_id=event.matter_id, actor=actor, run_id=event.run_id)
        self.assertEqual(current.planning_attempt_id, claim.planning_attempt_id)
        self.assertEqual(current.status, "CLAIMED")
        self.assertIsNone(current.recovered_proposal)
        self.assertEqual(current.planning_hash, planning.planning_hash)

    @unittest.skipUnless(os.environ.get("CASE_WORKBENCH_RUN_ONE_PLANNER_TRIAL") == "YES",
                         "paid single-call synthetic planner trial is opt-in")
    def test_one_synthetic_provider_plan_enters_durable_graph(self) -> None:
        from case_kernel.deepseek_case_agent_planner import (
            DeepSeekCaseAgentPlanner, DeepSeekPlannerCredentials, DeepSeekPlannerProviderConfig,
            PlannerExternalExecutionClaim, prepare_deepseek_planner_request,
            DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT,
        )
        from case_kernel.case_agent_worker_postgres import PostgresCaseAgentWorkerAdapter
        from case_kernel.case_agent_planner import CaseAgentPlannerCompiler
        from case_kernel.case_agent_case_context_adapters import CASE_CONTEXT_REVIEW_MANIFEST
        from case_kernel.skill_registry import default_case_skill_registry
        from case_api.case_agent_worker_runtime import _case_context_policy
        if not NATIVE_PREPARED_SOCKET:
            self.fail("paid trial requires the isolated native cluster")
        credentials = DeepSeekPlannerCredentials(os.environ["LAWCASE_AGENT_WORKER_DEEPSEEK_API_KEY"])
        # Same pinned model as the project's current managed composition.
        config = DeepSeekPlannerProviderConfig(endpoint=DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT,
            model="deepseek-v4-pro", allowed_models=("deepseek-v4-pro",),
            timeout_seconds=60, max_output_tokens=2048)
        event, profile, projection = self._create_document_planning_run(allow_one_external_call=True)
        source = next(item for item in projection.objects if item.object_id == profile.profile_id)
        planning = CasePlanningSnapshot.build(case_snapshot=event.payload.snapshot,
            authorized_inputs=(PlanningInputRef(ref_id=source.ref_id, kind=PlanningInputKind.PROCEDURAL_EVENT,
                object_version=source.object_version, content_hash=source.content_hash, status=source.status,
                allowed_skill_ids=("case_context_review",)),), signals=())
        actor = Actor(self.worker_id, self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        store = PostgresCaseAgentStore(WORKER_TEST_DSN)
        adapter = PostgresCaseAgentWorkerAdapter(store=store, actor=actor)
        compiler = CaseAgentPlannerCompiler(registry=default_case_skill_registry(case_context_review_enabled=True),
            adapters={CASE_CONTEXT_REVIEW_MANIFEST.tool_id: CASE_CONTEXT_REVIEW_MANIFEST},
            skill_policies=(_case_context_policy(),))
        skills = compiler.semantic_skill_catalog()
        prepared = prepare_deepseek_planner_request(goal=event.payload.goal, snapshot=planning,
                                                   skills=skills, config=config)
        self.assertLessEqual(len(prepared.body), 32768)
        claim = adapter.claim_next_planning_command(matter_id=event.matter_id, actor=actor,
            run_id=event.run_id, expected_event_version=1, planning_hash=planning.planning_hash,
            idempotency_key="single-provider-plan-claim", lease_owner=case_agent_worker_id(self.firm_id),
            lease_seconds=120)
        planner = DeepSeekCaseAgentPlanner(credentials=credentials, config=config, request_guard=adapter)
        print("Single synthetic planner request: cap=1, output_tokens=2048; durable run=" + event.run_id, flush=True)
        proposal = planner.plan(goal=event.payload.goal, snapshot=planning, skills=skills,
            execution=PlannerExternalExecutionClaim(external_request_id=claim.external_request_id,
                run_id=claim.run_id, claim_lease_id=claim.claim_lease_id, lease_token=claim.lease_token,
                matter_version=event.payload.snapshot.matter_version))
        graph = compiler.compile(graph_id=claim.graph_id, graph_version=claim.graph_version,
            goal=event.payload.goal, snapshot=planning, proposal=proposal, run_budget=event.payload.budget)
        adapter.accept_planning_graph(matter_id=event.matter_id, actor=actor, claim=claim,
            expected_attempt_version=claim.attempt_version, idempotency_key="single-provider-plan-accept", graph=graph)
        current = store.read_projection(matter_id=event.matter_id, actor=actor, run_id=event.run_id).state
        self.assertEqual(current.graph.graph_hash, graph.graph_hash)
        self.assertGreater(len(current.graph.tasks), 0)

    def test_web_role_prepares_active_plan_execution_in_read_only_transaction(self) -> None:
        matter_id = str(uuid4())
        self.workflow.create_matter(
            self.actor,
            matter_id=matter_id,
            title="Synthetic active-plan matter",
            idempotency_key="active-plan-prepare-matter-0001",
        )
        goal_id, source_run_id, plan_id = str(uuid4()), str(uuid4()), str(uuid4())
        promotion_id, profile_id, graph_id, receipt_id = (
            str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4())
        )
        item_ids = (str(uuid4()), str(uuid4()))
        hashes = {name: sha256(name.encode()).hexdigest() for name in (
            "goal", "snapshot", "projection", "plan", "profile", "graph", "verification",
        )}
        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (self.firm_id,))
            # This fixture bypasses unrelated promotion FKs/triggers only; the
            # production Store call below runs as the real Web role with every
            # RLS policy, privilege and SELECT shape enabled.
            connection.execute("SET LOCAL session_replication_role = replica")
            connection.execute(
                """
                INSERT INTO case_agent_goals (
                    goal_id, firm_id, matter_id, objective, success_criteria,
                    constraints, requested_by, goal_hash, requested_deliverables
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    goal_id, self.firm_id, matter_id, "Produce reviewed deliverables",
                    Jsonb(["memo", "ledger"]), Jsonb([]), self.actor_id,
                    hashes["goal"], Jsonb(["CASE_REVIEW_MEMO", "PAYMENT_LEDGER"]),
                ),
            )
            connection.execute(
                """
                INSERT INTO case_agent_runs (
                    run_id, firm_id, matter_id, goal_id, status,
                    current_event_version, snapshot_matter_version,
                    snapshot_schema_version, snapshot_hash, run_budget,
                    projection_hash, created_by
                ) VALUES (%s,%s,%s,%s,'READY_FOR_REVIEW',1,1,%s,%s,%s,%s,%s)
                """,
                (
                    source_run_id, self.firm_id, matter_id, goal_id,
                    "case-ledger-snapshot-v1", hashes["snapshot"],
                    Jsonb({"max_tasks": 2}), hashes["projection"], self.actor_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO case_work_plans (
                    plan_id, firm_id, matter_id, plan_version, status,
                    planned_matter_version, profile_id, profile_version, profile_hash,
                    objective_approval_id, agent_goal_id, objective_hash,
                    claim_scope_hash, procedure_context_hash, legal_context_hash,
                    context_hash, candidate_input_hash, plan_hash, agent_id,
                    agent_version, generated_at, required_court_document_kinds,
                    primary_court_document_kind, registered_by, confirmed_by,
                    confirmation_hash, confirmed_at, activated_matter_version
                ) VALUES (
                    %s,%s,%s,1,'ACTIVE',1,%s,1,%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s,
                    'case-agent','1.0.0',clock_timestamp(),'[]'::jsonb,NULL,%s,%s,%s,
                    clock_timestamp(),1
                )
                """,
                (
                    plan_id, self.firm_id, matter_id, profile_id, hashes["profile"],
                    goal_id, hashes["goal"], hashes["goal"], hashes["goal"],
                    hashes["goal"], hashes["goal"], hashes["goal"], hashes["plan"],
                    self.actor_id, self.actor_id, hashes["plan"],
                ),
            )
            for sequence, (item_id, kind) in enumerate(zip(
                item_ids, ("CASE_REVIEW_MEMO", "PAYMENT_LEDGER"), strict=True
            ), start=1):
                connection.execute(
                    """
                    INSERT INTO case_work_plan_items (
                        item_id, plan_id, firm_id, matter_id, sequence, item_kind,
                        readiness, title, purpose, rationale, risk_if_omitted,
                        confidence, review_gate, delivery_target, deliverable_kind,
                        required_for_delivery, is_primary_document
                    ) VALUES (
                        %s,%s,%s,%s,%s,'DOCUMENT_CANDIDATE','ACTIONABLE',%s,%s,%s,%s,
                        0.9,'LEAD_LAWYER_CONFIRMATION','INTERNAL_WORK_PRODUCT',%s,false,false
                    )
                    """,
                    (
                        item_id, plan_id, self.firm_id, matter_id, sequence,
                        f"Prepare {kind}", f"Generate {kind}", "Reviewed source is ready",
                        "Omission blocks the requested deliverable", kind,
                    ),
                )
            connection.execute(
                """
                INSERT INTO case_work_plan_heads (
                    matter_id, firm_id, latest_plan_version, current_plan_id
                ) VALUES (%s,%s,1,%s)
                """,
                (matter_id, self.firm_id, plan_id),
            )
            connection.execute(
                """
                INSERT INTO case_agent_work_plan_promotions (
                    promotion_id, plan_id, firm_id, matter_id, run_id, graph_id,
                    graph_version, graph_hash, snapshot_matter_version,
                    snapshot_schema_version, snapshot_hash, goal_id, goal_hash,
                    posture_profile_id, posture_profile_version, posture_profile_hash,
                    verification_receipt_id, verification_hash, verifier_actor_id,
                    execution_actor_id, task_count, promoted_by
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,1,%s,1,%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s,2,%s
                )
                """,
                (
                    promotion_id, plan_id, self.firm_id, matter_id, source_run_id,
                    graph_id, hashes["graph"], "case-ledger-snapshot-v1",
                    hashes["snapshot"], goal_id, hashes["goal"], profile_id,
                    hashes["profile"], receipt_id, hashes["verification"],
                    self.verifier_id, self.worker_id, self.actor_id,
                ),
            )

        prepared = PostgresCaseAgentStore(
            WEB_APPLICATION_TEST_DSN
        ).prepare_active_plan_execution(
            matter_id=matter_id,
            actor=self.actor,
            expected_matter_version=1,
        )
        self.assertEqual(prepared.execution.plan_id, plan_id)
        self.assertEqual(
            tuple(item.deliverable_kind for item in prepared.execution.items),
            (
                AgentDeliverableKind.CASE_REVIEW_MEMO,
                AgentDeliverableKind.PAYMENT_LEDGER,
            ),
        )
        self.assertIsNone(prepared.existing_run_id)

    def _create_0049_followup(
        self, *, decision: str, route_decision: bool = True
    ) -> dict[str, str]:
        """Build only the immutable pre-0049 boundary needed by the test.

        The extraction batch deliberately uses a superuser-only fixture insert
        with FK triggers disabled for that one statement.  The 0031/0042
        execution-artifact lineage is tested elsewhere; all 0047 group
        integrity checks and every 0049 trigger, FK, RLS policy and definer
        command remain enabled here.
        """

        matter_id = str(uuid4())
        run_id = str(uuid4())
        goal_id = str(uuid4())
        batch_id = str(uuid4())
        candidate_id = str(uuid4())
        evidence_file_id = str(uuid4())
        evidence_page_id = str(uuid4())
        session_id = str(uuid4())
        session_token_sha256 = uuid4().hex + uuid4().hex
        csrf_token_sha256 = uuid4().hex + uuid4().hex
        decision_id = str(uuid4())
        self.workflow.create_matter(
            self.actor,
            matter_id=matter_id,
            title=f"Synthetic 0049 {decision}",
            idempotency_key=f"create-0049-{decision.lower()}",
        )
        if decision == "REQUEST_REEXTRACTION":
            reason_codes = ["OCR_DERIVED"]
            reason_code = "SOURCE_QUALITY_INSUFFICIENT"
            reason_note = "重新核验原始页"
        elif decision == "REQUEST_MORE_EVIDENCE":
            reason_codes = ["PARTY_AMBIGUOUS"]
            reason_code = "EVIDENCE_GAP"
            reason_note = "补充新的受管证据来源"
        elif decision == "DEFER_WITH_REASON":
            reason_codes = ["LOW_CONFIDENCE"]
            reason_code = "NEEDS_LEAD_REVIEW"
            reason_note = "等待承办律师复核"
        else:  # pragma: no cover - test helper contract
            raise AssertionError(f"unsupported synthetic decision: {decision}")

        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)", (self.firm_id,)
            )
            connection.execute(
                """
                INSERT INTO web_sessions (
                    session_id, firm_id, user_id, issuer,
                    session_token_sha256, csrf_token_sha256,
                    authenticated_at, created_at, expires_at
                ) VALUES (
                    %s,%s,%s,'https://synthetic-oidc.test',
                    decode(%s, 'hex'),decode(%s, 'hex'),%s,%s,%s
                )
                """,
                (
                    session_id,
                    self.firm_id,
                    self.actor_id,
                    session_token_sha256,
                    csrf_token_sha256,
                    datetime.now(timezone.utc) - timedelta(minutes=1),
                    datetime.now(timezone.utc),
                    datetime.now(timezone.utc) + timedelta(hours=1),
                ),
            )
            connection.execute(
                """
                INSERT INTO case_agent_goals (
                    goal_id, firm_id, matter_id, objective,
                    success_criteria, constraints, requested_by, goal_hash
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    goal_id,
                    self.firm_id,
                    matter_id,
                    "Synthetic verified extraction review",
                    Jsonb(["route every exception group"]),
                    Jsonb(["no formal ledger write"]),
                    self.actor_id,
                    "3" * 64,
                ),
            )
            connection.execute(
                """
                INSERT INTO case_agent_runs (
                    run_id, firm_id, matter_id, goal_id, status,
                    current_event_version, snapshot_matter_version,
                    snapshot_schema_version, snapshot_hash, run_budget,
                    projection_hash, created_by
                ) VALUES (
                    %s,%s,%s,%s,'READY_FOR_REVIEW',1,1,
                    'synthetic-0049-snapshot-v1',%s,%s,%s,%s
                )
                """,
                (
                    run_id,
                    self.firm_id,
                    matter_id,
                    goal_id,
                    "4" * 64,
                    Jsonb({"max_tasks": 10}),
                    "5" * 64,
                    self.actor_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO evidence_original_files (
                    evidence_file_id, firm_id, matter_id, original_label,
                    original_file_sha256, byte_size, media_type, page_count,
                    source_scan_fingerprint
                ) VALUES (%s,%s,%s,%s,%s,128,'application/pdf',1,%s)
                """,
                (
                    evidence_file_id,
                    self.firm_id,
                    matter_id,
                    "[合成] 原始证据.pdf",
                    "6" * 64,
                    "7" * 64,
                ),
            )
            connection.execute(
                """
                INSERT INTO evidence_pages (
                    evidence_page_id, firm_id, matter_id,
                    evidence_file_id, page_number, rendered_page_sha256
                ) VALUES (%s,%s,%s,%s,1,%s)
                """,
                (
                    evidence_page_id,
                    self.firm_id,
                    matter_id,
                    evidence_file_id,
                    "8" * 64,
                ),
            )
            connection.execute("SET LOCAL session_replication_role = replica")
            connection.execute(
                """
                INSERT INTO case_agent_ledger_extraction_batches (
                    extraction_batch_id, run_id, graph_id, task_id,
                    artifact_id, verification_receipt_id, firm_id, matter_id,
                    source_matter_version, staged_matter_version,
                    artifact_content_sha256, source_hash, task_input_hash,
                    candidate_count, eligible_candidate_count, staged_by
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,1,1,%s,%s,%s,1,0,%s
                )
                """,
                (
                    batch_id,
                    run_id,
                    str(uuid4()),
                    str(uuid4()),
                    str(uuid4()),
                    str(uuid4()),
                    self.firm_id,
                    matter_id,
                    "9" * 64,
                    "a" * 64,
                    "b" * 64,
                    self.worker_id,
                ),
            )
            connection.execute("SET LOCAL session_replication_role = origin")
            connection.execute(
                """
                INSERT INTO case_agent_ledger_extraction_candidates (
                    extraction_candidate_id, extraction_batch_id,
                    firm_id, matter_id, candidate_hash, candidate_kind,
                    confidence, review_lane, eligible_for_bulk_promotion,
                    review_reason_codes, candidate_payload, review_status
                ) VALUES (
                    %s,%s,%s,%s,%s,'FACT',0.5,'EXCEPTION_REVIEW',false,
                    %s,%s,'NEEDS_LAWYER_REVIEW'
                )
                """,
                (
                    candidate_id,
                    batch_id,
                    self.firm_id,
                    matter_id,
                    "c" * 64,
                    reason_codes,
                    Jsonb({"synthetic": True}),
                ),
            )
            connection.execute(
                """
                INSERT INTO case_agent_ledger_extraction_candidate_pages (
                    extraction_candidate_id, evidence_page_id,
                    firm_id, matter_id, source_text_sha256
                ) VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    candidate_id,
                    evidence_page_id,
                    self.firm_id,
                    matter_id,
                    "d" * 64,
                ),
            )
            connection.execute(
                "SELECT materialize_case_agent_ledger_exception_groups(%s,%s,%s)",
                (batch_id, self.firm_id, matter_id),
            )
            group = connection.execute(
                """
                SELECT exception_group_id, group_key_hash,
                       candidate_set_hash, candidate_count
                  FROM case_agent_ledger_exception_groups
                 WHERE extraction_batch_id = %s
                """,
                (batch_id,),
            ).fetchone()
            self.assertIsNotNone(group)
            if not route_decision:
                return {
                    "matter_id": matter_id,
                    "run_id": run_id,
                    "goal_id": goal_id,
                    "batch_id": batch_id,
                    "group_id": str(group["exception_group_id"]),
                    "session_id": session_id,
                    "decision": decision,
                    "reason_code": reason_code,
                    "reason_note": reason_note,
                    "old_evidence_file_id": evidence_file_id,
                    "evidence_page_id": evidence_page_id,
                }
            connection.execute(
                """
                INSERT INTO case_agent_ledger_exception_group_decisions (
                    exception_decision_id, exception_group_id,
                    extraction_batch_id, run_id, firm_id, matter_id,
                    bound_group_key_hash, bound_candidate_set_hash,
                    bound_candidate_count, decision, reason_code, reason_note,
                    decision_hash, expected_matter_version, decided_by,
                    idempotency_key, request_hash
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s
                )
                """,
                (
                    decision_id,
                    group["exception_group_id"],
                    batch_id,
                    run_id,
                    self.firm_id,
                    matter_id,
                    group["group_key_hash"],
                    group["candidate_set_hash"],
                    group["candidate_count"],
                    decision,
                    reason_code,
                    reason_note,
                    "0" * 64,
                    self.actor_id,
                    f"synthetic-route-{decision.lower()}",
                    "e" * 64,
                ),
            )
            lifecycle = connection.execute(
                """
                SELECT followup.followup_id, followup.followup_kind,
                       head.current_state, control.current_state AS control_health
                  FROM case_agent_ledger_exception_followups followup
                  JOIN case_agent_ledger_exception_followup_heads head
                    ON head.followup_id = followup.followup_id
                  JOIN case_agent_ledger_exception_control_heads control
                    ON control.firm_id = followup.firm_id
                   AND control.matter_id = followup.matter_id
                 WHERE followup.origin_exception_decision_id = %s
                """,
                (decision_id,),
            ).fetchone()
            self.assertIsNotNone(lifecycle)
            self.assertEqual(lifecycle["current_state"], "ACTIVE")
            self.assertEqual(lifecycle["control_health"], "HEALTHY")
        return {
            "matter_id": matter_id,
            "run_id": run_id,
            "goal_id": goal_id,
            "batch_id": batch_id,
            "group_id": str(group["exception_group_id"]),
            "followup_id": str(lifecycle["followup_id"]),
            "session_id": session_id,
            "old_evidence_file_id": evidence_file_id,
            "evidence_page_id": evidence_page_id,
        }

    def _create_0048_low_risk_batch(
        self, *, exception_lane: bool = False
    ) -> dict[str, str]:
        """Create one complete verified review lane for the 0048 definers."""

        ids = {
            name: str(uuid4())
            for name in (
                "matter_id", "goal_id", "run_id", "graph_id", "task_id",
                "attempt_id", "receipt_id", "artifact_id", "batch_id",
                "candidate_id", "evidence_file_id", "evidence_page_id",
                "session_id",
            )
        }
        self.workflow.create_matter(
            self.actor,
            matter_id=ids["matter_id"],
            title=(
                "Synthetic 0048 exception decision"
                if exception_lane
                else "Synthetic 0048 low-risk confirmation"
            ),
            idempotency_key=(
                "create-0048-exception-decision"
                if exception_lane
                else "create-0048-low-risk-confirmation"
            ),
        )
        snapshot_hash = "4" * 64
        graph_hash = "5" * 64
        verification_hash = "6" * 64
        task_input_hash = "7" * 64
        candidate_hash = "8" * 64
        source_text = "2024年1月2日，双方签署借款合同。"
        source_text_hash = sha256(source_text.encode("utf-8")).hexdigest()
        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (self.firm_id,),
            )
            connection.execute(
                """
                INSERT INTO web_sessions (
                    session_id, firm_id, user_id, issuer,
                    session_token_sha256, csrf_token_sha256,
                    authenticated_at, created_at, expires_at
                ) VALUES (
                    %s,%s,%s,'https://synthetic-oidc.test',
                    decode(%s,'hex'),decode(%s,'hex'),
                    clock_timestamp() - interval '1 minute',
                    clock_timestamp(), clock_timestamp() + interval '1 hour'
                )
                """,
                (
                    ids["session_id"], self.firm_id, self.actor_id,
                    uuid4().hex + uuid4().hex, uuid4().hex + uuid4().hex,
                ),
            )
            connection.execute(
                """
                INSERT INTO case_agent_goals (
                    goal_id, firm_id, matter_id, objective,
                    success_criteria, constraints, requested_by, goal_hash
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    ids["goal_id"], self.firm_id, ids["matter_id"],
                    "Confirm one verified low-risk fact",
                    Jsonb(["write the complete low-risk lane"]),
                    Jsonb(["preserve exact source binding"]),
                    self.actor_id, "3" * 64,
                ),
            )
            connection.execute(
                """
                INSERT INTO case_agent_runs (
                    run_id, firm_id, matter_id, goal_id, status,
                    current_event_version, snapshot_matter_version,
                    snapshot_schema_version, snapshot_hash, run_budget,
                    current_graph_id, current_graph_version,
                    current_graph_hash, projection_hash, verification_hash,
                    created_by
                ) VALUES (
                    %s,%s,%s,%s,'READY_FOR_REVIEW',1,1,
                    'synthetic-0048-snapshot-v1',%s,%s,%s,1,%s,%s,%s,%s
                )
                """,
                (
                    ids["run_id"], self.firm_id, ids["matter_id"],
                    ids["goal_id"], snapshot_hash, Jsonb({"max_tasks": 1}),
                    ids["graph_id"], graph_hash, "9" * 64,
                    verification_hash, self.actor_id,
                ),
            )
            connection.execute("SET LOCAL session_replication_role = replica")
            connection.execute(
                """
                INSERT INTO case_agent_task_graphs (
                    graph_id, run_id, firm_id, matter_id, graph_version,
                    goal_hash, snapshot_matter_version,
                    snapshot_schema_version, snapshot_hash, graph_hash,
                    accepted_event_sequence
                ) VALUES (%s,%s,%s,%s,1,%s,1,%s,%s,%s,1)
                """,
                (
                    ids["graph_id"], ids["run_id"], self.firm_id,
                    ids["matter_id"], "3" * 64,
                    "synthetic-0048-snapshot-v1", snapshot_hash, graph_hash,
                ),
            )
            connection.execute(
                """
                INSERT INTO case_agent_tasks (
                    graph_id, task_id, run_id, firm_id, matter_id, sequence,
                    title, purpose, rationale, input_refs, input_hash,
                    skill_id, skill_version, tool_id, tool_version,
                    adapter_id, adapter_version, granted_scopes,
                    execution_mode, network_policy, allowed_domains,
                    sandbox_profile, sandbox_policy_version,
                    sandbox_policy_hash, reads_case_objects,
                    writes_managed_derivatives,
                    external_request_approval_required, risk_level,
                    autonomy_level, approval_gate, retry_mode, resource_budget
                ) VALUES (
                    %s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s,
                    'synthetic.extract','1.0.0','synthetic.tool','1.0.0',
                    'synthetic.adapter','1.0.0',%s,
                    'IN_PROCESS','DENY',%s,'standard','1.0.0',%s,%s,
                    false,false,'LOW','A1_PROPOSE','LAWYER_REVIEW',
                    'IDEMPOTENT',%s
                )
                """,
                (
                    ids["graph_id"], ids["task_id"], ids["run_id"],
                    self.firm_id, ids["matter_id"], "Extract ledger fact",
                    "Produce one source-bound fact", "Synthetic real-PG gate",
                    Jsonb([f"evidence-page:{ids['evidence_page_id']}"]),
                    task_input_hash, Jsonb([]), Jsonb([]), "a" * 64,
                    Jsonb([f"evidence-page:{ids['evidence_page_id']}"]),
                    Jsonb({"max_seconds": 30}),
                ),
            )
            connection.execute(
                """
                INSERT INTO case_agent_verification_attempts (
                    verification_attempt_id, run_id, graph_id, firm_id,
                    matter_id, execution_actor_id, verifier_actor_id,
                    verifier_id, verifier_version, policy_hash, graph_hash,
                    snapshot_hash, started_event_sequence
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,'synthetic.verifier','1.0.0',%s,%s,%s,1)
                """,
                (
                    ids["attempt_id"], ids["run_id"], ids["graph_id"],
                    self.firm_id, ids["matter_id"], self.worker_id,
                    self.verifier_id, "b" * 64, graph_hash, snapshot_hash,
                ),
            )
            connection.execute(
                """
                INSERT INTO case_agent_verification_receipts (
                    verification_receipt_id, verification_attempt_id, run_id,
                    firm_id, matter_id, outcome, verifier_id,
                    verifier_version, policy_hash, graph_hash, snapshot_hash,
                    task_receipts_hash, artifact_manifest_hash,
                    artifact_lineage, verification_hash, verifier_actor_id,
                    execution_actor_id, verified_at, terminal_event_sequence
                ) VALUES (
                    %s,%s,%s,%s,%s,'PASSED','synthetic.verifier','1.0.0',
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp(),2
                )
                """,
                (
                    ids["receipt_id"], ids["attempt_id"], ids["run_id"],
                    self.firm_id, ids["matter_id"], "b" * 64, graph_hash,
                    snapshot_hash, "c" * 64, "d" * 64,
                    Jsonb([{
                        "artifact_id": ids["artifact_id"],
                        "artifact_kind": "CASE_LEDGER_EXTRACTION_CANDIDATE",
                    }]),
                    verification_hash, self.verifier_id, self.worker_id,
                ),
            )
            connection.execute("SET LOCAL session_replication_role = origin")
            connection.execute(
                """
                INSERT INTO evidence_original_files (
                    evidence_file_id, firm_id, matter_id, original_label,
                    original_file_sha256, byte_size, media_type, page_count,
                    source_scan_fingerprint
                ) VALUES (%s,%s,%s,%s,%s,128,'application/pdf',1,%s)
                """,
                (
                    ids["evidence_file_id"], self.firm_id, ids["matter_id"],
                    "[合成] 低风险事实.pdf", "e" * 64, "f" * 64,
                ),
            )
            connection.execute(
                """
                INSERT INTO evidence_pages (
                    evidence_page_id, firm_id, matter_id, evidence_file_id,
                    page_number, rendered_page_sha256
                ) VALUES (%s,%s,%s,%s,1,%s)
                """,
                (
                    ids["evidence_page_id"], self.firm_id, ids["matter_id"],
                    ids["evidence_file_id"], "1" * 64,
                ),
            )
            connection.execute("SET LOCAL session_replication_role = replica")
            connection.execute(
                """
                INSERT INTO case_agent_ledger_extraction_batches (
                    extraction_batch_id, run_id, graph_id, task_id,
                    artifact_id, verification_receipt_id, firm_id, matter_id,
                    source_matter_version, staged_matter_version,
                    artifact_content_sha256, source_hash, task_input_hash,
                    candidate_count, eligible_candidate_count, staged_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,1,%s,%s,%s,1,%s,%s)
                """,
                (
                    ids["batch_id"], ids["run_id"], ids["graph_id"],
                    ids["task_id"], ids["artifact_id"], ids["receipt_id"],
                    self.firm_id, ids["matter_id"], "2" * 64, "3" * 64,
                    task_input_hash, 0 if exception_lane else 1, self.worker_id,
                ),
            )
            connection.execute("SET LOCAL session_replication_role = origin")
            connection.execute(
                """
                INSERT INTO case_agent_ledger_extraction_candidates (
                    extraction_candidate_id, extraction_batch_id, firm_id,
                    matter_id, candidate_hash, candidate_kind, confidence,
                    review_lane, eligible_for_bulk_promotion,
                    review_reason_codes, candidate_payload, review_status
                ) VALUES (
                    %s,%s,%s,%s,%s,'FACT',%s,%s,%s,%s,%s,
                    'NEEDS_LAWYER_REVIEW'
                )
                """,
                (
                    ids["candidate_id"], ids["batch_id"], self.firm_id,
                    ids["matter_id"], candidate_hash,
                    0.5 if exception_lane else 0.99,
                    (
                        "EXCEPTION_REVIEW"
                        if exception_lane
                        else "BULK_PROMOTION_ELIGIBLE"
                    ),
                    not exception_lane,
                    ["LOW_CONFIDENCE"] if exception_lane else [],
                    Jsonb({
                        "kind": "FACT",
                        "fact_text": "双方于2024年1月2日签署借款合同",
                        "evidence_page_ids": [ids["evidence_page_id"]],
                        "supporting_excerpts": [{
                            "evidence_page_id": ids["evidence_page_id"],
                            "text": "双方签署借款合同",
                        }],
                    }),
                ),
            )
            connection.execute(
                """
                INSERT INTO case_agent_ledger_extraction_candidate_pages (
                    extraction_candidate_id, evidence_page_id, firm_id,
                    matter_id, source_text_sha256
                ) VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    ids["candidate_id"], ids["evidence_page_id"],
                    self.firm_id, ids["matter_id"], source_text_hash,
                ),
            )
            if exception_lane:
                connection.execute(
                    "SELECT materialize_case_agent_ledger_exception_groups(%s,%s,%s)",
                    (ids["batch_id"], self.firm_id, ids["matter_id"]),
                )
                group = connection.execute(
                    """
                    SELECT exception_group_id
                      FROM case_agent_ledger_exception_groups
                     WHERE extraction_batch_id = %s
                       AND firm_id = %s AND matter_id = %s
                    """,
                    (ids["batch_id"], self.firm_id, ids["matter_id"]),
                ).fetchone()
                self.assertIsNotNone(group)
                ids["group_id"] = str(group["exception_group_id"])
        ids.update({
            "candidate_hash": candidate_hash,
            "source_text_hash": source_text_hash,
            "task_input_hash": task_input_hash,
        })
        return ids

    def test_command_is_idempotent_and_audited_under_tenant_scope(self) -> None:
        matter_id = str(uuid4())
        first = self.workflow.create_matter(
            self.actor,
            matter_id=matter_id,
            title="Synthetic UUID Matter",
            idempotency_key="create-001",
        )
        repeated = self.workflow.create_matter(
            self.actor,
            matter_id=matter_id,
            title="Synthetic UUID Matter",
            idempotency_key="create-001",
        )
        self.assertEqual(first, repeated)
        found = self.workflow.get_matter(self.actor, matter_id=matter_id)
        self.assertEqual(found.stage, MatterStage.CREATED)
        accessible = self.store.list_accessible(actor=self.actor)
        self.assertEqual([matter_id], [item["matter_id"] for item in accessible])
        self.assertEqual("Synthetic UUID Matter", accessible[0]["title"])
        self.assertEqual(0, accessible[0]["material_count"])

        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)", (self.firm_id,)
            )
            connection.execute(
                """
                INSERT INTO evidence_original_files (
                    evidence_file_id, firm_id, matter_id, original_label,
                    original_file_sha256, byte_size, media_type, page_count,
                    source_scan_fingerprint
                ) VALUES (%s, %s, %s, %s, %s, %s, 'application/pdf', %s, %s)
                """,
                (
                    str(uuid4()), self.firm_id, matter_id,
                    "synthetic-browser-material.pdf", "1" * 64, 128, 1,
                    "2" * 64,
                ),
            )
        refreshed = self.store.list_accessible(actor=self.actor)
        self.assertEqual(1, refreshed[0]["material_count"])
        events = self.store.audit_events(matter_id, firm_id=self.firm_id)
        self.assertEqual(["MATTER_CREATED"], [event.event_type for event in events])

        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)", (self.firm_id,)
            )
            service_roles = connection.execute(
                """
                SELECT user_id, role
                FROM matter_actor_roles
                WHERE matter_id = %s AND user_id = ANY(%s)
                ORDER BY user_id
                """,
                (matter_id, [self.worker_id, self.verifier_id]),
            ).fetchall()
        self.assertEqual(
            {(self.worker_id, "SYSTEM_WORKER"), (self.verifier_id, "SYSTEM_WORKER")},
            {(str(row["user_id"]), row["role"]) for row in service_roles},
        )

        advanced = self.workflow.advance(
            self.actor,
            matter_id=matter_id,
            expected_version=1,
            idempotency_key="advance-001",
        )
        self.assertEqual(advanced.matter_version, 2)
        self.assertEqual(self.workflow.get_matter(self.actor, matter_id=matter_id).stage, MatterStage.INGESTING)

    def test_image_only_web_material_is_counted_once_after_reload(self) -> None:
        matter_id = str(uuid4())
        self.workflow.create_matter(
            self.actor,
            matter_id=matter_id,
            title="Synthetic image-only Web matter",
            idempotency_key="create-image-only-001",
        )
        upload_id = str(uuid4())
        material_object_id = str(uuid4())
        evidence_page_id = str(uuid4())
        attempt_id = str(uuid4())
        audit_event_id = str(uuid4())
        outbox_id = str(uuid4())
        now = datetime.now(timezone.utc)
        content_sha256 = "3" * 64
        inspection_hash = "4" * 64
        source_object_key = (
            f"original-images/v1/{self.firm_id}/{matter_id}/"
            f"{content_sha256[:2]}/{content_sha256}/{material_object_id}.jpg"
        )
        source_reference_hash = sha256(source_object_key.encode("utf-8")).hexdigest()
        agent_source_ref = f"evidence-page:{evidence_page_id}"

        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)", (self.firm_id,)
            )
            connection.execute(
                """
                INSERT INTO web_common_material_uploads (
                    upload_id, material_object_id, firm_id, matter_id, actor_id,
                    session_id, expected_matter_version, display_name,
                    declared_byte_size, declared_media_type,
                    reserve_idempotency_key, reserve_request_hash,
                    content_idempotency_key, status, attempt_id, attempt_count,
                    created_at, expires_at, claimed_at,
                    admitted_format, canonical_kind, admitted_media_type, route,
                    admitted_byte_size, admitted_content_sha256,
                    admitted_inspection_hash, scanner_name,
                    scanner_definitions_version, review_flags, review_status,
                    formal_fact, formal_transaction, legal_conclusion,
                    evidence_decision, court_ready, source_object_key,
                    source_reference_hash, object_stored_at, updated_at
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,1,'synthetic-receipt.jpg',128,'image/jpeg',
                    'reserve-image-only-001',%s,'content-image-only-001',
                    'OBJECT_STORED',%s,1,%s,%s,%s,
                    'JPEG','IMAGE','image/jpeg','VISUAL_OCR',128,%s,%s,
                    'synthetic-scanner','synthetic-definitions-v1',%s,
                    'NEEDS_LAWYER_REVIEW',false,false,false,false,false,%s,%s,%s,%s
                )
                """,
                (
                    upload_id, material_object_id, self.firm_id, matter_id,
                    self.actor_id, str(uuid4()), "5" * 64, attempt_id,
                    now, now + timedelta(minutes=15), now,
                    content_sha256, inspection_hash, Jsonb([]),
                    source_object_key, source_reference_hash, now, now,
                ),
            )
            connection.execute(
                """
                INSERT INTO case_material_objects (
                    material_object_id, firm_id, matter_id, source_upload_id,
                    original_display_name, admitted_format, canonical_kind,
                    media_type, content_sha256, byte_size, inspection_hash,
                    scanner_name, scanner_definitions_version, route,
                    review_flags, status, record_version, original_locked,
                    formal_fact, formal_transaction, legal_conclusion,
                    evidence_decision, court_ready, agent_status,
                    agent_source_ref, source_object_key, source_reference_hash,
                    created_matter_version, created_by, created_at
                ) VALUES (
                    %s,%s,%s,%s,'synthetic-receipt.jpg','JPEG','IMAGE',
                    'image/jpeg',%s,128,%s,'synthetic-scanner',
                    'synthetic-definitions-v1','VISUAL_OCR',%s,
                    'NEEDS_LAWYER_REVIEW',1,true,false,false,false,false,false,
                    'AGENT_READY',%s,%s,%s,2,%s,%s
                )
                """,
                (
                    material_object_id, self.firm_id, matter_id, upload_id,
                    content_sha256, inspection_hash, Jsonb([]), agent_source_ref,
                    source_object_key, source_reference_hash, self.actor_id, now,
                ),
            )
            connection.execute(
                """
                INSERT INTO evidence_original_files (
                    evidence_file_id, firm_id, matter_id, original_label,
                    original_file_sha256, byte_size, media_type, page_count,
                    source_scan_fingerprint, created_at
                ) VALUES (%s,%s,%s,'synthetic-receipt.jpg',%s,128,'image/jpeg',1,%s,%s)
                """,
                (
                    material_object_id, self.firm_id, matter_id,
                    content_sha256, inspection_hash, now,
                ),
            )
            connection.execute(
                """
                INSERT INTO evidence_pages (
                    evidence_page_id, firm_id, matter_id, evidence_file_id,
                    page_number, rendered_page_sha256, created_at
                ) VALUES (%s,%s,%s,%s,1,NULL,%s)
                """,
                (evidence_page_id, self.firm_id, matter_id, material_object_id, now),
            )
            connection.execute(
                """
                INSERT INTO web_evidence_native_image_source_objects (
                    evidence_file_id, firm_id, matter_id, source_object_key,
                    source_object_sha256, source_object_bytes, source_media_type,
                    source_reference_hash, admitted_by, created_at
                ) VALUES (%s,%s,%s,%s,%s,128,'image/jpeg',%s,%s,%s)
                """,
                (
                    material_object_id, self.firm_id, matter_id,
                    source_object_key, content_sha256, source_reference_hash,
                    self.actor_id, now,
                ),
            )
            connection.execute(
                "UPDATE matters SET version = 2, updated_at = %s WHERE matter_id = %s",
                (now, matter_id),
            )
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_id, firm_id, matter_id, actor_id, event_type,
                    input_version, output_version, request_id, payload, occurred_at
                ) VALUES (%s,%s,%s,%s,'COMMON_MATERIAL_ADMITTED',1,2,%s,%s,%s)
                """,
                (
                    audit_event_id, self.firm_id, matter_id, self.actor_id,
                    str(uuid4()), Jsonb({"format": "JPEG"}), now,
                ),
            )
            connection.execute(
                """
                INSERT INTO outbox_events (
                    outbox_id, firm_id, matter_id, aggregate_version,
                    event_type, payload, created_at
                ) VALUES (%s,%s,%s,2,'COMMON_MATERIAL_ADMITTED',%s,%s)
                """,
                (
                    outbox_id, self.firm_id, matter_id,
                    Jsonb({"audit_event_id": audit_event_id}), now,
                ),
            )
            connection.execute(
                """
                UPDATE web_common_material_uploads
                   SET status = 'COMPLETED', result_matter_version = 2,
                       agent_status = 'AGENT_READY', agent_source_ref = %s,
                       audit_event_id = %s, outbox_id = %s,
                       completed_at = %s, updated_at = %s
                 WHERE upload_id = %s
                """,
                (
                    agent_source_ref, audit_event_id, outbox_id,
                    now, now, upload_id,
                ),
            )

        reloaded = next(
            item
            for item in self.store.list_accessible(actor=self.actor)
            if item["matter_id"] == matter_id
        )
        self.assertEqual(1, reloaded["material_count"])

    def test_0049_preflight_rls_and_table_privileges_are_enforced_by_postgres(
        self,
    ) -> None:
        fixture = self._create_0049_followup(decision="DEFER_WITH_REASON")
        preflight_case_agent_ledger_exception_followup_schema(
            dsn=TEST_DSN,
            firm_id=self.firm_id,
        )

        immutable_owner_reads = (
            "web_sessions",
            "users",
            "matter_actor_roles",
            "case_agent_runs",
            "case_agent_ledger_exception_groups",
            "case_agent_ledger_exception_group_members",
            "case_agent_ledger_exception_group_decisions",
        )
        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            unexpected_updates = connection.execute(
                """
                SELECT object_name
                  FROM unnest(%s::text[]) AS required(object_name)
                 WHERE has_table_privilege(
                    'lawcase_ledger_confirmation_owner',
                    'public.' || object_name,
                    'UPDATE'
                 )
                """,
                (list(immutable_owner_reads),),
            ).fetchall()
        self.assertEqual(unexpected_updates, [])

        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(f"SET LOCAL ROLE {RLS_TEST_ROLE}")
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (self.firm_id,),
            )
            own = connection.execute(
                """
                SELECT count(*) AS count
                  FROM case_agent_ledger_exception_followup_heads
                 WHERE matter_id = %s
                """,
                (fixture["matter_id"],),
            ).fetchone()
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (str(uuid4()),),
            )
            other = connection.execute(
                """
                SELECT count(*) AS count
                  FROM case_agent_ledger_exception_followup_heads
                 WHERE matter_id = %s
                """,
                (fixture["matter_id"],),
            ).fetchone()
        self.assertEqual(own["count"], 1)
        self.assertEqual(other["count"], 0)

        for role_name in ("lawcase_web_application", "lawcase_agent_worker"):
            with self.subTest(role=role_name), psycopg.connect(
                TEST_DSN, row_factory=dict_row
            ) as connection:
                connection.execute(f"SET LOCAL ROLE {role_name}")
                connection.execute(
                    "SELECT set_config('app.firm_id', %s, true)",
                    (self.firm_id,),
                )
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    connection.execute(
                        """
                        UPDATE public.case_agent_ledger_exception_followup_heads
                           SET updated_at = clock_timestamp()
                         WHERE followup_id = %s
                        """,
                        (fixture["followup_id"],),
                    )

        # Startup attestation must remain callable through each real runtime
        # role even though information_schema no longer exposes hidden
        # recovery columns to the Web role.
        for role_name in ("lawcase_web_application", "lawcase_agent_worker"):
            with self.subTest(preflight_role=role_name):
                preflight_case_agent_ledger_exception_followup_schema(
                    dsn=make_conninfo(TEST_DSN, options=f"-c role={role_name}"),
                    firm_id=self.firm_id,
                )

        safe_runtime_queries = {
            "lawcase_web_application": (
                """
                SELECT control_assignment_id, firm_id, matter_id,
                       assignment_sequence, control_run_id, state_after
                  FROM case_agent_ledger_exception_control_assignments
                 LIMIT 1
                """,
            ),
            "lawcase_agent_worker": (
                """
                SELECT control_assignment_id, firm_id, matter_id,
                       assignment_sequence, control_run_id, state_after
                  FROM case_agent_ledger_exception_control_assignments
                 LIMIT 1
                """,
                """
                SELECT followup_event_id, followup_id, event_sequence,
                       firm_id, matter_id, subject_hash, state_after,
                       expected_matter_version, event_hash
                  FROM case_agent_ledger_exception_followup_events
                 LIMIT 1
                """,
                """
                SELECT recovery_intent_id, firm_id, matter_id,
                       replacement_run_id
                  FROM case_agent_ledger_exception_recovery_intents
                 LIMIT 1
                """,
                """
                SELECT recovery_intent_id, firm_id, matter_id,
                       current_outcome, transfer_control_assignment_id
                  FROM case_agent_ledger_exception_recovery_intent_heads
                 LIMIT 1
                """,
                """
                SELECT run_id, firm_id, matter_id
                  FROM case_agent_ledger_exception_recovery_quarantines
                 LIMIT 1
                """,
            ),
        }
        for role_name, queries in safe_runtime_queries.items():
            with self.subTest(safe_projection_role=role_name), psycopg.connect(
                TEST_DSN, row_factory=dict_row
            ) as connection:
                connection.execute(f"SET LOCAL ROLE {role_name}")
                connection.execute(
                    "SELECT set_config('app.firm_id', %s, true)",
                    (self.firm_id,),
                )
                for query in queries:
                    connection.execute(query).fetchall()

        sensitive_columns = (
            (
                "case_agent_ledger_exception_control_assignments",
                "web_session_id",
            ),
            ("case_agent_ledger_exception_followup_events", "web_session_id"),
            (
                "case_agent_ledger_exception_recovery_intents",
                "prepared_web_session_id",
            ),
            (
                "case_agent_ledger_exception_recovery_intent_heads",
                "outcome_web_session_id",
            ),
        )
        for role_name in ("lawcase_web_application", "lawcase_agent_worker"):
            for table_name, column_name in sensitive_columns:
                with self.subTest(
                    denied_role=role_name,
                    denied_column=f"{table_name}.{column_name}",
                ), psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
                    connection.execute(f"SET LOCAL ROLE {role_name}")
                    connection.execute(
                        "SELECT set_config('app.firm_id', %s, true)",
                        (self.firm_id,),
                    )
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        connection.execute(
                            f"SELECT {column_name} FROM {table_name} LIMIT 1"
                        ).fetchall()

        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            owner_session_reads = connection.execute(
                """
                SELECT table_name, column_name,
                       has_column_privilege(
                           'lawcase_ledger_confirmation_owner',
                           'public.' || table_name,
                           column_name,
                           'SELECT'
                       ) AS owner_can_select
                  FROM unnest(%s::text[], %s::text[])
                       AS sensitive(table_name, column_name)
                """,
                (
                    [table for table, _ in sensitive_columns],
                    [column for _, column in sensitive_columns],
                ),
            ).fetchall()
        self.assertTrue(owner_session_reads)
        self.assertTrue(all(row["owner_can_select"] for row in owner_session_reads))

    def test_0048_web_exception_decision_executes_integrity_and_0049_lifecycle(
        self,
    ) -> None:
        fixture = self._create_0048_low_risk_batch(exception_lane=True)
        fixture.update({
            "decision": "DEFER_WITH_REASON",
            "reason_code": "NEEDS_LEAD_REVIEW",
            "reason_note": "等待承办律师复核",
        })
        request_hash = exception_decision_request_hash(
            matter_id=fixture["matter_id"],
            expected_version=1,
            exception_group_id=fixture["group_id"],
            decision=LedgerExceptionDecision.DEFER_WITH_REASON,
            reason=LedgerExceptionReason.NEEDS_LEAD_REVIEW,
            reason_note=fixture["reason_note"],
        )
        with psycopg.connect(
            WEB_APPLICATION_TEST_DSN,
            row_factory=dict_row,
        ) as connection:
            receipt = connection.execute(
                """
                SELECT public.decide_case_agent_ledger_exception_group_from_web_session(
                    %s,%s,%s,1,%s,%s,%s,%s,%s
                ) AS receipt
                """,
                (
                    fixture["session_id"],
                    fixture["matter_id"],
                    fixture["group_id"],
                    "web-exception-decision-real-pg-0001",
                    fixture["decision"],
                    fixture["reason_code"],
                    fixture["reason_note"],
                    request_hash,
                ),
            ).fetchone()["receipt"]
        self.assertEqual(receipt["command_name"], "DECIDE_CASE_LEDGER_EXCEPTION_GROUP")
        self.assertEqual(receipt["idempotency_key"], "web-exception-decision-real-pg-0001")

        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (self.firm_id,),
            )
            lifecycle = connection.execute(
                """
                SELECT decision.exception_decision_id,
                       followup.followup_id, head.current_state
                  FROM case_agent_ledger_exception_group_decisions decision
                  JOIN case_agent_ledger_exception_followups followup
                    ON followup.origin_exception_decision_id =
                        decision.exception_decision_id
                  JOIN case_agent_ledger_exception_followup_heads head
                    ON head.followup_id = followup.followup_id
                   AND head.firm_id = followup.firm_id
                   AND head.matter_id = followup.matter_id
                 WHERE decision.exception_group_id = %s
                   AND decision.firm_id = %s
                   AND decision.matter_id = %s
                """,
                (fixture["group_id"], self.firm_id, fixture["matter_id"]),
            ).fetchone()
        self.assertIsNotNone(lifecycle)
        self.assertEqual(lifecycle["current_state"], "ACTIVE")

    def test_0048_low_risk_authorize_finalize_commits_0042_deferred_integrity(
        self,
    ) -> None:
        fixture = self._create_0048_low_risk_batch()
        idempotency_key = "web-low-risk-real-pg-0001"
        request_hash = _payload_hash({
            "matter_id": fixture["matter_id"],
            "expected_version": 1,
            "extraction_batch_id": fixture["batch_id"],
        })
        with psycopg.connect(
            WEB_APPLICATION_TEST_DSN,
            row_factory=dict_row,
        ) as connection:
            authorization = connection.execute(
                """
                SELECT public.authorize_case_agent_ledger_extraction_low_risk_confirmation(
                    %s,%s,%s,1,%s,%s
                ) AS authorization
                """,
                (
                    fixture["session_id"], fixture["matter_id"],
                    fixture["batch_id"], idempotency_key, request_hash,
                ),
            ).fetchone()["authorization"]
        self.assertEqual(authorization["status"], "AUTHORIZED")

        source_binding = (
            f"{fixture['candidate_hash']}:{fixture['evidence_page_id']}:"
            f"{fixture['source_text_hash']}"
        )
        source_verification_hash = sha256(
            (
                "case-agent-ledger-source-verification-v1|"
                f"{fixture['batch_id']}|{fixture['run_id']}|"
                f"{fixture['task_id']}|{fixture['task_input_hash']}|"
                f"{fixture['candidate_hash']}|{source_binding}"
            ).encode("utf-8")
        ).hexdigest()
        with psycopg.connect(
            WEB_APPLICATION_TEST_DSN,
            row_factory=dict_row,
        ) as connection:
            receipt = connection.execute(
                """
                SELECT public.finalize_case_agent_ledger_extraction_low_risk_confirmation(
                    %s,%s
                ) AS receipt
                """,
                (authorization["approval_id"], source_verification_hash),
            ).fetchone()["receipt"]
            # Force both 0042 DEFERRABLE constraint triggers before leaving
            # the transaction so the test cannot pass on an uncommitted row.
            connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
        self.assertEqual(
            receipt["command_name"],
            "CONFIRM_LOW_RISK_CASE_LEDGER_EXTRACTION_BATCH",
        )
        self.assertEqual(receipt["matter_version"], 2)

        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (self.firm_id,),
            )
            committed = connection.execute(
                """
                SELECT matter.version, fact.status,
                       promotion.extraction_candidate_id,
                       confirmation.confirmed_candidate_count
                  FROM matters matter
                  JOIN case_agent_ledger_extraction_batch_confirmations confirmation
                    ON confirmation.matter_id = matter.matter_id
                   AND confirmation.firm_id = matter.firm_id
                  JOIN case_agent_ledger_extraction_promotions promotion
                    ON promotion.extraction_batch_id =
                        confirmation.extraction_batch_id
                   AND promotion.firm_id = confirmation.firm_id
                   AND promotion.matter_id = confirmation.matter_id
                  JOIN case_facts fact
                    ON fact.fact_id = promotion.target_object_id
                   AND fact.firm_id = promotion.firm_id
                   AND fact.matter_id = promotion.matter_id
                 WHERE matter.matter_id = %s
                   AND matter.firm_id = %s
                   AND confirmation.extraction_batch_id = %s
                """,
                (fixture["matter_id"], self.firm_id, fixture["batch_id"]),
            ).fetchone()
        self.assertIsNotNone(committed)
        self.assertEqual(committed["version"], 2)
        self.assertEqual(committed["status"], "CONFIRMED")
        self.assertEqual(committed["confirmed_candidate_count"], 1)

    def test_0049_lead_session_resolve_replays_after_commit_was_lost(
        self,
    ) -> None:
        fixture = self._create_0049_followup(decision="DEFER_WITH_REASON")
        command = lambda: self.exception_followup_store.resolve_followup(
            matter_id=fixture["matter_id"],
            actor=self.actor,
            server_session_id=fixture["session_id"],
            expected_version=1,
            idempotency_key="deferred-resume-0001",
            followup_id=fixture["followup_id"],
            action=LedgerExceptionFollowupAction.RESUME,
            reason_note="恢复办理",
        )
        first = command()
        replay = command()
        self.assertEqual(first, replay)
        self.assertEqual(first.matter_version, 2)

        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (self.firm_id,),
            )
            state = connection.execute(
                """
                SELECT current_state, head_sequence
                  FROM case_agent_ledger_exception_followup_heads
                 WHERE followup_id = %s
                """,
                (fixture["followup_id"],),
            ).fetchone()
            event_count = connection.execute(
                """
                SELECT count(*) AS count
                  FROM case_agent_ledger_exception_followup_events
                 WHERE followup_id = %s
                """,
                (fixture["followup_id"],),
            ).fetchone()["count"]
            idempotency_count = connection.execute(
                """
                SELECT count(*) AS count
                  FROM command_idempotency
                 WHERE firm_id = %s AND matter_id = %s
                   AND actor_id = %s
                   AND command_name =
                        'RESOLVE_CASE_LEDGER_EXCEPTION_FOLLOWUP'
                   AND idempotency_key = 'deferred-resume-0001'
                """,
                (self.firm_id, fixture["matter_id"], self.actor_id),
            ).fetchone()["count"]
        self.assertEqual(dict(state), {"current_state": "RESUMED", "head_sequence": 2})
        self.assertEqual(event_count, 2)
        self.assertEqual(idempotency_count, 1)

    def test_0049_more_evidence_accepts_only_the_exact_new_managed_source(
        self,
    ) -> None:
        fixture = self._create_0049_followup(decision="REQUEST_MORE_EVIDENCE")
        new_source_id = str(uuid4())
        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (self.firm_id,),
            )
            connection.execute(
                """
                INSERT INTO evidence_original_files (
                    evidence_file_id, firm_id, matter_id, original_label,
                    original_file_sha256, byte_size, media_type, page_count,
                    source_scan_fingerprint
                ) VALUES (%s,%s,%s,%s,%s,256,'application/pdf',1,%s)
                """,
                (
                    new_source_id,
                    self.firm_id,
                    fixture["matter_id"],
                    "[合成] 新增银行流水.pdf",
                    "f" * 64,
                    "1" * 64,
                ),
            )

        eligible = self.exception_followup_store.list_eligible_managed_evidence_sources(
            matter_id=fixture["matter_id"],
            actor=self.actor,
            followup_id=fixture["followup_id"],
            offset=0,
            limit=50,
        )
        self.assertEqual(
            [
                (item.object_type.value, item.object_id)
                for item in eligible.sources
            ],
            [("EVIDENCE_FILE", new_source_id)],
        )
        self.assertNotIn(
            fixture["old_evidence_file_id"],
            {item.object_id for item in eligible.sources},
        )

        receipt = self.exception_followup_store.resolve_followup(
            matter_id=fixture["matter_id"],
            actor=self.actor,
            server_session_id=fixture["session_id"],
            expected_version=1,
            idempotency_key="more-evidence-confirm-0001",
            followup_id=fixture["followup_id"],
            action=LedgerExceptionFollowupAction.CONFIRM_MORE_EVIDENCE,
            reason_note="已核验新增银行流水",
            managed_evidence_sources=(
                ManagedEvidenceSourceRef(
                    object_type=ManagedEvidenceSourceType.EVIDENCE_FILE,
                    object_id=new_source_id,
                ),
            ),
        )
        self.assertEqual(receipt.matter_version, 2)
        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (self.firm_id,),
            )
            row = connection.execute(
                """
                SELECT head.current_state, binding.source_type,
                       binding.source_object_id, binding.source_content_hash
                  FROM case_agent_ledger_exception_followup_heads head
                  JOIN case_agent_ledger_exception_managed_evidence_requests request
                    ON request.followup_id = head.followup_id
                  JOIN case_agent_ledger_exception_evidence_source_bindings binding
                    ON binding.evidence_request_id = request.evidence_request_id
                 WHERE head.followup_id = %s
                """,
                (fixture["followup_id"],),
            ).fetchone()
        self.assertEqual(row["current_state"], "SATISFIED")
        self.assertEqual(row["source_type"], "EVIDENCE_FILE")
        self.assertEqual(str(row["source_object_id"]), new_source_id)
        self.assertEqual(row["source_content_hash"], "f" * 64)

    def test_0049_failed_control_transfers_to_replacement_and_blocks_current_cancel(
        self,
    ) -> None:
        fixture = self._create_0049_followup(decision="REQUEST_REEXTRACTION")
        worker_inbox = PostgresCaseAgentRunInbox(
            dsn=TEST_DSN,
            actor=Actor(
                self.worker_id,
                self.firm_id,
                frozenset({Role.SYSTEM_WORKER}),
            ),
        )
        old_claim = worker_inbox.claim_next_run(
            lease_owner=case_agent_worker_id(self.firm_id),
            lease_seconds=120,
        )
        self.assertIsNotNone(old_claim)
        assert old_claim is not None
        self.assertEqual(old_claim.run_id, fixture["run_id"])
        failed_event_id = str(uuid4())
        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (self.firm_id,),
            )
            connection.execute(
                """
                INSERT INTO case_agent_events (
                    event_id, run_id, firm_id, matter_id, event_sequence,
                    event_type, actor_id, payload, event_hash, occurred_at
                ) VALUES (
                    %s,%s,%s,%s,2,'VERIFICATION_FAILED',%s,%s,%s,
                    clock_timestamp()
                )
                """,
                (
                    failed_event_id,
                    fixture["run_id"],
                    self.firm_id,
                    fixture["matter_id"],
                    self.worker_id,
                    Jsonb({"error_code": "SYNTHETIC_VERIFICATION_FAILED"}),
                    "2" * 64,
                ),
            )
            recovery = connection.execute(
                """
                SELECT head.current_state, head.head_sequence,
                       assignment.source_agent_event_id
                  FROM case_agent_ledger_exception_control_heads head
                  JOIN case_agent_ledger_exception_control_assignments assignment
                    ON assignment.control_assignment_id =
                        head.current_control_assignment_id
                 WHERE head.firm_id = %s AND head.matter_id = %s
                """,
                (self.firm_id, fixture["matter_id"]),
            ).fetchone()
        self.assertEqual(recovery["current_state"], "RECOVERY_REQUIRED")
        self.assertEqual(recovery["head_sequence"], 2)
        self.assertEqual(str(recovery["source_agent_event_id"]), failed_event_id)

        replacement_run_id = str(uuid4())
        recovery_goal = build_web_case_agent_recovery_goal(
            actor=self.actor, replacement_run_id=replacement_run_id
        )
        replacement_goal_id = recovery_goal.goal_id
        replacement_event_id = str(uuid4())
        prepared = self.exception_followup_store.prepare_control_recovery(
            matter_id=fixture["matter_id"],
            actor=self.actor,
            server_session_id=fixture["session_id"],
            replacement_run_id=replacement_run_id,
            expected_version=1,
            idempotency_key="control-recovery-0001",
        )
        self.assertEqual(prepared.recovery_state, "PENDING")
        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (self.firm_id,),
            )
            binding = connection.execute(
                """
                SELECT recovery_goal_id, recovery_goal_hash
                  FROM case_agent_ledger_exception_recovery_intents
                 WHERE recovery_intent_id = %s
                """,
                (prepared.recovery_intent_id,),
            ).fetchone()
            self.assertEqual(str(binding["recovery_goal_id"]), replacement_run_id)
            self.assertEqual(
                str(binding["recovery_goal_hash"]), recovery_goal.goal_hash
            )
            # Reproduce the audited attack: a generic create-run transaction
            # tries to occupy the prepared run UUID with a different goal.
            # 0052 must reject the run and roll the whole collision back.
            with self.assertRaisesRegex(
                psycopg.DatabaseError,
                "recovery run goal differs from its immutable binding",
            ), connection.transaction():
                malicious_goal_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO case_agent_goals (
                        goal_id, firm_id, matter_id, objective,
                        success_criteria, constraints, requested_by, goal_hash
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        malicious_goal_id,
                        self.firm_id,
                        fixture["matter_id"],
                        "Attacker-selected ordinary goal",
                        Jsonb(["execute unrelated work"]),
                        Jsonb([]),
                        self.actor_id,
                        "3" * 64,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO case_agent_runs (
                        run_id, firm_id, matter_id, goal_id, status,
                        current_event_version, snapshot_matter_version,
                        snapshot_schema_version, snapshot_hash, run_budget,
                        projection_hash, created_by
                    ) VALUES (
                        %s,%s,%s,%s,'CREATED',1,1,
                        'malicious-generic-run-v1',%s,%s,%s,%s
                    )
                    """,
                    (
                        replacement_run_id,
                        self.firm_id,
                        fixture["matter_id"],
                        malicious_goal_id,
                        "4" * 64,
                        Jsonb({"max_tasks": 10}),
                        "5" * 64,
                        self.actor_id,
                    ),
                )
            collision = connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM case_agent_runs WHERE run_id = %s
                ) AS run_exists
                """,
                (replacement_run_id,),
            ).fetchone()
            self.assertFalse(collision["run_exists"])

            connection.execute(
                """
                INSERT INTO case_agent_goals (
                    goal_id, firm_id, matter_id, objective,
                    success_criteria, constraints, requested_by, goal_hash
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    replacement_goal_id,
                    self.firm_id,
                    fixture["matter_id"],
                    recovery_goal.objective,
                    Jsonb(list(recovery_goal.success_criteria)),
                    Jsonb(list(recovery_goal.constraints)),
                    self.actor_id,
                    recovery_goal.goal_hash,
                ),
            )
            connection.execute(
                """
                INSERT INTO case_agent_runs (
                    run_id, firm_id, matter_id, goal_id, status,
                    current_event_version, snapshot_matter_version,
                    snapshot_schema_version, snapshot_hash, run_budget,
                    projection_hash, created_by
                ) VALUES (
                    %s,%s,%s,%s,'CREATED',1,1,
                    'synthetic-0049-recovery-v1',%s,%s,%s,%s
                )
                """,
                (
                    replacement_run_id,
                    self.firm_id,
                    fixture["matter_id"],
                    replacement_goal_id,
                    "4" * 64,
                    Jsonb({"max_tasks": 10}),
                    "5" * 64,
                    self.actor_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO case_agent_events (
                    event_id, run_id, firm_id, matter_id, event_sequence,
                    event_type, actor_id, payload, event_hash, occurred_at
                ) VALUES (
                    %s,%s,%s,%s,1,'RUN_CREATED',%s,%s,%s,
                    clock_timestamp()
                )
                """,
                (
                    replacement_event_id,
                    replacement_run_id,
                    self.firm_id,
                    fixture["matter_id"],
                    self.actor_id,
                    Jsonb({"objective": "recover active follow-ups"}),
                    "6" * 64,
                ),
            )

        # RUN_CREATED is deliberately a separate transaction.  Its wake
        # projection must remain QUIET while the immutable recovery intent is
        # PENDING, even if every ACTIVE follow-up were later closed.
        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (self.firm_id,),
            )
            pending_inbox = connection.execute(
                """
                SELECT inbox_status, lease_token
                  FROM case_agent_run_inbox
                 WHERE run_id = %s AND firm_id = %s AND matter_id = %s
                """,
                (replacement_run_id, self.firm_id, fixture["matter_id"]),
            ).fetchone()
        self.assertEqual(pending_inbox["inbox_status"], "QUIET")
        self.assertIsNone(pending_inbox["lease_token"])
        pending_claim = PostgresCaseAgentRunInbox(
            dsn=TEST_DSN,
            actor=Actor(
                self.worker_id,
                self.firm_id,
                frozenset({Role.SYSTEM_WORKER}),
            ),
        ).claim_next_run(
            lease_owner=case_agent_worker_id(self.firm_id),
            lease_seconds=120,
        )
        self.assertIsNone(pending_claim)

        # A browser reload loses its in-memory operation key.  A fresh key
        # must discover the already-created server-side intent/run and resume
        # it, without creating or exposing a second run identity.
        refreshed_request_run_id = str(uuid4())
        refreshed = self.exception_followup_store.prepare_control_recovery(
            matter_id=fixture["matter_id"],
            actor=self.actor,
            server_session_id=fixture["session_id"],
            replacement_run_id=refreshed_request_run_id,
            expected_version=1,
            idempotency_key="control-recovery-after-reload-0001",
        )
        self.assertEqual(refreshed.recovery_intent_id, prepared.recovery_intent_id)
        self.assertEqual(refreshed.replacement_run_id, replacement_run_id)
        self.assertEqual(
            refreshed.transfer_idempotency_key, "control-recovery-0001"
        )
        self.assertTrue(refreshed.run_exists)

        # Optimistic matter-version drift makes the old intent stale.  A new
        # current-version request terminates it, parks its run, and opens a
        # new PENDING intent for the same failed control source.
        advanced = self.workflow.advance(
            self.actor,
            matter_id=fixture["matter_id"],
            expected_version=1,
            idempotency_key="advance-during-control-recovery-0001",
        )
        self.assertEqual(advanced.matter_version, 2)
        stale_replacement_run_id = replacement_run_id
        replacement_run_id = str(uuid4())
        recovery_goal = build_web_case_agent_recovery_goal(
            actor=self.actor, replacement_run_id=replacement_run_id
        )
        replacement_goal_id = recovery_goal.goal_id
        replacement_event_id = str(uuid4())
        prepared = self.exception_followup_store.prepare_control_recovery(
            matter_id=fixture["matter_id"],
            actor=self.actor,
            server_session_id=fixture["session_id"],
            replacement_run_id=replacement_run_id,
            expected_version=2,
            idempotency_key="control-recovery-version-2-0001",
        )
        self.assertEqual(prepared.recovery_state, "PENDING")
        self.assertEqual(prepared.replacement_run_id, replacement_run_id)
        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (self.firm_id,),
            )
            stale = connection.execute(
                """
                SELECT head.current_outcome, head.outcome_reason_code,
                       inbox.inbox_status
                  FROM case_agent_ledger_exception_recovery_intents intent
                  JOIN case_agent_ledger_exception_recovery_intent_heads head
                    ON head.recovery_intent_id = intent.recovery_intent_id
                  JOIN case_agent_run_inbox inbox
                    ON inbox.run_id = intent.replacement_run_id
                   AND inbox.firm_id = intent.firm_id
                   AND inbox.matter_id = intent.matter_id
                 WHERE intent.replacement_run_id = %s
                """,
                (stale_replacement_run_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO case_agent_goals (
                    goal_id, firm_id, matter_id, objective,
                    success_criteria, constraints, requested_by, goal_hash
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    replacement_goal_id,
                    self.firm_id,
                    fixture["matter_id"],
                    recovery_goal.objective,
                    Jsonb(list(recovery_goal.success_criteria)),
                    Jsonb(list(recovery_goal.constraints)),
                    self.actor_id,
                    recovery_goal.goal_hash,
                ),
            )
            connection.execute(
                """
                INSERT INTO case_agent_runs (
                    run_id, firm_id, matter_id, goal_id, status,
                    current_event_version, snapshot_matter_version,
                    snapshot_schema_version, snapshot_hash, run_budget,
                    projection_hash, created_by
                ) VALUES (
                    %s,%s,%s,%s,'CREATED',1,2,
                    'synthetic-0050-recovery-v2',%s,%s,%s,%s
                )
                """,
                (
                    replacement_run_id,
                    self.firm_id,
                    fixture["matter_id"],
                    replacement_goal_id,
                    "a" * 64,
                    Jsonb({"max_tasks": 10}),
                    "b" * 64,
                    self.actor_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO case_agent_events (
                    event_id, run_id, firm_id, matter_id, event_sequence,
                    event_type, actor_id, payload, event_hash, occurred_at
                ) VALUES (
                    %s,%s,%s,%s,1,'RUN_CREATED',%s,%s,%s,
                    clock_timestamp()
                )
                """,
                (
                    replacement_event_id,
                    replacement_run_id,
                    self.firm_id,
                    fixture["matter_id"],
                    self.actor_id,
                    Jsonb({"objective": "recover active follow-ups at v2"}),
                    "c" * 64,
                ),
            )
        self.assertEqual(stale["current_outcome"], "ABANDONED")
        self.assertEqual(stale["outcome_reason_code"], "MATTER_VERSION_CHANGED")
        self.assertEqual(stale["inbox_status"], "QUIET")
        self.assertIsNone(
            worker_inbox.claim_next_run(
                lease_owner=case_agent_worker_id(self.firm_id),
                lease_seconds=120,
            )
        )

        transfer = lambda: self.exception_followup_store.transfer_control_to_recovery_run(
            matter_id=fixture["matter_id"],
            actor=self.actor,
            server_session_id=fixture["session_id"],
            replacement_run_id=replacement_run_id,
            expected_version=2,
            idempotency_key="control-recovery-version-2-0001",
        )
        # A claim that committed before the failure/transfer keeps the old
        # cursor authoritative until it settles.  Transfer does not revoke an
        # in-flight lease and pretend the already-running step was stopped.
        with self.assertRaises(IdempotencyConflict):
            transfer()
        self.assertTrue(worker_inbox.settle_run(old_claim, quiet=True))
        first = transfer()
        replay = transfer()
        self.assertEqual(first, replay)
        self.assertIs(first.control_health, LedgerExceptionControlHealth.HEALTHY)
        current = self.exception_followup_store.read_current_control_state(
            matter_id=fixture["matter_id"], actor=self.actor
        )
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.control_run_id, replacement_run_id)
        self.assertEqual(current.head_sequence, 3)

        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (self.firm_id,),
            )
            inbox_rows = connection.execute(
                """
                SELECT run_id, inbox_status, lease_token
                  FROM case_agent_run_inbox
                 WHERE firm_id = %s AND matter_id = %s
                   AND run_id IN (%s, %s)
                 ORDER BY run_id
                """,
                (
                    self.firm_id,
                    fixture["matter_id"],
                    fixture["run_id"],
                    replacement_run_id,
                ),
            ).fetchall()
            outcome = connection.execute(
                """
                SELECT head.current_outcome,
                       head.transfer_control_assignment_id
                  FROM case_agent_ledger_exception_recovery_intents intent
                  JOIN case_agent_ledger_exception_recovery_intent_heads head
                    ON head.recovery_intent_id = intent.recovery_intent_id
                 WHERE intent.replacement_run_id = %s
                   AND intent.firm_id = %s AND intent.matter_id = %s
                """,
                (replacement_run_id, self.firm_id, fixture["matter_id"]),
            ).fetchone()
        status_by_run = {
            str(row["run_id"]): (row["inbox_status"], row["lease_token"])
            for row in inbox_rows
        }
        self.assertEqual(status_by_run[fixture["run_id"]], ("QUIET", None))
        self.assertEqual(status_by_run[replacement_run_id], ("READY", None))
        self.assertEqual(outcome["current_outcome"], "TRANSFERRED")
        self.assertEqual(
            str(outcome["transfer_control_assignment_id"]),
            first.control_assignment_id,
        )

        # A late append that was waiting on the old run row may commit after
        # transfer.  The replaced wake trigger must not resurrect the
        # superseded cursor's inbox.
        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (self.firm_id,),
            )
            connection.execute(
                """
                UPDATE case_agent_runs
                   SET status = 'FAILED', current_event_version = 2,
                       failure_code = 'SYNTHETIC_VERIFICATION_FAILED',
                       updated_at = clock_timestamp()
                 WHERE run_id = %s AND firm_id = %s AND matter_id = %s
                """,
                (fixture["run_id"], self.firm_id, fixture["matter_id"]),
            )
            late_wake = connection.execute(
                """
                SELECT inbox_status, lease_token
                  FROM case_agent_run_inbox
                 WHERE run_id = %s AND firm_id = %s AND matter_id = %s
                """,
                (fixture["run_id"], self.firm_id, fixture["matter_id"]),
            ).fetchone()
        self.assertEqual(late_wake["inbox_status"], "QUIET")
        self.assertIsNone(late_wake["lease_token"])

        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (self.firm_id,),
            )
            with self.assertRaisesRegex(
                psycopg.DatabaseError,
                "cancellation is blocked by an active exception follow-up",
            ):
                connection.execute(
                    """
                    INSERT INTO case_agent_events (
                        event_id, run_id, firm_id, matter_id, event_sequence,
                        event_type, actor_id, payload, event_hash, occurred_at
                    ) VALUES (
                        %s,%s,%s,%s,2,'RUN_CANCELLED',%s,%s,%s,
                        clock_timestamp()
                    )
                    """,
                    (
                        str(uuid4()),
                        replacement_run_id,
                        self.firm_id,
                        fixture["matter_id"],
                        self.actor_id,
                        Jsonb({"reason": "synthetic blocked cancellation"}),
                        "7" * 64,
                    ),
                )

        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (self.firm_id,),
            )
            connection.execute(
                """
                INSERT INTO case_agent_events (
                    event_id, run_id, firm_id, matter_id, event_sequence,
                    event_type, actor_id, payload, event_hash, occurred_at
                ) VALUES (
                    %s,%s,%s,%s,3,'RUN_CANCELLED',%s,%s,%s,
                    clock_timestamp()
                )
                """,
                (
                    str(uuid4()),
                    fixture["run_id"],
                    self.firm_id,
                    fixture["matter_id"],
                    self.actor_id,
                    Jsonb({"reason": "synthetic prior-run cancellation"}),
                    "8" * 64,
                ),
            )

        # Close the final ACTIVE projection and advance the matter.  A lost
        # HTTP response can still replay the exact prepared intent and final
        # transfer receipt without recreating or rediscovering a run id.
        closed = self.exception_followup_store.resolve_followup(
            matter_id=fixture["matter_id"],
            actor=self.actor,
            server_session_id=fixture["session_id"],
            expected_version=2,
            idempotency_key="control-recovery-close-0001",
            followup_id=fixture["followup_id"],
            action=LedgerExceptionFollowupAction.WITHDRAW,
            reason_note="恢复办理",
        )
        self.assertEqual(closed.matter_version, 3)
        prepared_replay = self.exception_followup_store.prepare_control_recovery(
            matter_id=fixture["matter_id"],
            actor=self.actor,
            server_session_id=fixture["session_id"],
            replacement_run_id=replacement_run_id,
            expected_version=2,
            idempotency_key="control-recovery-version-2-0001",
        )
        self.assertEqual(prepared_replay.recovery_state, "TRANSFERRED")
        self.assertEqual(transfer(), first)

    def test_0052_recovery_goal_binding_serializes_both_commit_orders(
        self,
    ) -> None:
        def mark_recovery_required(fixture: dict[str, str], seed: str) -> None:
            with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
                connection.execute(
                    "SELECT set_config('app.firm_id', %s, true)",
                    (self.firm_id,),
                )
                connection.execute(
                    """
                    INSERT INTO case_agent_events (
                        event_id, run_id, firm_id, matter_id, event_sequence,
                        event_type, actor_id, payload, event_hash, occurred_at
                    ) VALUES (
                        %s,%s,%s,%s,2,'VERIFICATION_FAILED',%s,%s,%s,
                        clock_timestamp()
                    )
                    """,
                    (
                        str(uuid4()),
                        fixture["run_id"],
                        self.firm_id,
                        fixture["matter_id"],
                        self.worker_id,
                        Jsonb({"error_code": "SYNTHETIC_GOAL_BINDING_RACE"}),
                        seed * 64,
                    ),
                )

        def insert_ordinary_run(
            connection: psycopg.Connection,
            *,
            fixture: dict[str, str],
            run_id: str,
            goal_id: str,
            seed: str,
        ) -> None:
            connection.execute(
                """
                INSERT INTO case_agent_goals (
                    goal_id, firm_id, matter_id, objective,
                    success_criteria, constraints, requested_by, goal_hash
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    goal_id,
                    self.firm_id,
                    fixture["matter_id"],
                    "Attacker-selected ordinary goal",
                    Jsonb(["execute unrelated work"]),
                    Jsonb([]),
                    self.actor_id,
                    seed * 64,
                ),
            )
            connection.execute(
                """
                INSERT INTO case_agent_runs (
                    run_id, firm_id, matter_id, goal_id, status,
                    current_event_version, snapshot_matter_version,
                    snapshot_schema_version, snapshot_hash, run_budget,
                    projection_hash, created_by
                ) VALUES (
                    %s,%s,%s,%s,'CREATED',1,1,
                    'malicious-generic-race-v1',%s,%s,%s,%s
                )
                """,
                (
                    run_id,
                    self.firm_id,
                    fixture["matter_id"],
                    goal_id,
                    seed * 64,
                    Jsonb({"max_tasks": 10}),
                    seed * 64,
                    self.actor_id,
                ),
            )

        # Generic run transaction wins the lock first.  Prepare must wait,
        # then observe the committed collision instead of creating an intent
        # from an older MVCC snapshot.
        generic_first = self._create_0049_followup(
            decision="REQUEST_REEXTRACTION"
        )
        mark_recovery_required(generic_first, "7")
        generic_first_run_id = str(uuid4())
        generic_first_goal_id = str(uuid4())
        prepare_hash = control_transfer_request_hash(
            matter_id=generic_first["matter_id"],
            expected_version=1,
            replacement_run_id=generic_first_run_id,
        )
        with psycopg.connect(
            TEST_DSN, row_factory=dict_row
        ) as generic_connection, psycopg.connect(
            WEB_APPLICATION_TEST_DSN,
            autocommit=True,
            row_factory=dict_row,
        ) as prepare_connection:
            generic_connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (self.firm_id,),
            )
            insert_ordinary_run(
                generic_connection,
                fixture=generic_first,
                run_id=generic_first_run_id,
                goal_id=generic_first_goal_id,
                seed="8",
            )
            prepare_connection.execute("SET lock_timeout = '250ms'")
            with self.assertRaises(psycopg.errors.LockNotAvailable):
                prepare_connection.execute(
                    """
                    SELECT public.prepare_case_agent_ledger_exception_control_recovery_from_web_session(
                        %s,%s,%s,1,%s,%s
                    )
                    """,
                    (
                        generic_first["session_id"],
                        generic_first["matter_id"],
                        generic_first_run_id,
                        "goal-binding-generic-first-0001",
                        prepare_hash,
                    ),
                )
            generic_connection.commit()
        with self.assertRaises(IdempotencyConflict):
            self.exception_followup_store.prepare_control_recovery(
                matter_id=generic_first["matter_id"],
                actor=self.actor,
                server_session_id=generic_first["session_id"],
                replacement_run_id=generic_first_run_id,
                expected_version=1,
                idempotency_key="goal-binding-generic-first-0001",
            )

        # Prepare wins the lock first.  A generic run insert with the same UUID
        # must wait; after the intent commits, its retry is rejected by the
        # immutable goal binding guard.
        prepare_first = self._create_0049_followup(
            decision="REQUEST_REEXTRACTION"
        )
        mark_recovery_required(prepare_first, "9")
        prepare_first_run_id = str(uuid4())
        prepare_first_goal_id = str(uuid4())
        prepare_first_hash = control_transfer_request_hash(
            matter_id=prepare_first["matter_id"],
            expected_version=1,
            replacement_run_id=prepare_first_run_id,
        )
        with psycopg.connect(
            WEB_APPLICATION_TEST_DSN, row_factory=dict_row
        ) as prepare_connection, psycopg.connect(
            TEST_DSN,
            autocommit=True,
            row_factory=dict_row,
        ) as generic_connection:
            prepared_row = prepare_connection.execute(
                """
                SELECT public.prepare_case_agent_ledger_exception_control_recovery_from_web_session(
                    %s,%s,%s,1,%s,%s
                ) AS receipt
                """,
                (
                    prepare_first["session_id"],
                    prepare_first["matter_id"],
                    prepare_first_run_id,
                    "goal-binding-prepare-first-0001",
                    prepare_first_hash,
                ),
            ).fetchone()
            self.assertEqual(prepared_row["receipt"]["recovery_state"], "PENDING")
            generic_connection.execute("SET lock_timeout = '250ms'")
            with self.assertRaises(psycopg.errors.LockNotAvailable):
                with generic_connection.transaction():
                    generic_connection.execute(
                        "SELECT set_config('app.firm_id', %s, true)",
                        (self.firm_id,),
                    )
                    insert_ordinary_run(
                        generic_connection,
                        fixture=prepare_first,
                        run_id=prepare_first_run_id,
                        goal_id=prepare_first_goal_id,
                        seed="a",
                    )
            prepare_connection.commit()
            with self.assertRaisesRegex(
                psycopg.DatabaseError,
                "recovery run goal differs from its immutable binding",
            ):
                with generic_connection.transaction():
                    generic_connection.execute(
                        "SELECT set_config('app.firm_id', %s, true)",
                        (self.firm_id,),
                    )
                    insert_ordinary_run(
                        generic_connection,
                        fixture=prepare_first,
                        run_id=prepare_first_run_id,
                        goal_id=prepare_first_goal_id,
                        seed="a",
                    )

    def test_0050_closed_authority_abandons_pending_and_legacy_shape_never_claims(
        self,
    ) -> None:
        fixture = self._create_0049_followup(decision="REQUEST_REEXTRACTION")
        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (self.firm_id,),
            )
            connection.execute(
                """
                INSERT INTO case_agent_events (
                    event_id, run_id, firm_id, matter_id, event_sequence,
                    event_type, actor_id, payload, event_hash, occurred_at
                ) VALUES (
                    %s,%s,%s,%s,2,'VERIFICATION_FAILED',%s,%s,%s,
                    clock_timestamp()
                )
                """,
                (
                    str(uuid4()),
                    fixture["run_id"],
                    self.firm_id,
                    fixture["matter_id"],
                    self.worker_id,
                    Jsonb({"error_code": "SYNTHETIC_AUTHORITY_ENDED"}),
                    "d" * 64,
                ),
            )

        pending_run_id = str(uuid4())
        pending = self.exception_followup_store.prepare_control_recovery(
            matter_id=fixture["matter_id"],
            actor=self.actor,
            server_session_id=fixture["session_id"],
            replacement_run_id=pending_run_id,
            expected_version=1,
            idempotency_key="control-recovery-authority-ended-0001",
        )
        self.assertEqual(pending.recovery_state, "PENDING")
        closed = self.exception_followup_store.resolve_followup(
            matter_id=fixture["matter_id"],
            actor=self.actor,
            server_session_id=fixture["session_id"],
            expected_version=1,
            idempotency_key="close-before-recovery-run-0001",
            followup_id=fixture["followup_id"],
            action=LedgerExceptionFollowupAction.WITHDRAW,
            reason_note="恢复办理",
        )
        self.assertEqual(closed.matter_version, 2)
        terminal = self.exception_followup_store.prepare_control_recovery(
            matter_id=fixture["matter_id"],
            actor=self.actor,
            server_session_id=fixture["session_id"],
            replacement_run_id=pending_run_id,
            expected_version=1,
            idempotency_key="control-recovery-authority-ended-0001",
        )
        self.assertEqual(terminal.recovery_state, "ABANDONED")

        legacy_goal_id = str(uuid4())
        legacy_run_id = str(uuid4())
        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (self.firm_id,),
            )
            outcome = connection.execute(
                """
                SELECT head.current_outcome, head.outcome_reason_code,
                       head.outcome_actor_id, head.outcome_web_session_id,
                       head.outcome_at,
                       (
                           SELECT count(*)
                             FROM case_agent_ledger_exception_recovery_intent_heads pending_head
                            WHERE pending_head.firm_id = intent.firm_id
                              AND pending_head.matter_id = intent.matter_id
                              AND pending_head.current_outcome = 'PENDING'
                       ) AS pending_count
                  FROM case_agent_ledger_exception_recovery_intents intent
                  JOIN case_agent_ledger_exception_recovery_intent_heads head
                    ON head.recovery_intent_id = intent.recovery_intent_id
                 WHERE intent.recovery_intent_id = %s
                """,
                (terminal.recovery_intent_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO case_agent_goals (
                    goal_id, firm_id, matter_id, objective,
                    success_criteria, constraints, requested_by, goal_hash
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    legacy_goal_id,
                    self.firm_id,
                    fixture["matter_id"],
                    "恢复本案异常材料后续工作并基于当前权威台账继续研判",
                    Jsonb([
                        "接管全部待完成异常分流工作",
                        "重新提取任务覆盖原异常组完整受管来源并通过独立校验",
                        "全部后续工作完成后基于当前案件版本重新规划",
                    ]),
                    Jsonb([
                        "不得自动确认正式事实、法律口径或对外提交",
                        "不得读取其他案件或使用浏览器提供的运行、图谱或对象定位",
                    ]),
                    self.actor_id,
                    "e" * 64,
                ),
            )
            # A normal post-cutover insert is rejected by the new wake
            # trigger.  The replica-mode fixture below models an old INSERT
            # statement that began before cutover and resumed afterwards, so
            # the Worker claim predicate is exercised independently.
            with self.assertRaisesRegex(
                psycopg.DatabaseError,
                "recovery run requires a durable intent",
            ), connection.transaction():
                connection.execute(
                    """
                    INSERT INTO case_agent_runs (
                        run_id, firm_id, matter_id, goal_id, status,
                        current_event_version, snapshot_matter_version,
                        snapshot_schema_version, snapshot_hash, run_budget,
                        projection_hash, created_by
                    ) VALUES (
                        %s,%s,%s,%s,'CREATED',1,2,
                        'synthetic-legacy-recovery-v1',%s,%s,%s,%s
                    )
                    """,
                    (
                        legacy_run_id,
                        self.firm_id,
                        fixture["matter_id"],
                        legacy_goal_id,
                        "f" * 64,
                        Jsonb({"max_tasks": 10}),
                        "1" * 64,
                        self.actor_id,
                    ),
                )
            rejected_projection = connection.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1 FROM case_agent_runs WHERE run_id = %s
                    ) AS run_exists,
                    EXISTS (
                        SELECT 1 FROM case_agent_run_inbox WHERE run_id = %s
                    ) AS inbox_exists
                """,
                (legacy_run_id, legacy_run_id),
            ).fetchone()
            self.assertEqual(
                dict(rejected_projection),
                {"run_exists": False, "inbox_exists": False},
            )
            connection.execute("SET LOCAL session_replication_role = replica")
            connection.execute(
                """
                INSERT INTO case_agent_runs (
                    run_id, firm_id, matter_id, goal_id, status,
                    current_event_version, snapshot_matter_version,
                    snapshot_schema_version, snapshot_hash, run_budget,
                    projection_hash, created_by
                ) VALUES (
                    %s,%s,%s,%s,'CREATED',1,2,
                    'synthetic-legacy-recovery-v1',%s,%s,%s,%s
                )
                """,
                (
                    legacy_run_id,
                    self.firm_id,
                    fixture["matter_id"],
                    legacy_goal_id,
                    "f" * 64,
                    Jsonb({"max_tasks": 10}),
                    "1" * 64,
                    self.actor_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO case_agent_events (
                    event_id, run_id, firm_id, matter_id, event_sequence,
                    event_type, actor_id, payload, event_hash, occurred_at
                ) VALUES (
                    %s,%s,%s,%s,1,'RUN_CREATED',%s,%s,%s,
                    clock_timestamp()
                )
                """,
                (
                    str(uuid4()), legacy_run_id, self.firm_id,
                    fixture["matter_id"], self.actor_id,
                    Jsonb({"objective": "legacy recovery"}), "2" * 64,
                ),
            )
            connection.execute(
                """
                INSERT INTO case_agent_run_inbox (
                    run_id, firm_id, matter_id, inbox_status,
                    observed_event_version, available_at, inbox_version,
                    updated_at
                ) VALUES (%s,%s,%s,'READY',1,clock_timestamp(),1,clock_timestamp())
                """,
                (legacy_run_id, self.firm_id, fixture["matter_id"]),
            )
            connection.execute("SET LOCAL session_replication_role = origin")

        self.assertEqual(outcome["current_outcome"], "ABANDONED")
        self.assertEqual(outcome["outcome_reason_code"], "CONTROL_AUTHORITY_ENDED")
        self.assertEqual(str(outcome["outcome_actor_id"]), self.actor_id)
        self.assertEqual(str(outcome["outcome_web_session_id"]), fixture["session_id"])
        self.assertIsNotNone(outcome["outcome_at"])
        self.assertEqual(outcome["pending_count"], 0)
        legacy_claim = PostgresCaseAgentRunInbox(
            dsn=TEST_DSN,
            actor=Actor(
                self.worker_id,
                self.firm_id,
                frozenset({Role.SYSTEM_WORKER}),
            ),
        ).claim_next_run(
            lease_owner=case_agent_worker_id(self.firm_id),
            lease_seconds=120,
        )
        self.assertIsNone(legacy_claim)

    def test_row_level_security_hides_another_firm_matter(self) -> None:
        matter_id = str(uuid4())
        self.workflow.create_matter(
            self.actor,
            matter_id=matter_id,
            title="Synthetic UUID Matter",
            idempotency_key="create-rls-001",
        )
        other_firm_id = str(uuid4())
        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(f"SET LOCAL ROLE {RLS_TEST_ROLE}")
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (other_firm_id,))
            row = connection.execute("SELECT count(*) AS count FROM matters WHERE matter_id = %s", (matter_id,)).fetchone()
        self.assertEqual(row["count"], 0)

    def test_new_matter_service_binding_makes_triggered_inbox_claimable(self) -> None:
        matter_id, goal_id, run_id = str(uuid4()), str(uuid4()), str(uuid4())
        self.workflow.create_matter(
            self.actor,
            matter_id=matter_id,
            title="Synthetic Agent Claimable Matter",
            idempotency_key="create-agent-claimable-001",
        )
        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)", (self.firm_id,)
            )
            connection.execute(
                """
                INSERT INTO case_agent_goals (
                    goal_id, firm_id, matter_id, objective, success_criteria,
                    constraints, requested_by, goal_hash
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    goal_id,
                    self.firm_id,
                    matter_id,
                    "Read admitted matter materials",
                    Jsonb(["produce review candidates"]),
                    Jsonb(["read only"]),
                    self.actor_id,
                    "a" * 64,
                ),
            )
            connection.execute(
                """
                INSERT INTO case_agent_runs (
                    run_id, firm_id, matter_id, goal_id, status,
                    current_event_version, snapshot_matter_version,
                    snapshot_schema_version, snapshot_hash, run_budget,
                    projection_hash, created_by
                ) VALUES (
                    %s,%s,%s,%s,'CREATED',1,1,'case-ledger-snapshot-v1',
                    %s,%s,%s,%s
                )
                """,
                (
                    run_id,
                    self.firm_id,
                    matter_id,
                    goal_id,
                    "b" * 64,
                    Jsonb({"max_tasks": 10}),
                    "c" * 64,
                    self.actor_id,
                ),
            )

        inbox = PostgresCaseAgentRunInbox(
            dsn=WORKER_TEST_DSN,
            actor=Actor(
                self.worker_id,
                self.firm_id,
                frozenset({Role.SYSTEM_WORKER}),
            ),
        )
        claim = inbox.claim_next_run(
            lease_owner=case_agent_worker_id(self.firm_id), lease_seconds=120
        )
        self.assertIsNotNone(claim)
        self.assertEqual(claim.run_id, run_id)
        self.assertEqual(claim.matter_id, matter_id)

    def test_fact_candidate_and_decision_share_matter_version_audit_and_rls_boundary(self) -> None:
        matter_id = str(uuid4())
        self.workflow.create_matter(
            self.actor,
            matter_id=matter_id,
            title="Synthetic Persistent Fact Matter",
            idempotency_key="create-ledger-001",
        )
        candidate = self.case_ledger_store.create_fact_candidate(
            matter_id=matter_id,
            actor=self.actor,
            expected_version=1,
            idempotency_key="persist-fact-001",
            original_text="[合成] 被告主张已支付一笔款项。",
            origin=AssertionOrigin.DEFENDANT_STATEMENT,
            evidence_links=(
                EvidenceLink(
                    evidence_id="synthetic-integration-evidence",
                    original_file_sha256="a" * 64,
                    page_number=1,
                    region_id="synthetic-integration-region",
                    original_label="[合成] 原始账单第1页",
                ),
            ),
        )
        decided = self.case_ledger_store.decide_fact(
            matter_id=matter_id,
            fact_id=candidate.object_id,
            actor=self.actor,
            expected_version=2,
            idempotency_key="persist-fact-decision-001",
            status=FactStatus.CONFIRMED,
            decision_hash="b" * 64,
        )
        facts = self.case_ledger_store.list_facts(matter_id=matter_id, actor=self.actor)
        self.assertEqual(decided.matter_version, 3)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].status, FactStatus.CONFIRMED)

    def test_full_case_ledger_command_chain_preserves_versions_and_normalized_links(self) -> None:
        matter_id = str(uuid4())
        self.workflow.create_matter(
            self.actor,
            matter_id=matter_id,
            title="Synthetic Full Persistent Ledger Matter",
            idempotency_key="create-full-ledger-001",
        )
        original = (
            EvidenceLink(
                evidence_id="synthetic-full-ledger-evidence",
                original_file_sha256="c" * 64,
                page_number=1,
                region_id="synthetic-full-ledger-region",
                original_label="[合成] 原始材料第1页",
            ),
        )
        fact = self.case_ledger_store.create_fact_candidate(
            matter_id=matter_id,
            actor=self.actor,
            expected_version=1,
            idempotency_key="full-fact-create",
            original_text="[合成] 被告主张已支付一笔款项。",
            origin=AssertionOrigin.DEFENDANT_STATEMENT,
            evidence_links=original,
        )
        self.case_ledger_store.decide_fact(
            matter_id=matter_id,
            fact_id=fact.object_id,
            actor=self.actor,
            expected_version=2,
            idempotency_key="full-fact-confirm",
            status=FactStatus.CONFIRMED,
            decision_hash="d" * 64,
        )
        claim = self.case_ledger_store.create_claim_candidate_from_confirmed_facts(
            matter_id=matter_id,
            actor=self.actor,
            expected_version=3,
            idempotency_key="full-claim-create",
            original_claim_text="[合成] 原告主张本金1,000.00元。",
            claimed_amount=Decimal("1000.00"),
            currency="CNY",
            confirmed_fact_ids=(fact.object_id,),
        )
        self.case_ledger_store.confirm_claim_scope(
            matter_id=matter_id,
            claim_id=claim.object_id,
            actor=self.actor,
            expected_version=4,
            idempotency_key="full-claim-confirm",
            confirmation_hash="e" * 64,
        )
        self.case_ledger_store.set_claim_response(
            matter_id=matter_id,
            claim_id=claim.object_id,
            actor=self.actor,
            expected_version=5,
            idempotency_key="full-response-set",
            position=ClaimResponsePosition.PARTIALLY_ADMIT,
            confirmed_fact_ids=(fact.object_id,),
            partial_amount=Decimal("800.00"),
            currency="CNY",
            approval_hash="f" * 64,
        )
        issue = self.case_ledger_store.create_dispute_issue_candidate(
            matter_id=matter_id,
            actor=self.actor,
            expected_version=6,
            idempotency_key="full-issue-create",
            question="[合成] 已付款项应如何计入？",
            claim_ids=(claim.object_id,),
            confirmed_fact_ids=(fact.object_id,),
        )
        self.case_ledger_store.confirm_dispute_issue(
            matter_id=matter_id,
            issue_id=issue.object_id,
            actor=self.actor,
            expected_version=7,
            idempotency_key="full-issue-confirm",
            approval_hash="1" * 64,
        )
        transaction = self.case_ledger_store.create_transaction_candidate(
            matter_id=matter_id,
            actor=self.actor,
            expected_version=8,
            idempotency_key="full-transaction-create",
            local_date=date(2020, 8, 20),
            date_precision=DatePrecision.EXACT_DATE,
            amount=Decimal("1000.00"),
            currency="CNY",
            direction=TransactionDirection.OUTGOING,
            payer_label="[合成] 被告",
            payee_label="[合成] 原告",
            channel=TransactionChannel.WECHAT,
            transaction_reference="synthetic-full-ledger-reference",
            evidence_links=original,
        )
        self.case_ledger_store.confirm_transaction(
            matter_id=matter_id,
            transaction_id=transaction.object_id,
            actor=self.actor,
            expected_version=9,
            idempotency_key="full-transaction-confirm",
            confirmation_hash="2" * 64,
        )
        classification = self.case_ledger_store.create_payment_classification_candidate(
            matter_id=matter_id,
            transaction_id=transaction.object_id,
            actor=self.actor,
            expected_version=10,
            idempotency_key="full-classification-create",
            origin=ClassificationOrigin.DEFENDANT_STATEMENT,
            nature=PaymentNature.INTEREST_PAYMENT,
            allocations=(ObligationAllocation("synthetic-obligation", Decimal("1000.00"), "CNY"),),
            same_day_sequence=1,
            evidence_links=original,
        )
        final = self.case_ledger_store.approve_payment_classification(
            matter_id=matter_id,
            classification_id=classification.object_id,
            actor=self.actor,
            expected_version=11,
            idempotency_key="full-classification-approve",
            approval_hash="3" * 64,
        )
        self.assertEqual(final.matter_version, 12)
        snapshot = self.case_ledger_store.get_case_snapshot(matter_id=matter_id, actor=self.actor)
        self.assertEqual(snapshot.version, 12)
        self.assertEqual(len(snapshot.facts), 1)
        self.assertEqual(len(snapshot.claims), 1)
        self.assertEqual(len(snapshot.issues), 1)
        self.assertEqual(len(snapshot.transactions), 1)
        self.assertEqual(len(snapshot.payment_classifications), 1)
        with psycopg.connect(TEST_DSN, row_factory=dict_row) as connection:
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (self.firm_id,))
            response_links = connection.execute(
                "SELECT count(*) AS count FROM case_claim_response_facts WHERE matter_id = %s",
                (matter_id,),
            ).fetchone()["count"]
            allocation_links = connection.execute(
                "SELECT count(*) AS count FROM case_payment_allocations WHERE matter_id = %s",
                (matter_id,),
            ).fetchone()["count"]
        self.assertEqual(response_links, 1)
        self.assertEqual(allocation_links, 1)

class PostgresMatterStoreBoundaryTests(unittest.TestCase):
    def test_alpha_identifiers_are_rejected_before_connection(self) -> None:
        store = PostgresMatterStore("postgresql://not-used.invalid/lawcase_workbench_test")
        workflow = MatterWorkflow(store)
        with patch("case_kernel.postgres_store.psycopg.connect") as connect:
            with self.assertRaisesRegex(ValueError, "requires UUID"):
                workflow.create_matter(
                    Actor("alpha_lead_lawyer", "alpha_firm_001", frozenset({Role.LEAD_LAWYER})),
                    matter_id="alpha_matter_001",
                    title="[合成] 不可进入持久化库",
                    idempotency_key="alpha-create-001",
                )
        connect.assert_not_called()

    def test_create_fails_closed_without_server_owned_agent_mappings(self) -> None:
        firm_id, actor_id = str(uuid4()), str(uuid4())
        store = PostgresMatterStore(
            "postgresql://not-used.invalid/lawcase_workbench_test"
        )
        workflow = MatterWorkflow(store)
        with patch("case_kernel.postgres_store.psycopg.connect") as connect:
            with self.assertRaisesRegex(
                CaseAgentMatterProvisioningBlocked, "not provisioned"
            ):
                workflow.create_matter(
                    Actor(actor_id, firm_id, frozenset({Role.LEAD_LAWYER})),
                    matter_id=str(uuid4()),
                    title="[synthetic] must not create an Agent-orphaned case",
                    idempotency_key="missing-agent-bindings-001",
                )
        connect.assert_not_called()

    def test_mapping_constructor_rejects_self_verifier_or_partial_config(self) -> None:
        firm_id, actor_id = str(uuid4()), str(uuid4())
        with self.assertRaisesRegex(ValueError, "supplied together"):
            PostgresMatterStore(
                "postgresql://not-used.invalid/lawcase_workbench_test",
                system_worker_ids_by_firm={firm_id: actor_id},
            )
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            PostgresMatterStore(
                "postgresql://not-used.invalid/lawcase_workbench_test",
                system_worker_ids_by_firm={firm_id: actor_id},
                system_verifier_ids_by_firm={firm_id: actor_id},
            )

    def test_create_sql_binds_exact_distinct_server_principals_in_one_transaction(self) -> None:
        firm_id, lead_id, worker_id, verifier_id, matter_id = (
            str(uuid4()),
            str(uuid4()),
            str(uuid4()),
            str(uuid4()),
            str(uuid4()),
        )
        store = PostgresMatterStore(
            "postgresql://not-used.invalid/lawcase_workbench_test",
            system_worker_ids_by_firm={firm_id: worker_id},
            system_verifier_ids_by_firm={firm_id: verifier_id},
        )
        actor = Actor(lead_id, firm_id, frozenset({Role.LEAD_LAWYER}))
        matter = Matter(
            matter_id=matter_id,
            firm_id=firm_id,
            title="[synthetic] exact service-principal binding",
        )
        connection = _MatterCreateShapeConnection(
            firm_id=firm_id,
            worker_id=worker_id,
            verifier_id=verifier_id,
        )
        with patch.object(store, "_transaction", return_value=_ConnectionContext(connection)):
            store.create(
                matter=matter,
                actor=actor,
                idempotency_key="exact-agent-principal-bindings-001",
            )

        service_insert = next(
            (sql, params)
            for sql, params in connection.calls
            if "INSERT INTO matter_actor_roles" in sql
            and sql.count("'SYSTEM_WORKER'") == 2
        )
        self.assertEqual(service_insert[0].count(") VALUES"), 1)
        self.assertEqual(
            service_insert[1],
            (
                matter_id,
                firm_id,
                worker_id,
                matter_id,
                firm_id,
                verifier_id,
            ),
        )
        authorization_sql, authorization_params = next(
            (sql, params)
            for sql, params in connection.calls
            if "FOR UPDATE OF principal" in sql
        )
        self.assertIn("principal.status", authorization_sql)
        self.assertIn("role.role <> 'SYSTEM_WORKER'", authorization_sql)
        self.assertEqual(authorization_params, (firm_id, firm_id, [worker_id, verifier_id]))

    def test_create_authorization_rejects_inactive_cross_firm_or_human_service_identity(self) -> None:
        firm_id, worker_id, verifier_id = str(uuid4()), str(uuid4()), str(uuid4())
        valid = {
            "actor_id": worker_id,
            "firm_id": firm_id,
            "status": "ACTIVE",
            "has_active_non_worker_role": False,
        }
        verifier = {
            **valid,
            "actor_id": verifier_id,
        }
        invalid_rows = (
            ({**valid, "status": "SUSPENDED"}, verifier),
            (valid, {**verifier, "firm_id": str(uuid4())}),
            (valid, {**verifier, "has_active_non_worker_role": True}),
            (valid,),
        )
        for rows in invalid_rows:
            with self.subTest(rows=rows), self.assertRaisesRegex(
                CaseAgentMatterProvisioningBlocked, "dedicated active"
            ):
                PostgresMatterStore._authorize_case_agent_principals(
                    _AuthorizationOnlyConnection(rows),
                    firm_id=firm_id,
                    execution_actor_id=worker_id,
                    verifier_actor_id=verifier_id,
                )

    def test_web_startup_preflight_locks_every_configured_service_principal_pair(self) -> None:
        firm_id, worker_id, verifier_id = str(uuid4()), str(uuid4()), str(uuid4())
        connection = _MatterProvisioningPreflightConnection(
            firm_id=firm_id,
            worker_id=worker_id,
            verifier_id=verifier_id,
        )
        with patch(
            "case_kernel.postgres_store.psycopg.connect",
            return_value=_ConnectionContext(connection),
        ):
            preflight_case_agent_matter_provisioning_contract(
                dsn="postgresql://not-used.invalid/lawcase_workbench_test",
                system_worker_ids_by_firm={firm_id: worker_id},
                system_verifier_ids_by_firm={firm_id: verifier_id},
            )

        authorization = next(
            (sql, params)
            for sql, params in connection.calls
            if "FOR UPDATE OF principal" in sql
        )
        self.assertEqual(authorization[1], (firm_id, firm_id, [worker_id, verifier_id]))


class _ConnectionContext:
    def __init__(self, connection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_args):
        return False


class _ShapeCursor:
    def __init__(self, *, rows=(), row=None, rowcount=1) -> None:
        self._rows = list(rows)
        self._row = row
        self.rowcount = rowcount

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._row


class _AuthorizationOnlyConnection:
    def __init__(self, rows) -> None:
        self.rows = rows

    def execute(self, _sql, _params=()):
        return _ShapeCursor(rows=self.rows)


class _MatterCreateShapeConnection:
    def __init__(self, *, firm_id: str, worker_id: str, verifier_id: str) -> None:
        self.firm_id = firm_id
        self.worker_id = worker_id
        self.verifier_id = verifier_id
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        if "SELECT request_hash, response_json" in normalized:
            return _ShapeCursor(row=None)
        if "FOR UPDATE OF principal" in normalized:
            return _ShapeCursor(
                rows=(
                    {
                        "actor_id": self.worker_id,
                        "firm_id": self.firm_id,
                        "status": "ACTIVE",
                        "has_active_non_worker_role": False,
                    },
                    {
                        "actor_id": self.verifier_id,
                        "firm_id": self.firm_id,
                        "status": "ACTIVE",
                        "has_active_non_worker_role": False,
                    },
                )
            )
        return _ShapeCursor(rowcount=1)


class _MatterProvisioningPreflightConnection(_MatterCreateShapeConnection):
    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        if "set_config('app.firm_id'" in normalized:
            self.firm_id = params[0]
            return _ShapeCursor(rowcount=1)
        if "FOR UPDATE OF principal" in normalized:
            return _ShapeCursor(
                rows=(
                    {
                        "actor_id": self.worker_id,
                        "firm_id": self.firm_id,
                        "status": "ACTIVE",
                        "has_active_non_worker_role": False,
                    },
                    {
                        "actor_id": self.verifier_id,
                        "firm_id": self.firm_id,
                        "status": "ACTIVE",
                        "has_active_non_worker_role": False,
                    },
                )
            )
        return _ShapeCursor(rowcount=1)


if __name__ == "__main__":
    unittest.main()
