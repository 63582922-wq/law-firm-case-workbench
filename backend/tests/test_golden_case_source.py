from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest

from case_kernel.golden_case_source import (
    AUTHORITATIVE_SPEC_SHA256,
    GoldenCaseSourceError,
    deduplicate_pages,
    extract_identity_fields,
    extract_ledger_rows,
    generate_golden_case,
    load_authoritative_case,
    read_generated_pages,
    score_deduplication,
    score_extraction,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class GoldenCaseSourceTests(unittest.TestCase):
    def test_unassisted_draft_has_no_machine_markers_or_metadata_reader(self) -> None:
        from PIL import Image
        from pypdf import PdfReader
        from case_kernel.golden_case_source import UNASSISTED_DRAFT_SCHEMA

        with TemporaryDirectory() as temporary:
            generated = generate_golden_case(Path(temporary) / "draft", self.spec, unassisted_draft=True)
            self.assertEqual(generated.schema_version, UNASSISTED_DRAFT_SCHEMA)
            self.assertEqual(len(generated.files), 11)
            self.assertEqual(sum(item.page_count for item in generated.files), 88)
            by_code = {item.logical_code: item for item in generated.files}
            self.assertEqual(by_code["F2a"].file_sha256, by_code["F2b"].file_sha256)
            self.assertEqual(by_code["F5"].file_sha256, by_code["F9"].file_sha256)
            self.assertNotEqual(by_code["F3a"].file_sha256, by_code["F3b"].file_sha256)
            court = PdfReader(Path(generated.sources_root) / by_code["F1"].file_name)
            pleading = court.pages[1].extract_text()
            for statement in ("诉讼请求", "500,000", "147,000", "205,000", "本金分文未还"):
                self.assertIn(statement, pleading)
            defendant = court.pages[4].extract_text()
            for statement in ("50,000", "30,000", "港币8,000", "我不同意"):
                self.assertIn(statement, defendant)
            for answer in ("时效抗辩不成立", "金标准", "测试陷阱", "并入 #"):
                self.assertNotIn(answer, pleading + defendant)
            for item in generated.files:
                path = Path(generated.sources_root) / item.file_name
                if item.media_type == "application/pdf":
                    reader = PdfReader(path)
                    text = "\n".join(page.extract_text() for page in reader.pages)
                    fonts = reader.pages[0]["/Resources"]["/Font"].get_object()
                    self.assertTrue(any(
                        "/FontFile2" in font.get_object().get("/FontDescriptor", {}).get_object()
                        for font in fonts.values() if "/FontDescriptor" in font.get_object()
                    ), "draft must embed its Chinese font rather than require external CMaps")
                    for marker in ("@GC_TX", "@GC_ID", "材料说明：", "不得并入人民币合计"):
                        self.assertNotIn(marker, text)
                else:
                    with Image.open(path) as image:
                        self.assertNotIn(270, image.getexif())
                        # The old v1 unassisted image had an empty interior. Body
                        # ink must exist across multiple lines, not just a title.
                        self.assertGreaterEqual(image.width, 1500)
                        gray = image.convert("L")
                        for top in (290, 380, 470, 560):
                            body = gray.crop((100, top, 1480, top + 75))
                            self.assertGreater(sum(body.histogram()[:100]), 200)
            for value in (generated, generated.root):
                with self.assertRaisesRegex(GoldenCaseSourceError, "production readers"):
                    read_generated_pages(value)

    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = load_authoritative_case(PROJECT_ROOT)

    def test_authoritative_sha_and_sections_4_5_7_are_parsed_exactly(self) -> None:
        spec = self.spec
        self.assertEqual(spec.spec_sha256, AUTHORITATIVE_SPEC_SHA256)
        self.assertEqual(len(spec.materials), 9)
        self.assertEqual(sum(item.page_count for item in spec.materials), 88)
        self.assertEqual(len(spec.identities), 3)
        self.assertEqual(len(spec.transactions), 47)
        self.assertEqual(tuple(item.row_number for item in spec.transactions), tuple(range(1, 48)))

        identities = {item.role: item for item in spec.identities}
        self.assertEqual(identities["原告"].real_name, "周建国")
        self.assertEqual(identities["原告"].bank_tail, "9021")
        self.assertEqual(identities["被告"].wechat_nickname, "阿强")
        self.assertEqual(identities["代付人"].bank_tail, "6688")
        self.assertIsNone(identities["代付人"].wechat_id)

        row_35 = spec.transactions[34]
        self.assertEqual((row_35.row_number, row_35.amount, row_35.currency), (35, "8000.00", "HKD"))
        self.assertEqual(tuple(item.key for item in row_35.sources), ("F4p30", "F5p12"))
        self.assertEqual(spec.transactions[44].duplicate_group, "G1")
        self.assertEqual(spec.transactions[45].gold_classification, "并入 #21")
        json.dumps(asdict(spec), ensure_ascii=False)
        json.loads(spec.to_json())

    def test_changed_authoritative_spec_is_blocked_before_parsing(self) -> None:
        with TemporaryDirectory() as temporary:
            project = Path(temporary)
            docs = project / "docs"
            docs.mkdir()
            original = (PROJECT_ROOT / "docs" / "GOLDEN_CASE_SYNTHETIC.md").read_bytes()
            (docs / "GOLDEN_CASE_SYNTHETIC.md").write_bytes(original + b"\n")
            with self.assertRaisesRegex(GoldenCaseSourceError, "SHA-256 changed"):
                load_authoritative_case(project)

    def test_generation_has_11_shuffled_files_88_pages_and_required_byte_relations(self) -> None:
        with TemporaryDirectory() as temporary:
            generated = generate_golden_case(Path(temporary) / "case", self.spec)
            self.assertEqual(len(generated.files), 11)
            self.assertEqual(sum(item.page_count for item in generated.files), 88)
            self.assertNotEqual(generated.ingest_order, tuple(sorted(generated.ingest_order)))
            sources = Path(generated.sources_root)
            by_code: dict[str, list] = {}
            for item in generated.files:
                by_code.setdefault(item.source_code, []).append(item)
                path = sources / item.file_name
                self.assertEqual(path.stat().st_mode & 0o777, 0o400)
                self.assertEqual(sha256(path.read_bytes()).hexdigest(), item.file_sha256)
                if item.media_type == "application/pdf":
                    self.assertTrue(path.read_bytes().startswith(b"%PDF-"))
                else:
                    self.assertTrue(path.read_bytes().startswith(b"\xff\xd8"))

            f2 = sorted(by_code["F2"], key=lambda item: item.file_name)
            self.assertEqual(
                (sources / f2[0].file_name).read_bytes(),
                (sources / f2[1].file_name).read_bytes(),
            )
            self.assertEqual(
                (sources / by_code["F5"][0].file_name).read_bytes(),
                (sources / by_code["F9"][0].file_name).read_bytes(),
            )
            f3 = sorted(by_code["F3"], key=lambda item: item.logical_code)
            self.assertNotEqual(
                (sources / f3[0].file_name).read_bytes(),
                (sources / f3[1].file_name).read_bytes(),
            )
            pages = read_generated_pages(generated)
            self.assertEqual(len(pages), 88)
            json.dumps(asdict(generated), ensure_ascii=False)
            json.dumps(asdict(pages[0]), ensure_ascii=False)

    def test_real_page_hash_dedup_has_zero_misses_and_preserves_f3(self) -> None:
        with TemporaryDirectory() as temporary:
            generated = generate_golden_case(Path(temporary) / "case", self.spec)
            pages = read_generated_pages(generated)
            result = deduplicate_pages(pages)
            metrics = score_deduplication(result, generated)
            self.assertEqual(metrics["gold_file_duplicate_groups"], 2)
            self.assertEqual(metrics["gold_exact_page_groups"], 13)
            self.assertEqual(metrics["true_positive_exact_page_groups"], 13)
            self.assertEqual(metrics["missed_exact_page_groups"], 0)
            self.assertEqual(metrics["false_positive_exact_page_groups"], 0)
            self.assertEqual(metrics["false_removals"], 0)
            self.assertEqual(metrics["actual_duplicate_page_exclusions"], 13)
            self.assertEqual((metrics["input_pages"], metrics["canonical_pages"]), (88, 75))
            self.assertTrue(metrics["f3_near_pair_detected"])
            self.assertTrue(metrics["f3_near_pair_preserved"])
            self.assertFalse(metrics["f3_near_pair_merged"])
            f3_pages = [page for page in pages if page.source_code == "F3"]
            self.assertEqual(len(f3_pages), 2)
            self.assertNotEqual(f3_pages[0].file_sha256, f3_pages[1].file_sha256)
            self.assertNotEqual(f3_pages[0].page_sha256, f3_pages[1].page_sha256)
            canonical_keys = {page.page_key for page in result.canonical_pages}
            self.assertTrue({page.page_key for page in f3_pages} <= canonical_keys)

    def test_extracts_all_47_rows_with_perfect_field_score_and_complete_source_refs(self) -> None:
        with TemporaryDirectory() as temporary:
            generated = generate_golden_case(Path(temporary) / "case", self.spec)
            pages = read_generated_pages(generated)
            result = deduplicate_pages(pages)
            rows = extract_ledger_rows(result)
            identities = extract_identity_fields(result)
            metrics = score_extraction(rows, self.spec, identities)
            self.assertEqual(len(rows), 47)
            self.assertEqual(metrics["gold_rows"], 47)
            self.assertEqual(metrics["predicted_rows"], 47)
            self.assertEqual(metrics["row_exact_matches"], 47)
            self.assertEqual((metrics["fp"], metrics["fn"]), (0, 0))
            self.assertEqual((metrics["precision"], metrics["recall"]), (1.0, 1.0))
            self.assertEqual(metrics["source_ref_coverage"], 1.0)
            self.assertEqual(metrics["source_refs_total"], metrics["source_refs_complete"])
            self.assertEqual(
                metrics["gold_identity_occurrences"],
                metrics["predicted_identity_occurrences"],
            )
            self.assertEqual(
                metrics["identity_exact_matches"],
                metrics["gold_identity_occurrences"],
            )
            for kind in ("name", "nickname", "wechat_id", "bank_tail", "mobile_tail"):
                self.assertEqual(metrics["per_field"][kind]["precision"], 1.0)
                self.assertEqual(metrics["per_field"][kind]["recall"], 1.0)

            by_number = {item.row_number: item for item in rows}
            self.assertEqual((by_number[35].amount, by_number[35].currency), ("8000.00", "HKD"))
            self.assertEqual(
                {(ref.material_code, ref.page_number) for ref in by_number[46].source_refs},
                {("F9", 8)},
            )
            page_by_locator = {
                (page.file_name, page.page_number): page for page in result.all_pages
            }
            source_root = Path(generated.sources_root)
            for row in rows:
                self.assertTrue(row.source_refs)
                for ref in row.source_refs:
                    page = page_by_locator[(ref.file_name, ref.page_number)]
                    self.assertEqual(ref.file_sha256, sha256((source_root / ref.file_name).read_bytes()).hexdigest())
                    self.assertEqual(ref.page_sha256, page.page_sha256)
                    self.assertEqual(ref.excerpt_sha256, sha256(ref.excerpt.encode("utf-8")).hexdigest())
                    self.assertRegex(ref.source_ref_id, r"^[0-9a-f]{64}$")
                    self.assertLess(ref.bbox[0], ref.bbox[2])
                    self.assertLess(ref.bbox[1], ref.bbox[3])

    def test_generated_sources_do_not_invent_identity_or_full_account_numbers(self) -> None:
        with TemporaryDirectory() as temporary:
            generated = generate_golden_case(Path(temporary) / "case", self.spec)
            pages = read_generated_pages(generated)
            visible_text = "\n".join(
                line
                for page in pages
                for line in page.text.splitlines()
                if not line.startswith("@GC_")
            )
            self.assertIsNone(re.search(r"(?<!\d)\d{17}[\dXx](?!\d)", visible_text))
            self.assertIsNone(re.search(r"(?<!\d)\d{16,19}(?!\d)", visible_text))
            self.assertIn("规格未提供身份证号码，未生成号码", visible_text)
            for identity in self.spec.identities:
                self.assertTrue(identity.bank_tail is None or len(identity.bank_tail) == 4)
                self.assertTrue(identity.mobile_tail is None or len(identity.mobile_tail) == 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
