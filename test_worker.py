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
import logging
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


class PreflightChecks(unittest.TestCase):
    """Gap 4: scaling-lib/auth/mount misconfigurations must fail fast and loud at startup,
    not three stages deep as a per-file 'llm_failed:AuthenticationError'."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.in_mount = os.path.join(self.d, "in")
        self.out_mount = os.path.join(self.d, "out")
        os.makedirs(self.in_mount)
        os.makedirs(self.out_mount)
        self._orig_env = {k: os.environ.get(k) for k in ("INPUT_MOUNT", "OUTPUT_MOUNT")}
        os.environ["INPUT_MOUNT"] = self.in_mount
        os.environ["OUTPUT_MOUNT"] = self.out_mount

        import scaling_lib.queue as sl_queue
        self.sl_queue = sl_queue
        self._orig_queue_status = sl_queue.queue_status
        sl_queue.queue_status = lambda: {"queue_count": 3, "dead_letter_count": 0}

        self.cfg = Config(root=self.in_mount, rulepack=load_rulepack(None))

    def tearDown(self):
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.sl_queue.queue_status = self._orig_queue_status
        shutil.rmtree(self.d, ignore_errors=True)

    def test_all_pass_when_mounts_ok_and_llm_off(self):
        checks = worker._preflight_checks(self.cfg)
        self.assertTrue(all(c["ok"] for c in checks), checks)
        names = {c["name"] for c in checks}
        self.assertIn("scaling_lib import", names)
        self.assertIn("INPUT_MOUNT", names)
        self.assertIn("OUTPUT_MOUNT", names)
        self.assertNotIn("Azure AI credential (LLM/OCR)", names)   # not checked when LLM/OCR off

    def test_missing_input_mount_fails_a_required_check(self):
        os.environ["INPUT_MOUNT"] = os.path.join(self.d, "does-not-exist")
        checks = worker._preflight_checks(self.cfg)
        c = next(c for c in checks if c["name"] == "INPUT_MOUNT")
        self.assertFalse(c["ok"])
        self.assertTrue(c["required"])

    def test_queue_unreachable_fails_a_required_check(self):
        def boom():
            raise RuntimeError("no route to host")
        self.sl_queue.queue_status = boom
        checks = worker._preflight_checks(self.cfg)
        c = next(c for c in checks if c["name"] == "storage queue/table reachable")
        self.assertFalse(c["ok"])
        self.assertTrue(c["required"])

    def test_llm_credential_checked_only_when_llm_or_ocr_enabled(self):
        checks = worker._preflight_checks(self.cfg)   # use_llm/use_ocr default False
        self.assertFalse(any(c["name"] == "Azure AI credential (LLM/OCR)" for c in checks))

        from dataclasses import replace
        import scaling_lib._config as sl_config
        orig_cred = sl_config._credential

        class _FakeCred:
            def get_token(self, scope):
                raise RuntimeError("no az login")
        sl_config._credential = lambda: _FakeCred()
        try:
            checks2 = worker._preflight_checks(replace(self.cfg, use_llm=True))
        finally:
            sl_config._credential = orig_cred
        c = next(c for c in checks2 if c["name"] == "Azure AI credential (LLM/OCR)")
        self.assertFalse(c["ok"])
        self.assertFalse(c["required"])   # warns, never blocks startup by itself

    def test_run_preflight_or_exit_raises_systemexit_on_required_failure(self):
        os.environ["INPUT_MOUNT"] = os.path.join(self.d, "does-not-exist")
        with self.assertRaises(SystemExit):
            worker._run_preflight_or_exit(self.cfg)
        events = list((Path(self.out_mount) / "_events").glob("preflight_fail_*.json"))
        self.assertEqual(len(events), 1)

    def test_run_preflight_or_exit_does_not_exit_on_optional_failure_only(self):
        from dataclasses import replace
        import scaling_lib._config as sl_config
        orig_cred = sl_config._credential

        class _FakeCred:
            def get_token(self, scope):
                raise RuntimeError("no az login")
        sl_config._credential = lambda: _FakeCred()
        try:
            worker._run_preflight_or_exit(replace(self.cfg, use_llm=True))   # must NOT raise
        finally:
            sl_config._credential = orig_cred
        events = list((Path(self.out_mount) / "_events").glob("preflight_warn_*.json"))
        self.assertEqual(len(events), 1)


class ResolveLogLevel(unittest.TestCase):
    """Gap 8: LOG_LEVEL=DEBUG must actually raise the log level; an unset/typo'd value must
    default safely to INFO rather than raising."""

    def test_debug_maps_to_debug_level(self):
        self.assertEqual(worker._resolve_log_level("DEBUG"), logging.DEBUG)

    def test_lowercase_is_accepted(self):
        self.assertEqual(worker._resolve_log_level("debug"), logging.DEBUG)

    def test_empty_defaults_to_info(self):
        self.assertEqual(worker._resolve_log_level(""), logging.INFO)

    def test_unrecognised_value_defaults_to_info_not_raises(self):
        self.assertEqual(worker._resolve_log_level("not-a-level"), logging.INFO)


class CpuAwareConcurrency(unittest.TestCase):
    """'Scale up' (more vCPU per replica) and 'multi-thread within a replica' should move
    together -- default concurrency is sized off the container's actual cgroup CPU quota,
    not a flat number that leaves a bigger container's extra vCPU idle."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _write(self, rel_path, content):
        full = os.path.join(self.d, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)

    def test_cgroup_v2_quota_parsed(self):
        self._write("cpu.max", "200000 100000\n")   # 2.0 vCPU
        self.assertAlmostEqual(worker._detect_cpu_quota(self.d), 2.0)

    def test_cgroup_v2_max_falls_through_to_v1_then_none(self):
        self._write("cpu.max", "max 100000\n")   # unlimited -- not a real quota
        self.assertIsNone(worker._detect_cpu_quota(self.d))

    def test_cgroup_v1_quota_parsed(self):
        self._write("cpu/cpu.cfs_quota_us", "50000\n")
        self._write("cpu/cpu.cfs_period_us", "100000\n")   # 0.5 vCPU
        self.assertAlmostEqual(worker._detect_cpu_quota(self.d), 0.5)

    def test_v2_preferred_over_v1_when_both_present(self):
        self._write("cpu.max", "400000 100000\n")   # 4.0 vCPU
        self._write("cpu/cpu.cfs_quota_us", "50000\n")
        self._write("cpu/cpu.cfs_period_us", "100000\n")
        self.assertAlmostEqual(worker._detect_cpu_quota(self.d), 4.0)

    def test_no_cgroup_files_returns_none(self):
        self.assertIsNone(worker._detect_cpu_quota(self.d))

    def test_default_concurrency_unknown_quota_falls_back_to_four(self):
        self.assertEqual(worker._default_concurrency(self.d), 4)

    def test_default_concurrency_scales_with_quota(self):
        self._write("cpu.max", "800000 100000\n")   # 8.0 vCPU -> x2 = 16
        self.assertEqual(worker._default_concurrency(self.d), 16)

    def test_default_concurrency_floors_at_two(self):
        self._write("cpu.max", "10000 100000\n")   # 0.1 vCPU -> x2 = 0.2, floored to 2
        self.assertEqual(worker._default_concurrency(self.d), 2)

    def test_default_concurrency_caps_at_sixteen(self):
        self._write("cpu.max", "2000000 100000\n")   # 20 vCPU -> x2 = 40, capped to 16
        self.assertEqual(worker._default_concurrency(self.d), 16)


if __name__ == "__main__":
    unittest.main()
