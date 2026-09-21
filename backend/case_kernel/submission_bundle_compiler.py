"""Deterministic, minimal court-submission ZIP compiler.

The court ZIP contains only lawyer-approved court-facing files.  Technical
lineage and hashes are written to a separate internal manifest so that a court
upload is not polluted with system descriptions or audit metadata.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
from tempfile import TemporaryDirectory
from typing import Callable
import unicodedata
from uuid import UUID
from zipfile import BadZipFile, ZIP_STORED, ZipFile, ZipInfo


class SubmissionBundleCompilationBlocked(ValueError):
    """The bundle is incomplete, stale, unsafe, or not court-facing."""


_ALLOWED_MEDIA_TYPES = {
    "application/pdf": ".pdf",
}
_AMBIGUOUS_VERSION_MARKER = re.compile(
    r"(?:最新|最终|修订|终稿|定稿|第\s*\d+\s*版|\bv\d+(?:\.\d+)*\b|\bfinal\b)",
    re.IGNORECASE,
)
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_FIXED_ZIP_TIME = (2020, 1, 1, 0, 0, 0)


@dataclass(frozen=True)
class SubmissionDependency:
    dependency_kind: str
    object_id: str
    object_sha256: str


@dataclass(frozen=True)
class SubmissionArtifactBinding:
    component_id: str
    sequence: int
    document_kind: str
    court_filename: str
    media_type: str
    object_key: str
    artifact_sha256: str
    byte_size: int
    approval_hash: str
    status: str = "APPROVED"
    audience: str = "COURT_SUBMISSION"


@dataclass(frozen=True)
class SubmissionBundleDescriptor:
    bundle_id: str
    matter_id: str
    matter_version: int
    export_profile: str
    currency: str
    approved_input_hash: str
    required_document_kinds: tuple[str, ...]
    required_dependency_kinds: tuple[str, ...]
    dependencies: tuple[SubmissionDependency, ...]
    approved_by: str
    approved_at: datetime
    approval_hash: str


@dataclass(frozen=True)
class SubmissionBundleCompilationResult:
    bundle_id: str
    input_hash: str
    court_zip_path: Path
    court_zip_sha256: str
    court_zip_bytes: int
    internal_manifest_path: Path
    internal_manifest_sha256: str
    component_count: int


@dataclass(frozen=True)
class SubmissionBundleVerification:
    verified: bool
    component_count: int
    court_zip_sha256: str
    internal_manifest_sha256: str
    input_hash: str


def compile_submission_bundle(
    descriptor: SubmissionBundleDescriptor,
    components: tuple[SubmissionArtifactBinding, ...],
    *,
    artifact_reader: Callable[[str, str], bytes],
    output_directory: str | Path,
    max_component_bytes: int = 128 * 1024 * 1024,
    max_bundle_bytes: int = 256 * 1024 * 1024,
) -> SubmissionBundleCompilationResult:
    """Compile a deterministic PDF-only court ZIP plus an internal manifest."""

    normalized = _validate_inputs(
        descriptor,
        components,
        max_component_bytes=max_component_bytes,
        max_bundle_bytes=max_bundle_bytes,
    )
    output = _prepare_empty_output(output_directory)
    payloads: list[tuple[SubmissionArtifactBinding, bytes]] = []
    total_bytes = 0
    for component in normalized:
        plaintext = artifact_reader(component.object_key, component.artifact_sha256)
        if not isinstance(plaintext, bytes):
            raise SubmissionBundleCompilationBlocked("artifact reader must return bytes")
        if len(plaintext) != component.byte_size:
            raise SubmissionBundleCompilationBlocked(
                f"artifact byte size changed for component {component.component_id}"
            )
        if sha256(plaintext).hexdigest() != component.artifact_sha256:
            raise SubmissionBundleCompilationBlocked(
                f"artifact hash authentication failed for component {component.component_id}"
            )
        _validate_pdf_bytes(plaintext, component.court_filename)
        total_bytes += len(plaintext)
        if total_bytes > max_bundle_bytes:
            raise SubmissionBundleCompilationBlocked("court bundle exceeds configured byte limit")
        payloads.append((component, plaintext))

    input_payload = _input_payload(descriptor, normalized)
    compiler_payload_hash = _canonical_hash(input_payload)
    input_hash = descriptor.approved_input_hash
    manifest = {
        "schema_version": "court-submission-internal-manifest-v1",
        "bundle": {
            "bundle_id": descriptor.bundle_id,
            "matter_id": descriptor.matter_id,
            "matter_version": descriptor.matter_version,
            "export_profile": descriptor.export_profile,
            "currency": descriptor.currency,
            "approved_by": descriptor.approved_by,
            "approved_at": descriptor.approved_at.isoformat(),
            "approval_hash": descriptor.approval_hash,
            "input_hash": input_hash,
            "compiler_payload_hash": compiler_payload_hash,
        },
        "dependencies": [asdict(item) for item in _sorted_dependencies(descriptor.dependencies)],
        "court_files": [
            {
                "component_id": component.component_id,
                "sequence": component.sequence,
                "document_kind": component.document_kind,
                "court_filename": component.court_filename,
                "media_type": component.media_type,
                "artifact_sha256": component.artifact_sha256,
                "byte_size": component.byte_size,
                "approval_hash": component.approval_hash,
            }
            for component in normalized
        ],
        "court_zip_contains_internal_metadata": False,
    }
    manifest_bytes = _canonical_json_bytes(manifest)
    manifest_path = output / "提交包内部清单.json"
    _write_atomically(manifest_path, manifest_bytes)

    zip_path = output / "法院提交材料.zip"
    temporary_zip = output / ".法院提交材料.zip.part"
    try:
        with ZipFile(temporary_zip, "x", compression=ZIP_STORED, allowZip64=True) as archive:
            for component, plaintext in payloads:
                info = ZipInfo(component.court_filename, date_time=_FIXED_ZIP_TIME)
                info.compress_type = ZIP_STORED
                info.create_system = 3
                info.external_attr = 0o600 << 16
                info.flag_bits |= 0x800
                archive.writestr(info, plaintext)
        _replace_new_file(temporary_zip, zip_path)
    finally:
        temporary_zip.unlink(missing_ok=True)

    return SubmissionBundleCompilationResult(
        bundle_id=descriptor.bundle_id,
        input_hash=input_hash,
        court_zip_path=zip_path,
        court_zip_sha256=_file_sha256(zip_path),
        court_zip_bytes=zip_path.stat().st_size,
        internal_manifest_path=manifest_path,
        internal_manifest_sha256=sha256(manifest_bytes).hexdigest(),
        component_count=len(normalized),
    )


def verify_submission_bundle(
    result: SubmissionBundleCompilationResult,
) -> SubmissionBundleVerification:
    """Verify the ZIP has exactly the files bound by the internal manifest."""

    court_zip_bytes = result.court_zip_path.read_bytes()
    manifest_bytes = result.internal_manifest_path.read_bytes()
    return verify_submission_export_bytes(
        court_zip_bytes=court_zip_bytes,
        manifest_bytes=manifest_bytes,
        expected_court_zip_sha256=result.court_zip_sha256,
        expected_manifest_sha256=result.internal_manifest_sha256,
        expected_bundle_id=result.bundle_id,
        expected_input_hash=result.input_hash,
        expected_component_count=result.component_count,
    )


def verify_submission_export_bytes(
    *,
    court_zip_bytes: bytes,
    manifest_bytes: bytes,
    expected_court_zip_sha256: str,
    expected_manifest_sha256: str,
    expected_bundle_id: str,
    expected_input_hash: str,
    expected_component_count: int,
) -> SubmissionBundleVerification:
    """Re-verify stored export bytes before they are registered as court-ready."""

    if sha256(court_zip_bytes).hexdigest() != expected_court_zip_sha256:
        raise SubmissionBundleCompilationBlocked("court ZIP changed after compilation")
    if sha256(manifest_bytes).hexdigest() != expected_manifest_sha256:
        raise SubmissionBundleCompilationBlocked("internal submission manifest changed after compilation")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SubmissionBundleCompilationBlocked("internal submission manifest is invalid") from error
    if manifest.get("schema_version") != "court-submission-internal-manifest-v1":
        raise SubmissionBundleCompilationBlocked("unsupported internal submission manifest")
    expected = manifest.get("court_files")
    if not isinstance(expected, list) or not expected:
        raise SubmissionBundleCompilationBlocked("internal manifest has no court files")
    expected_by_name = {item["court_filename"]: item for item in expected}
    if len(expected_by_name) != len(expected):
        raise SubmissionBundleCompilationBlocked("internal manifest contains duplicate court filenames")
    if len(expected) != expected_component_count:
        raise SubmissionBundleCompilationBlocked("court ZIP component count changed after approval")
    try:
        with ZipFile(BytesIO(court_zip_bytes), "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != set(expected_by_name):
                raise SubmissionBundleCompilationBlocked(
                    "court ZIP contents differ from the internal approved manifest"
                )
            for info in archive.infolist():
                _validate_court_filename(info.filename)
                if info.is_dir() or info.compress_type != ZIP_STORED:
                    raise SubmissionBundleCompilationBlocked("court ZIP contains an unsafe entry")
                content = archive.read(info)
                item = expected_by_name[info.filename]
                if len(content) != item["byte_size"] or sha256(content).hexdigest() != item["artifact_sha256"]:
                    raise SubmissionBundleCompilationBlocked(
                        f"court ZIP file verification failed: {info.filename}"
                    )
                _validate_pdf_bytes(content, info.filename)
    except BadZipFile as error:
        raise SubmissionBundleCompilationBlocked("court ZIP is structurally invalid") from error
    bundle = manifest.get("bundle") or {}
    if bundle.get("bundle_id") != expected_bundle_id or bundle.get("input_hash") != expected_input_hash:
        raise SubmissionBundleCompilationBlocked("compiled result is not bound to its internal manifest")
    if manifest.get("court_zip_contains_internal_metadata") is not False:
        raise SubmissionBundleCompilationBlocked("court ZIP metadata separation is not declared")
    return SubmissionBundleVerification(
        verified=True,
        component_count=len(expected),
        court_zip_sha256=expected_court_zip_sha256,
        internal_manifest_sha256=expected_manifest_sha256,
        input_hash=expected_input_hash,
    )


def _validate_inputs(
    descriptor: SubmissionBundleDescriptor,
    components: tuple[SubmissionArtifactBinding, ...],
    *,
    max_component_bytes: int,
    max_bundle_bytes: int,
) -> tuple[SubmissionArtifactBinding, ...]:
    for field_name, value in (
        ("bundle_id", descriptor.bundle_id),
        ("matter_id", descriptor.matter_id),
        ("approved_by", descriptor.approved_by),
    ):
        _validate_uuid(field_name, value)
    if descriptor.matter_version < 1:
        raise SubmissionBundleCompilationBlocked("matter version must be positive")
    if descriptor.export_profile != "COURT_PDF_ONLY_V1":
        raise SubmissionBundleCompilationBlocked("only the reviewed PDF-only court profile is supported")
    if descriptor.currency != "CNY":
        raise SubmissionBundleCompilationBlocked("court submission monetary currency must be CNY")
    _validate_sha256("bundle approval_hash", descriptor.approval_hash)
    _validate_sha256("approved_input_hash", descriptor.approved_input_hash)
    if descriptor.approved_at.tzinfo is None:
        raise SubmissionBundleCompilationBlocked("bundle approval time must include a timezone")
    if max_component_bytes < 1 or max_bundle_bytes < max_component_bytes:
        raise SubmissionBundleCompilationBlocked("submission byte limits are invalid")
    if not components or len(components) > 100:
        raise SubmissionBundleCompilationBlocked("court bundle must contain 1 to 100 files")
    if not descriptor.required_document_kinds:
        raise SubmissionBundleCompilationBlocked("court profile must declare required document kinds")
    if not descriptor.required_dependency_kinds:
        raise SubmissionBundleCompilationBlocked("court profile must declare required dependency kinds")

    dependencies = _sorted_dependencies(descriptor.dependencies)
    dependency_kinds = {item.dependency_kind for item in dependencies}
    missing_dependencies = sorted(set(descriptor.required_dependency_kinds) - dependency_kinds)
    if missing_dependencies:
        raise SubmissionBundleCompilationBlocked(
            "court bundle is missing approved dependencies: " + ", ".join(missing_dependencies)
        )
    seen_dependencies: set[tuple[str, str]] = set()
    for dependency in dependencies:
        key = (dependency.dependency_kind, dependency.object_id)
        if key in seen_dependencies:
            raise SubmissionBundleCompilationBlocked("court bundle contains a duplicate dependency")
        seen_dependencies.add(key)
        if not dependency.dependency_kind.strip():
            raise SubmissionBundleCompilationBlocked("dependency kind is required")
        _validate_uuid("dependency object_id", dependency.object_id)
        _validate_sha256("dependency object_sha256", dependency.object_sha256)

    normalized = tuple(sorted(components, key=lambda item: (item.sequence, item.component_id)))
    if [item.sequence for item in normalized] != list(range(1, len(normalized) + 1)):
        raise SubmissionBundleCompilationBlocked("court file sequence must be contiguous from 1")
    document_kinds = {item.document_kind for item in normalized}
    missing_kinds = sorted(set(descriptor.required_document_kinds) - document_kinds)
    if missing_kinds:
        raise SubmissionBundleCompilationBlocked(
            "court bundle is missing required documents: " + ", ".join(missing_kinds)
        )
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for component in normalized:
        _validate_uuid("component_id", component.component_id)
        if component.component_id in seen_ids:
            raise SubmissionBundleCompilationBlocked("court bundle contains a duplicate component")
        seen_ids.add(component.component_id)
        if component.status != "APPROVED" or component.audience != "COURT_SUBMISSION":
            raise SubmissionBundleCompilationBlocked(
                "court ZIP accepts only approved court-submission components"
            )
        if not component.document_kind.strip():
            raise SubmissionBundleCompilationBlocked("court document kind is required")
        normalized_name = _validate_court_filename(component.court_filename)
        comparison_name = unicodedata.normalize("NFC", normalized_name).casefold()
        if comparison_name in seen_names:
            raise SubmissionBundleCompilationBlocked("court filenames must be unique")
        seen_names.add(comparison_name)
        expected_extension = _ALLOWED_MEDIA_TYPES.get(component.media_type)
        if expected_extension is None or PurePosixPath(normalized_name).suffix.casefold() != expected_extension:
            raise SubmissionBundleCompilationBlocked("court profile accepts only correctly named PDF files")
        _validate_sha256("artifact_sha256", component.artifact_sha256)
        _validate_sha256("component approval_hash", component.approval_hash)
        if component.byte_size < 1 or component.byte_size > max_component_bytes:
            raise SubmissionBundleCompilationBlocked("court component exceeds configured byte limit")
        if component.object_key != _object_key(component.artifact_sha256):
            raise SubmissionBundleCompilationBlocked("component object key is not content-addressed")
    return normalized


def _input_payload(
    descriptor: SubmissionBundleDescriptor,
    components: tuple[SubmissionArtifactBinding, ...],
) -> dict:
    return {
        "schema_version": "court-submission-compiler-input-v1",
        "bundle_id": descriptor.bundle_id,
        "matter_id": descriptor.matter_id,
        "matter_version": descriptor.matter_version,
        "export_profile": descriptor.export_profile,
        "currency": descriptor.currency,
        "approved_input_hash": descriptor.approved_input_hash,
        "required_document_kinds": sorted(set(descriptor.required_document_kinds)),
        "required_dependency_kinds": sorted(set(descriptor.required_dependency_kinds)),
        "dependencies": [asdict(item) for item in _sorted_dependencies(descriptor.dependencies)],
        "components": [asdict(item) for item in components],
        "approved_by": descriptor.approved_by,
        "approved_at": descriptor.approved_at.isoformat(),
        "approval_hash": descriptor.approval_hash,
    }


def _sorted_dependencies(
    dependencies: tuple[SubmissionDependency, ...],
) -> tuple[SubmissionDependency, ...]:
    return tuple(sorted(dependencies, key=lambda item: (item.dependency_kind, item.object_id)))


def _validate_court_filename(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized or len(normalized.encode("utf-8")) > 180:
        raise SubmissionBundleCompilationBlocked("court filename is empty or too long")
    if _CONTROL_CHARACTER.search(normalized) or _AMBIGUOUS_VERSION_MARKER.search(normalized):
        raise SubmissionBundleCompilationBlocked("court filename contains an unsafe or ambiguous version marker")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or len(path.parts) != 1
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or normalized.startswith(".")
    ):
        raise SubmissionBundleCompilationBlocked("court filename must be a single safe filename")
    return normalized


def _validate_pdf_bytes(content: bytes, filename: str) -> None:
    if len(content) < 8 or not content.startswith(b"%PDF-") or b"%%EOF" not in content[-2048:]:
        raise SubmissionBundleCompilationBlocked(f"court component is not a complete PDF: {filename}")


def _prepare_empty_output(value: str | Path) -> Path:
    output = Path(value).expanduser()
    if output.exists() and output.is_symlink():
        raise SubmissionBundleCompilationBlocked("submission output cannot be a symbolic link")
    output.mkdir(parents=True, mode=0o700, exist_ok=True)
    output = output.resolve(strict=True)
    if not output.is_dir() or any(output.iterdir()):
        raise SubmissionBundleCompilationBlocked("submission output directory must be empty")
    output.chmod(0o700)
    return output


def _write_atomically(path: Path, content: bytes) -> None:
    temporary = path.parent / f".{path.name}.part"
    with temporary.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    try:
        _replace_new_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_new_file(temporary: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise SubmissionBundleCompilationBlocked("submission output already exists")
    os.link(temporary, destination)
    destination.chmod(0o600)
    temporary.unlink()


def _object_key(plaintext_hash: str) -> str:
    return f"{plaintext_hash[:2]}/{plaintext_hash[2:4]}/{plaintext_hash}.lca"


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _canonical_hash(value: object) -> str:
    return sha256(_canonical_json_bytes(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_sha256(label: str, value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise SubmissionBundleCompilationBlocked(f"{label} must be a lowercase SHA-256 value")


def _validate_uuid(label: str, value: str) -> None:
    try:
        UUID(value)
    except (ValueError, TypeError, AttributeError) as error:
        raise SubmissionBundleCompilationBlocked(f"{label} must be a UUID") from error
