"""Server-only source re-reading for lawyer fact-correction proposals."""
from hashlib import sha256

from case_kernel.case_agent_fact_correction_postgres import FactCorrectionBlocked
from case_kernel.case_agent_ledger_extraction import parse_case_ledger_extraction_candidate
from case_kernel.models import Role


class WebFactCorrectionOriginalReader:
    def __init__(self, *, artifact_review_service, evidence_projection, matter_store):
        for value, method in ((artifact_review_service,"read_verified_extraction_original"),
                              (evidence_projection,"load_pages"),(matter_store,"get")):
            if not callable(getattr(value,method,None)):
                raise ValueError("fact correction original reader is not fully configured")
        self._artifacts, self._pages, self._matters = artifact_review_service, evidence_projection, matter_store

    def read_verified_original(self, *, actor, matter_id, artifact_id, expected_matter_version):
        if Role.SYSTEM_WORKER in actor.roles or not actor.roles.intersection({Role.LEAD_LAWYER,Role.REVIEWER}):
            raise FactCorrectionBlocked("lawyer source review required")
        def version():
            return self._matters.get(matter_id,firm_id=actor.firm_id).version
        if version() != expected_matter_version:
            raise FactCorrectionBlocked("matter changed before source review")
        raw = self._artifacts.read_verified_extraction_original(
            actor=actor,matter_id=matter_id,artifact_id=artifact_id)
        value = parse_case_ledger_extraction_candidate(raw)
        sources = value["_source_pages"]
        # The existing original-PDF reader does not authenticate OCR results.
        # Never substitute native text and pretend OCR lineage was re-proven.
        if any(page["source_mode"] != "NATIVE_TEXT" for page in sources):
            raise FactCorrectionBlocked("OCR/visual correction requires its independent source reader")
        ids = tuple(sorted(page["evidence_page_id"] for page in sources))
        actual = self._pages.load_pages(actor=actor,matter_id=matter_id,evidence_page_ids=ids)
        by_id = {page.evidence_page_id: page for page in actual}
        if len(actual) != len(ids) or set(by_id) != set(ids):
            raise FactCorrectionBlocked("correction original page set differs")
        for source in sources:
            page = by_id[source["evidence_page_id"]]
            if (page.source_file_sha256 != source["source_file_sha256"]
                or page.page_number != source["page_number"]
                or sha256(page.extracted_text.encode()).hexdigest() != source["source_text_sha256"]
                or page.extracted_text_sha256 != source["source_text_sha256"]):
                raise FactCorrectionBlocked("correction original page content differs")
        for candidate in value["candidates"]:
            if any(excerpt["text"] not in by_id[excerpt["evidence_page_id"]].extracted_text
                   for excerpt in candidate["supporting_excerpts"]):
                raise FactCorrectionBlocked("correction original excerpt no longer matches")
        if version() != expected_matter_version:
            raise FactCorrectionBlocked("matter changed during source review")
        return raw
