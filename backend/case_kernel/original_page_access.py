"""Short-lived, one-use local rendering of a registered original PDF page.

The browser never receives the selected folder path or the complete source PDF.
The service resolves a hash-bound file only inside a current OS-authorized case
folder, renders one page to PNG in a temporary directory, verifies that the
source did not change, and returns the PNG only to a loopback client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from ipaddress import ip_address
from pathlib import Path
import shutil
import struct
import subprocess
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
from threading import Lock
from uuid import UUID, uuid4

from pypdf import PdfReader, PdfWriter

from .local_access_grants import LocalFolderGrantRegistry, LocalSessionProof
from .managed_artifact_store import LocalEncryptedArtifactStore, ManagedArtifactBlocked
from .models import Actor, Role


class OriginalPageAccessBlocked(PermissionError):
    """The requested original page cannot be safely exposed."""


@dataclass(frozen=True)
class OriginalPageLocator:
    firm_id: str
    matter_id: str
    evidence_page_id: str
    evidence_file_id: str
    original_label: str
    original_file_sha256: str
    byte_size: int
    media_type: str
    page_count: int
    page_number: int
    normalized_pdf_object_key: str | None = None
    normalized_pdf_sha256: str | None = None


@dataclass(frozen=True)
class IssuedOriginalPageAccess:
    grant_id: str
    access_token: str = field(repr=False, compare=False)
    evidence_page_id: str
    expires_at: datetime


@dataclass(frozen=True)
class OriginalPageDelivery:
    evidence_page_id: str
    media_type: str
    file_name: str
    content_sha256: str
    width: int
    height: int
    content: bytes = field(repr=False, compare=False)


@dataclass(frozen=True)
class _AccessRecord:
    grant_id: str
    token_hash: str
    actor_id: str
    firm_id: str
    matter_id: str
    session_id: str
    folder_grant_id: str
    source_relative_path: str
    locator: OriginalPageLocator
    expires_at: datetime


_READ_ROLES = frozenset(
    {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER}
)


class OriginalPageAccessBroker:
    def __init__(
        self,
        *,
        folder_grants: LocalFolderGrantRegistry,
        preview_ttl: timedelta = timedelta(seconds=60),
        rendered_dpi: int = 144,
        max_source_bytes: int = 512 * 1024 * 1024,
        max_rendered_bytes: int = 30 * 1024 * 1024,
        max_outstanding: int = 1_000,
        artifact_store: LocalEncryptedArtifactStore | None = None,
    ) -> None:
        if preview_ttl <= timedelta(0) or preview_ttl > timedelta(minutes=2):
            raise ValueError("preview_ttl must be between 1 second and 2 minutes")
        if rendered_dpi < 72 or rendered_dpi > 200:
            raise ValueError("rendered_dpi must be between 72 and 200")
        if max_source_bytes < 1 or max_rendered_bytes < 1:
            raise ValueError("original preview byte limits must be positive")
        if max_outstanding < 1 or max_outstanding > 100_000:
            raise ValueError("max_outstanding is outside the supported boundary")
        self._folder_grants = folder_grants
        self._preview_ttl = preview_ttl
        self._rendered_dpi = rendered_dpi
        self._max_source_bytes = max_source_bytes
        self._max_rendered_bytes = max_rendered_bytes
        self._max_outstanding = max_outstanding
        self._artifact_store = artifact_store
        self._records: dict[str, _AccessRecord] = {}
        self._lock = Lock()

    @property
    def folder_grants(self) -> LocalFolderGrantRegistry:
        return self._folder_grants

    @property
    def artifact_store(self) -> LocalEncryptedArtifactStore | None:
        return self._artifact_store

    def issue(
        self,
        *,
        locator: OriginalPageLocator,
        folder_grant_id: str,
        actor: Actor,
        session: LocalSessionProof,
        now: datetime | None = None,
    ) -> IssuedOriginalPageAccess:
        current = _aware_now(now)
        _validate_identity(actor, session=session, matter_id=locator.matter_id, now=current)
        _validate_locator(locator, actor=actor, max_source_bytes=self._max_source_bytes)
        source = self._folder_grants.resolve_registered_original(
            grant_id=folder_grant_id,
            actor=actor,
            matter_id=locator.matter_id,
            session=session,
            expected_sha256=locator.original_file_sha256,
            expected_byte_size=locator.byte_size,
            original_label=locator.original_label,
            now=current,
        )
        expires_at = min(current + self._preview_ttl, session.expires_at)
        if expires_at <= current:
            raise OriginalPageAccessBlocked("the authenticated session expires before preview access can be issued")
        token = token_urlsafe(32)
        token_hash = sha256(token.encode("ascii")).hexdigest()
        record = _AccessRecord(
            grant_id=str(uuid4()),
            token_hash=token_hash,
            actor_id=actor.actor_id,
            firm_id=actor.firm_id,
            matter_id=locator.matter_id,
            session_id=session.session_id,
            folder_grant_id=folder_grant_id,
            source_relative_path=source.relative_path,
            locator=locator,
            expires_at=expires_at,
        )
        with self._lock:
            self._remove_expired(current)
            if len(self._records) >= self._max_outstanding:
                raise OriginalPageAccessBlocked("too many outstanding original-page preview grants")
            self._records[token_hash] = record
        return IssuedOriginalPageAccess(
            grant_id=record.grant_id,
            access_token=token,
            evidence_page_id=locator.evidence_page_id,
            expires_at=expires_at,
        )

    def deliver(
        self,
        *,
        access_token: str,
        actor: Actor,
        matter_id: str,
        evidence_page_id: str,
        session: LocalSessionProof,
        client_ip: str,
        now: datetime | None = None,
    ) -> OriginalPageDelivery:
        current = _aware_now(now)
        _validate_loopback(client_ip)
        _validate_identity(actor, session=session, matter_id=matter_id, now=current)
        if not 20 <= len(access_token) <= 200 or not access_token.isascii():
            raise OriginalPageAccessBlocked("original-page access token is invalid")
        token_hash = sha256(access_token.encode("ascii")).hexdigest()
        with self._lock:
            self._remove_expired(current)
            record = self._records.get(token_hash)
            if record is None:
                raise OriginalPageAccessBlocked("original-page access token is missing, expired, or already used")
            if (
                record.actor_id != actor.actor_id
                or record.firm_id != actor.firm_id
                or record.matter_id != matter_id
                or record.session_id != session.session_id
                or record.locator.evidence_page_id != evidence_page_id
            ):
                raise OriginalPageAccessBlocked("original-page access token is outside the authenticated scope")
            del self._records[token_hash]
        source = self._folder_grants.resolve_registered_original(
            grant_id=record.folder_grant_id,
            actor=actor,
            matter_id=matter_id,
            session=session,
            expected_sha256=record.locator.original_file_sha256,
            expected_byte_size=record.locator.byte_size,
            original_label=record.locator.original_label,
            now=current,
        )
        if source.relative_path != record.source_relative_path:
            raise OriginalPageAccessBlocked("the original file binding changed after preview authorization")
        if record.locator.normalized_pdf_object_key is None:
            content, width, height = _render_pdf_page(
                source.path,
                page_number=record.locator.page_number,
                expected_page_count=record.locator.page_count,
                expected_sha256=record.locator.original_file_sha256,
                rendered_dpi=self._rendered_dpi,
                max_rendered_bytes=self._max_rendered_bytes,
            )
        else:
            content, width, height = self._render_normalized_page(record.locator)
        if _file_sha256(source.path) != record.locator.original_file_sha256:
            raise OriginalPageAccessBlocked("the original source changed while its page was rendered")
        content_hash = sha256(content).hexdigest()
        return OriginalPageDelivery(
            evidence_page_id=evidence_page_id,
            media_type="image/png",
            file_name=f"original-page-{record.locator.page_number}.png",
            content_sha256=content_hash,
            width=width,
            height=height,
            content=content,
        )

    def _render_normalized_page(self, locator: OriginalPageLocator) -> tuple[bytes, int, int]:
        if self._artifact_store is None or locator.normalized_pdf_sha256 is None:
            raise OriginalPageAccessBlocked("normalized evidence preview requires the encrypted artifact store")
        try:
            normalized_content = self._artifact_store.read_bytes(
                locator.normalized_pdf_object_key,
                expected_sha256=locator.normalized_pdf_sha256,
            )
        except ManagedArtifactBlocked as error:
            raise OriginalPageAccessBlocked("normalized evidence representation is unavailable or invalid") from error
        with TemporaryDirectory(prefix="normalized-evidence-preview-") as temporary:
            normalized_pdf = Path(temporary) / "normalized.pdf"
            with normalized_pdf.open("xb") as output:
                output.write(normalized_content)
                output.flush()
            normalized_pdf.chmod(0o600)
            return _render_pdf_page(
                normalized_pdf,
                page_number=locator.page_number,
                expected_page_count=locator.page_count,
                expected_sha256=locator.normalized_pdf_sha256,
                rendered_dpi=self._rendered_dpi,
                max_rendered_bytes=self._max_rendered_bytes,
            )

    def revoke_session(self, session_id: str) -> int:
        with self._lock:
            matches = [key for key, record in self._records.items() if record.session_id == session_id]
            for key in matches:
                del self._records[key]
        return len(matches)

    def _remove_expired(self, current: datetime) -> None:
        for token_hash in [
            token_hash for token_hash, record in self._records.items() if record.expires_at <= current
        ]:
            del self._records[token_hash]


def _render_pdf_page(
    path: Path,
    *,
    page_number: int,
    expected_page_count: int,
    expected_sha256: str,
    rendered_dpi: int,
    max_rendered_bytes: int,
) -> tuple[bytes, int, int]:
    if shutil.which("pdftoppm") is None:
        raise OriginalPageAccessBlocked("pdftoppm is required for original-page preview")
    before_hash = _file_sha256(path)
    if before_hash != expected_sha256:
        raise OriginalPageAccessBlocked("the original PDF hash differs from the registered evidence")
    try:
        reader = PdfReader(str(path))
    except Exception as error:
        raise OriginalPageAccessBlocked("the registered original is not a readable PDF") from error
    if reader.is_encrypted:
        raise OriginalPageAccessBlocked("encrypted source PDFs require a separate approved unlock workflow")
    if len(reader.pages) != expected_page_count:
        raise OriginalPageAccessBlocked("the original PDF page count differs from the registered evidence")
    if page_number < 1 or page_number > len(reader.pages):
        raise OriginalPageAccessBlocked("the requested page is outside the registered original")
    with TemporaryDirectory(prefix="original-page-preview-") as temporary:
        root = Path(temporary)
        one_page = root / "source-page.pdf"
        writer = PdfWriter()
        writer.add_page(reader.pages[page_number - 1])
        with one_page.open("wb") as destination:
            writer.write(destination)
        prefix = root / "rendered-page"
        try:
            completed = subprocess.run(
                [
                    "pdftoppm",
                    "-f",
                    "1",
                    "-l",
                    "1",
                    "-singlefile",
                    "-r",
                    str(rendered_dpi),
                    "-png",
                    str(one_page),
                    str(prefix),
                ],
                check=False,
                capture_output=True,
                timeout=45,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise OriginalPageAccessBlocked("original-page rendering failed safely") from error
        rendered = prefix.with_suffix(".png")
        if completed.returncode != 0 or not rendered.is_file():
            raise OriginalPageAccessBlocked("original-page rendering failed safely")
        if rendered.stat().st_size < 24 or rendered.stat().st_size > max_rendered_bytes:
            raise OriginalPageAccessBlocked("rendered original page is empty or exceeds the preview limit")
        content = rendered.read_bytes()
    if _file_sha256(path) != before_hash:
        raise OriginalPageAccessBlocked("the original PDF changed while its page was rendered")
    width, height = _png_dimensions(content)
    if width > 12_000 or height > 12_000 or width * height > 80_000_000:
        raise OriginalPageAccessBlocked("rendered original page dimensions exceed the preview limit")
    return content, width, height


def _png_dimensions(content: bytes) -> tuple[int, int]:
    if content[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
        raise OriginalPageAccessBlocked("renderer returned a non-PNG original page")
    width, height = struct.unpack(">II", content[16:24])
    if width < 1 or height < 1:
        raise OriginalPageAccessBlocked("renderer returned invalid original-page dimensions")
    return width, height


def _validate_locator(locator: OriginalPageLocator, *, actor: Actor, max_source_bytes: int) -> None:
    for label, value in (
        ("firm_id", locator.firm_id),
        ("matter_id", locator.matter_id),
        ("evidence_page_id", locator.evidence_page_id),
        ("evidence_file_id", locator.evidence_file_id),
    ):
        try:
            UUID(value)
        except (TypeError, ValueError) as error:
            raise OriginalPageAccessBlocked(f"original-page preview requires UUID {label}") from error
    if locator.firm_id != actor.firm_id:
        raise OriginalPageAccessBlocked("the original page is outside the authenticated firm")
    has_representation = locator.normalized_pdf_object_key is not None or locator.normalized_pdf_sha256 is not None
    if locator.media_type == "application/pdf":
        if has_representation:
            raise OriginalPageAccessBlocked("a PDF original must not use a normalized representation")
    elif locator.normalized_pdf_object_key is None or locator.normalized_pdf_sha256 is None:
        raise OriginalPageAccessBlocked("non-PDF evidence preview requires a verified normalized representation")
    if (
        len(locator.original_file_sha256) != 64
        or any(character not in "0123456789abcdef" for character in locator.original_file_sha256)
    ):
        raise OriginalPageAccessBlocked("the registered original SHA-256 is invalid")
    if locator.normalized_pdf_sha256 is not None and (
        len(locator.normalized_pdf_sha256) != 64
        or any(character not in "0123456789abcdef" for character in locator.normalized_pdf_sha256)
    ):
        raise OriginalPageAccessBlocked("the normalized PDF SHA-256 is invalid")
    if locator.normalized_pdf_object_key is not None:
        expected_object_key = (
            f"{locator.normalized_pdf_sha256[:2]}/{locator.normalized_pdf_sha256[2:4]}/"
            f"{locator.normalized_pdf_sha256}.lca"
        )
        if locator.normalized_pdf_object_key != expected_object_key:
            raise OriginalPageAccessBlocked("the normalized PDF object key is invalid")
    if locator.byte_size < 1 or locator.byte_size > max_source_bytes:
        raise OriginalPageAccessBlocked("the registered original exceeds the preview source limit")
    if locator.page_count < 1 or locator.page_number < 1 or locator.page_number > locator.page_count:
        raise OriginalPageAccessBlocked("the registered original page metadata is invalid")


def _validate_identity(
    actor: Actor,
    *,
    session: LocalSessionProof,
    matter_id: str,
    now: datetime,
) -> None:
    for label, value in (
        ("actor_id", actor.actor_id),
        ("firm_id", actor.firm_id),
        ("matter_id", matter_id),
        ("session_id", session.session_id),
    ):
        try:
            UUID(value)
        except (TypeError, ValueError) as error:
            raise OriginalPageAccessBlocked(f"original-page access requires UUID {label}") from error
    if not actor.roles.intersection(_READ_ROLES) or actor.roles.intersection(
        {Role.FIRM_ADMIN, Role.SYSTEM_WORKER}
    ):
        raise OriginalPageAccessBlocked("the current role cannot preview original evidence pages")
    if session.authentication_method != "OS_BOUND_LOCAL_SESSION":
        raise OriginalPageAccessBlocked("original-page access requires an OS-bound local session")
    if session.authenticated_at.tzinfo is None or session.expires_at.tzinfo is None:
        raise OriginalPageAccessBlocked("original-page access session timestamps must be timezone-aware")
    if session.authenticated_at > now or session.expires_at <= now:
        raise OriginalPageAccessBlocked("the original-page access session is not currently valid")


def _validate_loopback(client_ip: str) -> None:
    try:
        address = ip_address(client_ip)
    except ValueError as error:
        raise OriginalPageAccessBlocked("original-page delivery requires a valid client address") from error
    if not address.is_loopback:
        raise OriginalPageAccessBlocked("original-page delivery is restricted to the local device")


def _aware_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise OriginalPageAccessBlocked("original-page access time must be timezone-aware")
    return current


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()
