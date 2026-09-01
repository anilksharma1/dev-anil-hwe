"""End-to-end: run the real runner over a real directory and inspect the real CSV.

There was no integration test in any prior version -- every test was a unit test on an
in-memory FileRecord. These walk a corpus on disk through runner.run(), with OCR and both
LLM stages injected as counting fakes, and assert on the produced inventory. That is the
only level at which "one OCR pass, and stage 2 gated behind stage 1" can actually be
verified, and it is also where the no-PII-in-output guarantee becomes testable.
"""
import csv
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii_triage import runner
from pii_triage.config import Config
from pii_triage.detection import CompiledRules
from pii_triage.routing import FIELDNAMES, NR_LANE

# Sentinel PII. None of these strings may appear anywhere in any output artefact.
SENTINELS = ["123-45-6789", "sentinel.person@example.invalid", "Zzyzxvyle Qqbrontor",
             "4111111111111111"]


def _corpus(td):
    """A small corpus that exercises the clear-responsive, clear-non-responsive,
    structured-roster, unsupported and empty paths."""
    files = {
        "responsive_ssn.txt": f"Employee record\nName: {SENTINELS[2]}\nSSN: {SENTINELS[0]}\n",
        "policy_mentions_only.txt": ("Privacy Policy\nWe collect SSN, passport number and "
                                     "account number from applicants. No values here.\n"),
        "roster.csv": ("name,email,phone\n"
                       + "".join(f"Person{i},p{i}@example.invalid,555-0{i:03d}\n"
                                 for i in range(60))),
        "invoice.txt": ("INVOICE 4471\nAcme Corp\nQty 3 widgets\nTotal $1,240.00\n"
                        "Purchase order 88231\n"),
        "empty.txt": "",
        "unsupported.dwg": "binary-ish",
    }
    for name, body in files.items():
        with open(os.path.join(td, name), "w", encoding="utf-8") as fh:
            fh.write(body)
    return files


class _Fakes:
    """Counting fakes for the three Azure entry points."""

    def __init__(self, s2_level="clear_yes"):
        self.ocr_calls = []
        self.s1_calls = []
        self.s2_calls = []
        self.s2_level = s2_level

    def ocr(self, path, cfg):
        self.ocr_calls.append(path)
        return "ocr text", {"text_extractable": "text", "page_or_sheet_count": 2}

    def s1(self, text, cfg):
        self.s1_calls.append(text[:40])
        # Recall-first, like the real stage-1 prompt's tie-break.
        return {"responsive": True, "person_count": 0, "tokens": 11}

    def s1_clears(self, text, cfg):
        self.s1_calls.append(text[:40])
        return {"responsive": False, "person_count": 0, "tokens": 11}

    def s2(self, text, cfg):
        self.s2_calls.append(text[:40])
        return {"responsiveness": self.s2_level, "tokens": 5}


def _run(td, fakes, s1_fn=None, **cfgkw):
    out = os.path.join(td, "inventory.csv")
    cfg = Config(root=td, use_ocr=True, use_llm=True, **cfgkw)
    orig = (runner._CFG, runner._RULES, runner._OCR_FN, runner._LLM_FN,
            runner._BDE_FN, runner._S2_FN)

    def _init(c):
        runner._CFG = c
        runner._RULES = CompiledRules.from_pack(c.rulepack)
        runner._OCR_FN = fakes.ocr
        runner._LLM_FN = s1_fn or fakes.s1
        runner._BDE_FN = None
        runner._S2_FN = fakes.s2

    real_init = runner._init_worker
    runner._init_worker = _init
    try:
        paths = runner.discover_files(td)
        paths = [p for p in paths if not p.endswith(("inventory.csv", ".manifest.json"))]
        summary = runner.run(cfg, paths, out, workers=1, progress_interval=99,
                             chunksize=1, restart=True)
    finally:
        runner._init_worker = real_init
        (runner._CFG, runner._RULES, runner._OCR_FN, runner._LLM_FN,
         runner._BDE_FN, runner._S2_FN) = orig
    with open(out, newline="", encoding="utf-8") as fh:
        rows = {r["rel_path"]: r for r in csv.DictReader(fh)}
    return rows, summary, out


