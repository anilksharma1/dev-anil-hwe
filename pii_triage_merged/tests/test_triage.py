"""Unit tests: run with `python -m unittest` from the package root."""
import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii_triage.config import DEFAULT_RULEPACK, load_rulepack
from pii_triage.detection import (luhn_valid, CompiledRules, detect,
                                  detect_names, detect_addresses)
from pii_triage.routing import (bucket_of, complexity_bucket, choose_lane,
                                estimate_entities, FileRecord)
from pii_triage.sampling import estimate


class TestLuhn(unittest.TestCase):
    def test_valid(self): self.assertTrue(luhn_valid("4111111111111111"))
    def test_invalid(self): self.assertFalse(luhn_valid("4111111111111112"))


class TestDetection(unittest.TestCase):
    def setUp(self):
        self.rules = CompiledRules.from_pack(DEFAULT_RULEPACK, use_ner=False)

    def test_counts_and_labels(self):
        text = ("Mr. John Smith ssn 123-45-6789 email a@b.com email A@B.com "
                "card 4111 1111 1111 1111 phone (555) 123-4567 123 Main Street")
        counts, labels, cats = detect(text, self.rules)
        self.assertIn("Government-Issued Identification", cats)
        self.assertEqual(counts["SSN"], 1)
        self.assertEqual(counts["EMAIL"], 1)
        self.assertEqual(counts["CARD"], 1)
        self.assertEqual(counts["PHONE"], 1)
        self.assertGreaterEqual(counts["NAME"], 1)
        self.assertGreaterEqual(counts["ADDRESS"], 1)
        self.assertIn("Name", labels)
        self.assertIn("Address", labels)

    def test_no_values_leak(self):
        counts, labels, cats = detect("ssn 123-45-6789 secret@x.com Mr. John Smith", self.rules)
        blob = repr(counts) + repr(labels)
        for v in ("123-45-6789", "secret@x.com", "John Smith"):
            self.assertNotIn(v, blob)

    def test_names_structural(self):
        # titles and explicit field labels are detected; bare name guesses are not
        names = detect_names("Mr. Robert Jones met the team. Patient: Mary Adams.", use_ner=False)
        self.assertIn("robert jones", names)
        self.assertIn("mary adams", names)
        self.assertEqual(detect_names("spoke with Andrew Wallach", use_ner=False), set())

    def test_address(self):
        self.assertEqual(detect_addresses("123 Main Street, Springfield 62704"), 1)
        self.assertEqual(detect_addresses("no address here"), 0)


class TestRouting(unittest.TestCase):
    def test_buckets_match_spec(self):
        self.assertEqual(bucket_of(0), "0-10")
        self.assertEqual(bucket_of(10), "0-10")
        self.assertEqual(bucket_of(15), "10-20")
        self.assertEqual(bucket_of(75), "50-100")
        self.assertEqual(bucket_of(500), "100+")

    def test_complexity(self):
        self.assertEqual(complexity_bucket(3), "1-4 pages")
        self.assertEqual(complexity_bucket(60), "51+ pages")

    def test_lanes(self):
        r = FileRecord("a", "a", ".pdf", 1, searchable=False, text_extractable="image_only")
        self.assertEqual(choose_lane(r), "nonsearchable_sample")
        r = FileRecord("a", "a", ".docx", 1, searchable=True, estimated_entities=0, entities_found="")
        self.assertEqual(choose_lane(r), "likely_non_responsive")
        r = FileRecord("a", "a", ".xlsx", 1, searchable=True, estimated_entities=80,
                       entities_found="SSN", is_structured=True, is_bde=True)
        self.assertEqual(choose_lane(r), "structured_bde")
        r = FileRecord("a", "a", ".zip", 1, status="container")
        self.assertEqual(choose_lane(r), "container_expand")

    def test_estimate(self):
        n = estimate_entities({}, {"SSN": 0}, ["Health"], ("SSN", "NAME"))
        self.assertEqual(n, 1)  # floor when a signal exists
        n = estimate_entities({"is_structured": True, "structured_entity_rows": 42}, {}, [], ())
        self.assertEqual(n, 42)


class TestRulepack(unittest.TestCase):
    def test_partial_merges(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write('{"name":"matterX","bde_threshold":100}'); path = fh.name
        try:
            pack = load_rulepack(path)
            self.assertEqual(pack["bde_threshold"], 100)
            self.assertTrue(any(e["key"] == "SSN" for e in pack["entities"]))
        finally:
            os.remove(path)


class TestExtrapolation(unittest.TestCase):
    def test_estimate_table2(self):
        d = tempfile.mkdtemp()
        inv = os.path.join(d, "inv.csv"); samp = os.path.join(d, "s.csv"); out = os.path.join(d, "t2.csv")
        from pii_triage.routing import FIELDNAMES
        with open(inv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDNAMES); w.writeheader()
            for i in range(100):
                r = {k: "" for k in FIELDNAMES}
                r.update(rel_path=f"f{i}.pdf", ext=".pdf", status="ok", searchable="False",
                         suggested_lane="nonsearchable_sample", complexity_bucket="5-10 pages")
                w.writerow(r)
        # Coded sample: 5 files, 1 responsive, 1 BDE -> 20% resp, 20% bde
        with open(samp, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["rel_path", "complexity_bucket", "file_type",
                                               "gold_responsive", "gold_bde"]); w.writeheader()
            for i in range(5):
                w.writerow({"rel_path": f"f{i}.pdf", "complexity_bucket": "5-10 pages",
                            "file_type": ".pdf", "gold_responsive": "1" if i == 0 else "0",
                            "gold_bde": "1" if i == 1 else "0"})
        table = estimate(inv, samp, out)
        self.assertEqual(table[0]["# of Files"], 100)
        self.assertEqual(table[0]["# of Responsive (Predicted)"], 20)  # 20% of 100
        self.assertEqual(table[0]["# of BDEs (Predicted)"], 20)


if __name__ == "__main__":
    unittest.main()
