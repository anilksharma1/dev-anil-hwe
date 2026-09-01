"""Tests for 2.9.18 distinct-subject BDE tiering (precision fix).

The counter must be OCCURRENCE-INDEPENDENT: the same person repeated across many rows
counts once, so single-subject ledgers stop reading as huge BDEs, while genuine N-person
rosters still read ~N. And it must not inflate on bare 9-digit numbers (account/reference
numbers) the NR SSN detector over-matches.
"""
# --- MERGE NOTE (3.0.0) ------------------------------------------------------
# This file tests the 2.9.18 partial-coverage OCR / scanned-BDE design:
#   enrich.structured_subject_count
# Those symbols exist in NO version we were given (classic 2.10.2, Daniel 2.9.9,
# new-Anna 2.11.0) -- confirmed by grep across all three trees. The tests describe
# a real and coherent feature (OCR a PAGE SAMPLE of a large scan, keep the file
# non-searchable so the sampled text never reaches the NR decision, and extrapolate
# a BDE person-count from sample + page ratio). That design is directly relevant to
# the OCR cost work, but it is NOT part of this merge and would change the OCR
# contract, so the tests are skipped rather than deleted -- deleting them would lose
# the specification. See PROJECT_PLAN.md Q-2.
# ----------------------------------------------------------------------------
import unittest as _unittest
raise _unittest.SkipTest(
    "2.9.18 partial-coverage OCR design not present in any available version "
    "(enrich.structured_subject_count) -- see PROJECT_PLAN.md Q-2")

import unittest
from dataclasses import fields
from pii_triage.enrich import structured_subject_count
from pii_triage.config import Config


class Cfg:
    bde_threshold = 51


class TestStructuredSubjectCount(unittest.TestCase):
    def test_single_person_repeated_counts_once(self):
        # a 1-person ledger: same name on 500 transaction rows
        text = "\n".join([f"Ackerman, Robert   TXN {i}   $ {i*12}.00" for i in range(500)])
        self.assertEqual(structured_subject_count(text, Cfg()), 1)

    def test_real_roster_counts_distinct(self):
        names = ["Ackerman, Robert", "Boyle, Susan", "Chen, Wei", "Diaz, Maria", "Evans, John"]
        text = "\n".join(names * 3)   # each repeated 3x -> still 5 distinct
        self.assertEqual(structured_subject_count(text, Cfg()), 5)

    def test_60_distinct_names_is_bde_scale(self):
        text = "\n".join(f"Sur{chr(65+i//26)}{chr(65+i%26)}, John" for i in range(60))
        self.assertEqual(structured_subject_count(text, Cfg()), 60)

    def test_strict_ssn_counts_distinct(self):
        text = "\n".join(f"{100+i:03d}-45-{6000+i:04d}" for i in range(55))
        self.assertEqual(structured_subject_count(text, Cfg()), 55)

    def test_bare_9digit_account_numbers_do_NOT_inflate(self):
        # the failure mode: "SSN" header + thousands of 9-digit account numbers.
        # strict counter requires ddd-dd-dddd separators, so bare digits don't count.
        text = "SSN Report\n" + "\n".join(f"Acct {200000000+i}  balance $ {i}" for i in range(5000))
        self.assertEqual(structured_subject_count(text, Cfg()), 0)

    def test_takes_max_of_names_and_ssns(self):
        text = "Smith, John 111-22-3333\nSmith, John 111-22-3333"  # 1 name, 1 ssn
        self.assertEqual(structured_subject_count(text, Cfg()), 1)

    def test_empty(self):
        self.assertEqual(structured_subject_count("", Cfg()), 0)


class TestFlagDefaultOff(unittest.TestCase):
    def test_default_is_off_so_behavior_unchanged(self):
        # opt-in: must default False so shipping the build cannot regress recall
        f = next(f for f in fields(Config) if f.name == "bde_tier_distinct")
        self.assertFalse(f.default)


if __name__ == "__main__":
    unittest.main()