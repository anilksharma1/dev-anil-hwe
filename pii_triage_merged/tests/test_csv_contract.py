"""The inventory CSV is an interface, not an implementation detail.

report.build_table1, benchmark.run_benchmark and the offline BDE scorer all read this CSV
BY COLUMN NAME. Merging two tools' outputs is exactly the situation where a rename or a
reorder silently breaks a downstream consumer with no test failing, so the schema is pinned
here: the 27 legacy columns keep their names, meanings and ORDER, and everything new is
appended after them.
"""
import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii_triage.report import build_table1
from pii_triage.routing import (FIELDNAMES, LEGACY_FIELDNAMES, STAGE2_FIELDNAMES,
                                FileRecord, NR_LANE)

EXPECTED_FIELDNAMES = [
    # --- the 27 columns that existed in 2.10.2, in order ---
    "rel_path", "file_name", "ext", "size_bytes", "status", "searchable", "programmatic",
    "text_extractable", "is_structured", "page_or_sheet_count", "attachment_count",
    "estimated_entities", "estimate_truncated", "bde_person_count", "bde_confirmed",
    "entity_bucket", "entities_found", "value_signal", "pi_categories", "is_bde",
    "complexity_bucket", "ambiguity", "llm_consulted", "llm_responsive", "llm_tokens",
    "suggested_lane", "detail",
    # --- OCR / DI accounting (3.0.0) ---
    "ocr_attempted", "ocr", "ocr_pages", "img_ocr_qualifying", "img_ocr_calls",
    "img_ocr_ok", "img_decode_failed", "di_calls", "elapsed_s",
    # --- derived stage-1 answers ---
    "nr_stage1", "bde_stage1",
    # --- stage 2 ---
    "s2_ran", "s2_skip_reason", "s2_llm_consulted", "s2_llm_responsiveness",
    "s2_llm_responsive", "s2_llm_tokens", "s2_is_bde", "s2_lane", "s2_nr", "s2_detail",
    # --- rollup ---
    "llm_tokens_total",
]


class TestSchemaFrozen(unittest.TestCase):
    def test_fieldnames_exact_and_ordered(self):
        self.assertEqual(FIELDNAMES, EXPECTED_FIELDNAMES)

    def test_legacy_27_come_first_in_original_order(self):
        self.assertEqual(FIELDNAMES[:27], LEGACY_FIELDNAMES)
        self.assertEqual(len(LEGACY_FIELDNAMES), 27)

    def test_stage2_block_is_all_prefixed(self):
        for f in STAGE2_FIELDNAMES:
            self.assertTrue(f.startswith("s2_"), f)
            self.assertIn(f, FIELDNAMES)

    def test_no_legacy_field_is_a_stage2_field(self):
        self.assertEqual(set(LEGACY_FIELDNAMES) & set(STAGE2_FIELDNAMES), set())

    def test_llm_reasoning_does_not_exist(self):
        """Daniel's build wrote the model's reasoning to the CSV while its README promised
        no PII in output, and the prompt asks the model to quote the value it found. The
        column is removed outright -- not flag-gated."""
        self.assertNotIn("llm_reasoning", FIELDNAMES)
        self.assertNotIn("s2_llm_reasoning", FIELDNAMES)
        self.assertFalse(hasattr(FileRecord(rel_path="a", file_name="b", ext=".c",
                                            size_bytes=1), "llm_reasoning"))


class TestDownstreamConsumers(unittest.TestCase):
    def test_report_columns_present(self):
        for c in ("rel_path", "ext", "programmatic", "entities_found", "entity_bucket",
                  "searchable", "status"):
            self.assertIn(c, FIELDNAMES, c)

    def test_benchmark_columns_present(self):
        for c in ("rel_path", "file_name", "suggested_lane", "is_bde", "is_structured",
                  "llm_consulted", "llm_responsive", "entities_found"):
            self.assertIn(c, FIELDNAMES, c)

    def test_score_bde_columns_present(self):
        for c in ("bde_person_count", "estimated_entities", "is_bde", "is_structured",
                  "entity_bucket", "file_name", "detail", "searchable"):
            self.assertIn(c, FIELDNAMES, c)

    def test_build_table1_still_works_on_the_merged_schema(self):
        with tempfile.TemporaryDirectory() as td:
            inv = os.path.join(td, "inv.csv")
            out = os.path.join(td, "t1.csv")
            with open(inv, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=FIELDNAMES)
                w.writeheader()
                base = {f: "" for f in FIELDNAMES}
                w.writerow({**base, "rel_path": "a.pdf", "ext": ".pdf", "status": "ok",
                            "searchable": "True", "programmatic": "False",
                            "entities_found": "Name | SSN", "entity_bucket": "1-10",
                            "s2_lane": "standard", "di_calls": "2"})
                w.writerow({**base, "rel_path": "b.jpg", "ext": ".jpg", "status": "ok",
                            "searchable": "False", "entity_bucket": ""})      # filtered: not searchable
                w.writerow({**base, "rel_path": "c.pdf", "ext": ".pdf", "status": "error",
                            "searchable": "True"})                            # filtered: status
            build_table1(inv, out)
            with open(out, newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
                fh.seek(0)
                header = next(csv.reader(fh))
        self.assertEqual(header, ["File ID", "File Type", "Searchable", "Programmatic",
                                  "Entities Found", "Entity Bucket"])
        self.assertEqual(len(rows), 1, "only the searchable+ok row should survive")
        self.assertEqual(rows[0]["File ID"], "a.pdf")
        self.assertEqual(rows[0]["Entities Found"], "Name | SSN")


class TestResumeReadsMergedSchema(unittest.TestCase):
    def test_partial_trailing_row_is_repaired_and_tallied(self):
        from pii_triage.runner import _load_done_and_repair
        with tempfile.TemporaryDirectory() as td:
            inv = os.path.join(td, "inv.csv")
            base = {f: "" for f in FIELDNAMES}
            with open(inv, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=FIELDNAMES)
                w.writeheader()
                w.writerow({**base, "rel_path": "a.pdf", "status": "ok",
                            "suggested_lane": "standard", "llm_tokens": "10",
                            "s2_llm_tokens": "5", "di_calls": "2", "ocr_attempted": "True",
                            "ocr": "True", "ocr_pages": "3", "img_ocr_calls": "1",
                            "s2_ran": "True", "s2_llm_responsiveness": "clear_yes"})
                w.writerow({**base, "rel_path": "b.pdf", "status": "ok",
                            "suggested_lane": NR_LANE, "llm_tokens": "7",
                            "s2_skip_reason": "stage1_nr"})
            # simulate a hard crash mid-write
            with open(inv, "a", encoding="utf-8") as fh:
                fh.write("c.pdf,c.pdf,.pdf,123,ok")
            counters, lanes, llm, ocr, s2 = {}, {}, {}, {}, {}
            done = _load_done_and_repair(inv, counters, lanes, llm, ocr, s2)
        self.assertEqual(done, {"a.pdf", "b.pdf"}, "the partial row must be dropped")
        self.assertEqual(llm["tokens"], 17)
        self.assertEqual(s2["tokens"], 5)
        self.assertEqual(ocr["di_calls"], 2)
        self.assertEqual(ocr["pages"], 3)
        self.assertEqual(lanes["standard"], 1)
        self.assertEqual(lanes[NR_LANE], 1)


if __name__ == "__main__":
    unittest.main()
