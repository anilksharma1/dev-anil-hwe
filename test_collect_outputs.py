"""Unit tests for collect_outputs.py's incremental --watch mode.

No Azure needed: scaling_lib.status._fetch_entities and collect_outputs._read_completed_entity
are monkeypatched directly, the same style test_hwe_scaled_ui.py already uses for
hwe_scaled_store.py. Point SCALING_LIB_SRC at a scaling-lib checkout for local runs where
scaling-lib isn't pip-installed (mirrors hwe_scaled_store.py's own dev-mode path insertion).

Run:  python -m unittest test_collect_outputs -v
"""
import contextlib
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

_SL_SRC = os.environ.get("SCALING_LIB_SRC")
if _SL_SRC and os.path.isdir(_SL_SRC) and _SL_SRC not in sys.path:
    sys.path.insert(0, _SL_SRC)

import collect_outputs as co


def _entity(job_id: str, row_key: str, file_name: str, status: str = "completed") -> dict:
    return {"PartitionKey": job_id, "RowKey": row_key, "file_name": file_name, "status": status}


class CollectIncremental(unittest.TestCase):
    """collect_incremental() -- the building block --watch polls on an interval."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.out = os.path.join(self.d, "inventory.csv")
        import scaling_lib.status as sl_status
        self.sl_status = sl_status
        self._orig_fetch = sl_status._fetch_entities
        self._orig_read = co._read_completed_entity

    def tearDown(self):
        self.sl_status._fetch_entities = self._orig_fetch
        co._read_completed_entity = self._orig_read
        shutil.rmtree(self.d, ignore_errors=True)

    def test_appends_new_rows_and_flushes_immediately(self):
        ents = [_entity("J", "r1", "a.pdf")]
        self.sl_status._fetch_entities = lambda status_filter=None, since=None: ents
        co._read_completed_entity = lambda e: ({"rel_path": "a.pdf", "status": "ok"}, None)

        n, seen = co.collect_incremental(self.out, set())
        self.assertEqual(n, 1)
        self.assertIn("J/r1", seen)
        with open(self.out, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rel_path"], "a.pdf")

    def test_already_seen_row_is_not_re_appended(self):
        ents = [_entity("J", "r1", "a.pdf")]
        self.sl_status._fetch_entities = lambda status_filter=None, since=None: ents
        co._read_completed_entity = lambda e: self.fail("must not re-read an already-seen row")

        n, seen = co.collect_incremental(self.out, {"J/r1"})
        self.assertEqual(n, 0)

    def test_windows_leg_stub_marked_seen_without_writing_a_row(self):
        ents = [_entity("J", "r1", "report.doc")]
        self.sl_status._fetch_entities = lambda status_filter=None, since=None: ents
        co._read_completed_entity = lambda e: (None, None)   # forwarded.json stub

        n, seen = co.collect_incremental(self.out, set())
        self.assertEqual(n, 0)
        self.assertIn("J/r1", seen)              # marked seen so it isn't re-checked forever
        self.assertFalse(os.path.exists(self.out))   # nothing real to write yet

    def test_genuinely_missing_output_stays_unseen_for_retry(self):
        ents = [_entity("J", "r1", "a.pdf")]
        self.sl_status._fetch_entities = lambda status_filter=None, since=None: ents
        co._read_completed_entity = lambda e: (None, "still writing")

        n, seen = co.collect_incremental(self.out, set())
        self.assertEqual(n, 0)
        self.assertNotIn("J/r1", seen)   # left unseen -- retried on the next pass

    def test_second_pass_only_appends_the_genuinely_new_row(self):
        ents = [_entity("J", "r1", "a.pdf")]
        self.sl_status._fetch_entities = lambda status_filter=None, since=None: ents
        co._read_completed_entity = lambda e: ({"rel_path": "a.pdf", "status": "ok"}, None)
        n1, seen = co.collect_incremental(self.out, set())
        self.assertEqual(n1, 1)

        ents.append(_entity("J", "r2", "b.pdf"))
        reads = {"r1": ({"rel_path": "a.pdf", "status": "ok"}, None),
                 "r2": ({"rel_path": "b.pdf", "status": "ok"}, None)}
        co._read_completed_entity = lambda e: reads[e["RowKey"]]
        n2, seen = co.collect_incremental(self.out, seen)
        self.assertEqual(n2, 1)
        with open(self.out, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(sorted(r["rel_path"] for r in rows), ["a.pdf", "b.pdf"])


class WatchStateSidecar(unittest.TestCase):
    """The <out>.watch_state.json sidecar: crash-safe resume + the untracked-file guard."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.out = os.path.join(self.d, "inventory.csv")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_save_and_load_roundtrip(self):
        state_path = co._watch_state_path(self.out)
        co._save_watch_state(state_path, {"J/r1", "J/r2"})
        self.assertEqual(co._load_watch_state(state_path), {"J/r1", "J/r2"})

    def test_missing_state_file_is_empty_set(self):
        self.assertEqual(co._load_watch_state(co._watch_state_path(self.out)), set())

    def test_refuses_to_append_to_an_untracked_existing_csv(self):
        # An inventory.csv from a prior one-shot collect() run, with no sidecar -- watch() must
        # refuse rather than risk silently duplicating already-collected rows.
        with open(self.out, "w", encoding="utf-8") as fh:
            fh.write("rel_path\nx\n")
        with self.assertRaises(SystemExit) as ctx:
            co.watch(self.out, interval=0.01, max_iterations=1)
        self.assertEqual(ctx.exception.code, 2)


