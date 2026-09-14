from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import unittest
from uuid import uuid4

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_api.web_common_material_upload import (
    CommonMaterialUploadOperation,
    CommonMaterialUploadStatus,
)
from case_api.web_common_material_upload_postgres import PostgresCommonMaterialUploadStore
from case_kernel.case_agent_planning_snapshot_postgres import (
    _read_evidence_page_objects,
    _read_material_objects,
)
from case_kernel.models import Actor, Role
from case_kernel.web_common_material_admission import (
    CommonMaterialFormat,
    CommonMaterialRoute,
)


class _PlanningConnection:
    def __init__(self, material_object_id: str, content_sha256: str) -> None:
        self.material_object_id = material_object_id
        self.content_sha256 = content_sha256
        self.query = ""

    def execute(self, query: str, parameters: tuple[object, ...]):
        self.query = query
        if "case_agent_material_objects" not in query:
            raise AssertionError("planning did not read the executable material bridge")
        return self

    def fetchall(self):
        return [
            {
                "material_object_id": self.material_object_id,
                "content_sha256": self.content_sha256,
            }
        ]


class _EvidencePlanningConnection:
    def __init__(self, evidence_page_id: str, evidence_file_id: str) -> None:
        self.evidence_page_id = evidence_page_id
        self.evidence_file_id = evidence_file_id

    def execute(self, query: str, parameters: tuple[object, ...]):
        if "FROM evidence_pages" not in query:
            raise AssertionError("planning did not read the native image evidence page")
        return self

    def fetchall(self):
        return [
            {
                "evidence_page_id": self.evidence_page_id,
                "evidence_file_id": self.evidence_file_id,
                "page_number": 1,
                "rendered_page_sha256": None,
                "original_file_sha256": "d" * 64,
                "decision_id": None,
                "disposition": None,
                "decision_status": None,
                "approval_hash": None,
            }
        ]


class _Result:
    def __init__(self, row=None) -> None:
        self._row = row

    def fetchone(self):
        return self._row


class _RegistrationConnection:
    """Small transaction double that preserves the rows 0041 projects to Agent planning."""

    def __init__(self, operation: CommonMaterialUploadOperation) -> None:
        self.upload_row = _database_row(operation)
        self.executable_material: dict[str, object] | None = None
        self.evidence_page: dict[str, object] | None = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query: str, parameters: tuple[object, ...] = ()):
        normalized = " ".join(query.split())
        if "SELECT set_config" in normalized:
            return _Result()
        if "SELECT * FROM web_common_material_uploads" in normalized:
            return _Result(dict(self.upload_row))
        if "SELECT m.version" in normalized and "AS permitted" in normalized:
            return _Result({"version": self.upload_row["expected_matter_version"], "permitted": True})
        if "INSERT INTO case_agent_material_objects" in normalized:
            self.executable_material = {
                "material_object_id": parameters[0],
                "content_sha256": parameters[5],
            }
            return _Result()
        if "INSERT INTO evidence_pages" in normalized:
            self.evidence_page = {
                "evidence_page_id": parameters[0],
                "evidence_file_id": parameters[3],
            }
            return _Result()
        if normalized.startswith("UPDATE matters SET version = version + 1"):
            return _Result({"version": int(self.upload_row["expected_matter_version"]) + 1})
        if normalized.startswith("UPDATE web_common_material_uploads SET status = 'COMPLETED'"):
            completed = dict(self.upload_row)
            completed.update(
                {
                    "status": "COMPLETED",
                    "result_matter_version": parameters[0],
                    "agent_status": parameters[1],
                    "agent_source_ref": parameters[2],
                    "audit_event_id": parameters[3],
                    "outbox_id": parameters[4],
                    "completed_at": parameters[5],
                    "updated_at": parameters[6],
                }
            )
            self.upload_row = completed
            return _Result(dict(completed))
        return _Result()


