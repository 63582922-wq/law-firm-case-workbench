from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from case_kernel.case_ledger_postgres import CaseLedgerPersistenceBlocked
from case_kernel.models import Actor, Role
from case_kernel.reviewable_draft_postgres import (
    PostgresReviewableDraftStore,
    _DOCX,
    _review_input_hash,
)
from case_kernel.reviewable_draft_access import ReviewableDraftAccessPurpose


def _docx_bytes() -> bytes:
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document/>")
    return stream.getvalue()


def _pdf_bytes() -> bytes:
    return b"%PDF-1.4\n1 0 obj <<>>\nendobj\ntrailer <<>>\n%%EOF\n"


@dataclass
class _Result:
    row: dict | None = None
    rows: tuple[dict, ...] = ()

    def fetchone(self):
        return self.row

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(self, *, review_input_hash: str) -> None:
        self.review_input_hash = review_input_hash
        self.executed: list[tuple[str, tuple | None]] = []
        self.pair_id = str(uuid4())

    def execute(self, sql: str, params: tuple | None = None) -> _Result:
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if normalized.startswith("SELECT 1 FROM matters m JOIN matter_actor_roles"):
            return _Result({"authorized": 1})
        if "SELECT request_hash, response_json" in normalized:
            return _Result()
        if normalized.startswith("SELECT m.version,"):
            return _Result({"version": 1, "permitted": True})
        if normalized.startswith("SELECT version FROM matters"):
            return _Result({"version": 3})
        if "SELECT pair_id, document_kind, editable_media_type" in normalized:
            return _Result(rows=(
                {
                    "pair_id": self.pair_id,
                    "document_kind": "DEFENCE_STATEMENT",
                    "editable_media_type": _DOCX,
                    "editable_sha256": "a" * 64,
                    "editable_bytes": 101,
                    "review_pdf_sha256": "b" * 64,
                    "review_pdf_bytes": 202,
                    "review_pdf_page_count": 1,
                    "approval_input_hash": "c" * 64,
                    "render_verification_hash": "d" * 64,
                    "review_input_hash": self.review_input_hash,
                    "status": "CANDIDATE",
                    "registered_by": str(uuid4()),
                    "approved_by": None,
                    "approval_hash": None,
                    "approved_at": None,
                    "created_at": datetime(2026, 8, 10),
                },
            ))
        if "SELECT editable_media_type, editable_object_key" in normalized:
            return _Result({
                "editable_media_type": _DOCX,
                "editable_object_key": f"aa/{'a' * 2}/{'a' * 64}.lca",
                "editable_sha256": "a" * 64,
                "editable_bytes": 101,
                "review_pdf_object_key": f"bb/{'b' * 2}/{'b' * 64}.lca",
                "review_pdf_sha256": "b" * 64,
                "review_pdf_bytes": 202,
                "status": "CANDIDATE",
            })
        if "FROM reviewable_office_draft_pairs" in normalized and "FOR UPDATE" in normalized:
            return _Result(
                {
                    "document_kind": "DEFENCE_STATEMENT",
                    "editable_sha256": "a" * 64,
                    "review_pdf_sha256": "b" * 64,
                    "review_input_hash": self.review_input_hash,
                    "status": "CANDIDATE",
                }
            )
        if "UPDATE matters SET version = version + 1" in normalized:
            return _Result({"version": 2})
        return _Result()


class _ConnectionContext:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, _type, _value, _traceback):
        return False