class TestEndToEnd(unittest.TestCase):
    def test_every_file_gets_exactly_one_row_with_the_full_schema(self):
        with tempfile.TemporaryDirectory() as td:
            files = _corpus(td)
            rows, summary, _ = _run(td, _Fakes())
        self.assertEqual(set(rows), set(files))
        self.assertEqual(summary["completed"], len(files))
        for r in rows.values():
            self.assertEqual(set(r), set(FIELDNAMES))

    def test_stage2_never_runs_on_a_stage1_nr_file(self):
        with tempfile.TemporaryDirectory() as td:
            _corpus(td)
            fakes = _Fakes()
            rows, summary, _ = _run(td, fakes, s1_fn=fakes.s1_clears)
        nr = [k for k, v in rows.items() if v["nr_stage1"] == "True"]
        self.assertTrue(nr, "the fixture should produce at least one stage-1 NR file")
        for k in nr:
            self.assertEqual(rows[k]["s2_skip_reason"], "stage1_nr", k)
            self.assertEqual(rows[k]["s2_ran"], "False", k)
            self.assertEqual(rows[k]["s2_lane"], "", k)
        graded = [k for k, v in rows.items() if v["s2_ran"] == "True"]
        self.assertEqual(len(fakes.s2_calls), len(graded),
                         "stage-2 call count must equal the number of graded files")
        for k in graded:
            self.assertEqual(rows[k]["nr_stage1"], "False", k)

    def test_nr_stage1_agrees_with_the_lane_on_every_row(self):
        with tempfile.TemporaryDirectory() as td:
            _corpus(td)
            rows, _, _ = _run(td, _Fakes())
        for k, r in rows.items():
            self.assertEqual(r["nr_stage1"] == "True",
                             r["suggested_lane"] == NR_LANE, k)
            self.assertEqual(r["bde_stage1"], r["is_bde"], k)

    def test_token_rollup_is_the_sum_of_both_stages(self):
        with tempfile.TemporaryDirectory() as td:
            _corpus(td)
            rows, summary, _ = _run(td, _Fakes())
        for k, r in rows.items():
            self.assertEqual(int(r["llm_tokens_total"]),
                             int(r["llm_tokens"] or 0) + int(r["s2_llm_tokens"] or 0), k)
        self.assertEqual(summary["cost"]["llm_tokens_total"],
                         sum(int(r["llm_tokens_total"]) for r in rows.values()))

    def test_no_stage2_produces_a_stage1_only_inventory(self):
        with tempfile.TemporaryDirectory() as td:
            _corpus(td)
            fakes = _Fakes()
            rows, _, _ = _run(td, fakes, use_stage2=False)
        self.assertEqual(fakes.s2_calls, [], "--no-stage2 must make zero stage-2 calls")
        for k, r in rows.items():
            self.assertEqual(r["s2_lane"], "", k)
            # Files that reached the stages say "stage2_disabled"; files that exited early
            # (unsupported extension, parse error) say why they never got there. What must
            # never happen is a BLANK reason, which would be indistinguishable from a bug.
            self.assertTrue(r["s2_skip_reason"], f"{k} has no skip reason")
            self.assertIn(r["s2_skip_reason"],
                          ("stage2_disabled", "no_parser", "skipped_too_large",
                           "timeout", "extract_error"), k)

    def test_di_calls_are_tallied_into_the_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            _corpus(td)
            rows, summary, out = _run(td, _Fakes())
            with open(out + ".manifest.json", encoding="utf-8") as fh:
                manifest = json.load(fh)
        self.assertIn("ocr_stats", manifest)
        self.assertIn("stage2_stats", manifest)
        self.assertIn("cost", manifest)
        self.assertEqual(manifest["cost"]["di_calls"],
                         sum(int(r["di_calls"]) for r in rows.values()))

    def test_resume_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            _corpus(td)
            first, _, out = _run(td, _Fakes())
            # Re-run WITHOUT restart: every file is already done, so nothing is re-scanned.
            cfg = Config(root=td, use_ocr=True, use_llm=True)
            fakes = _Fakes()
            orig = (runner._CFG, runner._RULES, runner._OCR_FN, runner._LLM_FN,
                    runner._BDE_FN, runner._S2_FN)
            real_init = runner._init_worker

            def _init(c):
                runner._CFG, runner._RULES = c, CompiledRules.from_pack(c.rulepack)
                runner._OCR_FN, runner._LLM_FN = fakes.ocr, fakes.s1
                runner._BDE_FN, runner._S2_FN = None, fakes.s2

            runner._init_worker = _init
            try:
                paths = [p for p in runner.discover_files(td)
                         if not p.endswith(("inventory.csv", ".manifest.json"))]
                summary = runner.run(cfg, paths, out, 1, 99, 1, restart=False)
            finally:
                runner._init_worker = real_init
                (runner._CFG, runner._RULES, runner._OCR_FN, runner._LLM_FN,
                 runner._BDE_FN, runner._S2_FN) = orig
            with open(out, newline="", encoding="utf-8") as fh:
                second = {r["rel_path"]: r for r in csv.DictReader(fh)}
        self.assertEqual(summary["newly_scanned"], 0, "resume re-scanned completed files")
        self.assertEqual(fakes.ocr_calls, [], "resume must not re-OCR anything")
        self.assertEqual(set(first), set(second))
        self.assertEqual(len(second), len(first), "resume duplicated rows")


