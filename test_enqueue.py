"""Unit tests for enqueue.py's chunked/concurrent submission (Gap 10).

No Azure needed: scaling_lib.status.init_task and scaling_lib.queue's helpers are
monkeypatched directly, the same style test_collect_outputs.py already uses.

Run:  python -m unittest test_enqueue -v
"""
import os
import shutil
import sys
import tempfile
import threading
import unittest

_SL_SRC = os.environ.get("SCALING_LIB_SRC")
if _SL_SRC and os.path.isdir(_SL_SRC) and _SL_SRC not in sys.path:
    sys.path.insert(0, _SL_SRC)

import enqueue as en


class Chunked(unittest.TestCase):
    def test_groups_into_full_chunks_plus_a_remainder(self):
        chunks = list(en._chunked(range(7), 3))
        self.assertEqual(chunks, [[0, 1, 2], [3, 4, 5], [6]])

    def test_empty_iterable_yields_nothing(self):
        self.assertEqual(list(en._chunked([], 3)), [])

    def test_exact_multiple_has_no_short_final_chunk(self):
        chunks = list(en._chunked(range(6), 3))
        self.assertEqual(chunks, [[0, 1, 2], [3, 4, 5]])


class _FakeQueueClient:
    def __init__(self, sink, name):
        self.sink = sink
        self.name = name

    def send_message(self, msg):
        self.sink.append((self.name, msg))


class EnqueueOne(unittest.TestCase):
    """_enqueue_one -- filtering + the two per-file network calls, isolated for testing."""

    def setUp(self):
        import scaling_lib.status as sl_status
        import scaling_lib.queue as sl_queue
        self.sl_status = sl_status
        self.sl_queue = sl_queue
        self._orig_init_task = sl_status.init_task
        self._orig_build_message = sl_queue._build_message
        self._orig_is_windows_file = sl_queue._is_windows_file
        self.init_calls = []
        sl_status.init_task = lambda job_id, job_type, file_name: self.init_calls.append(
            (job_id, job_type, file_name))
        sl_queue._build_message = lambda rel, job_id, job_type, posix: {"rel": str(rel)}
        sl_queue._is_windows_file = lambda p: p.suffix.lower() in (".doc", ".xls", ".ppt")
        self.d = tempfile.mkdtemp()
        self.f = os.path.join(self.d, "a.pdf")
        with open(self.f, "w", encoding="utf-8") as fh:
            fh.write("x")
        self.main_sink, self.win_sink = [], []
        self.main_client = _FakeQueueClient(self.main_sink, "main")
        self.win_client = _FakeQueueClient(self.win_sink, "win")

    def tearDown(self):
        self.sl_status.init_task = self._orig_init_task
        self.sl_queue._build_message = self._orig_build_message
        self.sl_queue._is_windows_file = self._orig_is_windows_file
        shutil.rmtree(self.d, ignore_errors=True)

    def test_enqueues_a_matching_file_to_the_main_queue(self):
        ok = en._enqueue_one(self.f, self.d, None, "J", "T", self.main_client, self.win_client)
        self.assertTrue(ok)
        self.assertEqual(len(self.init_calls), 1)
        self.assertEqual(len(self.main_sink), 1)
        self.assertEqual(len(self.win_sink), 0)

    def test_legacy_extension_routes_to_the_windows_queue(self):
        doc = os.path.join(self.d, "report.doc")
        with open(doc, "w", encoding="utf-8") as fh:
            fh.write("x")
        ok = en._enqueue_one(doc, self.d, None, "J", "T", self.main_client, self.win_client)
        self.assertTrue(ok)
        self.assertEqual(len(self.win_sink), 1)
        self.assertEqual(len(self.main_sink), 0)

    def test_filtered_out_file_is_skipped_without_any_network_call(self):
        ok = en._enqueue_one(self.f, self.d, keep=set(), job_id="J", job_type="T",
                             main_client=self.main_client, win_client=self.win_client)
        self.assertFalse(ok)
        self.assertEqual(self.init_calls, [])
        self.assertEqual(self.main_sink, [])

    def test_kept_file_is_enqueued(self):
        rel_posix = os.path.relpath(self.f, self.d).replace(os.sep, "/")
        ok = en._enqueue_one(self.f, self.d, keep={rel_posix}, job_id="J", job_type="T",
                             main_client=self.main_client, win_client=self.win_client)
        self.assertTrue(ok)


