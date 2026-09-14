import importlib.util
from pathlib import Path
import sys
import unittest
from xml.etree import ElementTree

import yaml


ROOT = Path(__file__).resolve().parents[2]


class IsolatedDocumentRendererDeploymentTests(unittest.TestCase):
    def _load_probe_module(self):
        path = ROOT / "deployment/local-managed-test/probes/document_renderer_probe.py"
        name = "_lawcase_document_renderer_probe_test"
        spec = importlib.util.spec_from_file_location(name, path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        spec.loader.exec_module(module)
        return module

    def test_renderer_has_no_host_port_or_egress_network_and_is_hardened(self):
        compose = yaml.safe_load((ROOT / "deployment/web/compose.yaml").read_text(encoding="utf-8"))
        renderer = compose["services"]["document-renderer"]
        self.assertNotIn("ports", renderer)
        self.assertNotIn("expose", renderer)
        self.assertEqual(renderer["networks"], ["document-render"])
        self.assertTrue(compose["networks"]["document-render"]["internal"])
        self.assertTrue(renderer["read_only"])
        self.assertEqual(renderer["user"], "10001:10001")
        self.assertEqual(renderer["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", renderer["security_opt"])
        self.assertEqual(renderer["profiles"], ["document-delivery"])
        self.assertTrue(any("/var/lib/lawcase/document-renderer" in item for item in renderer["tmpfs"]))
        self.assertNotIn("LAWCASE_AGENT_WORKER_DEEPSEEK_API_KEY", renderer["environment"])
        self.assertNotIn("LAWCASE_AGENT_WORKER_POSTGRES_DSN", renderer["environment"])

    def test_agent_worker_uses_internal_renderer_but_its_image_has_no_libreoffice(self):
        compose = yaml.safe_load((ROOT / "deployment/web/compose.yaml").read_text(encoding="utf-8"))
        worker = compose["services"]["case-agent-worker"]
        self.assertIn("document-render", worker["networks"])
        self.assertIn("provider-egress", worker["networks"])
        # The Worker still needs its private data network for PostgreSQL/object
        # storage; the renderer itself must never join that network.
        self.assertIn("data", worker["networks"])
        self.assertEqual(worker["build"]["args"]["INSTALL_DOCUMENT_RENDERER_TOOLS"], "false")
        self.assertEqual(
            compose["services"]["document-renderer"]["build"]["args"][
                "INSTALL_DOCUMENT_RENDERER_TOOLS"
            ],
            "true",
        )
        self.assertEqual(
            worker["environment"][
                "LAWCASE_AGENT_WORKER_DOCUMENT_RENDERER_SHARED_SECRET"
            ],
            "${LAWCASE_DOCUMENT_RENDERER_SHARED_SECRET:-}",
        )
        self.assertEqual(
            compose["services"]["document-renderer"]["environment"][
                "LAWCASE_DOCUMENT_RENDERER_SHARED_SECRET"
            ],
            "${LAWCASE_DOCUMENT_RENDERER_SHARED_SECRET:?set a unique base64url renderer secret in .env}",
        )
        self.assertEqual(
            worker["environment"][
                "LAWCASE_AGENT_WORKER_DOCUMENT_SOFFICE_EXECUTABLE"
            ],
            "${LAWCASE_AGENT_WORKER_DOCUMENT_SOFFICE_EXECUTABLE:-}",
        )
        self.assertEqual(
            worker["environment"][
                "LAWCASE_AGENT_WORKER_DOCUMENT_PDFTOPPM_EXECUTABLE"
            ],
            "${LAWCASE_AGENT_WORKER_DOCUMENT_PDFTOPPM_EXECUTABLE:-}",
        )

    def test_web_document_candidates_share_the_internal_renderer_without_local_office(self):
        compose = yaml.safe_load((ROOT / "deployment/web/compose.yaml").read_text(encoding="utf-8"))
        api = compose["services"]["api"]
        self.assertIn("document-render", api["networks"])
        self.assertNotIn("provider-egress", api["networks"])
        self.assertEqual(api["build"]["args"]["INSTALL_DOCUMENT_RENDERER_TOOLS"], "false")
        self.assertEqual(
            api["environment"]["LAWCASE_WEB_DOCUMENT_RENDERER_ENDPOINT"],
            "${LAWCASE_WEB_DOCUMENT_RENDERER_ENDPOINT:-http://document-renderer:8090}",
        )
        self.assertEqual(
            api["environment"]["LAWCASE_WEB_DOCUMENT_RENDERER_SHARED_SECRET"],
            "${LAWCASE_DOCUMENT_RENDERER_SHARED_SECRET:-}",
        )

    def test_local_worker_overlay_mounts_the_deterministic_document_generator(self):
        compose = yaml.safe_load(
            (
                ROOT
                / "deployment/local-managed-test/runtime/worker-code-overlay.compose.yaml"
            ).read_text(encoding="utf-8")
        )
        worker_volumes = compose["services"]["case-agent-worker"]["volumes"]
        self.assertIn(
            "../../backend/case_kernel/approved_draft_worker.py:/app/backend/case_kernel/approved_draft_worker.py:ro",
            worker_volumes,
        )
        self.assertIn(
            "../../backend/case_kernel/case_agent_document_delivery.py:/app/backend/case_kernel/case_agent_document_delivery.py:ro",
            worker_volumes,
        )

    def test_dockerfile_installs_rendering_tools_only_under_explicit_build_arg(self):
        for relative_path in (
            "deployment/web/api.Dockerfile",
            "deployment/local-managed-test/api.Dockerfile",
        ):
            with self.subTest(dockerfile=relative_path):
                dockerfile = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn("ARG INSTALL_DOCUMENT_RENDERER_TOOLS=false", dockerfile)
                self.assertIn(
                    'if [ "$INSTALL_DOCUMENT_RENDERER_TOOLS" = "true" ]',
                    dockerfile,
                )
                self.assertIn("libreoffice-writer", dockerfile)
                self.assertIn("libreoffice-calc", dockerfile)
                self.assertIn("fontconfig fonts-noto-cjk", dockerfile)
                self.assertIn("fonts-arphic-uming", dockerfile)
                self.assertIn("fonts-arphic-ukai", dockerfile)
                self.assertIn("fonts-wqy-microhei", dockerfile)
                self.assertIn("65-lawcase-cn-font-aliases.conf", dockerfile)
                self.assertIn("fc-cache -f", dockerfile)
                self.assertIn("Noto Serif CJK SC", dockerfile)
                self.assertIn("Noto Sans CJK SC", dockerfile)
                self.assertIn("NotoSerifCJKsc-Regular|2", dockerfile)
                self.assertIn("NotoSansCJKsc-Regular|2", dockerfile)
                self.assertIn("create_pdf_draft", dockerfile)
                self.assertIn('["/usr/bin/pdffonts", str(path)]', dockerfile)
                self.assertIn('row["embedded"] == row["subset"] == row["unicode"] == "yes"', dockerfile)
                self.assertIn('"umingcn" in names', dockerfile)
                self.assertIn('"ukaicn" in names', dockerfile)
                self.assertIn('"wenquanyimicrohei" in names', dockerfile)
                self.assertIn('"helvetica" not in names', dockerfile)
                self.assertIn('"stsong" not in names', dockerfile)
                self.assertIn(
                    "/usr/share/doc/fonts-arphic-uming/copyright", dockerfile
                )
                self.assertIn(
                    "/usr/share/doc/fonts-arphic-ukai/copyright", dockerfile
                )
                self.assertIn(
                    "/usr/share/doc/fonts-wqy-microhei/copyright", dockerfile
                )
                self.assertIn("license_manifest.stat().st_size > 0", dockerfile)
                self.assertIn('("\\U00020021", "U+20021")', dockerfile)
                self.assertIn('("e\\u0301", "U+0301")', dockerfile)
                self.assertIn('("\\ufffc", "U+FFFC")', dockerfile)
                self.assertIn('"℃ǎ", approved_draft_worker_module._PDF_HEADING_FONT', dockerfile)
                self.assertIn('"㖞", approved_draft_worker_module._PDF_SECONDARY_FONT', dockerfile)

    def test_renderer_fontconfig_contract_maps_domestic_names_to_noto_cjk(self):
        path = (
            ROOT
            / "backend/case_api/deployment/fontconfig/65-lawcase-cn-font-aliases.conf"
        )
        root = ElementTree.parse(path).getroot()
        aliases = {
            alias.findtext("family"): alias.findtext("prefer/family")
            for alias in root.findall("alias")
        }
        for family in (
            "宋体",
            "仿宋_GB2312",
            "楷体",
            "SimSun",
            "FangSong",
            "KaiTi",
            "Times New Roman",
        ):
            with self.subTest(family=family):
                self.assertEqual(aliases[family], "Noto Serif CJK SC")
        for family in ("黑体", "微软雅黑", "SimHei", "Microsoft YaHei", "Arial"):
            with self.subTest(family=family):
                self.assertEqual(aliases[family], "Noto Sans CJK SC")
        text = path.read_text(encoding="utf-8")
        self.assertIn("do not install or claim", text)

    def test_managed_probe_uses_production_generators_and_inspects_real_pdf(self):
        probe = (
            ROOT / "deployment/local-managed-test/probes/document_renderer_probe.py"
        ).read_text(encoding="utf-8")
        self.assertIn("create_reviewable_docx_draft", probe)
        self.assertIn("create_reviewable_xlsx_ledger", probe)
        self.assertIn("create_pdf_draft", probe)
        self.assertIn("parse_reviewable_document_candidate", probe)
        self.assertIn("build_deterministic_payment_ledger_candidate", probe)
        self.assertIn("DynamicDocumentTaskBinding", probe)
        self.assertIn("AuthoritativeDocumentSource", probe)
        self.assertIn('"date_precision": "EXACT_DATE"', probe)
        self.assertIn('"direction": direction', probe)
        self.assertIn('"channel": "BANK"', probe)
        self.assertIn("candidate.to_docx_input()", probe)
        self.assertIn("visible_document_source_labels(binding)", probe)
        self.assertNotIn('startswith("transaction:")', probe)
        self.assertNotIn("Document()", probe)
        self.assertNotIn("Workbook()", probe)
        for executable in ("/usr/bin/pdfinfo", "/usr/bin/pdffonts", "/usr/bin/pdftoppm"):
            self.assertIn(executable, probe)
        self.assertIn("PdfReader", probe)
        self.assertIn("Noto CJK SC", probe)
        self.assertIn("CJKjp", probe)
        self.assertIn("UMingCN".casefold(), probe.casefold())
        self.assertIn("UKaiCN".casefold(), probe.casefold())
        self.assertIn("WenQuanYiMicroHei".casefold(), probe.casefold())
        self.assertIn("embedded-direct-pdf", probe)
        self.assertIn("ApprovedDraftBlocked", probe)
        self.assertIn("U+20021", probe)
        self.assertIn("U+0301", probe)
        self.assertIn("U+FFFC", probe)
        self.assertIn("温度20℃与姓名ǎ", probe)
        self.assertIn("异体字㖞核对", probe)
        self.assertIn("待律师终审", probe)
        self.assertIn("非正式文书", probe)
        self.assertIn('_MIN_LEDGER_HEADER_FONT_POINTS = 9.0', probe)
        self.assertIn('_MIN_LEDGER_BODY_FONT_POINTS = 9.0', probe)
        self.assertIn('visitor_text=visitor_text', probe)
        self.assertIn('header_min_font_points', probe)
        self.assertIn('body_min_font_points', probe)
        self.assertIn('"1.2.4"', probe)
        self.assertIn(
            '64485435e38de144189d27252f3d1459b8cc9e2bafb5976e8930a5e0571cf53a',
            probe,
        )
        for code in ("EXACT_DATE", "OUTGOING", "INCOMING", "DISBURSEMENT"):
            self.assertIn(f'"{code}"', probe)
        self.assertIn("_SourceColumnLine", probe)
        self.assertIn("source_column_floor", probe)
        self.assertIn("same-day sequence value", probe)
        self.assertIn(
            'source reference did not preserve complete managed lines', probe
        )
        self.assertIn('spreadsheet source rows overlap or are vertically clipped', probe)
        self.assertIn('_MAX_SOURCE_LINE_GAP_POINTS = 18.0', probe)
        self.assertIn('_MAX_SOURCE_LINE_X_DRIFT_POINTS = 1.0', probe)
        self.assertIn('_MIN_SOURCE_COLUMN_X_RATIO = 0.80', probe)
        self.assertIn('_MAX_LEDGER_BODY_FONT_POINTS = 11.0', probe)
        self.assertIn('spreadsheet source reference escaped its managed column', probe)
        self.assertIn('spreadsheet source reference collided with its left field', probe)

    def test_managed_probe_rejects_overlapping_or_scattered_source_lines(self):
        probe = self._load_probe_module()
        title = "收付款核对表候选"
        header = "日期"
        source_header = "来源"
        codes = ("精确到日", "付款", "收款", "款项交付")
        source_one = "transaction:11111111-1111-4111-8111-111111111111"
        source_two = "transaction:22222222-2222-4222-8222-222222222222"
        fragments = {
            source_one: ("transaction:11111111", "-1111-4111-8111-", "111111111111"),
            source_two: ("transaction:22222222", "-2222-4222-8222-", "222222222222"),
        }

        class _Page:
            mediabox = type("_Box", (), {"width": 842.0, "height": 595.0})()

            def extract_text(self, *args, **kwargs):
                del args, kwargs
                return (
                    title
                    + header
                    + source_header
                    + "".join(codes)
                    + source_one
                    + source_two
                )

        reader = type("_Reader", (), {"pages": [_Page()]})()

        def runs_for(
            first_geometry: tuple[tuple[float, float], ...],
            second_geometry: tuple[tuple[float, float], ...],
            *,
            code_font_points: float = 9.5,
        ):
            runs = [
                probe._PdfTextRun(1, title, 14.0, 100.0, 550.0),
                probe._PdfTextRun(1, header, 10.0, 100.0, 530.0),
                probe._PdfTextRun(1, source_header, 10.0, 755.0, 530.0),
            ]
            runs.extend(
                probe._PdfTextRun(
                    1, code, code_font_points, 100.0, 510.0 - index * 12.0
                )
                for index, code in enumerate(codes)
            )
            for source, geometry in (
                (source_one, first_geometry),
                (source_two, second_geometry),
            ):
                runs.extend(
                    probe._PdfTextRun(1, text, 9.5, x, y)
                    for text, (x, y) in zip(fragments[source], geometry, strict=True)
                )
            return tuple(runs)

        def validate(runs):
            original = probe._spreadsheet_text_runs
            probe._spreadsheet_text_runs = lambda unused: runs
            try:
                return probe._assert_spreadsheet_typography(
                    reader,
                    title=title,
                    headers=(header, source_header),
                    body_values=(*codes, source_one, source_two),
                    source_refs=(source_one, source_two),
                )
            finally:
                probe._spreadsheet_text_runs = original

        # Two labels rendered onto the same physical row cannot be
        # reconstructed as independently reviewable source values.  The probe
        # may report either the geometry collision or the resulting failed
        # reconstruction; both fail closed.
        with self.assertRaisesRegex(
            RuntimeError,
            "source reference did not preserve complete managed lines|source rows overlap",
        ):
            validate(
                runs_for(
                    ((717.0, 300.0), (717.0, 286.0), (717.0, 272.0)),
                    ((717.0, 300.0), (717.0, 286.0), (717.0, 272.0)),
                )
            )
        with self.assertRaisesRegex(
            RuntimeError, "source reference did not preserve complete managed lines"
        ):
            validate(
                runs_for(
                    ((10.0, 500.0), (400.0, 300.0), (690.0, 100.0)),
                    ((717.0, 70.0), (717.0, 56.0), (717.0, 42.0)),
                )
            )
        with self.assertRaisesRegex(RuntimeError, "escaped its managed column"):
            validate(
                runs_for(
                    ((717.0, 300.0), (719.0, 286.0), (717.0, 272.0)),
                    ((717.0, 240.0), (717.0, 226.0), (717.0, 212.0)),
                )
            )
        with self.assertRaisesRegex(
            RuntimeError, "source reference did not preserve complete managed lines"
        ):
            validate(
                runs_for(
                    ((10.0, 300.0), (10.0, 286.0), (10.0, 272.0)),
                    ((10.0, 240.0), (10.0, 226.0), (10.0, 212.0)),
                )
            )
        with self.assertRaisesRegex(RuntimeError, "exceeds the managed print size"):
            validate(
                runs_for(
                    ((717.0, 300.0), (717.0, 286.0), (717.0, 272.0)),
                    ((717.0, 240.0), (717.0, 226.0), (717.0, 212.0)),
                    code_font_points=50.0,
                )
            )
        colliding_runs = runs_for(
            ((717.0, 300.0), (717.0, 286.0), (717.0, 272.0)),
            ((717.0, 240.0), (717.0, 226.0), (717.0, 212.0)),
        ) + (probe._PdfTextRun(1, "2", 9.5, 705.0, 286.0),)
        with self.assertRaisesRegex(RuntimeError, "collided with its left field"):
            validate(colliding_runs)
        result = validate(
            runs_for(
                ((717.0, 300.0), (717.0, 286.0), (717.0, 272.0)),
                ((717.0, 240.0), (717.0, 226.0), (717.0, 212.0)),
            )
        )
        self.assertGreaterEqual(result.body_min_font_points, 9.0)

    def test_managed_probe_accepts_split_source_cells_without_sequence_leakage(self):
        probe = self._load_probe_module()
        title = "收付款核对表候选"
        headers = ("日期", "来源")
        codes = ("精确到日", "付款", "收款", "款项交付")
        source_one = "来源02｜已确认交易2"
        source_two = "来源01｜已确认交易1"

        class _Page:
            mediabox = type("_Box", (), {"width": 842.0, "height": 595.0})()

            def extract_text(self, *args, **kwargs):
                del args, kwargs
                return title + "".join(headers) + "".join(codes) + source_one + source_two

        reader = type("_Reader", (), {"pages": [_Page()]})()
        runs = (
            probe._PdfTextRun(1, title, 14.0, 100.0, 550.0),
            # The review-state suffix contains the short enum “付款” but is
            # title chrome, not a 9pt ledger cell.
            probe._PdfTextRun(1, f"{title}｜律师复核候选", 8.7, 356.0, 566.0),
            probe._PdfTextRun(1, "日期", 10.0, 100.0, 530.0),
            probe._PdfTextRun(1, "来源", 10.0, 755.0, 530.0),
            *( 
                probe._PdfTextRun(1, code, 9.5, 100.0, 510.0 - index * 12.0)
                for index, code in enumerate(codes)
            ),
            # LibreOffice may split a narrow source cell into these four PDF
            # runs.  The visually adjacent same-day sequence at x=692 must
            # not be used to reconstruct the final source-label digit.
            probe._PdfTextRun(1, "来源", 9.5, 717.0, 300.0),
            probe._PdfTextRun(1, "02", 9.5, 735.0, 300.0),
            probe._PdfTextRun(1, "｜已确认交易", 9.5, 745.0, 300.0),
            probe._PdfTextRun(1, "2", 9.5, 800.0, 300.0),
            probe._PdfTextRun(1, "2", 9.5, 692.0, 300.0),
            probe._PdfTextRun(1, "来源", 9.5, 717.0, 270.0),
            probe._PdfTextRun(1, "01", 9.5, 735.0, 270.0),
            probe._PdfTextRun(1, "｜已确认交易", 9.5, 745.0, 270.0),
            probe._PdfTextRun(1, "1", 9.5, 800.0, 270.0),
        )

        def validate(candidate_runs):
            original = probe._spreadsheet_text_runs
            probe._spreadsheet_text_runs = lambda unused: candidate_runs
            try:
                return probe._assert_spreadsheet_typography(
                    reader,
                    title=title,
                    headers=headers,
                    body_values=(*codes, source_one, source_two),
                    source_refs=(source_one, source_two),
                )
            finally:
                probe._spreadsheet_text_runs = original

        result = validate(runs)
        self.assertGreaterEqual(result.body_min_font_points, 9.0)

        source_suffix_missing = tuple(
            run
            for run in runs
            if not (run.text == "2" and run.x == 800.0 and run.y == 300.0)
        )
        with self.assertRaisesRegex(
            RuntimeError, "source reference did not preserve complete managed lines"
        ):
            validate(source_suffix_missing)

        colliding = runs + (probe._PdfTextRun(1, "2", 9.5, 705.0, 270.0),)
        with self.assertRaisesRegex(RuntimeError, "collided with its left field"):
            validate(colliding)


if __name__ == "__main__":
    unittest.main()