class TestSkipReasonAlwaysSet(unittest.TestCase):
    def test_every_row_explains_its_stage2_status(self):
        """No row may carry a blank s2_skip_reason while s2_ran is False -- a blank is
        indistinguishable from a broken gate, which is the failure mode this whole schema
        exists to make visible."""
        with tempfile.TemporaryDirectory() as td:
            _corpus(td)
            rows, _, _ = _run(td, _Fakes())
        for k, r in rows.items():
            if r["s2_ran"] == "True":
                self.assertEqual(r["s2_skip_reason"], "", k)
            else:
                self.assertTrue(r["s2_skip_reason"], f"{k}: stage 2 did not run and did not say why")


class TestNoPiiInOutput(unittest.TestCase):
    """The tool's headline guarantee, tested end to end for the first time."""

    def test_sentinels_absent_from_csv_and_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            _corpus(td)
            _, _, out = _run(td, _Fakes())
            csv_bytes = open(out, "rb").read()
            man_bytes = open(out + ".manifest.json", "rb").read()
        for s in SENTINELS:
            self.assertNotIn(s.encode(), csv_bytes, f"{s!r} leaked into the inventory CSV")
            self.assertNotIn(s.encode(), man_bytes, f"{s!r} leaked into the manifest")

    def test_names_returned_by_the_llm_are_not_written_out(self):
        with tempfile.TemporaryDirectory() as td:
            _corpus(td)
            fakes = _Fakes()

            def leaky_s1(text, cfg):
                return {"responsive": True, "names": [SENTINELS[2]],
                        "person_count": 1, "reasoning": f"found {SENTINELS[0]}", "tokens": 3}

            def leaky_s2(text, cfg):
                return {"responsiveness": "clear_yes", "names": [SENTINELS[2]],
                        "reasoning": f"card {SENTINELS[3]}", "tokens": 4}

            fakes.s2 = leaky_s2
            _, _, out = _run(td, fakes, s1_fn=leaky_s1)
            body = open(out, "rb").read()
        for s in SENTINELS:
            self.assertNotIn(s.encode(), body,
                             f"{s!r} reached the CSV via an LLM response field")


if __name__ == "__main__":
    unittest.main()
