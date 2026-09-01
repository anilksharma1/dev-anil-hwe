"""Tests for the 2.9.17 scanned-PDF BDE recovery.

Covers: apply_scanned_bde (promote-only, NR-untouched, File-id heuristic + LLM count +
page extrapolation), the apply_ocr page-cap partial/full branches, and the PDF page-split
helper. None of this may touch the responsiveness (NR) decision.
"""
# --- MERGE NOTE (3.0.0) ------------------------------------------------------
# This file tests the 2.9.17 partial-coverage OCR / scanned-BDE design:
#   enrich.apply_scanned_bde, azure_clients._first_n_pages_pdf
# Those symbols exist in NO version we were given (classic 2.10.2, Daniel 2.9.9,
# new-Anna 2.11.0) -- confirmed by grep across all three trees. The tests describe
# a real and coherent feature (OCR a PAGE SAMPLE of a large scan, keep the file
# non-searchable so the sampled text never reaches the NR decision, and extrapolate
# a BDE person-count from sample + page ratio). That design is directly relevant to
# the OCR cost work, but it is NOT part of this merge and would change the OCR
# contract, so the tests are skipped rather than deleted -- deleting them would lose
# the specification. See PROJECT_PLAN.md Q-2.
# ----------------------------------------------------------------------------
import unittest as _unittest
raise _unittest.SkipTest(
    "2.9.17 partial-coverage OCR design not present in any available version "
    "(enrich.apply_scanned_bde, azure_clients._first_n_pages_pdf) -- see PROJECT_PLAN.md Q-2")

import io
import unittest

from pii_triage.routing import FileRecord
from pii_triage.enrich import apply_scanned_bde, apply_ocr
from pii_triage.azure_clients import _first_n_pages_pdf


class Cfg:
    bde_threshold = 51
    bde_scanned_pdf = True
    ocr_max_pages = 15
    phase_timing = False


def _rec(**kw):
    d = dict(rel_path="r", file_name="scan.pdf", ext=".pdf", size_bytes=2_000_000,
             status="ok", searchable=False, is_structured=False, estimated_entities=0,
             value_signal=False, llm_consulted=False, llm_responsive="",
             page_or_sheet_count=128, bde_person_count=0, bde_confirmed=False)
    d.update(kw)
    return FileRecord(**d)


def _fileid_text(n, pages=15):
    """OCR'd sample text with n distinct 'File: <id>' register markers."""
    lines = [f"Ackerman, Robert  File: {10000 + i}  Dept 07  $54,000" for i in range(n)]
    return "\n".join(lines)


def _llm(count, tokens=7):
    return lambda text, cfg: {"person_count": count, "tokens": tokens}


class TestScannedBDE(unittest.TestCase):
    def test_fileid_heuristic_extrapolates_without_llm(self):
        # 15 people found in the first 15 pages of a 128-page scan -> ~128 total.
        rec = _rec()
        meta = {"ocr_sample_text": _fileid_text(15), "ocr_sample_pages": 15}
        apply_scanned_bde(rec, meta, Cfg(), None)   # no LLM
        self.assertEqual(rec.bde_person_count, 128)  # 15 * 128/15
        self.assertIn("scanned_bde_ocr", rec.detail)

    def test_llm_count_used_when_larger(self):
        rec = _rec(page_or_sheet_count=128)
        meta = {"ocr_sample_text": "roster page ...", "ocr_sample_pages": 15}
        apply_scanned_bde(rec, meta, Cfg(), _llm(45))   # 45 in sample -> 45*128/15 = 384
        self.assertEqual(rec.bde_person_count, 384)

    def test_takes_max_of_heuristic_and_llm(self):
        rec = _rec(page_or_sheet_count=30)
        meta = {"ocr_sample_text": _fileid_text(10), "ocr_sample_pages": 15}
        # heuristic 10 -> 20; llm 5 -> 10; max sample = 10 -> extrapolate to 20
        apply_scanned_bde(rec, meta, Cfg(), _llm(5))
        self.assertEqual(rec.bde_person_count, 20)

    def test_never_touches_nr_fields(self):
        rec = _rec(llm_consulted=True, llm_responsive="no", estimated_entities=3,
                   searchable=False)
        meta = {"ocr_sample_text": _fileid_text(15), "ocr_sample_pages": 15}
        apply_scanned_bde(rec, meta, Cfg(), _llm(600))
        self.assertEqual(rec.llm_responsive, "no")      # untouched
        self.assertEqual(rec.estimated_entities, 3)     # NR-fallback input untouched
        self.assertFalse(rec.searchable)                # never promoted to searchable
        self.assertGreaterEqual(rec.bde_person_count, 51)

    def test_promote_only(self):
        rec = _rec(bde_person_count=900)
        meta = {"ocr_sample_text": _fileid_text(15), "ocr_sample_pages": 15}
        apply_scanned_bde(rec, meta, Cfg(), _llm(10))   # would compute less -> ignored
        self.assertEqual(rec.bde_person_count, 900)

    def test_skips_searchable_file(self):
        rec = _rec(searchable=True)
        meta = {"ocr_sample_text": _fileid_text(15), "ocr_sample_pages": 15}
        apply_scanned_bde(rec, meta, Cfg(), _llm(600))
        self.assertEqual(rec.bde_person_count, 0)

    def test_skips_non_pdf(self):
        rec = _rec(ext=".tiff", file_name="scan.tiff")
        meta = {"ocr_sample_text": _fileid_text(15), "ocr_sample_pages": 15}
        apply_scanned_bde(rec, meta, Cfg(), _llm(600))
        self.assertEqual(rec.bde_person_count, 0)

    def test_skips_when_disabled(self):
        class Off(Cfg):
            bde_scanned_pdf = False
        rec = _rec()
        meta = {"ocr_sample_text": _fileid_text(15), "ocr_sample_pages": 15}
        apply_scanned_bde(rec, meta, Off(), _llm(600))
        self.assertEqual(rec.bde_person_count, 0)

    def test_skips_when_already_bde(self):
        rec = _rec(estimated_entities=200)
        meta = {"ocr_sample_text": _fileid_text(15), "ocr_sample_pages": 15}
        apply_scanned_bde(rec, meta, Cfg(), _llm(600))
        self.assertEqual(rec.bde_person_count, 0)

    def test_no_sample_is_noop(self):
        rec = _rec()
        apply_scanned_bde(rec, {}, Cfg(), _llm(600))
        self.assertEqual(rec.bde_person_count, 0)

    def test_no_people_found_is_noop(self):
        rec = _rec()
        meta = {"ocr_sample_text": "cover page, no roster here", "ocr_sample_pages": 15}
        apply_scanned_bde(rec, meta, Cfg(), _llm(0))
        self.assertEqual(rec.bde_person_count, 0)