class EnqueueConcurrent(unittest.TestCase):
    """enqueue() end to end: chunked/concurrent submission, with per-file error isolation."""

    def setUp(self):
        import scaling_lib.status as sl_status
        import scaling_lib.queue as sl_queue
        self.sl_status = sl_status
        self.sl_queue = sl_queue
        self._orig = (sl_status.init_task, sl_queue._build_message, sl_queue._is_windows_file,
                     sl_queue._ensure_queues, sl_queue._ensure_table, sl_queue._get_queue_service)
        self.lock = threading.Lock()
        self.init_calls = []
        sl_status.init_task = lambda job_id, job_type, file_name: (
            self.lock.acquire(), self.init_calls.append(file_name), self.lock.release())
        sl_queue._build_message = lambda rel, job_id, job_type, posix: {"rel": str(rel)}
        sl_queue._is_windows_file = lambda p: False
        sl_queue._ensure_queues = lambda: None
        sl_queue._ensure_table = lambda: None
        self.sent = []
        sl_queue._get_queue_service = lambda: _FakeQueueService(self.sent, self.lock)

        self.d = tempfile.mkdtemp()
        self.files_dir = os.path.join(self.d, "job1", "files")
        os.makedirs(self.files_dir)
        for i in range(12):
            with open(os.path.join(self.files_dir, f"f{i}.pdf"), "w", encoding="utf-8") as fh:
                fh.write("x")
        self._orig_env = {k: os.environ.get(k) for k in
                          ("INPUT_MOUNT", "JOB_TYPE", "AZURE_QUEUE_NAME", "AZURE_WINDOWS_QUEUE_NAME")}
        os.environ["INPUT_MOUNT"] = self.d
        os.environ["JOB_TYPE"] = "T"
        os.environ["AZURE_QUEUE_NAME"] = "main-q"
        os.environ.pop("AZURE_WINDOWS_QUEUE_NAME", None)

    def tearDown(self):
        (self.sl_status.init_task, self.sl_queue._build_message, self.sl_queue._is_windows_file,
         self.sl_queue._ensure_queues, self.sl_queue._ensure_table,
         self.sl_queue._get_queue_service) = self._orig
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.d, ignore_errors=True)

    def test_all_files_enqueued_across_chunks(self):
        n = en.enqueue(self.files_dir, job_id="J", concurrency=4, batch_size=5)
        self.assertEqual(n, 12)
        self.assertEqual(len(self.init_calls), 12)
        self.assertEqual(len(self.sent), 12)

    def test_one_bad_file_does_not_abort_the_rest(self):
        real_build = self.sl_queue._build_message

        def flaky(rel, job_id, job_type, posix):
            if "f3" in str(rel):
                raise RuntimeError("simulated Table/Queue failure")
            return real_build(rel, job_id, job_type, posix)
        self.sl_queue._build_message = flaky

        n = en.enqueue(self.files_dir, job_id="J", concurrency=4, batch_size=5)
        self.assertEqual(n, 11)   # 12 files minus the one that failed


class _FakeQueueService:
    def __init__(self, sink, lock):
        self.sink = sink
        self.lock = lock

    def get_queue_client(self, name):
        return _LockedFakeQueueClient(self.sink, self.lock, name)


class _LockedFakeQueueClient:
    def __init__(self, sink, lock, name):
        self.sink = sink
        self.lock = lock
        self.name = name

    def send_message(self, msg):
        with self.lock:
            self.sink.append((self.name, msg))


if __name__ == "__main__":
    unittest.main()
