"""OCR / Document Intelligence accounting -- the numbers that were previously invisible.

Three real defects are pinned here:

1. apply_ocr REPLACES meta on success, rewriting text_extractable from "image_only" to
   "text". So that column counted OCR *failures*, and a successfully OCR'd file was
   identifiable nowhere. `ocr` / `ocr_attempted` survive the replacement.

2. apply_image_ocr returned (text, None) whenever no image yielded text, so a PDF whose
   images all OCR'd to nothing burned billable DI calls and left NO trace at all. It is
   the largest OCR cost centre (~95% of DI calls on the CNG corpus) and was the least
   observable part of the tool.

3. _pdf_extract_content_images needs Pillow for `img.image`, inside a bare
   `except Exception: continue`. With no Pillow EVERY image is silently skipped and
   embedded-image OCR becomes a no-op -- which is what happened in Daniel's run: 0
   embedded calls against Anna's 24,223 on the identical corpus, with nothing reporting it.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii_triage import extractors as ex
from pii_triage.config import Config
from pii_triage.enrich import apply_ocr, apply_image_ocr


def _cfg(**kw):
    c = Config(root=".")
    c.use_ocr = True
    for k, v in kw.items():
        setattr(c, k, v)
    return c


class TestFullFileOcrAccounting(unittest.TestCase):
    def test_success_records_attempt_and_pages_despite_meta_replacement(self):
        def ocr_fn(path, cfg):
            return "recovered text", {"text_extractable": "text", "page_or_sheet_count": 12}

        text, meta = apply_ocr("scan.pdf", "", {"text_extractable": "image_only"},
                               _cfg(), ocr_fn)
        self.assertEqual(text, "recovered text")
        self.assertEqual(meta["text_extractable"], "text")   # legacy behaviour preserved
        self.assertTrue(meta["ocr"])
        self.assertTrue(meta["ocr_attempted"])               # ...and the attempt survives
        self.assertEqual(meta["ocr_pages"], 12)

    def test_failure_is_still_a_billable_attempt(self):
        def boom(path, cfg):
            raise RuntimeError("DI 500")

        _, meta = apply_ocr("scan.pdf", "", {"text_extractable": "image_only"}, _cfg(), boom)
        self.assertTrue(meta["ocr_attempted"], "a failed DI call is still billable")
        self.assertFalse(meta.get("ocr", False))
        self.assertEqual(meta["ocr_pages"], 0)
        self.assertIn("ocr_failed", meta["detail"])

    def test_no_ocr_fn_records_no_attempt(self):
        _, meta = apply_ocr("scan.pdf", "", {"text_extractable": "image_only"}, _cfg(), None)
        self.assertFalse(meta.get("ocr_attempted", False))

    def test_text_file_is_never_ocrd(self):
        calls = []
        _, meta = apply_ocr("a.txt", "already text", {"text_extractable": "text"}, _cfg(),
                            lambda p, c: calls.append(1) or ("x", {}))
        self.assertEqual(calls, [])
        self.assertFalse(meta.get("ocr_attempted", False))


class TestEmbeddedImageOcrAccounting(unittest.TestCase):
    """Defect 2: the calls that used to vanish."""

    def _meta(self, *sizes):
        return {"content_images": [(i + 1, b"x" * n) for i, n in enumerate(sizes)]}

    def test_calls_recorded_even_when_no_image_yields_text(self):
        stats_holder = {}

        def silent(path, cfg):
            return "", {}          # a real case: photos and charts OCR to nothing

        text, summary, stats = apply_image_ocr("body", self._meta(5000, 6000, 7000),
                                               _cfg(), silent)
        self.assertEqual(stats["qualifying"], 3)
        self.assertEqual(stats["calls"], 3, "three billable calls were made")
        self.assertEqual(stats["ok"], 0)
        self.assertEqual(text, "body")
        self.assertIsNotNone(summary, "this is the case that used to leave no trace")
        self.assertIn("img_ocr:none", summary)

    def test_qualifying_and_calls_differ_by_the_sub_1kb_skips(self):
        text, summary, stats = apply_image_ocr("body", self._meta(500, 900, 5000),
                                               _cfg(), lambda p, c: ("found", {}))
        self.assertEqual(stats["qualifying"], 3, "three images were found...")
        self.assertEqual(stats["calls"], 1, "...but two were under 1KB and never sent")
        self.assertEqual(stats["skipped_small"], 2)
        self.assertEqual(stats["ok"], 1)

    def test_per_image_failure_is_counted_not_swallowed(self):
        def flaky(path, cfg):
            raise RuntimeError("throttled")

        _, _, stats = apply_image_ocr("body", self._meta(5000, 5000), _cfg(), flaky)
        self.assertEqual(stats["calls"], 2)
        self.assertEqual(stats["failed"], 2)
        self.assertEqual(stats["ok"], 0)

    def test_no_image_ocr_flag_makes_it_free(self):
        calls = []
        _, summary, stats = apply_image_ocr(
            "body", self._meta(5000, 5000), _cfg(use_image_ocr=False),
            lambda p, c: calls.append(1) or ("t", {}))
        self.assertEqual(calls, [], "--no-image-ocr must make zero DI calls")
        self.assertEqual(stats["calls"], 0)
        self.assertIsNone(summary)

    def test_stats_returned_even_with_no_images(self):
        _, summary, stats = apply_image_ocr("body", {}, _cfg(), lambda p, c: ("t", {}))
        self.assertEqual(stats["calls"], 0)
        self.assertEqual(stats["qualifying"], 0)
        self.assertIsNone(summary)


class TestPillowVisibility(unittest.TestCase):
    """Defect 3: a missing Pillow must be loud, not free."""

    def test_pillow_is_probed(self):
        self.assertIn("HAVE_PILLOW", dir(ex))

    def test_pillow_appears_in_the_dependency_report_when_absent(self):
        orig = ex.HAVE_PILLOW
        ex.HAVE_PILLOW = False
        try:
            self.assertIn("Pillow", ex.optional_dependency_report())
        finally:
            ex.HAVE_PILLOW = orig

    def test_undecodable_images_are_counted_not_silently_dropped(self):
        """Simulate a missing Pillow: img.image raises for every image. The old code
        returned [] with no record; now the failures are counted."""
        class _Img:
            name = "im1"

            @property
            def image(self):
                raise ImportError("no Pillow")

        class _Page:
            images = [_Img()]

        class _Reader:
            pages = [_Page(), _Page()]

        result = ex._pdf_extract_content_images(_Reader())
        self.assertIsInstance(result, tuple)
        images, stats = result
        self.assertEqual(images, [])
        # Two pages, same XObject name, and BOTH are counted: the `seen` dedup set is only
        # populated after a SUCCESSFUL decode, so a failing image is re-examined on every
        # page it appears on. That makes these counters decode-attempt counts rather than
        # distinct-image counts -- fine for diagnosing a missing Pillow (the signal is
        # "nonzero"), but do not read decode_failed as a distinct-image total.
        self.assertEqual(stats["seen"], 2)
        self.assertEqual(stats["decode_failed"], 2)
        self.assertGreater(stats["decode_failed"], 0,
                           "the missing-Pillow signal must be non-zero, not silent")

    def test_no_images_is_distinguishable_from_no_pillow(self):
        class _EmptyPage:
            images = []

        class _Reader:
            pages = [_EmptyPage()]

        images, stats = ex._pdf_extract_content_images(_Reader())
        self.assertEqual(images, [])
        self.assertEqual(stats["seen"], 0)
        self.assertEqual(stats["decode_failed"], 0)
        # seen==0/failed==0 vs seen>0/failed>0 is what tells the two cases apart.


class TestTallyAndCost(unittest.TestCase):
    def test_tally_sums_di_calls_across_files(self):
        from pii_triage.runner import _tally
        counters, lanes, llm, ocr, s2 = {}, {}, {}, {}, {}
        rows = [
            {"status": "ok", "suggested_lane": "standard", "ocr_attempted": "True",
             "ocr": "True", "ocr_pages": "12", "img_ocr_qualifying": "3",
             "img_ocr_calls": "2", "img_ocr_ok": "1", "img_decode_failed": "0",
             "di_calls": "3", "llm_tokens": "100", "s2_ran": "True",
             "s2_llm_consulted": "True", "s2_llm_responsiveness": "clear_yes",
             "s2_nr": "False", "s2_llm_tokens": "50"},
            {"status": "ok", "suggested_lane": "likely_non_responsive",
             "ocr_attempted": "False", "ocr": "False", "ocr_pages": "0",
             "img_ocr_qualifying": "0", "img_ocr_calls": "0", "img_ocr_ok": "0",
             "img_decode_failed": "5", "di_calls": "0", "llm_tokens": "80",
             "s2_ran": "False", "s2_skip_reason": "stage1_nr", "s2_llm_tokens": "0"},
        ]
        for r in rows:
            _tally(r, counters, lanes, llm, ocr, s2)
        self.assertEqual(ocr["di_calls"], 3)
        self.assertEqual(ocr["full_file_calls"], 1)
        self.assertEqual(ocr["full_file_ok"], 1)
        self.assertEqual(ocr["pages"], 12)
        self.assertEqual(ocr["img_calls"], 2)
        self.assertEqual(ocr["img_qualifying"], 3)
        self.assertEqual(ocr["img_decode_failed"], 5)
        self.assertEqual(ocr["files_with_di"], 1)
        self.assertEqual(s2["ran"], 1)
        self.assertEqual(s2["skip:stage1_nr"], 1)
        self.assertEqual(s2["tokens"], 50)
        self.assertEqual(s2["level:clear_yes"], 1)

    def test_cost_summary_reports_units_without_prices(self):
        from pii_triage.runner import _cost_summary
        c = _cost_summary(_cfg(), {"tokens": 1000}, {"di_calls": 10, "pages": 4, "img_calls": 6},
                          {"tokens": 500})
        self.assertEqual(c["llm_tokens_total"], 1500)
        self.assertEqual(c["di_calls"], 10)
        self.assertEqual(c["di_pages_billable"], 10)   # 4 full-file pages + 6 image pages
        self.assertNotIn("di_cost", c)                 # no price given -> no invented money

    def test_cost_summary_reports_money_with_prices(self):
        from pii_triage.runner import _cost_summary
        cfg = _cfg(price_per_1k_in=0.10, price_per_1k_out=0.40, price_per_1k_pages=10.0)
        c = _cost_summary(cfg, {"tokens": 1_000_000}, {"di_calls": 25_000, "pages": 1_000,
                                                      "img_calls": 24_000}, {"tokens": 0})
        self.assertEqual(c["di_pages_billable"], 25_000)
        self.assertEqual(c["di_cost"], 250.0)
        self.assertEqual(c["llm_cost_low"], 100.0)
        self.assertEqual(c["llm_cost_high"], 400.0)
        self.assertIn("split unknown", c["llm_cost_note"])
        self.assertIn("di_share_of_spend_pct", c)


if __name__ == "__main__":
    unittest.main()
