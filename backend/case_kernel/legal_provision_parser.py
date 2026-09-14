"""Pinpoint parser for encrypted official legal-text captures.

This parser produces review candidates, not legal approval.  It deliberately
anchors inside the republished private-lending interpretation because the SPC
page also contains many unrelated amended interpretations with identically
numbered articles.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from html.parser import HTMLParser
import json
import re

from .managed_artifact_store import LocalEncryptedArtifactStore
from .official_source_capture import CapturedOfficialSource


class LegalProvisionParseBlocked(ValueError):
    """Captured content cannot support the required pinpoint provisions."""


class LegalProvisionDocumentProjectionBlocked(ValueError):
    """A registered source cannot yield a bounded, pinpoint document extract."""


@dataclass(frozen=True)
class ProvisionCandidate:
    provision_key: str
    provision_label: str
    normalized_text: str
    semantic_sha256: str
    source_locator: str
    required_markers: tuple[str, ...]


@dataclass(frozen=True)
class ParsedLegalProvisionSnapshot:
    source_id: str
    source_url: str
    content_sha256: str
    document_title: str
    version_label: str
    provisions: tuple[ProvisionCandidate, ...]
    parsed_output_hash: str
    review_status: str = "HUMAN_REVIEW_REQUIRED"


@dataclass(frozen=True)
class DocumentLegalSourceProjection:
    """One deterministic, source-bound provision extract for a document.

    ``source_content_sha256`` remains on the enclosing document source.  This
    receipt hashes only the short, reproducible projection that may enter the
    document compiler; it never replaces or truncates the immutable source.
    """

    schema_version: str
    source_id: str
    provision_labels: tuple[str, ...]
    reviewed_text: str
    reviewed_text_sha256: str


_SOURCE_ID = "SPC-PRIVATE-LENDING-2020-SECOND-REVISION"
_TITLE = "最高人民法院关于审理民间借贷案件适用法律若干问题的规定"
_VERSION_MARKERS = ("第一次修正", "第二次修正")
_ARTICLE_RULES = (
    (
        "PRIVATE_LENDING_ARTICLE_24",
        "第二十四条",
        "第二十五条",
        ("没有约定利息", "自然人之间", "约定不明"),
    ),
    (
        "PRIVATE_LENDING_ARTICLE_25",
        "第二十五条",
        "第二十六条",
        ("合同成立时", "一年期贷款市场报价利率四倍", "2019年8月20日"),
    ),
    (
        "PRIVATE_LENDING_ARTICLE_26",
        "第二十六条",
        "第二十七条",
        ("债权凭证载明的借款金额", "预先在本金中扣除利息", "实际出借的金额"),
    ),
    (
        "PRIVATE_LENDING_ARTICLE_27",
        "第二十七条",
        "第二十八条",
        ("前期借款本息结算", "利息计入后期借款本金", "超过部分的利息"),
    ),
    (
        "PRIVATE_LENDING_ARTICLE_28",
        "第二十八条",
        "第二十九条",
        ("逾期利率", "当时一年期贷款市场报价利率", "借期内利率"),
    ),
    (
        "PRIVATE_LENDING_ARTICLE_29",
        "第二十九条",
        "第三十条",
        ("逾期利率", "违约金或者其他费用", "合同成立时一年期贷款市场报价利率四倍"),
    ),
    (
        "PRIVATE_LENDING_ARTICLE_31",
        "第三十一条",
        None,
        ("2020年8月20日之后", "2020年8月19日", "适用起诉时", "利率保护标准"),
    ),
)
_CIVIL_CODE_SOURCE_ID = "CN-CIVIL-CODE-680"
_CIVIL_CODE_TITLE = "中华人民共和国民法典"
_CIVIL_CODE_ARTICLE_RULES = (
    (
        "CIVIL_CODE_ARTICLE_679",
        "第六百七十九条",
        "第六百八十条",
        ("自然人之间", "贷款人提供借款时成立"),
    ),
    (
        "CIVIL_CODE_ARTICLE_680",
        "第六百八十条",
        "第六百八十一条",
        ("禁止高利放贷", "没有约定", "视为没有利息", "约定不明确"),
    ),
)
_DOCUMENT_PROJECTION_SCHEMA_VERSION = "registered-legal-provision-projection-v1"
_MAX_DOCUMENT_PROJECTION_CHARACTERS = 40_000
_VERIFIED_SOURCE_READER_LABEL_PREFIX = (
    "来源定位标签（由律师核验；系统未自动定位到精确条款）："
)
_CHINESE_ARTICLE_LABEL = re.compile(r"第[一二三四五六七八九十百千万零〇]+条")
_FIRST_REVISION_SOURCE_ID = "SPC-PRIVATE-LENDING-2020-FIRST-REVISION"
_FIRST_REVISION_RULES = (
    (
        "PRIVATE_LENDING_FIRST_REVISION_ARTICLE_26",
        "第二十六条",
        "第二十七条",
        ("合同成立时", "一年期贷款市场报价利率四倍", "2019年8月20日"),
    ),
    (
        "PRIVATE_LENDING_FIRST_REVISION_ARTICLE_32",
        "第三十二条",
        None,
        ("借贷行为发生在2019年8月20日之前", "原告起诉时", "一年期贷款市场报价利率四倍"),
    ),
)
_ORIGINAL_2015_SOURCE_ID = "SPC-PRIVATE-LENDING-2015-ORIGINAL"
_ORIGINAL_2015_RULES = (
    (
        "PRIVATE_LENDING_2015_ARTICLE_26",
        "第二十六条",
        "第二十七条",
        ("年利率24%", "年利率36%", "返还已支付"),
    ),
    (
        "PRIVATE_LENDING_2015_ARTICLE_31",
        "第三十一条",
        "第三十二条",
        ("自愿支付", "不当得利", "超过年利率36%"),
    ),
)


def parse_private_lending_second_revision(
    *,
    capture: CapturedOfficialSource,
    artifact_store: LocalEncryptedArtifactStore,
) -> ParsedLegalProvisionSnapshot:
    if capture.source_id != _SOURCE_ID:
        raise LegalProvisionParseBlocked("private-lending parser requires the second-revision source")
    if capture.media_type not in {"text/html", "application/xhtml+xml"}:
        raise LegalProvisionParseBlocked("private-lending source must be captured as HTML")
    body = artifact_store.read_bytes(
        capture.encrypted_object.object_key,
        expected_sha256=capture.content_sha256,
    )
    if sha256(body).hexdigest() != capture.content_sha256 or len(body) != capture.content_bytes:
        raise LegalProvisionParseBlocked("private-lending source bytes differ from capture receipt")
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LegalProvisionParseBlocked("private-lending source is not UTF-8") from error
    extractor = _VisibleTextExtractor()
    extractor.feed(decoded)
    text = _normalize_text(" ".join(extractor.parts))
    segment = _anchored_republished_segment(text)
    provisions = tuple(_extract_provision(segment, *rule) for rule in _ARTICLE_RULES)
    payload = {
        "schema_version": "parsed-legal-provisions-v1",
        "source_id": capture.source_id,
        "source_url": capture.final_url,
        "content_sha256": capture.content_sha256,
        "document_title": _TITLE,
        "version_label": "2020年第二次修正",
        "provisions": [
            {
                "provision_key": item.provision_key,
                "provision_label": item.provision_label,
                "semantic_sha256": item.semantic_sha256,
                "source_locator": item.source_locator,
                "required_markers": item.required_markers,
            }
            for item in provisions
        ],
        "review_status": "HUMAN_REVIEW_REQUIRED",
    }
    output_hash = sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ParsedLegalProvisionSnapshot(
        source_id=capture.source_id,
        source_url=capture.final_url,
        content_sha256=capture.content_sha256,
        document_title=_TITLE,
        version_label="2020年第二次修正",
        provisions=provisions,
        parsed_output_hash=output_hash,
    )


def parse_civil_code_borrowing_provisions(
    *,
    capture: CapturedOfficialSource,
    artifact_store: LocalEncryptedArtifactStore,
) -> ParsedLegalProvisionSnapshot:
    if capture.source_id != _CIVIL_CODE_SOURCE_ID:
        raise LegalProvisionParseBlocked("Civil Code parser requires the registered Civil Code source")
    if capture.media_type not in {"text/html", "application/xhtml+xml"}:
        raise LegalProvisionParseBlocked("current Civil Code parser requires captured official HTML")
    body = artifact_store.read_bytes(
        capture.encrypted_object.object_key,
        expected_sha256=capture.content_sha256,
    )
    if sha256(body).hexdigest() != capture.content_sha256 or len(body) != capture.content_bytes:
        raise LegalProvisionParseBlocked("Civil Code source bytes differ from capture receipt")
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LegalProvisionParseBlocked("Civil Code source is not UTF-8") from error
    extractor = _VisibleTextExtractor()
    extractor.feed(decoded)
    text = _normalize_text(" ".join(extractor.parts))
    if _CIVIL_CODE_TITLE not in text[:2000] or "2020年5月28日" not in text[:3000]:
        raise LegalProvisionParseBlocked("Civil Code title or adoption-date anchor is missing")
    positions = [text.find(rule[1]) for rule in _CIVIL_CODE_ARTICLE_RULES]
    if positions[0] < 0 or positions[1] <= positions[0]:
        raise LegalProvisionParseBlocked("Civil Code borrowing article order is invalid")
    provisions = tuple(_extract_provision(text, *rule) for rule in _CIVIL_CODE_ARTICLE_RULES)
    return _build_snapshot(
        capture=capture,
        document_title=_CIVIL_CODE_TITLE,
        version_label="2020年5月28日通过",
        provisions=provisions,
    )


def project_registered_legal_source_for_document(
    *,
    source_id: str,
    provision_locator: str,
    literal_text: str,
) -> DocumentLegalSourceProjection | None:
    """Return a bounded exact extract only for a registered parser/source pair.

    Most officially captured sources remain opaque to this helper: their
    existing literal text is retained and the document compiler applies its
    normal size gate.  The one supported source below has a version-specific
    parser and a fixed, lawyer-recorded locator.  The locator is a permission
    boundary, not a free-text search query: a changed or broadened locator
    blocks rather than selecting a different provision.
    """

    normalized_source_id = _normalized_source_id(source_id)
    if normalized_source_id != _CIVIL_CODE_SOURCE_ID:
        return None
    locator = _normalized_locator(provision_locator)
    expected_labels = tuple(rule[1] for rule in _CIVIL_CODE_ARTICLE_RULES)
    observed_labels = tuple(_CHINESE_ARTICLE_LABEL.findall(locator))
    if observed_labels != expected_labels:
        raise LegalProvisionDocumentProjectionBlocked(
            "Civil Code document projection requires the exact registered article locator"
        )
    text = _literal_text_without_reader_label(literal_text=literal_text, locator=locator)
    if _CIVIL_CODE_TITLE not in text[:2_000] or "2020年5月28日" not in text[:3_000]:
        raise LegalProvisionDocumentProjectionBlocked(
            "Civil Code document projection title or adoption-date anchor is missing"
        )
    positions = [text.find(rule[1]) for rule in _CIVIL_CODE_ARTICLE_RULES]
    if positions[0] < 0 or positions[1] <= positions[0]:
        raise LegalProvisionDocumentProjectionBlocked(
            "Civil Code document projection article order is invalid"
        )
    try:
        provisions = tuple(
            _extract_provision(text, *rule) for rule in _CIVIL_CODE_ARTICLE_RULES
        )
    except LegalProvisionParseBlocked as error:
        raise LegalProvisionDocumentProjectionBlocked(
            "Civil Code document projection cannot authenticate the required articles"
        ) from error
    reviewed_text = "\n".join(
        (
            f"《{_CIVIL_CODE_TITLE}》已核验条款摘录（仅限登记定位范围）",
            *(item.normalized_text for item in provisions),
        )
    )
    if len(reviewed_text) > _MAX_DOCUMENT_PROJECTION_CHARACTERS:
        raise LegalProvisionDocumentProjectionBlocked(
            "Civil Code document projection exceeds the document source limit"
        )
    return DocumentLegalSourceProjection(
        schema_version=_DOCUMENT_PROJECTION_SCHEMA_VERSION,
        source_id=normalized_source_id,
        provision_labels=expected_labels,
        reviewed_text=reviewed_text,
        reviewed_text_sha256=sha256(reviewed_text.encode("utf-8")).hexdigest(),
    )


def parse_private_lending_first_revision(
    *,
    capture: CapturedOfficialSource,
    artifact_store: LocalEncryptedArtifactStore,
) -> ParsedLegalProvisionSnapshot:
    text = _read_captured_html(
        capture=capture,
        artifact_store=artifact_store,
        expected_source_id=_FIRST_REVISION_SOURCE_ID,
        label="first-revision private-lending",
    )
    segment = _last_titled_segment(text, _TITLE)
    if "修正自2020年8月20日起施行" not in segment[:1000]:
        raise LegalProvisionParseBlocked("first-revision effective-date anchor is missing")
    provisions = tuple(_extract_provision(segment, *rule) for rule in _FIRST_REVISION_RULES)
    return _build_snapshot(
        capture=capture,
        document_title=_TITLE,
        version_label="2020年第一次修正（2020年8月20日起施行）",
        provisions=provisions,
    )


def parse_private_lending_2015_original(
    *,
    capture: CapturedOfficialSource,
    artifact_store: LocalEncryptedArtifactStore,
) -> ParsedLegalProvisionSnapshot:
    text = _read_captured_html(
        capture=capture,
        artifact_store=artifact_store,
        expected_source_id=_ORIGINAL_2015_SOURCE_ID,
        label="2015 private-lending",
    )
    segment = _last_titled_segment(text, _TITLE)
    if "法释〔2015〕18号" not in text or "2015年9月1日起施行" not in text:
        raise LegalProvisionParseBlocked("2015 private-lending version or effective-date anchor is missing")
    provisions = tuple(_extract_provision(segment, *rule) for rule in _ORIGINAL_2015_RULES)
    return _build_snapshot(
        capture=capture,
        document_title=_TITLE,
        version_label="法释〔2015〕18号（2015年9月1日起施行）",
        provisions=provisions,
    )


def _build_snapshot(
    *,
    capture: CapturedOfficialSource,
    document_title: str,
    version_label: str,
    provisions: tuple[ProvisionCandidate, ...],
) -> ParsedLegalProvisionSnapshot:
    payload = {
        "schema_version": "parsed-legal-provisions-v1",
        "source_id": capture.source_id,
        "source_url": capture.final_url,
        "content_sha256": capture.content_sha256,
        "document_title": document_title,
        "version_label": version_label,
        "provisions": [
            {
                "provision_key": item.provision_key,
                "provision_label": item.provision_label,
                "semantic_sha256": item.semantic_sha256,
                "source_locator": item.source_locator,
                "required_markers": item.required_markers,
            }
            for item in provisions
        ],
        "review_status": "HUMAN_REVIEW_REQUIRED",
    }
    output_hash = sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ParsedLegalProvisionSnapshot(
        source_id=capture.source_id,
        source_url=capture.final_url,
        content_sha256=capture.content_sha256,
        document_title=document_title,
        version_label=version_label,
        provisions=provisions,
        parsed_output_hash=output_hash,
    )


def _read_captured_html(
    *,
    capture: CapturedOfficialSource,
    artifact_store: LocalEncryptedArtifactStore,
    expected_source_id: str,
    label: str,
) -> str:
    if capture.source_id != expected_source_id:
        raise LegalProvisionParseBlocked(f"{label} parser received the wrong source")
    if capture.media_type not in {"text/html", "application/xhtml+xml"}:
        raise LegalProvisionParseBlocked(f"{label} source must be captured as HTML")
    body = artifact_store.read_bytes(
        capture.encrypted_object.object_key,
        expected_sha256=capture.content_sha256,
    )
    if sha256(body).hexdigest() != capture.content_sha256 or len(body) != capture.content_bytes:
        raise LegalProvisionParseBlocked(f"{label} bytes differ from capture receipt")
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LegalProvisionParseBlocked(f"{label} source is not UTF-8") from error
    extractor = _VisibleTextExtractor()
    extractor.feed(decoded)
    return _normalize_text(" ".join(extractor.parts))


def _last_titled_segment(text: str, title: str) -> str:
    flexible_title = r"\s*".join(re.escape(character) for character in title)
    matches = tuple(re.finditer(flexible_title, text))
    if not matches:
        raise LegalProvisionParseBlocked("republished legal document title anchor is missing")
    return text[matches[-1].start() :]


def _anchored_republished_segment(text: str) -> str:
    flexible_title = r"最高人民法院\s*关于审理民间借贷案件适用法律若干问题的规定"
    anchors = tuple(re.finditer(flexible_title, text))
    if not anchors:
        raise LegalProvisionParseBlocked("republished private-lending title anchor is missing")
    anchor = anchors[-1]
    segment = text[anchor.start() :]
    version_prefix = segment[:1200]
    if any(marker not in version_prefix for marker in _VERSION_MARKERS):
        raise LegalProvisionParseBlocked("republished private-lending version history is incomplete")
    article_24 = segment.find("第二十四条")
    article_31 = segment.find("第三十一条")
    if article_24 < 0 or article_31 <= article_24:
        raise LegalProvisionParseBlocked("republished private-lending article order is invalid")
    return segment


def _extract_provision(
    segment: str,
    provision_key: str,
    label: str,
    next_label: str | None,
    markers: tuple[str, ...],
) -> ProvisionCandidate:
    start = segment.find(label)
    if start < 0:
        raise LegalProvisionParseBlocked(f"required provision is missing: {label}")
    if next_label is None:
        final_candidates = tuple(
            (segment.find(marker, start), marker)
            for marker in ("以本规定为准。", "以本解释为准。")
            if segment.find(marker, start) >= 0
        )
        if not final_candidates:
            raise LegalProvisionParseBlocked(f"required final marker is missing: {label}")
        end, final_marker = min(final_candidates, key=lambda item: item[0])
        end += len(final_marker)
    else:
        end = segment.find(next_label, start + len(label))
        if end < 0:
            raise LegalProvisionParseBlocked(f"required next provision is missing: {label}")
    normalized = _normalize_text(segment[start:end])
    missing = tuple(marker for marker in markers if marker not in normalized)
    if missing:
        raise LegalProvisionParseBlocked(
            f"required provision markers are missing for {label}: {','.join(missing)}"
        )
    return ProvisionCandidate(
        provision_key=provision_key,
        provision_label=label,
        normalized_text=normalized,
        semantic_sha256=sha256(normalized.encode("utf-8")).hexdigest(),
        source_locator=f"official text / {label}",
        required_markers=markers,
    )


def _normalized_source_id(value: str) -> str:
    if not isinstance(value, str):
        raise LegalProvisionDocumentProjectionBlocked("legal source id is invalid")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 240
        or any(ord(character) < 32 for character in normalized)
    ):
        raise LegalProvisionDocumentProjectionBlocked("legal source id is invalid")
    return normalized


def _normalized_locator(value: str) -> str:
    if not isinstance(value, str):
        raise LegalProvisionDocumentProjectionBlocked("legal source locator is invalid")
    normalized = _normalize_text(value)
    if (
        not normalized
        or len(normalized) > 2_000
        or "\x00" in normalized
        or any(ord(character) < 32 for character in normalized)
    ):
        raise LegalProvisionDocumentProjectionBlocked("legal source locator is invalid")
    return normalized


def _literal_text_without_reader_label(*, literal_text: str, locator: str) -> str:
    if not isinstance(literal_text, str) or not literal_text.strip() or "\x00" in literal_text:
        raise LegalProvisionDocumentProjectionBlocked("verified legal source text is invalid")
    expected_prefix = f"{_VERIFIED_SOURCE_READER_LABEL_PREFIX}{locator}\n\n"
    if literal_text.startswith(_VERIFIED_SOURCE_READER_LABEL_PREFIX):
        if not literal_text.startswith(expected_prefix):
            raise LegalProvisionDocumentProjectionBlocked(
                "verified legal source reader label differs from the registered locator"
            )
        literal_text = literal_text[len(expected_prefix) :]
    normalized = _normalize_text(literal_text)
    if not normalized:
        raise LegalProvisionDocumentProjectionBlocked("verified legal source has no literal text")
    return normalized


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


class _VisibleTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0 and data.strip():
            self.parts.append(data.strip())