class TestApplyOcrPageCap(unittest.TestCase):
    def _ocr_fn(self, text, sample_pages):
        def fn(path, cfg):
            return text, {"text_extractable": "text", "ocr": True,
                          "page_or_sheet_count": sample_pages,
                          "ocr_sample_pages": sample_pages}
        return fn

    def test_partial_scan_stays_nonsearchable_and_stashes_sample(self):
        # 128-page scan, OCR covered only 15 pages -> partial -> NOT searchable.
        meta_in = {"text_extractable": "image_only", "page_or_sheet_count": 128}
        text, meta = apply_ocr("scan.pdf", "", meta_in, Cfg(),
                               self._ocr_fn("File: 10001 ...", 15))
        self.assertEqual(meta["text_extractable"], "image_only")   # NOT promoted
        self.assertEqual(text, "")                                 # no searchable text exposed
        self.assertEqual(meta["ocr_sample_text"], "File: 10001 ...")
        self.assertEqual(meta["ocr_sample_pages"], 15)
        self.assertIn("ocr_sampled:15/128", meta["detail"])

    def test_full_coverage_scan_becomes_searchable(self):
        # 8-page scan fully covered by OCR (<= ocr_max_pages) -> behaves as before.
        meta_in = {"text_extractable": "image_only", "page_or_sheet_count": 8}
        text, meta = apply_ocr("scan.pdf", "", meta_in, Cfg(),
                               self._ocr_fn("full ocr text", 8))
        self.assertEqual(meta["text_extractable"], "text")
        self.assertTrue(meta["ocr"])
        self.assertEqual(text, "full ocr text")

    def test_ocr_disabled_is_noop(self):
        meta_in = {"text_extractable": "image_only", "page_or_sheet_count": 128}
        text, meta = apply_ocr("scan.pdf", "", meta_in, Cfg(), None)
        self.assertEqual(meta["text_extractable"], "image_only")
        self.assertNotIn("ocr_sample_text", meta)

    def test_ocr_failure_fails_safe(self):
        def boom(path, cfg):
            raise TimeoutError("simulated hard timeout")
        meta_in = {"text_extractable": "image_only", "page_or_sheet_count": 128}
        text, meta = apply_ocr("scan.pdf", "", meta_in, Cfg(), boom)
        self.assertEqual(meta["text_extractable"], "image_only")   # not cleared, not promoted
        self.assertIn("ocr_failed:TimeoutError", meta["detail"])


class TestFirstNPagesPdf(unittest.TestCase):
    def _make_pdf(self, n):
        import pypdf
        w = pypdf.PdfWriter()
        for _ in range(n):
            w.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        w.write(buf)
        return buf.getvalue()

    def test_caps_pages(self):
        data = self._make_pdf(5)
        capped, kept = _first_n_pages_pdf(data, 2)
        self.assertEqual(kept, 2)
        import pypdf
        self.assertEqual(len(pypdf.PdfReader(io.BytesIO(capped)).pages), 2)

    def test_shorter_than_cap_returns_all(self):
        data = self._make_pdf(3)
        capped, kept = _first_n_pages_pdf(data, 10)
        self.assertEqual(kept, 3)

    def test_zero_cap_is_noop(self):
        data = self._make_pdf(3)
        capped, kept = _first_n_pages_pdf(data, 0)
        self.assertEqual(kept, 0)
        self.assertEqual(capped, data)


if __name__ == "__main__":
    unittest.main()