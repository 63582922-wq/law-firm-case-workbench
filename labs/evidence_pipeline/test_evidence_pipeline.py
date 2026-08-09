"""Fixed pass/fail checks for the synthetic evidence-page processing lab."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from PIL import Image
from pypdf import PdfReader

from .evidence_pipeline import file_sha256, load_manifest, run_pipeline
from .generate_fixture import create_synthetic_fixture


class EvidencePipelineLabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workspace = TemporaryDirectory(prefix="evidence-pipeline-lab-")
        cls.root = Path(cls.workspace.name)
        cls.source = create_synthetic_fixture(cls.root / "synthetic-source.pdf")
        cls.source_hash_before = file_sha256(cls.source)
        cls.output = cls.root / "pipeline-output"
        cls.result = run_pipeline(cls.source, cls.output, "Sample Claimant")
        cls.manifest = load_manifest(cls.result.manifest_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.workspace.cleanup()

    def test_original_pdf_is_unchanged_and_all_pages_have_a_disposition(self) -> None:
        self.assertEqual(self.source_hash_before, file_sha256(self.source))
        self.assertTrue(self.manifest["source"]["unchanged"])
        self.assertEqual(5, self.manifest["source"]["page_count"])
        self.assertEqual(5, len(self.manifest["pages"]))
        self.assertTrue(all(record["disposition"] for record in self.manifest["pages"]))

    def test_exact_visual_duplicate_is_grouped_and_only_one_page_is_exported(self) -> None:
        self.assertEqual(
            [{"canonical_source_page": 2, "source_pages": [2, 3]}],
            self.manifest["exact_visual_duplicate_groups"],
        )
        self.assertEqual([2], self.manifest["summary"]["selected_unique_source_pages"])
        self.assertEqual(1, len(PdfReader(str(self.result.related_pages_pdf)).pages))
        self.assertEqual(1, len(PdfReader(str(self.result.annotated_pages_pdf)).pages))

    def test_near_match_is_retained_for_lawyer_review_not_automatically_excluded(self) -> None:
        record = self.manifest["pages"][4]
        self.assertEqual("Sample Clamant", record["extracted_alias"])
        self.assertEqual("SIMILAR_REVIEW_REQUIRED", record["disposition"])
        self.assertGreaterEqual(record["alias_similarity_to_target"], 0.80)
        self.assertEqual(1, self.manifest["summary"]["similar_review_required"])

    def test_derived_red_box_is_recorded_and_visible_after_rendering(self) -> None:
        annotation = self.manifest["derivatives"]["annotations"][0]
        self.assertEqual("RED_BOX", annotation["type"])
        self.assertEqual(2, annotation["source_page_number"])
        self.assertEqual("PDF points, lower-left origin", annotation["coordinate_space"])

        render_prefix = self.root / "annotated"
        subprocess.run(
            ["pdftoppm", "-r", "72", "-png", str(self.result.annotated_pages_pdf), str(render_prefix)],
            check=True,
            capture_output=True,
            text=True,
        )
        rendered_page = self.root / "annotated-1.png"
        with Image.open(rendered_page).convert("RGB") as image:
            red_pixels = sum(
                1
                for red, green, blue in image.get_flattened_data()
                if red > 200 and green < 90 and blue < 90
            )
        self.assertGreater(red_pixels, 500, "Expected a visible red box in the rendered derived PDF")
        self.assertNotEqual(
            sha256(self.source.read_bytes()).hexdigest(),
            sha256(self.result.annotated_pages_pdf.read_bytes()).hexdigest(),
            "A derived PDF must not be the source file itself.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