class WatchPidLock(unittest.TestCase):
    """Two --watch processes must never write to the same CSV at once."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.out = os.path.join(self.d, "inventory.csv")
        import scaling_lib.status as sl_status
        self.sl_status = sl_status
        self._orig_fetch = sl_status._fetch_entities
        self._orig_drained = co._is_drained
        self.sl_status._fetch_entities = lambda status_filter=None, since=None: []
        co._is_drained = lambda: True

    def tearDown(self):
        self.sl_status._fetch_entities = self._orig_fetch
        co._is_drained = self._orig_drained
        shutil.rmtree(self.d, ignore_errors=True)

    def test_pid_alive_detects_this_own_process(self):
        self.assertTrue(co._pid_alive(os.getpid()))

    def test_pid_alive_false_for_a_pid_that_has_exited(self):
        # Spawn a process, wait for it to exit, then its PID is (almost always) free.
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        self.assertFalse(co._pid_alive(p.pid))

    def test_refuses_second_watch_while_first_pid_alive(self):
        with open(co._watch_pid_path(self.out), "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))   # this test process is very much alive
        with self.assertRaises(SystemExit) as ctx:
            co.watch(self.out, interval=0.01, max_iterations=1)
        self.assertEqual(ctx.exception.code, 2)

    def test_stale_pid_file_from_a_dead_process_is_ignored(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        with open(co._watch_pid_path(self.out), "w", encoding="utf-8") as fh:
            fh.write(str(p.pid))
        total = co.watch(self.out, interval=0.01)   # must NOT raise -- the old pid is dead
        self.assertEqual(total, 0)

    def test_pid_file_is_removed_on_clean_exit(self):
        co.watch(self.out, interval=0.01)
        self.assertFalse(os.path.exists(co._watch_pid_path(self.out)))

    def test_restart_clears_a_stale_pid_file_too(self):
        with open(co._watch_pid_path(self.out), "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        # without --restart this would refuse; with it, the lock (and out/state) are wiped first
        total = co.watch(self.out, interval=0.01, restart=True)
        self.assertEqual(total, 0)


class WatchLoop(unittest.TestCase):
    """watch() end-to-end: stop-on-drain and resume-from-sidecar."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.out = os.path.join(self.d, "inventory.csv")
        import scaling_lib.status as sl_status
        self.sl_status = sl_status
        self._orig_fetch = sl_status._fetch_entities
        self._orig_read = co._read_completed_entity
        self._orig_drained = co._is_drained

    def tearDown(self):
        self.sl_status._fetch_entities = self._orig_fetch
        co._read_completed_entity = self._orig_read
        co._is_drained = self._orig_drained
        shutil.rmtree(self.d, ignore_errors=True)

    def test_stops_automatically_once_drained(self):
        ents = [_entity("J", "r1", "a.pdf")]
        self.sl_status._fetch_entities = lambda status_filter=None, since=None: ents
        co._read_completed_entity = lambda e: ({"rel_path": "a.pdf", "status": "ok"}, None)
        co._is_drained = lambda: True   # already drained on the very first check

        total = co.watch(self.out, interval=0.01)
        self.assertEqual(total, 1)
        with open(self.out, newline="", encoding="utf-8") as fh:
            self.assertEqual(len(list(csv.DictReader(fh))), 1)

    def test_restart_discards_untracked_file_and_starts_fresh(self):
        with open(self.out, "w", encoding="utf-8") as fh:
            fh.write("rel_path\nstale\n")
        self.sl_status._fetch_entities = lambda status_filter=None, since=None: []
        co._is_drained = lambda: True

        total = co.watch(self.out, interval=0.01, restart=True)
        self.assertEqual(total, 0)   # nothing completed; the stale untracked row is gone, not carried over

    def test_resumes_from_sidecar_without_reappending(self):
        # Simulate a completed prior watch session: file + sidecar already on disk.
        with open(self.out, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["rel_path", "status"])
            w.writeheader()
            w.writerow({"rel_path": "a.pdf", "status": "ok"})
        co._save_watch_state(co._watch_state_path(self.out), {"J/r1"})

        ents = [_entity("J", "r1", "a.pdf")]   # the SAME already-collected row
        self.sl_status._fetch_entities = lambda status_filter=None, since=None: ents
        co._read_completed_entity = lambda e: self.fail("should not re-read an already-seen row")
        co._is_drained = lambda: True

        total = co.watch(self.out, interval=0.01)
        self.assertEqual(total, 1)   # the pre-existing row, correctly counted, never re-read


