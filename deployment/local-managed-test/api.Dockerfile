ARG PYTHON_IMAGE=mirror.gcr.io/library/python:3.12.13-slim-bookworm
FROM ${PYTHON_IMAGE}

# The local managed stack reuses the production Python package and gate, while
# keeping this Dockerfile isolated because its build context is the repo root.
ARG INSTALL_DOCUMENT_RENDERER_TOOLS=false

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_DEFAULT_TIMEOUT=120
ENV PIP_RETRIES=2

WORKDIR /app
COPY backend/case_api/deployment/fontconfig/65-lawcase-cn-font-aliases.conf /opt/lawcase-fontconfig/65-lawcase-cn-font-aliases.conf
RUN set -eux; \
    groupadd --system --gid 10001 lawcase; \
    useradd --system --uid 10001 --gid lawcase --home-dir /nonexistent --shell /usr/sbin/nologin lawcase; \
    apt-get update; \
    packages="clamav clamav-daemon clamdscan poppler-utils fonts-arphic-uming fonts-arphic-ukai fonts-wqy-microhei"; \
    if [ "$INSTALL_DOCUMENT_RENDERER_TOOLS" = "true" ]; then \
        packages="$packages fontconfig fonts-noto-cjk libreoffice-calc libreoffice-core libreoffice-writer"; \
    fi; \
    apt-get install --no-install-recommends -y $packages; \
    if [ "$INSTALL_DOCUMENT_RENDERER_TOOLS" = "true" ]; then \
        install -m 0644 /opt/lawcase-fontconfig/65-lawcase-cn-font-aliases.conf /etc/fonts/conf.d/65-lawcase-cn-font-aliases.conf; \
        fc-cache -f; \
        for family in '宋体' '仿宋_GB2312' '楷体' 'SimSun' 'FangSong' 'KaiTi' 'Times New Roman'; do \
            test "$(fc-match -f '%{family[0]}' "$family:lang=zh-cn")" = 'Noto Serif CJK SC'; \
            test "$(fc-match -f '%{postscriptname}|%{index}' "$family:lang=zh-cn")" = 'NotoSerifCJKsc-Regular|2'; \
        done; \
        for family in '黑体' '微软雅黑' 'SimHei' 'Microsoft YaHei' 'Arial'; do \
            test "$(fc-match -f '%{family[0]}' "$family:lang=zh-cn")" = 'Noto Sans CJK SC'; \
            test "$(fc-match -f '%{postscriptname}|%{index}' "$family:lang=zh-cn")" = 'NotoSansCJKsc-Regular|2'; \
        done; \
    fi; \
    rm -rf /opt/lawcase-fontconfig; \
    rm -rf /var/lib/apt/lists/*

# The bounded local acceptance command receives its one signed synthetic
# specification as a read-only Compose mount.  Keep only an empty mount point
# in the image so a distributable API artifact never packages project docs.
RUN mkdir -p /app/docs
COPY backend /app/backend
RUN pip install --no-cache-dir /app/backend
# ReportLab cannot embed Debian's CFF-based Noto CJK collections.  Every
# runtime that can call create_pdf_draft therefore carries redistributable
# Simplified-Chinese TrueType serif, kai and sans faces.  Exercise the production
# generator and fail the image build unless pdffonts reports only embedded,
# subsetted, Unicode-mapped resources from those managed fonts.
RUN python - <<'PY'
from hashlib import sha256
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory

from case_kernel.approved_draft_worker import (
    ApprovedDraft,
    ApprovedDraftBlocked,
    ApprovedSection,
    create_pdf_draft,
)
from case_kernel import approved_draft_worker as approved_draft_worker_module

for license_path in (
    "/usr/share/doc/fonts-arphic-uming/copyright",
    "/usr/share/doc/fonts-arphic-ukai/copyright",
    "/usr/share/doc/fonts-wqy-microhei/copyright",
):
    license_manifest = Path(license_path)
    assert license_manifest.is_file() and license_manifest.stat().st_size > 0

artifact = create_pdf_draft(
    ApprovedDraft(
        title="案件审阅意见候选",
        sections=(
            ApprovedSection(
                heading="一、字体与缺字验收",
                paragraphs=("律师复核候选正文：龘、㑇、喆。",),
                source_refs=("preflight-source-001",),
            ),
        ),
        approval_hash=sha256(b"lawcase-direct-pdf-font-preflight").hexdigest(),
    )
)
assert artifact.media_type == "application/pdf"
assert artifact.content_sha256 == sha256(artifact.content).hexdigest()
font_line = re.compile(
    r"^(?P<name>\S+)\s+.+?\s+\S+\s+"
    r"(?P<embedded>yes|no)\s+(?P<subset>yes|no)\s+"
    r"(?P<unicode>yes|no)\s+\d+\s+\d+$"
)
with TemporaryDirectory(prefix="lawcase-direct-pdf-font-preflight-") as temporary:
    path = Path(temporary) / "preflight.pdf"
    path.write_bytes(artifact.content)
    result = subprocess.run(
        ["/usr/bin/pdffonts", str(path)],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
    )
rows = [
    match.groupdict()
    for line in result.stdout.splitlines()
    if (match := font_line.fullmatch(line.strip())) is not None
]
assert rows
assert all(
    row["embedded"] == row["subset"] == row["unicode"] == "yes"
    for row in rows
)
names = " ".join(row["name"] for row in rows).casefold()
assert "umingcn" in names and "ukaicn" in names and "wenquanyimicrohei" in names
assert "helvetica" not in names and "stsong" not in names
assert approved_draft_worker_module._pdf_text_runs(
    "℃ǎ", approved_draft_worker_module._PDF_HEADING_FONT
) == ((approved_draft_worker_module._PDF_TEXT_FONT, "℃ǎ"),)
assert approved_draft_worker_module._pdf_text_runs(
    "㖞", approved_draft_worker_module._PDF_SECONDARY_FONT
) == ((approved_draft_worker_module._PDF_TEXT_FONT, "㖞"),)
for unsafe_text, expected_codepoint in (
    ("\U00020021", "U+20021"),
    ("e\u0301", "U+0301"),
    ("\u200d", "U+200D"),
    ("\u00ad", "U+00AD"),
    ("\ufffc", "U+FFFC"),
    ("\ufffd", "U+FFFD"),
):
    try:
        create_pdf_draft(
            ApprovedDraft(
                title=f"案件审阅意见候选{unsafe_text}",
                sections=(
                    ApprovedSection(
                        heading="一、原文语义门禁",
                        paragraphs=("不得静默改变法律原文。",),
                        source_refs=("preflight-source-002",),
                    ),
                ),
                approval_hash=sha256(
                    f"lawcase-direct-pdf-{expected_codepoint}".encode("ascii")
                ).hexdigest(),
            )
        )
    except ApprovedDraftBlocked as error:
        assert expected_codepoint in str(error)
    else:
        raise AssertionError(f"unsafe direct-PDF input passed: {expected_codepoint}")
PY
COPY deployment/web/api-gate.py /app/web_api_gate.py
ENV PYTHONPATH=/app/backend
USER lawcase

EXPOSE 8080
CMD ["sh", "-c", "if [ \"$LAWCASE_WEB_RUNTIME_MODE\" = \"SETUP_GATED\" ] || [ -z \"$LAWCASE_WEB_RUNTIME_MODE\" ]; then exec uvicorn web_api_gate:app --host 0.0.0.0 --port 8080 --no-access-log; else exec uvicorn case_api.web_runtime:create_web_runtime_app --factory --host 0.0.0.0 --port 8080 --no-access-log; fi"]
