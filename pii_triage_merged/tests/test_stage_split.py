"""3.0.0 single-pass pipeline: the shared pass runs ONCE, and stage 2 is gated on stage 1.

These are the tests that prove the merge's premise. The old workflow ran the whole tool
twice, so every surviving file was extracted and OCR'd twice; here the expensive shared
work happens once and both decision stages read the same text.

Idiom follows the existing suite: plain unittest, local Cfg stubs, and OCR/LLM injected as
callables so nothing touches Azure.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii_triage import runner
from pii_triage.config import Config
from pii_triage.detection import CompiledRules
from pii_triage.routing import (FileRecord, LEGACY_FIELDNAMES, STAGE2_FIELDNAMES,
                                FIELDNAMES, NR_LANE)


def _cfg(root, **kw):
    c = Config(root=root)
    c.use_llm = True
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _rec(**kw):
    d = dict(rel_path="r", file_name="f.txt", ext=".txt", size_bytes=100,
             status="ok", searchable=True, value_signal=True,
             estimated_entities=3, suggested_lane="standard")
    d.update(kw)
    r = FileRecord(**d)
    r.nr_stage1 = r.suggested_lane == NR_LANE
    r.bde_stage1 = bool(r.is_bde)
    return r


def _level(lvl, tokens=7):
    return lambda text, cfg: {"responsiveness": lvl, "tokens": tokens}


class TestSharedPassRunsOnce(unittest.TestCase):
    """The whole point of 3.0.0: extraction, OCR and detection happen once per file,
    and BOTH stages read that one result."""

    def test_extract_ocr_detect_each_called_once_per_file(self):
        calls = {"extract": 0, "ocr": 0, "detect": 0, "stage1_llm": 0, "stage2_llm": 0}
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "a.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("Jane Doe SSN 123-45-6789")

            real_detect = runner.detect

            def counting_detect(text, rules):
                calls["detect"] += 1
                return real_detect(text, rules)

            def counting_extract(p, cfg, rules):
                calls["extract"] += 1
                return "Jane Doe SSN 123-45-6789", {"text_extractable": "image_only"}

            def counting_ocr(p, cfg):
                calls["ocr"] += 1
                return "Jane Doe SSN 123-45-6789", {"text_extractable": "text",
                                                   "page_or_sheet_count": 1}

            def s1(text, cfg):
                calls["stage1_llm"] += 1
                return {"responsive": True, "tokens": 1}

            def s2(text, cfg):
                calls["stage2_llm"] += 1
                return {"responsiveness": "clear_yes", "tokens": 2}

            cfg = _cfg(td, use_ocr=True)
            runner._CFG = cfg
            runner._RULES = CompiledRules.from_pack(cfg.rulepack)
            runner._OCR_FN, runner._LLM_FN, runner._BDE_FN, runner._S2_FN = \
                counting_ocr, s1, None, s2
            orig_get, orig_detect = runner.get_extractor, runner.detect
            runner.get_extractor = lambda ext: counting_extract
            runner.detect = counting_detect
            try:
                rec = runner.process_file(path)
            finally:
                runner.get_extractor, runner.detect = orig_get, orig_detect

        self.assertEqual(calls["extract"], 1, "file extracted more than once")
        self.assertEqual(calls["ocr"], 1, "OCR ran more than once -- the bug 3.0.0 removes")
        self.assertEqual(calls["detect"], 1, "detection ran more than once")
        self.assertEqual(calls["stage2_llm"], 1, "stage 2 should have graded this file")
        self.assertTrue(rec["ocr"])
        self.assertTrue(rec["ocr_attempted"])
        self.assertEqual(rec["di_calls"], 1)
        self.assertGreater(rec["elapsed_s"], -1)


class TestStage2Gating(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg(".")

    def test_skipped_when_stage1_says_nr(self):
        calls = []
        rec = _rec(suggested_lane=NR_LANE)
        runner._stage2(rec, "text", self.cfg,
                       lambda t, c: calls.append(1) or {"responsiveness": "clear_yes"})
        self.assertEqual(calls, [], "stage 2 must never see a file stage 1 cleared")
        self.assertFalse(rec.s2_ran)
        self.assertEqual(rec.s2_skip_reason, "stage1_nr")
        self.assertEqual(rec.s2_lane, "")
        self.assertFalse(rec.s2_nr)

    def test_runs_when_stage1_says_responsive(self):
        rec = _rec(suggested_lane="standard")
        runner._stage2(rec, "text", self.cfg, _level("clear_yes"))
        self.assertTrue(rec.s2_ran)
        self.assertEqual(rec.s2_skip_reason, "")
        self.assertTrue(rec.s2_llm_consulted)
        self.assertEqual(rec.s2_lane, "standard")
        self.assertFalse(rec.s2_nr)

    def test_skipped_when_not_searchable(self):
        rec = _rec(searchable=False, suggested_lane="nonsearchable_sample")
        runner._stage2(rec, "", self.cfg, _level("clear_yes"))
        self.assertEqual(rec.s2_skip_reason, "not_searchable")
        self.assertFalse(rec.s2_llm_consulted)

    def test_skipped_when_no_text(self):
        rec = _rec(suggested_lane="standard")
        runner._stage2(rec, "   \n ", self.cfg, _level("clear_yes"))
        self.assertEqual(rec.s2_skip_reason, "no_text")

    def test_disabled_by_config(self):
        rec = _rec(suggested_lane="standard")
        runner._stage2(rec, "text", _cfg(".", use_stage2=False), _level("clear_yes"))
        self.assertEqual(rec.s2_skip_reason, "stage2_disabled")
        self.assertFalse(rec.s2_ran)

    def test_stage2_on_all_overrides_the_gate(self):
        rec = _rec(suggested_lane=NR_LANE)
        runner._stage2(rec, "text", _cfg(".", stage2_on_all=True), _level("likely_yes"))
        self.assertTrue(rec.s2_ran, "--stage2-on-all must grade stage-1 NR files too")
        self.assertEqual(rec.s2_skip_reason, "")
        self.assertTrue(rec.nr_stage1, "stage 1's own answer must be unchanged")


class TestStage1Isolation(unittest.TestCase):
    """Stage 2 must not be able to touch a stage-1 answer. This is the structural
    guarantee that the merge cannot lower NR recall."""

    def test_stage2_cannot_write_any_legacy_field(self):
        rec = _rec(suggested_lane="standard", is_bde=True, llm_responsive="yes",
                   bde_person_count=42, estimated_entities=99, entity_bucket="51-100")
        before = {f: getattr(rec, f) for f in LEGACY_FIELDNAMES}

        def adversarial(text, cfg):
            # Everything a hostile/buggy model might return, including stage-1 key names.
            return {"responsiveness": "clear_no", "responsive": False, "person_count": 100000,
                    "is_bde": False, "estimated_entities": 0, "llm_responsive": "no",
                    "suggested_lane": NR_LANE, "bde_person_count": 0, "detail": "wiped",
                    "tokens": 5}

        runner._stage2(rec, "text", _cfg("."), adversarial)
        after = {f: getattr(rec, f) for f in LEGACY_FIELDNAMES}
        self.assertEqual(before, after, "stage 2 mutated a stage-1 field")
        self.assertTrue(rec.s2_nr, "stage 2's own answer should still be recorded")

    def test_stage2_failure_is_isolated(self):
        rec = _rec(suggested_lane="standard")
        before = {f: getattr(rec, f) for f in LEGACY_FIELDNAMES}

        def boom(text, cfg):
            raise RuntimeError("azure down")

        runner._stage2(rec, "text", _cfg("."), boom)
        self.assertEqual(before, {f: getattr(rec, f) for f in LEGACY_FIELDNAMES})
        self.assertIn("llm_failed:RuntimeError", rec.s2_detail)
        self.assertTrue(rec.s2_ran)
        self.assertFalse(rec.s2_llm_consulted)
        # A failed stage-2 call must still land somewhere recall-safe, not silently blank.
        self.assertIn(rec.s2_llm_responsive, ("yes", "no"))

    def test_stage2_writes_only_declared_fields(self):
        rec = _rec(suggested_lane="standard")
        allowed = set(STAGE2_FIELDNAMES)
        before = {f: getattr(rec, f) for f in FIELDNAMES if f not in allowed}
        runner._stage2(rec, "text", _cfg("."), _level("borderline"))
        after = {f: getattr(rec, f) for f in FIELDNAMES if f not in allowed}
        self.assertEqual(before, after)


class TestDerivedColumns(unittest.TestCase):
    def test_nr_stage1_matches_the_lane(self):
        for lane in ("standard", "bde", "structured_bde", NR_LANE, "review_error",
                     "needs_parser", "nonsearchable_sample", "structured_unreadable",
                     "convert_lane"):
            r = _rec(suggested_lane=lane)
            self.assertEqual(r.nr_stage1, lane == NR_LANE, lane)

    def test_bde_stage1_aliases_is_bde(self):
        for v in (True, False):
            self.assertEqual(_rec(is_bde=v).bde_stage1, v)


class TestNoLLMFallback(unittest.TestCase):
    def test_stage2_without_llm_uses_recall_first_rules(self):
        rec = _rec(suggested_lane="standard", value_signal=True)
        runner._stage2(rec, "text", _cfg("."), None)
        self.assertTrue(rec.s2_ran)
        self.assertFalse(rec.s2_llm_consulted)
        self.assertEqual(rec.s2_llm_responsive, "yes")
        self.assertIn("rules_fallback", rec.s2_detail)

    def test_rules_fallback_clears_when_no_signal(self):
        rec = _rec(suggested_lane="standard", value_signal=False, is_structured=False,
                   estimated_entities=0)
        runner._stage2(rec, "text", _cfg("."), None)
        self.assertEqual(rec.s2_llm_responsive, "no")
        self.assertTrue(rec.s2_nr)


if __name__ == "__main__":
    unittest.main()