class DumpTimingCollapsesLegacyPairs(unittest.TestCase):
    """Gap 6: _timing.json's files_completed/tasks must not double-count a Windows-leg
    pre-conversion + post-conversion row pair (collect()'s own inventory.csv never had this
    bug -- this is the OTHER place the same raw-row count was leaking)."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.out = os.path.join(self.d, "inventory.csv")
        import scaling_lib.metrics as sl_metrics
        self.sl_metrics = sl_metrics
        self._orig_run_metrics = sl_metrics.run_metrics
        self._orig_worker_config = co._fetch_worker_config
        co._fetch_worker_config = lambda: None   # skip the ARM/pricing network calls

    def tearDown(self):
        self.sl_metrics.run_metrics = self._orig_run_metrics
        co._fetch_worker_config = self._orig_worker_config
        shutil.rmtree(self.d, ignore_errors=True)

    def test_files_completed_and_tasks_collapse_the_legacy_pair(self):
        sl_metrics = self.sl_metrics
        now = datetime.now(timezone.utc)
        tasks = [
            sl_metrics.TaskRecord(file_name="report.doc", job_id="J", file_size_bytes=10,
                                  started_at=now, status="completed", completed_at=now),
            sl_metrics.TaskRecord(file_name="report.docx", job_id="J", file_size_bytes=20,
                                  started_at=now, status="completed", completed_at=now),
            sl_metrics.TaskRecord(file_name="memo.pdf", job_id="J", file_size_bytes=30,
                                  started_at=now, status="completed", completed_at=now),
        ]
        sl_metrics.run_metrics = lambda: sl_metrics.RunMetrics(tasks=tasks)

        self.assertTrue(co.dump_timing(self.out))
        snap_path = self.out[:-len(".csv")] + "_timing.json"
        with open(snap_path, encoding="utf-8") as fh:
            snap = json.load(fh)
        self.assertEqual(snap["files_completed"], 2)   # not 3 -- the legacy pair collapses to 1
        self.assertEqual(len(snap["tasks"]), 2)
        self.assertEqual({t["file_name"] for t in snap["tasks"]}, {"report.docx", "memo.pdf"})


class DebugGating(unittest.TestCase):
    """Gap 8: LOG_LEVEL=DEBUG gates the extra per-pass debug lines onto stderr."""

    def test_writes_to_stderr_when_enabled(self):
        orig = co._DEBUG
        co._DEBUG = True
        try:
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                co._debug("hello")
            self.assertIn("hello", buf.getvalue())
        finally:
            co._DEBUG = orig

    def test_silent_when_disabled(self):
        orig = co._DEBUG
        co._DEBUG = False
        try:
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                co._debug("hello")
            self.assertEqual(buf.getvalue(), "")
        finally:
            co._DEBUG = orig


if __name__ == "__main__":
    unittest.main()