class ReviewableDraftPostgresTests(TestCase):
    def setUp(self) -> None:
        self.matter_id = str(uuid4())
        self.firm_id = str(uuid4())
        self.system = Actor(str(uuid4()), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        self.lawyer = Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER}))
        self.editable = _docx_bytes()
        self.review_pdf = _pdf_bytes()
        self.editable_hash = sha256(self.editable).hexdigest()
        self.pdf_hash = sha256(self.review_pdf).hexdigest()
        self.approval_input_hash = "c" * 64
        self.render_hash = "d" * 64
        self.review_input_hash = _review_input_hash(
            editable_media_type=_DOCX,
            editable_sha256=self.editable_hash,
            editable_bytes=len(self.editable),
            review_pdf_sha256=self.pdf_hash,
            review_pdf_bytes=len(self.review_pdf),
            review_pdf_page_count=1,
            approval_input_hash=self.approval_input_hash,
            render_verification_hash=self.render_hash,
        )
        self.store = PostgresReviewableDraftStore(
            "postgresql://not-used.invalid/lawcase_test",
            artifact_reader=lambda _key, expected: (
                self.editable if expected == self.editable_hash else self.review_pdf
            ),
        )

    def _run(self, connection: _Connection, callback):
        with patch(
            "case_kernel.reviewable_draft_postgres.psycopg.connect",
            return_value=_ConnectionContext(connection),
        ):
            return callback()

    def _register(self, connection: _Connection, *, review_input_hash: str | None = None):
        return self._run(
            connection,
            lambda: self.store.register_reviewable_office_draft_pair(
                matter_id=self.matter_id,
                actor=self.system,
                expected_version=1,
                idempotency_key="office-pair-register-001",
                document_kind="DEFENCE_STATEMENT",
                editable_media_type=_DOCX,
                editable_object_key=f"{self.editable_hash[:2]}/{self.editable_hash[2:4]}/{self.editable_hash}.lca",
                editable_sha256=self.editable_hash,
                editable_bytes=len(self.editable),
                review_pdf_object_key=f"{self.pdf_hash[:2]}/{self.pdf_hash[2:4]}/{self.pdf_hash}.lca",
                review_pdf_sha256=self.pdf_hash,
                review_pdf_bytes=len(self.review_pdf),
                review_pdf_page_count=1,
                approval_input_hash=self.approval_input_hash,
                render_verification_hash=self.render_hash,
                review_input_hash=review_input_hash or self.review_input_hash,
            ),
        )

    def test_registration_authenticates_both_objects_and_binds_exact_pair_hash(self) -> None:
        connection = _Connection(review_input_hash=self.review_input_hash)
        receipt = self._register(connection)
        self.assertEqual(receipt.matter_version, 1)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("INSERT INTO reviewable_office_draft_pairs", sql)
        self.assertNotIn("UPDATE matters SET version = version + 1", sql)
        self.assertNotIn("INSERT INTO outbox_events", sql)
        self.assertNotIn("UPDATE submission_bundles SET validity = 'STALE'", sql)
        audit = next(
            entry for entry in connection.executed if "INSERT INTO audit_events" in entry[0]
        )
        self.assertEqual(
            audit[1][4:7],
            ("REVIEWABLE_OFFICE_DRAFT_PAIR_REGISTERED", 1, 1),
        )

    def test_registration_rejects_review_hash_that_does_not_describe_stored_pair(self) -> None:
        connection = _Connection(review_input_hash=self.review_input_hash)
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "differs from the exact stored outputs"):
            self._register(connection, review_input_hash="e" * 64)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertNotIn("INSERT INTO reviewable_office_draft_pairs", sql)

    def test_lawyer_approval_requires_exact_review_pair_hash(self) -> None:
        connection = _Connection(review_input_hash=self.review_input_hash)
        receipt = self._run(
            connection,
            lambda: self.store.approve_reviewable_office_draft_pair(
                matter_id=self.matter_id,
                actor=self.lawyer,
                expected_version=1,
                idempotency_key="office-pair-approve-001",
                pair_id=str(uuid4()),
                approval_hash=self.review_input_hash,
            ),
        )
        self.assertEqual(receipt.matter_version, 1)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("UPDATE reviewable_office_draft_pairs", sql)
        self.assertNotIn("UPDATE matters SET version = version + 1", sql)
        self.assertNotIn("INSERT INTO outbox_events", sql)
        audit = next(
            entry for entry in connection.executed if "INSERT INTO audit_events" in entry[0]
        )
        self.assertEqual(
            audit[1][4:7],
            ("REVIEWABLE_OFFICE_DRAFT_PAIR_APPROVED", 1, 1),
        )
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "exact review pair hash"):
            self._run(
                _Connection(review_input_hash=self.review_input_hash),
                lambda: self.store.approve_reviewable_office_draft_pair(
                    matter_id=self.matter_id,
                    actor=self.lawyer,
                    expected_version=1,
                    idempotency_key="office-pair-approve-mismatch-001",
                    pair_id=str(uuid4()),
                    approval_hash="e" * 64,
                ),
            )

    def test_snapshot_hides_encrypted_object_keys_and_locator_selects_review_pdf(self) -> None:
        connection = _Connection(review_input_hash=self.review_input_hash)
        snapshot = self._run(
            connection,
            lambda: self.store.get_reviewable_office_draft_snapshot(
                matter_id=self.matter_id, actor=self.lawyer
            ),
        )
        self.assertEqual(snapshot.matter_version, 3)
        self.assertEqual(snapshot.pairs[0]["pair_id"], connection.pair_id)
        self.assertFalse(any("object_key" in pair for pair in snapshot.pairs))
        self.assertEqual(snapshot.pairs[0]["registered_by"], snapshot.pairs[0]["registered_by"])

        locator = self._run(
            connection,
            lambda: self.store.get_reviewable_office_draft_artifact_locator(
                matter_id=self.matter_id,
                pair_id=connection.pair_id,
                purpose=ReviewableDraftAccessPurpose.REVIEW_PDF,
                actor=self.lawyer,
            ),
        )
        self.assertEqual(locator.media_type, "application/pdf")
        self.assertEqual(locator.artifact_sha256, "b" * 64)
        self.assertEqual(locator.object_key, f"bb/{'b' * 2}/{'b' * 64}.lca")
