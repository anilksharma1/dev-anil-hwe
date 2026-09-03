"""Tests for pii_triage.legacy_pairs.collapse_legacy_pairs -- the shared fix for the
Windows-leg two-Table-row artefact, used by both hwe_scaled_store.py (live Monitor) and
collect_outputs.py (dump_timing's _timing.json snapshot)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from pii_triage.legacy_pairs import collapse_legacy_pairs


class CollapseLegacyPairsDicts(unittest.TestCase):
    """The dict-shaped usage (Table entities, as hwe_scaled_store.py uses it)."""

    def test_completed_pair_collapses_to_one(self):
        ents = [{"file_name": "report.doc", "status": "completed"},
                {"file_name": "report.docx", "status": "processing"}]
        out = collapse_legacy_pairs(ents)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["file_name"], "report.docx")

    def test_unconverted_legacy_row_is_kept(self):
        ents = [{"file_name": "report.doc", "status": "pending"}]
        out = collapse_legacy_pairs(ents)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["file_name"], "report.doc")

    def test_non_legacy_files_untouched(self):
        ents = [{"file_name": "memo.pdf", "status": "completed"},
                {"file_name": "notes.txt", "status": "pending"}]
        self.assertEqual(collapse_legacy_pairs(ents), ents)

    def test_mixed_corpus_total_matches_real_file_count(self):
        ents = [
            {"file_name": "a.xls", "status": "completed"},
            {"file_name": "a.xlsx", "status": "completed"},
            {"file_name": "b.pdf", "status": "completed"},
            {"file_name": "c.pptx", "status": "pending"},
        ]
        self.assertEqual(len(collapse_legacy_pairs(ents)), 3)


class _FakeTask:
    def __init__(self, file_name, status):
        self.file_name = file_name
        self.status = status


class CollapseLegacyPairsObjects(unittest.TestCase):
    """The attribute-shaped usage (scaling_lib TaskRecord, as collect_outputs.py uses it)."""

    def test_task_records_collapse_via_name_of(self):
        tasks = [_FakeTask("report.doc", "completed"), _FakeTask("report.docx", "completed"),
                 _FakeTask("memo.pdf", "completed")]
        out = collapse_legacy_pairs(tasks, name_of=lambda t: t.file_name)
        self.assertEqual(len(out), 2)
        self.assertEqual({t.file_name for t in out}, {"report.docx", "memo.pdf"})


if __name__ == "__main__":
    unittest.main()
