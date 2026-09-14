from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image

from case_kernel.visual_page_understanding import (
    VISUAL_PAGE_SCHEMA_VERSION,
    VisualCandidateStatus,
    VisualPageBlocked,
    VisualSourceKind,
    build_visual_page_projection,
    parse_visual_page_candidate,
    visual_page_request_hash,
)


def digest(value: bytes | str) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return sha256(raw).hexdigest()


def png_bytes(*, mode: str = "RGB", size: tuple[int, int] = (120, 80)) -> bytes:
    color = (255, 0, 0, 80) if mode == "RGBA" else (245, 245, 245)
    image = Image.new(mode, size, color)
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


class VisualPageUnderstandingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matter_id = str(uuid4())
        self.page_id = str(uuid4())
        self.source = png_bytes()
        self.projection = build_visual_page_projection(
            matter_id=self.matter_id,
            evidence_page_id=self.page_id,
            page_number=2,
            source_kind=VisualSourceKind.RENDERED_PDF_PAGE,
            source_file_sha256=digest("source-file"),
            source_page_sha256=digest(self.source),
            source_media_type="image/png",
            source_bytes=self.source,
        )

    def response(self) -> dict[str, object]:
        return {
            "schema_version": VISUAL_PAGE_SCHEMA_VERSION,
            "request_hash": visual_page_request_hash(self.projection),
            "matter_id": self.projection.matter_id,
            "evidence_page_id": self.projection.evidence_page_id,
            "source_file_sha256": self.projection.source_file_sha256,
            "source_page_sha256": self.projection.source_page_sha256,
            "rendered_page_sha256": self.projection.rendered_page_sha256,
            "projection_hash": self.projection.projection_hash,
            "provider_id": "qwen",
            "model_id": "qwen3.5-ocr",
            "text_blocks": [
                {
                    "block_id": "block-1",
                    "kind": "TEXT",
                    "text": "转账金额 100.00",
                    "region": {"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.1},
                    "confidence": 0.94,
                }
            ],
            "tables": [
                {
                    "table_id": "table-1",
                    "region": {"x": 0.05, "y": 0.3, "width": 0.9, "height": 0.4},
                    "row_count": 2,
                    "column_count": 2,
                    "cells": [["日期", "金额"], ["2026-01-01", "100.00"]],
                    "confidence": 0.88,
                }
            ],
            "fields": [
                {
                    "field_id": "field-1",
                    "kind": "AMOUNT",
                    "value": "100.00",
                    "currency": "CNY",
                    "region": {"x": 0.6, "y": 0.45, "width": 0.2, "height": 0.08},
                    "confidence": 0.91,
                }
            ],
            "quality_risks": [
                {
                    "code": "BLUR",
                    "severity": "LOW",
                    "region": None,
                    "confidence": 0.7,
                    "note": "局部文字边缘略模糊，仅提示复核。",
                }
            ],
        }

    def parse(self, payload: dict[str, object]):
        return parse_visual_page_candidate(
            json.dumps(payload, ensure_ascii=False),
            projection=self.projection,
            expected_provider_id="qwen",
            expected_model_id="qwen3.5-ocr",
            provider_request_ref_hash=digest("provider-request-1"),
        )

    def test_projection_is_source_bound_and_hides_raster_repr(self) -> None:
        self.assertEqual(self.projection.media_type, "image/png")
        self.assertEqual(self.projection.width, 120)
        self.assertEqual(self.projection.height, 80)
        self.assertEqual(
            self.projection.rendered_page_sha256,
            digest(self.projection.raster_content),
        )
        self.assertNotIn(repr(self.projection.raster_content), repr(self.projection))
        self.assertNotIn("raster_content=", repr(self.projection))

    def test_native_image_and_scanned_pdf_page_use_same_contract(self) -> None:
        native = build_visual_page_projection(
            matter_id=self.matter_id,
            evidence_page_id=str(uuid4()),
            page_number=1,
            source_kind=VisualSourceKind.NATIVE_IMAGE,
            source_file_sha256=digest(self.source),
            source_page_sha256=digest(self.source),
            source_media_type="image/png",
            source_bytes=self.source,
        )
        self.assertEqual(native.media_type, self.projection.media_type)
        self.assertEqual(native.raster_content, self.projection.raster_content)
        self.assertNotEqual(native.projection_hash, self.projection.projection_hash)

    def test_transparency_is_flattened_before_model_input(self) -> None:
        source = png_bytes(mode="RGBA")
        projection = build_visual_page_projection(
            matter_id=self.matter_id,
            evidence_page_id=str(uuid4()),
            page_number=1,
            source_kind=VisualSourceKind.NATIVE_IMAGE,
            source_file_sha256=digest(source),
            source_page_sha256=digest(source),
            source_media_type="image/png",
            source_bytes=source,
        )
        self.assertTrue(projection.had_transparency)
        with Image.open(BytesIO(projection.raster_content)) as normalized:
            self.assertEqual(normalized.mode, "RGB")
            self.assertNotIn("transparency", normalized.info)

    def test_exif_orientation_is_applied_and_bound(self) -> None:
        image = Image.new("RGB", (40, 20), "white")
        exif = Image.Exif()
        exif[274] = 6
        output = BytesIO()
        image.save(output, "JPEG", exif=exif)
        source = output.getvalue()
        projection = build_visual_page_projection(
            matter_id=self.matter_id,
            evidence_page_id=str(uuid4()),
            page_number=1,
            source_kind=VisualSourceKind.NATIVE_IMAGE,
            source_file_sha256=digest(source),
            source_page_sha256=digest(source),
            source_media_type="image/jpeg",
            source_bytes=source,
        )
        self.assertEqual(projection.orientation_applied, 6)
        self.assertEqual((projection.width, projection.height), (20, 40))

    def test_source_hash_and_decoded_media_type_are_not_trusted_from_name(self) -> None:
        with self.assertRaisesRegex(VisualPageBlocked, "hash differs"):
            build_visual_page_projection(
                matter_id=self.matter_id,
                evidence_page_id=self.page_id,
                page_number=1,
                source_kind=VisualSourceKind.NATIVE_IMAGE,
                source_file_sha256=digest("file"),
                source_page_sha256=digest("wrong"),
                source_media_type="image/png",
                source_bytes=self.source,
            )
        with self.assertRaisesRegex(VisualPageBlocked, "media type differs"):
            build_visual_page_projection(
                matter_id=self.matter_id,
                evidence_page_id=self.page_id,
                page_number=1,
                source_kind=VisualSourceKind.NATIVE_IMAGE,
                source_file_sha256=digest("file"),
                source_page_sha256=digest(self.source),
                source_media_type="image/jpeg",
                source_bytes=self.source,
            )

    def test_multiframe_and_decompression_dimensions_are_blocked(self) -> None:
        frames = [Image.new("RGB", (10, 10), "white"), Image.new("RGB", (10, 10), "black")]
        output = BytesIO()
        frames[0].save(output, "TIFF", save_all=True, append_images=frames[1:])
        source = output.getvalue()
        with self.assertRaisesRegex(VisualPageBlocked, "exactly one"):
            build_visual_page_projection(
                matter_id=self.matter_id,
                evidence_page_id=str(uuid4()),
                page_number=1,
                source_kind=VisualSourceKind.NATIVE_IMAGE,
                source_file_sha256=digest(source),
                source_page_sha256=digest(source),
                source_media_type="image/tiff",
                source_bytes=source,
            )

        # A tiny compressed object that declares hostile dimensions is
        # rejected before a decompressed raster can be allocated.
        with patch("case_kernel.visual_page_understanding.Image.open") as opened:
            image = opened.return_value.__enter__.return_value
            image.format = "PNG"
            image.size = (50_000, 50_000)
            image.n_frames = 1
            image.is_animated = False
            with self.assertRaisesRegex(VisualPageBlocked, "dimensions exceed"):
                build_visual_page_projection(
                    matter_id=self.matter_id,
                    evidence_page_id=str(uuid4()),
                    page_number=1,
                    source_kind=VisualSourceKind.NATIVE_IMAGE,
                    source_file_sha256=digest(self.source),
                    source_page_sha256=digest(self.source),
                    source_media_type="image/png",
                    source_bytes=self.source,
                )

    def test_strict_candidate_remains_review_only(self) -> None:
        candidate = self.parse(self.response())
        self.assertEqual(candidate.status, VisualCandidateStatus.NEEDS_REVIEW)
        self.assertEqual(candidate.fields[0].currency, "CNY")
        self.assertEqual(candidate.text_blocks[0].text, "转账金额 100.00")
        self.assertEqual(len(candidate.candidate_hash), 64)
        self.assertFalse(hasattr(candidate, "is_authentic"))
        self.assertFalse(hasattr(candidate, "legal_conclusion"))

    def test_exact_qwen_json_fence_is_unwrapped_but_surrounding_text_is_rejected(self) -> None:
        raw = json.dumps(self.response(), ensure_ascii=False)
        fenced = f"```json\n{raw}\n```"
        candidate = parse_visual_page_candidate(
            fenced,
            projection=self.projection,
            expected_provider_id="qwen",
            expected_model_id="qwen3.5-ocr",
            provider_request_ref_hash=digest("provider-request-1"),
        )
        self.assertEqual(candidate.status, VisualCandidateStatus.NEEDS_REVIEW)
        for invalid in (
            f"以下是结果\n{fenced}",
            f"{fenced}\n完成",
            f"```JSON\n{raw}\n```",
            f"```json\n{raw}\n```\n```json\n{{}}\n```",
        ):
            with self.subTest(invalid=invalid[:20]):
                with self.assertRaisesRegex(
                    VisualPageBlocked, "must be one JSON object"
                ):
                    parse_visual_page_candidate(
                        invalid,
                        projection=self.projection,
                        expected_provider_id="qwen",
                        expected_model_id="qwen3.5-ocr",
                        provider_request_ref_hash=digest("provider-request-1"),
                    )

    def test_unknown_response_field_is_rejected(self) -> None:
        payload = self.response()
        payload["legal_conclusion"] = "已证明还款"
        with self.assertRaisesRegex(VisualPageBlocked, "schema is invalid"):
            self.parse(payload)

    def test_duplicate_json_keys_are_rejected(self) -> None:
        raw = json.dumps(self.response(), ensure_ascii=False)
        duplicated = raw[:-1] + ',"schema_version":"visual-page-understanding-v1"}'
        with self.assertRaisesRegex(VisualPageBlocked, "duplicate JSON keys"):
            parse_visual_page_candidate(
                duplicated,
                projection=self.projection,
                expected_provider_id="qwen",
                expected_model_id="qwen3.5-ocr",
                provider_request_ref_hash=digest("provider-request-1"),
            )

    def test_wrong_page_or_hash_is_rejected(self) -> None:
        payload = self.response()
        payload["evidence_page_id"] = str(uuid4())
        with self.assertRaisesRegex(VisualPageBlocked, "bound evidence page"):
            self.parse(payload)
        payload = self.response()
        payload["rendered_page_sha256"] = digest("another-page")
        with self.assertRaisesRegex(VisualPageBlocked, "bound evidence page"):
            self.parse(payload)

    def test_out_of_bounds_region_and_illegal_enum_are_rejected(self) -> None:
        payload = self.response()
        payload["text_blocks"][0]["region"] = {
            "x": 0.9,
            "y": 0.1,
            "width": 0.2,
            "height": 0.2,
        }
        with self.assertRaisesRegex(VisualPageBlocked, "exceeds"):
            self.parse(payload)
        payload = self.response()
        payload["quality_risks"][0]["code"] = "IMAGE_IS_FAKE"
        with self.assertRaisesRegex(VisualPageBlocked, "code is invalid"):
            self.parse(payload)

    def test_tampered_projection_bytes_are_rejected_before_parsing(self) -> None:
        tampered = replace(self.projection, raster_content=self.projection.raster_content + b"x")
        with self.assertRaisesRegex(VisualPageBlocked, "raster differs"):
            parse_visual_page_candidate(
                json.dumps(self.response(), ensure_ascii=False),
                projection=tampered,
                expected_provider_id="qwen",
                expected_model_id="qwen3.5-ocr",
                provider_request_ref_hash=digest("provider-request-1"),
            )


if __name__ == "__main__":
    unittest.main()
