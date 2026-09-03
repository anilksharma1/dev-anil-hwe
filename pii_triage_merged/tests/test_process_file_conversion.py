"""Tests for _process_file's inline legacy-conversion handling (runner.py) -- the graceful
"lost cause, no retry" timeout behavior that used to live in worker.py's now-removed
_convert_and_forward (the Windows-leg two-hop dance), moved here now that conversion runs
inline via LibreOffice headless, cross-platform, for whichever worker dequeues the file.

pii_triage.conversion.convert_legacy_office is monkeypatched directly, so these run without a
real LibreOffice install (see test_conversion.py for that limitation's fuller note).
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import pii_triage.conversion as conversion_mod
import pii_triage.runner as runner
from pii_triage.config import Config, load_rulepack
from pii_triage.detection import CompiledRules


class ProcessFileConversionHandling(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.src = os.path.join(self.d, "report.doc")
        with open(self.src, "wb") as fh:
            fh.write(b"fake legacy office bytes")

        self._orig_convert = conversion_mod.convert_legacy_office
        self._orig_cfg = runner._CFG
        self._orig_rules = runner._RULES
        cfg = Config(root=self.d, rulepack=load_rulepack(None), timeout_s=1)
        runner._CFG = cfg
        runner._RULES = CompiledRules.from_pack(cfg.rulepack, use_ner=False)

    def tearDown(self):
        conversion_mod.convert_legacy_office = self._orig_convert
        runner._CFG = self._orig_cfg
        runner._RULES = self._orig_rules
        shutil.rmtree(self.d, ignore_errors=True)

    def test_conversion_timeout_is_a_bounded_no_retry_result(self):
        def fake_convert(src, dest_dir, timeout_s):
            raise conversion_mod.ConversionTimeout("soffice exceeded 1s")
        conversion_mod.convert_legacy_office = fake_convert

        rec = runner.process_file(self.src)   # must NOT raise
        self.assertEqual(rec["status"], "timeout")
        self.assertEqual(rec["file_name"], "report.doc")
        self.assertEqual(rec["suggested_lane"], "review_error")

    def test_ordinary_conversion_failure_degrades_to_original_file(self):
        conversion_mod.convert_legacy_office = lambda src, dest_dir, timeout_s: None
        rec = runner.process_file(self.src)   # must NOT raise -- this is the key assertion
        # falls through to get_extractor(".doc") on the ORIGINAL (still-.doc) file rather
        # than erroring out. Whether that resolves to "ok" (antiword/catdoc happen to be
        # installed) or "no_parser" (neither is) depends on the box running this test --
        # the file identity is the part this test actually owns.
        self.assertIn(rec["status"], ("ok", "no_parser"))
        self.assertEqual(rec["file_name"], "report.doc")
        self.assertEqual(rec["ext"], ".doc")

    def test_convert_timeout_env_var_overrides_general_file_timeout(self):
        seen = {}

        def fake_convert(src, dest_dir, timeout_s):
            seen["timeout_s"] = timeout_s
            raise conversion_mod.ConversionTimeout("x")
        conversion_mod.convert_legacy_office = fake_convert

        os.environ["CONVERT_TIMEOUT_S"] = "7"
        try:
            runner.process_file(self.src)
        finally:
            del os.environ["CONVERT_TIMEOUT_S"]
        self.assertEqual(seen["timeout_s"], 7)

    def test_successful_conversion_still_reports_the_original_identity(self):
        def fake_convert(src, dest_dir, timeout_s):
            dest = Path(dest_dir) / "report.docx"
            dest.write_bytes(b"docx bytes")
            return dest
        conversion_mod.convert_legacy_office = fake_convert

        rec = runner.process_file(self.src)
        # the record reports the ORIGINAL file's identity -- only the extractor path
        # switches internally to read the converted file.
        self.assertEqual(rec["file_name"], "report.doc")
        self.assertEqual(rec["ext"], ".doc")


if __name__ == "__main__":
    unittest.main()
