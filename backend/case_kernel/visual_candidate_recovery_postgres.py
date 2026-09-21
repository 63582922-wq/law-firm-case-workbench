"""Append-only OCR recovery storage. Not wired to HTTP or task success writes."""

from contextlib import contextmanager
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from .models import Actor, Role
from .visual_candidate_recovery import (
    POLICY, VisualCandidateRecoveryBlocked, VisualRecoverySource,
    prepare_visual_candidate_recovery,
)
from .web_object_store import StoredCaseAgentReviewCandidate


class PostgresVisualRecoveryStore:
    def __init__(self, *, dsn, worker_actor: Actor, object_store, connection_factory=None):
        if not isinstance(worker_actor, Actor) or worker_actor.roles != frozenset({Role.SYSTEM_WORKER}):
            raise PermissionError("dedicated recovery worker required")
        UUID(worker_actor.actor_id)
        UUID(worker_actor.firm_id)
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("recovery DSN required")
        self._dsn, self._actor, self._objects = dsn, worker_actor, object_store
        self._connect = connection_factory or psycopg.connect

    @contextmanager
    def _transaction(self, *, read_only):
        with self._connect(self._dsn, row_factory=dict_row) as db:
            if read_only:
                db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            db.execute("SELECT set_config('app.firm_id',%s,true), set_config('app.actor_id',%s,true)",
                       (self._actor.firm_id, self._actor.actor_id))
            yield db

    def stage(self, *, matter_id, run_id, source_artifact_id, expected_event_version):
        for value in (matter_id, run_id, source_artifact_id):
            UUID(value)
        if type(expected_event_version) is not int or expected_event_version < 1:
            raise ValueError("expected event version required")
        args = dict(matter_id=matter_id, run_id=run_id, artifact_id=source_artifact_id,
                    expected_event_version=expected_event_version)
        with self._transaction(read_only=True) as db:
            row, source = self._binding(db, **args)
            prior = self._prior(db, source_artifact_id)
        original = self._objects.read_case_agent_review_candidate(
            self._stored_original(row), artifact_id=source_artifact_id)
        draft = prepare_visual_candidate_recovery(source=source, original=original)
        if prior is not None:
            self._verify_prior(prior, draft=draft, source=source)
            return draft
        stored = self._objects.put_case_agent_review_candidate(
            draft.payload, firm_id=self._actor.firm_id, matter_id=matter_id,
            artifact_id=draft.recovery_id, content_sha256=draft.content_sha256)
        if (not isinstance(stored, StoredCaseAgentReviewCandidate)
                or stored.content_sha256 != draft.content_sha256 or stored.byte_size != len(draft.payload)):
            raise VisualCandidateRecoveryBlocked("recovery object receipt differs")
        if self._objects.read_case_agent_review_candidate(stored, artifact_id=draft.recovery_id) != draft.payload:
            raise VisualCandidateRecoveryBlocked("recovery object readback differs")
        with self._transaction(read_only=False) as db:
            # Consistent lock order and optimistic version check after object I/O.
            db.execute("SELECT run_id FROM case_agent_runs WHERE run_id=%s AND firm_id=%s FOR UPDATE",
                       (run_id, self._actor.firm_id))
            db.execute("SELECT matter_id FROM matters WHERE matter_id=%s AND firm_id=%s FOR SHARE",
                       (matter_id, self._actor.firm_id))
            current_row, current_source = self._binding(db, **args)
            if current_source != source or self._stored_original(current_row) != self._stored_original(row):
                raise VisualCandidateRecoveryBlocked("source changed while preparing recovery")
            db.execute("""INSERT INTO case_agent_visual_recovery_candidates
                (recovery_id,source_artifact_id,run_id,firm_id,matter_id,source_event_version,
                 source_content_sha256,interpretation_policy,content_sha256,byte_size,
                 object_key,object_version_id,recovered_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (source_artifact_id,interpretation_policy) DO NOTHING""",
                (draft.recovery_id,source_artifact_id,run_id,self._actor.firm_id,matter_id,
                 expected_event_version,source.content_sha256,POLICY,draft.content_sha256,
                 len(draft.payload),stored.object_key,stored.object_version_id,self._actor.actor_id))
            prior = self._prior(db, source_artifact_id)
            self._verify_prior(prior, draft=draft, source=source)
        return draft

    @staticmethod
    def _stored_original(row):
        return StoredCaseAgentReviewCandidate(
            object_key=row["source_object_key"], content_sha256=row["content_sha256"],
            byte_size=row["byte_size"], object_version_id=row["source_object_version_id"])

    def _prior(self, db, artifact_id):
        return db.execute("""SELECT * FROM case_agent_visual_recovery_candidates
            WHERE firm_id=%s AND source_artifact_id=%s AND interpretation_policy=%s""",
            (self._actor.firm_id, artifact_id, POLICY)).fetchone()

    def _verify_prior(self, row, *, draft, source):
        if row is None or any(str(row[key]) != str(value) for key, value in {
            "recovery_id": draft.recovery_id, "source_artifact_id": source.artifact_id,
            "run_id": source.run_id, "firm_id": source.firm_id, "matter_id": source.matter_id,
            "source_event_version": source.run_event_version,
            "source_content_sha256": source.content_sha256, "interpretation_policy": POLICY,
            "content_sha256": draft.content_sha256, "byte_size": len(draft.payload),
            "review_status": "NEEDS_LAWYER_REVIEW",
        }.items()):
            raise VisualCandidateRecoveryBlocked("existing recovery metadata differs")
        stored = StoredCaseAgentReviewCandidate(object_key=row["object_key"],
            content_sha256=row["content_sha256"], byte_size=row["byte_size"],
            object_version_id=row["object_version_id"])
        if self._objects.read_case_agent_review_candidate(stored, artifact_id=draft.recovery_id) != draft.payload:
            raise VisualCandidateRecoveryBlocked("existing recovery bytes differ")

    def _binding(self, db, *, matter_id, run_id, artifact_id, expected_event_version):
        rows = db.execute("""
            SELECT c.*, r.status AS run_status,r.current_event_version,r.failure_code,
                   r.snapshot_matter_version, receipt.attempt_id,receipt.result_status,
                   receipt.external_request_id,receipt.cost_minor_units,task.input_refs
            FROM case_agent_review_candidates c
            JOIN case_agent_runs r USING (run_id,firm_id,matter_id)
            JOIN matters m USING (matter_id,firm_id)
            JOIN case_agent_artifacts a USING (artifact_id,run_id,firm_id,matter_id)
            JOIN case_agent_task_receipts receipt USING (receipt_id,run_id,firm_id,matter_id)
            JOIN case_agent_tasks task ON task.task_id=c.task_id AND task.graph_id=c.graph_id
                AND task.run_id=c.run_id
            JOIN case_agent_task_heads head ON head.task_id=task.task_id AND head.graph_id=task.graph_id
                AND head.run_id=task.run_id AND head.is_current
            WHERE c.artifact_id=%s AND r.run_id=%s AND r.matter_id=%s AND r.firm_id=%s
              AND r.current_event_version=%s AND r.snapshot_matter_version=m.version
              AND NOT r.is_stale AND NOT r.is_cancelled AND c.graph_id=r.current_graph_id
              AND c.artifact_kind='VISUAL_PAGE_REVIEW_CANDIDATE' AND c.review_status='NEEDS_LAWYER_REVIEW'
              AND a.content_hash=c.content_sha256 AND a.byte_size=c.byte_size
              AND a.source_input_hash=c.task_input_hash AND receipt.input_hash=c.task_input_hash
              AND receipt.task_id=c.task_id AND receipt.external_calls=1
              AND receipt.external_submission_state='SUBMITTED' AND task.skill_id='image_visual_ocr'
              AND head.status='SUCCEEDED'
              AND EXISTS (SELECT 1 FROM matter_actor_roles role
                JOIN users principal ON principal.user_id=role.user_id AND principal.firm_id=role.firm_id
                WHERE role.firm_id=r.firm_id AND role.matter_id=r.matter_id AND role.user_id=%s
                  AND role.role='SYSTEM_WORKER' AND role.revoked_at IS NULL AND principal.status='ACTIVE')
            """, (artifact_id,run_id,matter_id,self._actor.firm_id,expected_event_version,self._actor.actor_id)).fetchall()
        if len(rows) != 1:
            raise VisualCandidateRecoveryBlocked("current authorised recovery source unavailable")
        row = rows[0]
        source = VisualRecoverySource(
            firm_id=str(row["firm_id"]),matter_id=str(row["matter_id"]),matter_version=row["snapshot_matter_version"],
            run_id=str(row["run_id"]),run_event_version=row["current_event_version"],run_status=row["run_status"],
            failure_code=row["failure_code"],task_id=str(row["task_id"]),task_status=row["result_status"],
            attempt_id=str(row["attempt_id"]),artifact_id=str(row["artifact_id"]),
            content_sha256=row["content_sha256"],byte_size=row["byte_size"],task_input_hash=row["task_input_hash"],
            external_request_id=row["external_request_id"],cost_minor_units=row["cost_minor_units"],
            input_refs=tuple(row["input_refs"]))
        return row, source
