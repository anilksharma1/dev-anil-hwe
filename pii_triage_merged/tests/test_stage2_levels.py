"""Stage-2 graded responsiveness: the five levels, and the recall-safe defaults.

Stage 2 collapses clear_yes/likely_yes to responsive and everything else to NR. That is
DANIEL'S rule and it deliberately differs from stage 1: stage 1's prompt rounds genuine
uncertainty UP to responsive, stage 2's expresses it as `borderline` and then clears it.
Which is exactly why stage 1 owns NR removal. These tests pin that asymmetry so nobody
"fixes" it by accident, and pin the recall-safe behaviour for anything unexpected.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii_triage import runner
from pii_triage.config import Config
from pii_triage.routing import FileRecord, NR_LANE


def _cfg(**kw):
    c = Config(root=".")
    c.use_llm = True
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _rec(**kw):
    d = dict(rel_path="r", file_name="f.txt", ext=".txt", size_bytes=100, status="ok",
             searchable=True, value_signal=True, estimated_entities=3,
             suggested_lane="standard")
    d.update(kw)
    r = FileRecord(**d)
    r.nr_stage1 = r.suggested_lane == NR_LANE
    return r


def _fn(payload):
    return lambda text, cfg: payload


class TestLevelCollapse(unittest.TestCase):
    def test_responsive_levels(self):
        for lvl in ("clear_yes", "likely_yes"):
            rec = _rec()
            runner._stage2(rec, "t", _cfg(), _fn({"responsiveness": lvl}))
            self.assertEqual(rec.s2_llm_responsive, "yes", lvl)
            self.assertFalse(rec.s2_nr, lvl)
            self.assertEqual(rec.s2_llm_responsiveness, lvl)

    def test_non_responsive_levels(self):
        for lvl in ("borderline", "likely_no", "clear_no"):
            rec = _rec()
            runner._stage2(rec, "t", _cfg(), _fn({"responsiveness": lvl}))
            self.assertEqual(rec.s2_llm_responsive, "no", lvl)
            self.assertTrue(rec.s2_nr, lvl)
            self.assertEqual(rec.s2_lane, NR_LANE, lvl)

    def test_borderline_clears_at_stage2_but_stage1_is_untouched(self):
        """The asymmetry, stated as a test: a file stage 1 kept can be cleared by stage 2,
        and stage 1's own answer does not move."""
        rec = _rec(suggested_lane="standard")
        runner._stage2(rec, "t", _cfg(), _fn({"responsiveness": "borderline"}))
        self.assertTrue(rec.s2_nr)
        self.assertEqual(rec.suggested_lane, "standard")
        self.assertFalse(rec.nr_stage1)


class TestRecallSafeDefaults(unittest.TestCase):
    def test_unknown_level_does_not_clear(self):
        rec = _rec()
        runner._stage2(rec, "t", _cfg(), _fn({"responsiveness": "maybe_probably"}))
        self.assertEqual(rec.s2_llm_responsive, "yes",
                         "an unrecognised level must never clear a file")
        self.assertFalse(rec.s2_nr)
        self.assertIn("unknown_level", rec.s2_detail)

    def test_legacy_boolean_shape_is_honoured(self):
        """Older deployments answer {"responsive": true} with no level."""
        rec = _rec()
        runner._stage2(rec, "t", _cfg(), _fn({"responsive": True}))
        self.assertEqual(rec.s2_llm_responsive, "yes")
        self.assertIn("legacy_boolean", rec.s2_detail)
        self.assertEqual(rec.s2_llm_responsiveness, "")

    def test_legacy_boolean_false_clears(self):
        rec = _rec()
        runner._stage2(rec, "t", _cfg(), _fn({"responsive": False}))
        self.assertEqual(rec.s2_llm_responsive, "no")
        self.assertTrue(rec.s2_nr)

    def test_empty_response_falls_back_not_clears(self):
        rec = _rec(value_signal=True)
        runner._stage2(rec, "t", _cfg(), _fn({}))
        self.assertEqual(rec.s2_llm_responsive, "no")  # {} -> responsive=False, legacy shape absent
        self.assertTrue(rec.s2_llm_consulted)


class TestStage2Bde(unittest.TestCase):
    def test_s2_bde_uses_its_own_threshold(self):
        rec = _rec(estimated_entities=20)
        runner._stage2(rec, "t", _cfg(bde_threshold=51, s2_bde_threshold=10),
                       _fn({"responsiveness": "clear_yes"}))
        self.assertTrue(rec.s2_is_bde)
        self.assertEqual(rec.s2_lane, "bde")
        self.assertFalse(rec.is_bde, "stage 1's BDE flag must be independent")

    def test_s2_threshold_defaults_to_stage1(self):
        rec = _rec(estimated_entities=20)
        runner._stage2(rec, "t", _cfg(bde_threshold=10, s2_bde_threshold=0),
                       _fn({"responsiveness": "clear_yes"}))
        self.assertTrue(rec.s2_is_bde)

    def test_structured_bde_lane(self):
        rec = _rec(estimated_entities=200, is_structured=True)
        runner._stage2(rec, "t", _cfg(bde_threshold=51),
                       _fn({"responsiveness": "clear_yes"}))
        self.assertEqual(rec.s2_lane, "structured_bde")

    def test_cleared_file_gets_no_bde_lane(self):
        rec = _rec(estimated_entities=200, is_structured=True)
        runner._stage2(rec, "t", _cfg(bde_threshold=51),
                       _fn({"responsiveness": "clear_no"}))
        self.assertEqual(rec.s2_lane, NR_LANE)


class TestStage2Tokens(unittest.TestCase):
    def test_tokens_recorded_separately_from_stage1(self):
        rec = _rec(llm_tokens=1000)
        runner._stage2(rec, "t", _cfg(), _fn({"responsiveness": "clear_yes", "tokens": 250}))
        self.assertEqual(rec.s2_llm_tokens, 250)
        self.assertEqual(rec.llm_tokens, 1000, "stage-1 token count must not move")


class TestGradedClientDropsReasoning(unittest.TestCase):
    """The PII leak fix: Daniel's build wrote the model's reasoning to the CSV while its
    README promised no PII in output, and the prompt asks the model to quote the value it
    found. The graded client must drop it at the boundary."""

    def test_reasoning_not_in_returned_dict(self):
        import json
        from pii_triage import azure_clients as ac

        _PAYLOAD = json.dumps({"responsiveness": "clear_yes", "names": ["Jane Doe"],
                               "person_count": 1,
                               "reasoning": "found SSN 123-45-6789 for Jane Doe"})

        class _Client:
            def __init__(self):
                self.tokens_in = 0
                self.tokens_out = 0

            def complete(self, message, system_prompt=None, **kw):
                self.tokens_out += 10
                return _PAYLOAD

        orig = ac._llm_client
        ac._llm_client = lambda cfg: _Client()
        try:
            out = ac.llm_classify_graded("some text", _cfg())
        finally:
            ac._llm_client = orig
        self.assertNotIn("reasoning", out)
        self.assertEqual(out["responsiveness"], "clear_yes")
        self.assertEqual(out["tokens"], 10)
        # the SSN the model quoted must not survive anywhere in the returned payload
        self.assertNotIn("123-45-6789", json.dumps(out))


if __name__ == "__main__":
    unittest.main()
