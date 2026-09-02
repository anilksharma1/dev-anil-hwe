"""Unit tests for collect_outputs.py's incremental --watch mode.

No Azure needed: scaling_lib.status._fetch_entities and collect_outputs._read_completed_entity
are monkeypatched directly, the same style test_hwe_scaled_ui.py already uses for
hwe_scaled_store.py. Point SCALING_LIB_SRC at a scaling-lib checkout for local runs where
scaling-lib isn't pip-installed (mirrors hwe_scaled_store.py's own dev-mode path insertion).

Run:  python -m unittest test_collect_outputs -v
"""
import csv
import os
import shutil
import sys
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
