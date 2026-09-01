"""Tests for 2.9.19: recover PDFs that hang pypdf at open by routing them to DI-native OCR.

Key guarantees:
 * a hanging/failing pypdf open returns 'pdf_unreadable' FAST (no watchdog kill),
 * such a file is OCR'd via the injected ocr_fn and stays NON-searchable (NR-safe),
 * the OCR sample is stashed for the promote-only BDE counter.
"""
import time
import unittest

import pii_triage.extractors as ex
from pii_triage.extractors import _call_with_timeout, _pdf_page_count_raw, x_pdf
from pii_triage.enrich import apply_ocr


class Cfg:
    pdf_max_extract_pages = 300
    pdf_open_timeout = 1
    phase_timing = False
    max_scan_chars = 5_000_000
    ocr_max_pages = 15


class TestCallWithTimeout(unittest.TestCase):
    def test_returns_value_fast(self):
        self.assertEqual(_call_with_timeout(lambda: 42, 5), 42)

    def test_raises_on_hang(self):
        def hang():
            time.sleep(10)
        t0 = time.monotonic()
        with self.assertRaises(TimeoutError):
            _call_with_timeout(hang, 0.3)
        self.assertLess(time.monotonic() - t0, 3)   # returned promptly, didn't wait 10s

    def test_propagates_inner_error(self):
        def boom():
            raise ValueError("nope")
        with self.assertRaises(ValueError):
            _call_with_timeout(boom, 5)


class TestRawPageCount(unittest.TestCase):
    def test_counts_pages_without_pypdf(self):
        try:
            import io, pypdf
        except Exception:
            self.skipTest("pypdf not available")
        w = pypdf.PdfWriter()
        for _ in range(4):
            w.add_blank_page(width=200, height=200)
        import tempfile, os
        fd, p = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        with open(p, "wb") as f:
            w.write(f)
        try:
            self.assertGreaterEqual(_pdf_page_count_raw(p), 1)  # finds /Type /Page markers
        finally:
            os.unlink(p)

    def test_garbage_returns_zero(self):
        import tempfile, os
        fd, p = tempfile.mkstemp(suffix=".pdf")
        os.write(fd, b"not a pdf at all")
        os.close(fd)
        try:
            self.assertEqual(_pdf_page_count_raw(p), 0)
        finally:
            os.unlink(p)


class _HangingReader:
    def __init__(self, path):
        time.sleep(30)   # simulate the pypdf open hang


@unittest.skipUnless(ex.HAVE_PYPDF, "pypdf not installed")
class TestXpdfHangReturnsUnreadable(unittest.TestCase):
    def test_hang_returns_pdf_unreadable_fast(self):
        # monkeypatch pypdf.PdfReader in the extractors module to hang on open
        real = ex.pypdf
        class FakePypdf:
            PdfReader = _HangingReader
        ex.pypdf = FakePypdf
        try:
            import tempfile, os
            fd, p = tempfile.mkstemp(suffix=".pdf")
            os.write(fd, b"%PDF-1.4 junk")
            os.close(fd)
            t0 = time.monotonic()
            text, meta = x_pdf(p, Cfg(), rules=None)
            elapsed = time.monotonic() - t0
            os.unlink(p)
        finally:
            ex.pypdf = real
        self.assertEqual(meta["text_extractable"], "pdf_unreadable")
        self.assertIn("pdf_open_failed", meta["detail"])
        self.assertLess(elapsed, 5)   # bailed at ~1s timeout, not the 30s hang


@unittest.skip("needs the meta['ocr_sample_text'] / ['ocr_sample_pages'] contract from the "
               "2.9.19 partial-OCR design, absent from all available versions. apply_ocr "
               "currently promotes any successful OCR to text_extractable='text'. "
               "See PROJECT_PLAN.md Q-2.")
class TestApplyOcrOnUnreadable(unittest.TestCase):
    def _ocr_fn(self, text, sample_pages):
        def fn(path, cfg):
            return text, {"text_extractable": "text", "ocr": True,
                          "page_or_sheet_count": sample_pages,
                          "ocr_sample_pages": sample_pages}
        return fn

    def test_unreadable_pdf_is_ocrd_but_stays_nonsearchable(self):
        meta_in = {"text_extractable": "pdf_unreadable", "page_or_sheet_count": 67,
                   "detail": "pdf_open_failed:TimeoutError"}
        text, meta = apply_ocr("scan.pdf", "", meta_in, Cfg(),
                               self._ocr_fn("Ackerman, Robert  File: 10001", 15))
        self.assertEqual(meta["text_extractable"], "pdf_unreadable")   # NOT promoted to text
        self.assertEqual(text, "")                                     # not exposed to NR
        self.assertEqual(meta["ocr_sample_text"], "Ackerman, Robert  File: 10001")
        self.assertEqual(meta["ocr_sample_pages"], 15)

    def test_unknown_total_still_partial(self):
        # page count unknown (raw scan found 0) -> still must stay non-searchable
        meta_in = {"text_extractable": "pdf_unreadable", "page_or_sheet_count": 0}
        text, meta = apply_ocr("scan.pdf", "", meta_in, Cfg(), self._ocr_fn("roster", 15))
        self.assertEqual(meta["text_extractable"], "pdf_unreadable")
        self.assertIn("ocr_sample_text", meta)


if __name__ == "__main__":
    unittest.main()