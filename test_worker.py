"""Unit tests for worker.py's Windows-leg conversion handling: a genuinely lost (timed-out)
legacy conversion must complete gracefully -- once, no retry -- while an ordinary conversion
failure keeps the normal scaling-lib retry behavior.

No Azure needed: pii_triage.conversion.convert_legacy_office is monkeypatched directly (same
style test_hwe_scaled_ui.py already uses). Point SCALING_LIB_SRC at a scaling-lib checkout for
local runs where scaling-lib isn't pip-installed.

Run:  python -m unittest test_worker -v
"""
import contextlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_SL_SRC = os.environ.get("SCALING_LIB_SRC")
if _SL_SRC and os.path.isdir(_SL_SRC) and _SL_SRC not in sys.path:
    sys.path.insert(0, _SL_SRC)

os.environ.setdefault("INPUT_MOUNT", tempfile.gettempdir())

import worker
import pii_triage.conversion as conversion_mod
from pii_triage.config import Config, load_rulepack


class _FakeTask:
    def __init__(self, job_id="J"):
        self.job_id = job_id

    def checkpoint(self, label):
        return contextlib.nullcontext()


class ConvertAndForward(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.src = os.path.join(self.d, "report.doc")
        with open(self.src, "wb") as fh:
            fh.write(b"fake legacy office bytes")
        self.output_dir = Path(os.path.join(self.d, "out"))
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self._orig_convert = conversion_mod.convert_legacy_office
        self._orig_cfg = worker._cfg
        worker._cfg = Config(root=self.d, rulepack=load_rulepack(None), timeout_s=1)

    def tearDown(self):
        conversion_mod.convert_legacy_office = self._orig_convert
        worker._cfg = self._orig_cfg
        shutil.rmtree(self.d, ignore_errors=True)

    def test_timeout_writes_a_graceful_result_and_does_not_raise(self):
        def fake_convert(src, dest_dir, timeout_s):
            raise conversion_mod.ConversionTimeout("WINWORD.EXE exceeded 1s timeout")
        conversion_mod.convert_legacy_office = fake_convert

        # Must NOT raise -- a raised exception here is what would trigger scaling-lib's
        # retry, which is exactly what a genuinely lost file should skip.
        worker._convert_and_forward(self.src, self.output_dir, _FakeTask())

        result_file = self.output_dir / "result.json"
        self.assertTrue(result_file.exists())
        rec = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(rec["status"], "timeout")
        self.assertEqual(rec["file_name"], "report.doc")
        self.assertEqual(rec["suggested_lane"], "review_error")

    def test_ordinary_failure_still_raises_for_normal_retry(self):
        conversion_mod.convert_legacy_office = lambda src, dest_dir, timeout_s: None
        with self.assertRaises(RuntimeError):
            worker._convert_and_forward(self.src, self.output_dir, _FakeTask())
        self.assertFalse((self.output_dir / "result.json").exists())

    def test_convert_timeout_env_var_overrides_general_file_timeout(self):
        seen = {}

        def fake_convert(src, dest_dir, timeout_s):
            seen["timeout_s"] = timeout_s
            raise conversion_mod.ConversionTimeout("x")

        conversion_mod.convert_legacy_office = fake_convert
        os.environ["CONVERT_TIMEOUT_S"] = "7"
        try:
            worker._convert_and_forward(self.src, self.output_dir, _FakeTask())
        finally:
            del os.environ["CONVERT_TIMEOUT_S"]
        self.assertEqual(seen["timeout_s"], 7)


class DlqTripEvent(unittest.TestCase):
    """Gap 7: a circuit-breaker trip must leave a durable, UI-readable reason -- worker logs
    alone are invisible to the operator by this UI's own design."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._orig_output_mount = os.environ.get("OUTPUT_MOUNT")
        os.environ["OUTPUT_MOUNT"] = self.d

    def tearDown(self):
        if self._orig_output_mount is None:
            os.environ.pop("OUTPUT_MOUNT", None)
        else:
            os.environ["OUTPUT_MOUNT"] = self._orig_output_mount
        shutil.rmtree(self.d, ignore_errors=True)

    def test_writes_a_readable_json_event_under_events_dir(self):
        worker._write_dlq_trip_event(dlq_growth=12, done=100, rate=0.05)
        events_dir = Path(self.d) / "_events"
        files = list(events_dir.glob("dlq_trip_*.json"))
        self.assertEqual(len(files), 1)
        ev = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(ev["type"], "dlq_circuit_breaker")
        self.assertEqual(ev["dlq_growth"], 12)
        self.assertEqual(ev["completions"], 100)
        self.assertAlmostEqual(ev["rate"], 0.12)
        self.assertIn("worker_instance", ev)
        self.assertIn("at", ev)

    def test_zero_completions_does_not_raise_and_rate_is_null(self):
        worker._write_dlq_trip_event(dlq_growth=0, done=0, rate=0.05)
        events_dir = Path(self.d) / "_events"
        files = list(events_dir.glob("dlq_trip_*.json"))
        ev = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertIsNone(ev["rate"])


if __name__ == "__main__":
    unittest.main()
