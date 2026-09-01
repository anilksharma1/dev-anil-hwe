"""2.9.20: a likely scan bails out of the extract_text sweep after a few pages (so big
scans stop blowing the watchdog), while genuine text PDFs read to completion unchanged."""
import unittest
import pii_triage.extractors as ex
from pii_triage.extractors import x_pdf


class Cfg:
    pdf_max_extract_pages = 300
    pdf_scan_probe_pages = 5
    pdf_open_timeout = 30
    phase_timing = False
    max_scan_chars = 5_000_000


class _Page:
    def __init__(self, t): self._t = t
    def extract_text(self):
        _FakeReader.calls += 1
        return self._t


class _FakeReader:
    calls = 0
    def __init__(self, path, pages_text):
        self.pages = [_Page(t) for t in pages_text]


def _install(pages_text, image_based):
    _FakeReader.calls = 0
    class FakePypdf:
        PdfReader = lambda path: _FakeReader(path, pages_text)
    ex.pypdf = FakePypdf
    ex._pdf_image_based = lambda reader, pages: image_based
    ex._pdf_field_values = lambda reader: ""
    ex._pdf_extract_content_images = lambda reader: []


@unittest.skipUnless(ex.HAVE_PYPDF, "pypdf not installed")
class TestScanEarlyExit(unittest.TestCase):
    def setUp(self):
        self._save = (ex.pypdf, ex._pdf_image_based, ex._pdf_field_values, ex._pdf_extract_content_images)

    def tearDown(self):
        (ex.pypdf, ex._pdf_image_based, ex._pdf_field_values, ex._pdf_extract_content_images) = self._save

    def test_scan_stops_early(self):
        # 200 empty-text pages, image-based -> should stop ~probe_pages, not read all 200
        _install([""] * 200, image_based=True)
        text, meta = x_pdf("scan.pdf", Cfg(), rules=None)
        self.assertEqual(meta["text_extractable"], "image_only")
        self.assertLessEqual(_FakeReader.calls, 10)          # not 200
        self.assertEqual(meta["page_or_sheet_count"], 200)   # still reports true page count

    def test_text_pdf_reads_fully(self):
        # 40 pages of real text, not image-based -> reads all pages, stays 'text'
        _install(["Real sentence with lots of words. " * 40] * 40, image_based=False)
        text, meta = x_pdf("doc.pdf", Cfg(), rules=None)
        self.assertEqual(meta["text_extractable"], "text")
        self.assertEqual(_FakeReader.calls, 40)              # every page read

    def test_sparse_but_not_image_based_reads_on(self):
        # sparse text but NOT image-based (e.g. a mostly-blank form) -> must NOT early-exit
        _install([""] * 30, image_based=False)
        text, meta = x_pdf("form.pdf", Cfg(), rules=None)
        self.assertEqual(_FakeReader.calls, 30)              # read all -> no false early-exit


if __name__ == "__main__":
    unittest.main()