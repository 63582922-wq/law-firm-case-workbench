from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4
from zipfile import ZipFile

from case_kernel.submission_bundle_compiler import (
    SubmissionArtifactBinding,
    SubmissionBundleCompilationBlocked,
    SubmissionBundleDescriptor,
    SubmissionDependency,
    compile_submission_bundle,
    verify_submission_bundle,
)


def synthetic_pdf(label: str) -> bytes:
    return (
        b"%PDF-1.4\n"
        + f"% synthetic {label}\n".encode("utf-8")
        + b"1 0 obj <<>> endobj\ntrailer <<>>\n%%EOF\n"
    )


class SubmissionBundleCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payloads = {
            "01_民事答辩状.pdf": synthetic_pdf("defence"),
            "02_证据目录.pdf": synthetic_pdf("index"),
            "03_相关微信流水（红框标识）.pdf": synthetic_pdf("evidence"),
            "04_利息测算表.pdf": synthetic_pdf("interest"),
        }
        self.components = tuple(
            self._component(sequence, kind, filename, payload)
            for sequence, (kind, filename, payload) in enumerate(
                (
                    ("DEFENCE_STATEMENT", "01_民事答辩状.pdf", self.payloads["01_民事答辩状.pdf"]),
                    ("EVIDENCE_INDEX", "02_证据目录.pdf", self.payloads["02_证据目录.pdf"]),
                    ("EVIDENCE_MATERIAL", "03_相关微信流水（红框标识）.pdf", self.payloads["03_相关微信流水（红框标识）.pdf"]),
                    ("INTEREST_CALCULATION", "04_利息测算表.pdf", self.payloads["04_利息测算表.pdf"]),
                ),
                start=1,
            )
        )
        self.descriptor = SubmissionBundleDescriptor(
            bundle_id=str(uuid4()),
            matter_id=str(uuid4()),
            matter_version=23,
            export_profile="COURT_PDF_ONLY_V1",
            currency="CNY",
            approved_input_hash="9" * 64,
            required_document_kinds=(
                "DEFENCE_STATEMENT",
                "EVIDENCE_INDEX",
                "EVIDENCE_MATERIAL",
                "INTEREST_CALCULATION",
            ),
            required_dependency_kinds=(
                "EVIDENCE_MANIFEST",
                "LEGAL_RULE_BUNDLE",
                "CALCULATION_RUN",
                "FINAL_TEXT_APPROVAL",
            ),
            dependencies=tuple(
                SubmissionDependency(kind, str(uuid4()), character * 64)
                for kind, character in (
                    ("EVIDENCE_MANIFEST", "a"),
                    ("LEGAL_RULE_BUNDLE", "b"),
                    ("CALCULATION_RUN", "c"),
                    ("FINAL_TEXT_APPROVAL", "d"),
                )
            ),
            approved_by=str(uuid4()),
            approved_at=datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc),
            approval_hash="e" * 64,
        )

    def _component(
        self,
        sequence: int,
        document_kind: str,
        filename: str,
        payload: bytes,
    ) -> SubmissionArtifactBinding:
        digest = sha256(payload).hexdigest()
        return SubmissionArtifactBinding(
            component_id=str(uuid4()),
            sequence=sequence,
            document_kind=document_kind,
            court_filename=filename,
            media_type="application/pdf",
            object_key=f"{digest[:2]}/{digest[2:4]}/{digest}.lca",
            artifact_sha256=digest,
            byte_size=len(payload),
            approval_hash=sha256(f"approval:{filename}".encode()).hexdigest(),
        )

    def _reader(self, _object_key: str, expected_sha256: str) -> bytes:
        return next(
            payload
            for payload in self.payloads.values()
            if sha256(payload).hexdigest() == expected_sha256
        )

    def test_compiles_only_court_files_and_keeps_internal_manifest_outside_zip(self) -> None:
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            result = compile_submission_bundle(
                self.descriptor,
                self.components,
                artifact_reader=self._reader,
                output_directory=output,
            )
            verification = verify_submission_bundle(result)
            self.assertTrue(verification.verified)
            self.assertEqual(verification.component_count, 4)
            with ZipFile(result.court_zip_path) as archive:
                self.assertEqual(archive.namelist(), list(self.payloads))
                self.assertNotIn("提交包内部清单.json", archive.namelist())
                for name, payload in self.payloads.items():
                    self.assertEqual(archive.read(name), payload)
            manifest = json.loads(result.internal_manifest_path.read_text(encoding="utf-8"))
            self.assertFalse(manifest["court_zip_contains_internal_metadata"])
            self.assertEqual(manifest["bundle"]["currency"], "CNY")
            self.assertEqual(manifest["bundle"]["input_hash"], result.input_hash)

    def test_same_approved_inputs_compile_to_identical_zip_and_manifest(self) -> None:
        with TemporaryDirectory() as first, TemporaryDirectory() as second:
            one = compile_submission_bundle(
                self.descriptor,
                self.components,
                artifact_reader=self._reader,
                output_directory=Path(first) / "output",
            )
            two = compile_submission_bundle(
                self.descriptor,
                tuple(reversed(self.components)),
                artifact_reader=self._reader,
                output_directory=Path(second) / "output",
            )
            self.assertEqual(one.input_hash, two.input_hash)
            self.assertEqual(one.court_zip_sha256, two.court_zip_sha256)
            self.assertEqual(one.internal_manifest_sha256, two.internal_manifest_sha256)

    def test_blocks_missing_required_document_or_dependency(self) -> None:
        with TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(SubmissionBundleCompilationBlocked, "missing required documents"):
                compile_submission_bundle(
                    self.descriptor,
                    self.components[:-1],
                    artifact_reader=self._reader,
                    output_directory=Path(temporary) / "documents",
                )
            with self.assertRaisesRegex(SubmissionBundleCompilationBlocked, "missing approved dependencies"):
                compile_submission_bundle(
                    replace(self.descriptor, dependencies=self.descriptor.dependencies[:-1]),
                    self.components,
                    artifact_reader=self._reader,
                    output_directory=Path(temporary) / "dependencies",
                )

    def test_blocks_ambiguous_version_names_paths_non_pdf_and_internal_files(self) -> None:
        unsafe = (
            replace(self.components[0], court_filename="01_民事答辩状最终版.pdf"),
            replace(self.components[0], court_filename="../民事答辩状.pdf"),
            replace(self.components[0], court_filename="01_民事答辩状.docx", media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            replace(self.components[0], audience="INTERNAL_ONLY"),
        )
        for index, component in enumerate(unsafe):
            with self.subTest(index=index), TemporaryDirectory() as temporary:
                candidate = (component, *self.components[1:])
                with self.assertRaises(SubmissionBundleCompilationBlocked):
                    compile_submission_bundle(
                        self.descriptor,
                        candidate,
                        artifact_reader=self._reader,
                        output_directory=Path(temporary) / "output",
                    )

    def test_blocks_changed_source_bytes_and_tampered_zip(self) -> None:
        with TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(SubmissionBundleCompilationBlocked, "hash authentication"):
                compile_submission_bundle(
                    self.descriptor,
                    self.components,
                    artifact_reader=lambda _key, _hash: synthetic_pdf("changed"),
                    output_directory=Path(temporary) / "changed",
                )
            result = compile_submission_bundle(
                self.descriptor,
                self.components,
                artifact_reader=self._reader,
                output_directory=Path(temporary) / "valid",
            )
            result.court_zip_path.write_bytes(result.court_zip_path.read_bytes() + b"tamper")
            with self.assertRaisesRegex(SubmissionBundleCompilationBlocked, "changed after compilation"):
                verify_submission_bundle(result)

    def test_output_must_be_empty_and_existing_files_are_not_overwritten(self) -> None:
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            output.mkdir()
            (output / "法院提交材料.zip").write_bytes(b"preserve")
            with self.assertRaisesRegex(SubmissionBundleCompilationBlocked, "must be empty"):
                compile_submission_bundle(
                    self.descriptor,
                    self.components,
                    artifact_reader=self._reader,
                    output_directory=output,
                )
            self.assertEqual((output / "法院提交材料.zip").read_bytes(), b"preserve")


if __name__ == "__main__":
    unittest.main()
