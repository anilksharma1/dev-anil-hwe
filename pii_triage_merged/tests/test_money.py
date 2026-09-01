"""Tests for the name+money responsiveness rule (v2.9.4).

A monetary amount alone is a weak business signal and must NOT flag or reach the
LLM. A person's NAME together with a monetary amount is that individual's
financial information -- responsive under the protocol -- and must flag AND
bypass the LLM (so the AI can never clear it). This is the payroll/benefits
pattern the tool previously cleared.
"""
import unittest

from pii_triage.config import DEFAULT_RULEPACK
from pii_triage.detection import CompiledRules, detect, detect_money, value_signal
from pii_triage.routing import classify_ambiguity, choose_lane, FileRecord

RULES = CompiledRules.from_pack(DEFAULT_RULEPACK, use_ner=False)


def _lane(text, structured=False, rows=0):
    counts, labels, _ = detect(text, RULES)
    amb = classify_ambiguity(counts, labels, structured, structured_rows=rows)
    rec = FileRecord(rel_path="x", file_name="x", ext=".pdf", size_bytes=1,
                     searchable=True, is_structured=structured)
    rec.value_signal = value_signal(counts, RULES)
    rec.ambiguity = amb
    rec.estimated_entities = rows
    # LLM deliberately NOT consulted: this is the worst case (AI off or failed),
    # and the rule must still flag a name+money file.
    return amb, choose_lane(rec)


class TestMoneyDetection(unittest.TestCase):
    def test_detects_common_money_formats(self):
        self.assertGreaterEqual(detect_money("$1,234.56"), 1)
        self.assertGreaterEqual(detect_money("USD 1,000"), 1)
        self.assertGreaterEqual(detect_money("12,345.00 paid"), 1)
        self.assertGreaterEqual(detect_money("net 500.00 dollars"), 1)

    def test_plain_integers_are_not_money(self):
        self.assertEqual(detect_money("page 2 of 7, item 1234567"), 0)


class TestNameMoneyRouting(unittest.TestCase):
    def test_name_plus_money_goes_to_llm_not_auto_flagged(self):
        # v2.9.6: name+money is no longer auto-flagged (it was the top over-call
        # source -- mostly invoices/receipts/orders). It now goes to the LLM, which
        # keeps individual financial records and clears business/transaction money.
        amb, lane = _lane("Name: Chassee James\nMedical 125.40  Dental 18.00")
        self.assertEqual(amb, "ambiguous")                 # the AI judges it
        # With the LLM unavailable (as in this helper), the recall-first fallback
        # still flags it on value_signal -- a name+money file is never silently cleared.
        self.assertEqual(lane, "standard")

    def test_money_alone_does_not_flag_and_does_not_reach_llm(self):
        amb, lane = _lane("Invoice total $48,200.00 due net 30. Acme Corp.")
        self.assertEqual(amb, "clear_non_responsive")      # no LLM call
        self.assertEqual(lane, "likely_non_responsive")

    def test_name_alone_still_routes_to_llm(self):
        amb, _ = _lane("Name: Jane Doe")
        self.assertEqual(amb, "ambiguous")                 # unchanged: AI judges a bare name

    def test_name_plus_money_value_signal_true(self):
        counts, _, _ = detect("Name: Chassee James  pay 1,250.00", RULES)
        self.assertTrue(value_signal(counts, RULES))

    def test_money_alone_value_signal_false(self):
        counts, _, _ = detect("Total due: $5,000.00", RULES)
        self.assertFalse(value_signal(counts, RULES))


if __name__ == "__main__":
    unittest.main()