class _CapturedMaterialPlanningConnection:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row

    def execute(self, query: str, parameters: tuple[object, ...]):
        if "case_agent_material_objects" not in query:
            raise AssertionError("planning did not read the executable material bridge")
        return self

    def fetchall(self):
        return [self.row]


def _database_row(operation: CommonMaterialUploadOperation) -> dict[str, object]:
    row = dict(vars(operation))
    row["status"] = operation.status.value
    row["admitted_format"] = (
        None if operation.admitted_format is None else operation.admitted_format.value
    )
    row["route"] = None if operation.route is None else operation.route.value
    row["review_flags"] = list(operation.review_flags)
    row["agent_status"] = None if operation.agent_status is None else operation.agent_status.value
    row["failure_code"] = None if operation.failure_code is None else operation.failure_code.value
    return row


def _registration_fixture(
    admitted_format: CommonMaterialFormat,
) -> tuple[ServerIdentityContext, CommonMaterialUploadOperation, datetime]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    actor = Actor(str(uuid4()), str(uuid4()), frozenset({Role.LEAD_LAWYER}))
    identity = ServerIdentityContext(
        actor=actor,
        session_id=str(uuid4()),
        issuer="https://identity.example.invalid",
        authentication_method=AuthenticationMethod.OIDC_MFA,
        authenticated_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=30),
    )
    material_object_id = str(uuid4())
    matter_id = str(uuid4())
    content_sha256 = sha256(f"{admitted_format.value}-source".encode()).hexdigest()
    if admitted_format is CommonMaterialFormat.DOCX:
        filename = "source.docx"
        canonical_kind = "WORD_DOCUMENT"
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        route = CommonMaterialRoute.COMMON_DOCUMENT_READER
        object_key = (
            f"case-materials/v1/{actor.firm_id}/{matter_id}/"
            f"{content_sha256[:2]}/{content_sha256}"
        )
    else:
        filename = "source.png"
        canonical_kind = "IMAGE"
        media_type = "image/png"
        route = CommonMaterialRoute.VISUAL_OCR
        object_key = (
            f"original-images/v1/{actor.firm_id}/{matter_id}/"
            f"{content_sha256[:2]}/{content_sha256}/{material_object_id}.png"
        )
    operation = CommonMaterialUploadOperation(
        upload_id=str(uuid4()),
        material_object_id=material_object_id,
        firm_id=actor.firm_id,
        matter_id=matter_id,
        actor_id=actor.actor_id,
        session_id=identity.session_id,
        expected_matter_version=8,
        display_name=filename,
        declared_byte_size=64,
        declared_media_type=media_type,
        reserve_idempotency_key="reserve-agent-bridge-001",
        reserve_request_hash="a" * 64,
        status=CommonMaterialUploadStatus.OBJECT_STORED,
        created_at=now - timedelta(minutes=2),
        expires_at=now + timedelta(minutes=10),
        content_idempotency_key="content-agent-bridge-001",
        attempt_id=str(uuid4()),
        attempt_count=1,
        claimed_at=now - timedelta(minutes=1),
        admitted_format=admitted_format,
        canonical_kind=canonical_kind,
        admitted_media_type=media_type,
        route=route,
        admitted_byte_size=64,
        admitted_content_sha256=content_sha256,
        admitted_inspection_hash="b" * 64,
        scanner_name="ClamAV",
        scanner_definitions_version="test-definitions",
        review_flags=(),
        source_object_key=object_key,
        source_object_version_id="version-1",
        source_reference_hash=sha256(object_key.encode("ascii")).hexdigest(),
        object_stored_at=now,
    )
    return identity, operation, now


class WebCommonMaterialAgentBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        backend = Path(__file__).parents[1]
        cls.store_source = (backend / "case_api" / "web_common_material_upload_postgres.py").read_text(
            encoding="utf-8"
        )
        cls.migration = (backend / "migrations" / "0041_web_common_material_admission.sql").read_text(
            encoding="utf-8"
        )

    def test_docx_xlsx_completion_bridges_into_the_existing_planning_projection(self) -> None:
        self.assertIn("INSERT INTO case_agent_material_objects", self.store_source)
        self.assertIn("case-materials/v1/", self.migration)
        material_object_id = str(uuid4())
        content_sha256 = "c" * 64
        connection = _PlanningConnection(material_object_id, content_sha256)
        projected = _read_material_objects(
            connection,
            firm_id=str(uuid4()),
            matter_id=str(uuid4()),
        )
        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0].ref_id, f"material-object:{material_object_id}")
        self.assertEqual(projected[0].content_hash, content_sha256)

    def test_actual_docx_registration_is_enumerated_by_the_existing_planner(self) -> None:
        identity, operation, now = _registration_fixture(CommonMaterialFormat.DOCX)
        connection = _RegistrationConnection(operation)
        store = PostgresCommonMaterialUploadStore(
            "postgresql://test.invalid/lawcase",
            connection_factory=lambda: connection,
        )
        completed = store.complete_registration(identity=identity, operation=operation, now=now)
        self.assertEqual(completed.agent_source_ref, f"material-object:{operation.material_object_id}")
        self.assertIsNotNone(connection.executable_material)
        projected = _read_material_objects(
            _CapturedMaterialPlanningConnection(connection.executable_material),
            firm_id=operation.firm_id,
            matter_id=operation.matter_id,
        )
        self.assertEqual(projected[0].ref_id, completed.agent_source_ref)

    def test_non_whitelisted_documents_are_truthfully_pending_and_images_bind_to_0038(self) -> None:
        self.assertIn("INGESTED_PENDING_ADAPTER", self.migration)
        self.assertIn("INSERT INTO evidence_original_files", self.store_source)
        self.assertIn("INSERT INTO evidence_pages", self.store_source)
        self.assertIn("INSERT INTO web_evidence_native_image_source_objects", self.store_source)
        self.assertIn("original-images/v1/", self.migration)
        evidence_page_id, evidence_file_id = str(uuid4()), str(uuid4())
        projected = _read_evidence_page_objects(
            _EvidencePlanningConnection(evidence_page_id, evidence_file_id),
            firm_id=str(uuid4()),
            matter_id=str(uuid4()),
            matter_version=9,
        )
        self.assertEqual(projected[0].ref_id, f"evidence-page:{evidence_page_id}")
        self.assertEqual(projected[0].status.value, "REVIEW_REQUIRED")

    def test_native_uuid_evidence_rows_have_a_stable_planning_hash(self) -> None:
        evidence_page_id, evidence_file_id = uuid4(), uuid4()
        projected = _read_evidence_page_objects(
            _EvidencePlanningConnection(evidence_page_id, evidence_file_id),
            firm_id=str(uuid4()),
            matter_id=str(uuid4()),
            matter_version=9,
        )
        self.assertEqual(projected[0].ref_id, f"evidence-page:{evidence_page_id}")
        self.assertEqual(len(projected[0].content_hash), 64)

    def test_actual_png_registration_is_enumerated_as_a_visual_evidence_page(self) -> None:
        identity, operation, now = _registration_fixture(CommonMaterialFormat.PNG)
        connection = _RegistrationConnection(operation)
        store = PostgresCommonMaterialUploadStore(
            "postgresql://test.invalid/lawcase",
            connection_factory=lambda: connection,
        )
        completed = store.complete_registration(identity=identity, operation=operation, now=now)
        self.assertIsNotNone(connection.evidence_page)
        projected = _read_evidence_page_objects(
            _EvidencePlanningConnection(
                str(connection.evidence_page["evidence_page_id"]),
                str(connection.evidence_page["evidence_file_id"]),
            ),
            firm_id=operation.firm_id,
            matter_id=operation.matter_id,
            matter_version=completed.result_matter_version,
        )
        self.assertEqual(projected[0].ref_id, completed.agent_source_ref)


if __name__ == "__main__":
    unittest.main()
