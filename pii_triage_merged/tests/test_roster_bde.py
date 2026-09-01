"""Tests for the improved roster BDE recovery (roster_entity_estimate).

The old rule only recovered a structured file when the identifier rules recognized
ZERO rows. A roster where a few rows matched (base_estimate 1..threshold-1) was left
under-counted and missed. The fix fires for any under-count below the threshold.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii_triage.routing import roster_entity_estimate, choose_lane, FileRecord, classify_ambiguity
from pii_triage.enrich import apply_bde_count

T = 51  # bde_threshold


class TestRosterEntityEstimate(unittest.TestCase):
    def _meta(self, total, structured=True):
        return {"is_structured": structured, "structured_total_rows": total}

    def test_partial_recognition_roster_is_recovered(self):
        # THE FIX: rules recognized 3 rows of a 500-row roster -> was 3 (missed), now 500.
        self.assertEqual(roster_entity_estimate(self._meta(500), 3, T), 500)

    def test_near_threshold_partial_roster_recovered(self):
        # 40 recognized of 55 total -> bumped to 55 (crosses threshold).
        self.assertEqual(roster_entity_estimate(self._meta(55), 40, T), 55)

    def test_zero_recognition_still_recovered(self):
        # Original base==0 behavior preserved.
        self.assertEqual(roster_entity_estimate(self._meta(500), 0, T), 500)

    def test_already_at_threshold_unchanged(self):
        # Recognized count already >= threshold -> no double-count, return as-is.
        self.assertEqual(roster_entity_estimate(self._meta(500), 60, T), 60)

    def test_small_table_not_bumped(self):
        # Fewer total rows than threshold -> genuinely small, leave alone.
        self.assertEqual(roster_entity_estimate(self._meta(30), 3, T), 3)

    def test_non_structured_untouched(self):
        self.assertEqual(roster_entity_estimate(self._meta(500, structured=False), 3, T), 3)

    def test_threshold_disabled(self):
        self.assertEqual(roster_entity_estimate(self._meta(500), 3, 0), 3)

    def test_missing_total_rows_defaults_zero(self):
        self.assertEqual(roster_entity_estimate({"is_structured": True}, 3, T), 3)


class TestRecoveredRosterRoutesToBDE(unittest.TestCase):
    """A recovered roster (estimate bumped >= threshold) must land in a BDE lane."""

    def test_structured_roster_routes_structured_bde_via_rules_fallback(self):
        # LLM not consulted -> rules fallback: structured & estimate>0 => responsive,
        # and estimate>=threshold => is_bde => structured_bde.
        est = roster_entity_estimate({"is_structured": True, "structured_total_rows": 500}, 3, T)
        rec = FileRecord(rel_path="r", file_name="roster.xlsx", ext=".xlsx", size_bytes=1,
                         searchable=True, is_structured=True, estimated_entities=est,
                         is_bde=(est >= T), llm_consulted=False)
        self.assertEqual(choose_lane(rec), "structured_bde")

    def test_structured_roster_routes_structured_bde_when_llm_says_responsive(self):
        est = roster_entity_estimate({"is_structured": True, "structured_total_rows": 500}, 3, T)
        rec = FileRecord(rel_path="r", file_name="roster.xlsx", ext=".xlsx", size_bytes=1,
                         searchable=True, is_structured=True, estimated_entities=est,
                         is_bde=(est >= T), llm_consulted=True, llm_responsive="yes")
        self.assertEqual(choose_lane(rec), "structured_bde")

    def test_llm_can_still_clear_a_non_person_big_table(self):
        # Precision guard: even after bump, the LLM's non-responsive call clears it.
        est = roster_entity_estimate({"is_structured": True, "structured_total_rows": 500}, 3, T)
        rec = FileRecord(rel_path="r", file_name="inventory.xlsx", ext=".xlsx", size_bytes=1,
                         searchable=True, is_structured=True, estimated_entities=est,
                         is_bde=(est >= T), llm_consulted=True, llm_responsive="no")
        self.assertEqual(choose_lane(rec), "likely_non_responsive")


if __name__ == "__main__":
    unittest.main()


class TestStructuredGateWidened(unittest.TestCase):
    """classify_ambiguity now sends a big table to the LLM even when the rules
    recognized zero person-rows (the under-read roster), keyed off total rows."""
    def test_under_read_roster_now_goes_to_llm(self):
        # rules recognized 0 rows, but the sheet has 500 rows -> ambiguous (was cleared).
        r = classify_ambiguity({}, [], is_structured=True, structured_rows=0, structured_total_rows=500)
        self.assertEqual(r, "ambiguous")

    def test_recognized_rows_still_go_to_llm(self):
        r = classify_ambiguity({}, [], is_structured=True, structured_rows=3, structured_total_rows=500)
        self.assertEqual(r, "ambiguous")

    def test_empty_structured_file_still_cleared(self):
        # genuinely no rows -> nothing to read -> not sent to LLM.
        r = classify_ambiguity({}, [], is_structured=True, structured_rows=0, structured_total_rows=0)
        self.assertEqual(r, "clear_non_responsive")

    def test_non_structured_volume_unchanged(self):
        # a plain file with no meaningful labels is NOT sent to the LLM by this gate.
        r = classify_ambiguity({}, [], is_structured=False, structured_rows=0, structured_total_rows=0)
        self.assertEqual(r, "clear_non_responsive")

    def test_strong_identifier_still_short_circuits(self):
        from pii_triage.routing import STRONG_KEYS
        k = next(iter(STRONG_KEYS))
        r = classify_ambiguity({k: 1}, [], is_structured=True, structured_rows=0, structured_total_rows=500)
        self.assertEqual(r, "clear_responsive")

    def test_money_only_still_not_flagged(self):
        # money-only on a NON-structured file stays cleared (no flood on payroll pop).
        r = classify_ambiguity({}, ["Money/Amount"], is_structured=False,
                     structured_rows=0, structured_total_rows=0)
        self.assertEqual(r, "clear_non_responsive")


class TestZeroReadStructuredGuard(unittest.TestCase):
    """A structured file that reads 0 entities but has real size must route to review,
    never be silently cleared. Must NOT affect non-structured files or files with entities."""
    def _rec(self, **kw):
        d = dict(rel_path="r", file_name="f.xlsx", ext=".xlsx", size_bytes=50000,
                 status="ok", searchable=True, is_structured=True, estimated_entities=0,
                 llm_consulted=False, value_signal=False)
        d.update(kw)
        return FileRecord(**d)

    def test_zero_read_structured_routes_to_review_not_cleared(self):
        self.assertEqual(choose_lane(self._rec()), "structured_unreadable")

    def test_tiny_empty_structured_still_cleared(self):
        # below the size floor -> genuinely empty, allowed to clear as before.
        self.assertEqual(choose_lane(self._rec(size_bytes=1000)), "likely_non_responsive")

    def test_structured_with_entities_unaffected(self):
        # read real entities -> normal path, not the guard.
        r = self._rec(estimated_entities=5, value_signal=True)
        self.assertNotEqual(choose_lane(r), "structured_unreadable")

    def test_non_structured_zero_read_unaffected(self):
        # a non-structured file reading 0 must behave exactly as before (cleared), NOT rerouted.
        r = self._rec(is_structured=False, ext=".pdf", file_name="f.pdf")
        self.assertEqual(choose_lane(r), "likely_non_responsive")

    def test_guard_does_not_fire_when_llm_flags_responsive(self):
        # if the LLM already flagged it responsive, that path still wins (guard is for the clear path).
        # (guard sits before the responsive block, but a real roster read >0 wouldn't hit it anyway;
        #  here we confirm a 0-read structured file the LLM called responsive still isn't cleared.)
        r = self._rec(llm_consulted=True, llm_responsive="yes")
        self.assertIn(choose_lane(r), ("structured_unreadable", "structured_bde", "bde", "standard"))

    def test_errored_file_still_goes_to_its_lane(self):
        # a genuine extraction error is unchanged (handled earlier in choose_lane).
        r = self._rec(status="error")
        self.assertEqual(choose_lane(r), "review_error")


class TestSeparateBDECounter(unittest.TestCase):
    """The separate BDE-only counter (apply_bde_count) rescues unparseable rosters,
    is promote-only, and never touches the responsiveness (NR) decision."""
    class Cfg:
        bde_threshold = 51

    def _rec(self, **kw):
        d = dict(rel_path="r", file_name="f.xlsx", ext=".xlsx", size_bytes=50000,
                 status="ok", searchable=True, is_structured=True, estimated_entities=0,
                 value_signal=False, llm_consulted=False, llm_responsive="",
                 bde_person_count=0, bde_confirmed=False)
        d.update(kw); return FileRecord(**d)

    def _fn(self, count):
        return lambda text, cfg: {"person_count": count, "tokens": 5}

    def test_counter_rescues_unparseable_roster(self):
        rec = self._rec()
        apply_bde_count(rec, "Ackerman, Robert ... 600 employees ...", self.Cfg(), self._fn(614))
        self.assertEqual(rec.bde_person_count, 614)

    def test_counter_never_touches_nr_decision(self):
        # even if NR said 'no', the counter does not change llm_responsive.
        rec = self._rec(llm_consulted=True, llm_responsive="no")
        apply_bde_count(rec, "roster text", self.Cfg(), self._fn(600))
        self.assertEqual(rec.llm_responsive, "no")          # untouched
        self.assertEqual(rec.estimated_entities, 0)          # NOT bumped (NR fallback input unchanged)
        self.assertEqual(rec.bde_person_count, 600)          # recorded separately

    def test_counter_is_authoritative(self):
        # 2.10.0: the LLM count is authoritative for the BDE tier -- it may LOWER a stale value
        rec = self._rec(bde_person_count=800)
        apply_bde_count(rec, "roster", self.Cfg(), self._fn(100))
        self.assertEqual(rec.bde_person_count, 100)

    def test_counter_skips_non_structured_below_floor(self):
        # non-structured AND below the entity floor -> not counted
        rec = self._rec(is_structured=False, ext=".pdf", file_name="f.pdf", estimated_entities=2)
        apply_bde_count(rec, "text", self.Cfg(), self._fn(600))
        self.assertEqual(rec.bde_person_count, 0)

    def test_counter_recounts_over_threshold(self):
        # 2.10.0: an over-threshold token estimate is exactly what we re-count to correct DOWN
        rec = self._rec(estimated_entities=200)
        apply_bde_count(rec, "text", self.Cfg(), self._fn(3))
        self.assertEqual(rec.bde_person_count, 3)

    def test_counter_skips_empty_text(self):
        rec = self._rec()
        apply_bde_count(rec, "   ", self.Cfg(), self._fn(600))
        self.assertEqual(rec.bde_person_count, 0)

    def test_counter_failure_is_safe(self):
        def boom(text, cfg): raise RuntimeError("azure down")
        rec = self._rec()
        apply_bde_count(rec, "text", self.Cfg(), boom)
        self.assertEqual(rec.bde_person_count, 0)   # record intact
        self.assertIn("bde_count_failed", rec.detail)


class TestConfirmedBDERouting(unittest.TestCase):
    """A confirmed-count BDE routes to BDE review even when NR cleared it; a heuristic
    bump still lets the LLM clear (2.9.12 preserved)."""
    def _rec(self, **kw):
        d = dict(rel_path="r", file_name="f.xlsx", ext=".xlsx", size_bytes=50000,
                 status="ok", searchable=True, is_structured=True, estimated_entities=0,
                 value_signal=False, llm_consulted=True, llm_responsive="no",
                 bde_person_count=600, bde_confirmed=True, is_bde=True)
        d.update(kw); return FileRecord(**d)

    def test_confirmed_bde_routes_to_review_despite_nr_no(self):
        # NR said non-responsive, but the counter read 600 -> must go to BDE review.
        self.assertEqual(choose_lane(self._rec()), "structured_bde")

    def test_confirmed_non_structured_bde_routes_bde(self):
        self.assertEqual(choose_lane(self._rec(is_structured=False, file_name="f.txt", ext=".txt")), "bde")

    def test_heuristic_bump_still_clearable_by_llm(self):
        # est bumped to 200 (roster hypothesis) but NOT bde_confirmed, LLM says no -> cleared.
        r = self._rec(estimated_entities=200, bde_person_count=0, bde_confirmed=False, is_bde=True)
        self.assertEqual(choose_lane(r), "likely_non_responsive")


class TestBDECounterPDFGate(unittest.TestCase):
    """The BDE counter now also fires on long searchable PDFs, but must skip short PDFs
    and non-text PDFs, and must not affect NR."""
    class Cfg:
        bde_threshold = 51
        bde_pdf_min_pages = 50

    def _rec(self, **kw):
        d = dict(rel_path="r", file_name="f.pdf", ext=".pdf", size_bytes=50000,
                 status="ok", searchable=True, is_structured=False, estimated_entities=3,
                 value_signal=True, llm_consulted=False, llm_responsive="",
                 page_or_sheet_count=128, text_extractable="text",
                 bde_person_count=0, bde_confirmed=False)
        d.update(kw); return FileRecord(**d)

    def _fn(self, count):
        return lambda text, cfg: {"person_count": count, "tokens": 9}

    def test_long_pdf_gets_counted(self):
        rec = self._rec(page_or_sheet_count=128, estimated_entities=90)  # many candidate tokens
        apply_bde_count(rec, "CASSIDY, MARGARET File: 400962 ... 383 people", self.Cfg(), self._fn(383))
        self.assertEqual(rec.bde_person_count, 383)

    def test_short_pdf_skipped(self):
        rec = self._rec(page_or_sheet_count=4)          # below 50-page floor
        apply_bde_count(rec, "a normal 4-page letter", self.Cfg(), self._fn(383))
        self.assertEqual(rec.bde_person_count, 0)       # not called

    def test_image_only_pdf_skipped(self):
        rec = self._rec(page_or_sheet_count=128, text_extractable="image_only")
        apply_bde_count(rec, "", self.Cfg(), self._fn(383))
        self.assertEqual(rec.bde_person_count, 0)       # no text -> skip (OCR's job)

    def test_pdf_counter_does_not_touch_nr(self):
        rec = self._rec(page_or_sheet_count=128, estimated_entities=90, llm_consulted=True, llm_responsive="no")
        apply_bde_count(rec, "roster text", self.Cfg(), self._fn(383))
        self.assertEqual(rec.llm_responsive, "no")      # NR verdict untouched
        self.assertEqual(rec.estimated_entities, 90)    # NR fallback input untouched
        self.assertEqual(rec.bde_person_count, 383)     # recorded separately

    def test_page_floor_is_configurable(self):
        cfg = self.Cfg(); cfg.bde_pdf_min_pages = 200   # raise the bar
        rec = self._rec(page_or_sheet_count=128)        # now below the floor
        apply_bde_count(rec, "roster", cfg, self._fn(383))
        self.assertEqual(rec.bde_person_count, 0)