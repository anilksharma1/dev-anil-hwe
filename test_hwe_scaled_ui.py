"""Unit tests for the scaled HWE Runner UI (Phase 2, read-only).

Run:  python -m unittest test_hwe_scaled_ui -v

Covers the brief's §8 requirements that apply to this phase:
  - the "not measured vs zero" formatter (measured());
  - command construction, and that UI-only form fields never leak into argv (§1.4 #12);
  - the Compare invariant: a rules-decided row that moves is caught, model-decided drift is not;
  - loopback-only binding.
(The filename sanitiser and the concurrency equal-timestamp sweep are Phase-4 helpers and get their
 tests when that code lands — they are not stubbed in ahead of use.)
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import hwe_scaled_ui as ui
import hwe_scaled_store as store

HERE = os.path.dirname(os.path.abspath(__file__))
SCALING_1 = os.path.join(HERE, "outputs", "scaling-1", "inventory.csv")


class MeasuredFormatter(unittest.TestCase):
    def test_none_is_not_measured_not_zero(self):
        # The single most important wording rule: a metric never recorded is None, not 0.
        self.assertIsNone(ui.measured(None))

    def test_real_zero_passes_through(self):
        self.assertEqual(ui.measured(0), 0)          # a real, recorded zero stays 0
        self.assertEqual(ui.measured(0.0), 0.0)

    def test_real_numbers_pass_through(self):
        self.assertEqual(ui.measured(1234), 1234)

    def test_bool_flag_is_not_coerced(self):
        self.assertIs(ui.measured(True), True)


class CsvCoercion(unittest.TestCase):
    def test_as_bool(self):
        for truthy in ("True", "true", "1", "yes", True):
            self.assertTrue(ui.as_bool(truthy))
        for falsy in ("False", "false", "0", "", "no", None):
            self.assertFalse(ui.as_bool(falsy))

    def test_as_int_and_float_default_on_junk(self):
        self.assertEqual(ui.as_int("51"), 51)
        self.assertEqual(ui.as_int(""), 0)
        self.assertEqual(ui.as_int("nan-ish"), 0)
        self.assertEqual(ui.as_float("1.5"), 1.5)
        self.assertEqual(ui.as_float(""), 0.0)


class CommandConstruction(unittest.TestCase):
    def test_report_argv(self):
        a = ui.build_report_argv("inv.csv", "out.csv")
        self.assertEqual(a[1:], ["-m", "pii_triage", "report", "inv.csv", "--out", "out.csv"])

    def test_collect_argv(self):
        a = ui.build_collect_argv("runs/2026/inventory.csv")
        self.assertTrue(a[1].endswith("collect_outputs.py"))
        self.assertEqual(a[-2:], ["--out", "runs/2026/inventory.csv"])

    def test_sample_argv_has_rate_and_seed(self):
        a = ui.build_sample_argv("inv.csv", "s.csv", 0.05, 12345)
        self.assertIn("--rate", a)
        self.assertIn("0.05", a)
        self.assertIn("--seed", a)
        self.assertIn("12345", a)

    def test_benchmark_argv_absent_means(self):
        self.assertNotIn("--absent-means", ui.build_benchmark_argv("inv.csv", "g.xlsx"))  # default off
        a = ui.build_benchmark_argv("inv.csv", "g.xlsx", absent_means="zero")
        self.assertIn("--absent-means", a)
        self.assertIn("zero", a)

    def test_benchmark_argv_omits_blank_overrides(self):
        a = ui.build_benchmark_argv("inv.csv", "gold.xlsx")
        self.assertEqual(a[-2:], ["inv.csv", "gold.xlsx"])
        self.assertNotIn("--id-col", a)
        a2 = ui.build_benchmark_argv("inv.csv", "gold.xlsx", id_col="File", sheet="Sheet1")
        self.assertIn("--id-col", a2)
        self.assertIn("File", a2)
        self.assertIn("--sheet", a2)

    def test_ui_only_fields_never_reach_argv(self):
        # §1.4 #12: keys that mean something only to the UI (id, name, no_count, absent_means) must
        # not become argv entries. The builders take explicit params, so leakage is structurally
        # impossible — assert it, so a future refactor to a dict-spread can't reintroduce it.
        # distinctive sentinels so a match can only be a real leak, never a substring of a real flag
        leaky = {"no_count": True, "absent_means": "zero"}
        leaky_values = ["ext:scaling-1", "my run name", "UI_ONLY_SENTINEL"]
        argvs = [
            ui.build_report_argv("inv.csv", "out.csv"),
            ui.build_sample_argv("inv.csv", "s.csv", 0.05, 12345),
            ui.build_benchmark_argv("inv.csv", "gold.xlsx", id_col="File"),
        ]
        for argv in argvs:
            for k in leaky:                       # exact-token membership, not substring
                self.assertNotIn(k, argv, f"UI-only key {k!r} leaked into argv: {argv}")
            for v in leaky_values:
                self.assertNotIn(v, argv, f"UI-only value {v!r} leaked into argv: {argv}")

    def test_enqueue_argv_basic_and_rescan(self):
        a = ui.build_enqueue_argv("/mnt/in/CNG/files", "CNG-2026")
        self.assertEqual(a[-3:], ["/mnt/in/CNG/files", "--job-id", "CNG-2026"])
        self.assertIn("--job-id", a)
        self.assertIn("CNG-2026", a)
        self.assertNotIn("--inventory", a)
        a2 = ui.build_enqueue_argv("/mnt/in/CNG/files", "CNG-2026",
                                   inventory="inv.csv", exclude_lanes=["likely_non_responsive"])
        self.assertIn("--inventory", a2)
        self.assertIn("inv.csv", a2)
        self.assertIn("--exclude-lanes", a2)
        self.assertIn("likely_non_responsive", a2)

    def test_enqueue_argv_no_ui_only_fields(self):
        # the submit form posts job_dir/mode/name/no_count/run_id — none may reach the enqueue argv
        a = ui.build_enqueue_argv("/mnt/in/CNG/files", "CNG-2026",
                                  inventory="inv.csv", exclude_lanes=["x"])
        for leak in ("mode", "name", "no_count", "job_dir", "run_id"):
            self.assertNotIn(leak, a)

    def test_enqueue_argv_bde_threshold(self):
        a = ui.build_enqueue_argv("/mnt/in/CNG/files", "CNG-1", bde_threshold=7)
        self.assertIn("--bde-threshold", a)
        self.assertIn("7", a)
        self.assertNotIn("--bde-threshold", ui.build_enqueue_argv("/mnt/in/CNG/files", "CNG-1"))

    def test_benchmark_argv_bde_threshold(self):
        a = ui.build_benchmark_argv("inv.csv", "g.xlsx", bde_threshold=51)
        self.assertIn("--bde-threshold", a)
        self.assertIn("51", a)
        self.assertNotIn("--bde-threshold", ui.build_benchmark_argv("inv.csv", "g.xlsx"))

    def test_score_argv(self):
        a = ui.build_score_argv("inv.csv", "ent.csv", "/out", id_col="Control ID",
                                count_col="Total Entities", bde_threshold=51, absent_means="zero")
        self.assertTrue(a[1].endswith(os.path.join("tools", "score_combined.py")))
        self.assertIn("--entities", a)
        self.assertIn("ent.csv", a)
        self.assertIn("--out-dir", a)
        self.assertIn("Control ID", a)
        self.assertIn("Total Entities", a)
        # score_combined uses '> threshold', so an N+ threshold (51) is passed as N-1 (50)
        self.assertEqual(a[a.index("--bde-threshold") + 1], "50")
        self.assertIn("--absent-means", a)
        self.assertIn("zero", a)
        # auto is the default -> no flag emitted
        self.assertNotIn("--absent-means", ui.build_score_argv("i", "e", "/o", absent_means="auto"))


class JobDirValidation(unittest.TestCase):
    def test_accepts_files_directory_path_and_normalizes_job_dir(self):
        old_mount = os.environ.get("INPUT_MOUNT")
        tmp = tempfile.mkdtemp(prefix="ui-path-")
        input_root = os.path.join(tmp, "mount")
        job_dir = os.path.join(input_root, "CNG_matter")
        files_dir = os.path.join(job_dir, "files")
        os.makedirs(files_dir)
        try:
            os.environ["INPUT_MOUNT"] = input_root
            v = ui.validate_job_dir(files_dir)
            self.assertTrue(v["ok"], v)
            self.assertEqual(v["job_dir"], job_dir)
            self.assertEqual(v["files_dir"], files_dir)
        finally:
            if old_mount is None:
                os.environ.pop("INPUT_MOUNT", None)
            else:
                os.environ["INPUT_MOUNT"] = old_mount
            shutil.rmtree(tmp, ignore_errors=True)


class CompareInvariant(unittest.TestCase):
    def _row(self, rel, lane, llm=False, s2=False, extra=None):
        r = {"rel_path": rel, "suggested_lane": lane, "llm_consulted": str(llm),
             "s2_llm_consulted": str(s2), "entities_found": "Name", "is_bde": "False"}
        if extra:
            r.update(extra)
        return r

    def test_clean_when_rules_rows_identical(self):
        a = [self._row("a.txt", "standard"), self._row("b.txt", "bde")]
        b = [self._row("a.txt", "standard"), self._row("b.txt", "bde")]
        res = ui.compare_rules_decided(a, b)
        self.assertEqual(res["verdict"], "clean")
        self.assertEqual(res["moved_count"], 0)
        self.assertEqual(res["both_rules_decided"], 2)

    def test_flags_moved_rules_decided_row(self):
        a = [self._row("a.txt", "standard")]
        b = [self._row("a.txt", "bde")]                 # deterministic lane changed on a rules row
        res = ui.compare_rules_decided(a, b)
        self.assertEqual(res["verdict"], "rules-decided row moved")
        self.assertEqual(res["moved_count"], 1)
        self.assertIn("suggested_lane", res["moved"][0]["columns"])

    def test_input_only_diff_is_not_a_regression(self):
        # size_bytes differs (a .DOC re-saved by the Windows leg) but the detector decisions match:
        # informational, NOT a rules-decided regression. This is the real scaling-1 vs scaling-2 case.
        a = [self._row("a.doc", "standard", extra={"size_bytes": "100"})]
        b = [self._row("a.doc", "standard", extra={"size_bytes": "200"})]
        res = ui.compare_rules_decided(a, b)
        self.assertEqual(res["verdict"], "clean")
        self.assertEqual(res["moved_count"], 0)
        self.assertEqual(res["input_changed_count"], 1)

    def test_model_decided_drift_is_allowed(self):
        # Same file, model-decided in both, lane differs -> NOT a finding (model rows may differ).
        a = [self._row("a.txt", "standard", llm=True)]
        b = [self._row("a.txt", "bde", llm=True)]
        res = ui.compare_rules_decided(a, b)
        self.assertEqual(res["verdict"], "clean")
        self.assertEqual(res["both_rules_decided"], 0)

    def test_only_in_a_and_b_counted(self):
        a = [self._row("a.txt", "standard"), self._row("only_a.txt", "standard")]
        b = [self._row("a.txt", "standard"), self._row("only_b.txt", "standard")]
        res = ui.compare_rules_decided(a, b)
        self.assertEqual(res["only_in_a"], 1)
        self.assertEqual(res["only_in_b"], 1)
        self.assertEqual(res["common"], 1)


class SummaryNotMeasured(unittest.TestCase):
    def test_store_only_metrics_are_none(self):
        rows = [{"suggested_lane": "standard", "llm_tokens_total": "100", "di_calls": "0",
                 "ocr_pages": "0", "img_ocr_calls": "0", "llm_consulted": "True",
                 "s2_llm_consulted": "False"}]
        s = ui.summarize_inventory(rows)
        # in/out split, cached: inventory can't carry them -> None (rendered "not measured")
        self.assertIsNone(s["tokens"]["input"])
        self.assertIsNone(s["tokens"]["output"])
        self.assertIsNone(s["tokens"]["cached"])
        # but a real recorded zero stays a zero, not "not measured"
        self.assertEqual(s["ocr"]["di_calls"], 0)
        self.assertEqual(s["tokens"]["total"], 100)


class RealInventory(unittest.TestCase):
    @unittest.skipUnless(os.path.isfile(SCALING_1), "historical outputs/scaling-1 not present")
    def test_reads_a_real_inventory(self):
        rows, fields, trunc = ui.read_inventory(SCALING_1)
        self.assertEqual(len(rows), 2000)
        self.assertIn("suggested_lane", fields)
        s = ui.summarize_inventory(rows)
        self.assertEqual(s["files"], 2000)
        self.assertGreater(sum(s["lanes"].values()), 0)
        # decision counts partition the corpus
        self.assertEqual(s["decision"]["rules_decided"] + s["decision"]["model_decided"], 2000)


class ConcurrencySweep(unittest.TestCase):
    def _peak(self, intervals):
        return max((c for _, c in store.concurrency_series(intervals)), default=0)

    def test_back_to_back_is_sequential_not_concurrent(self):
        # §7 caveat: when end_i == start_{i+1}, close must be applied BEFORE the next open, so
        # adjacent tasks read as 1 concurrent, not 2. This is the whole reason the rule exists.
        self.assertEqual(self._peak([(0, 1), (1, 2), (2, 3)]), 1)

    def test_six_submillisecond_calls_do_not_inflate(self):
        # six consecutive calls recorded back-to-back must NOT read as six concurrent.
        self.assertEqual(self._peak([(i, i + 1) for i in range(6)]), 1)

    def test_true_overlap_is_counted(self):
        self.assertEqual(self._peak([(0, 3), (1, 4), (2, 5)]), 3)

    def test_zero_length_intervals_contribute_nothing(self):
        self.assertEqual(store.concurrency_series([(5, 5), (5, 5)]), [])


class LegacyPairCollapse(unittest.TestCase):
    """A Windows-leg .doc/.xls/.ppt file produces TWO Table rows (findings §6); the KPI fix is
    that per-state counts must reflect real input files, not raw rows."""

    def test_completed_pair_collapses_to_one(self):
        ents = [{"file_name": "report.doc", "status": "completed"},
                {"file_name": "report.docx", "status": "processing"}]
        out = store._collapse_legacy_pairs(ents)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["file_name"], "report.docx")

    def test_unconverted_legacy_row_is_kept(self):
        # conversion hasn't forwarded yet -- no counterpart exists, so this row IS the file's state
        ents = [{"file_name": "report.doc", "status": "pending"}]
        out = store._collapse_legacy_pairs(ents)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["file_name"], "report.doc")

    def test_non_legacy_files_untouched(self):
        ents = [{"file_name": "memo.pdf", "status": "completed"},
                {"file_name": "notes.txt", "status": "pending"}]
        self.assertEqual(store._collapse_legacy_pairs(ents), ents)

    def test_mixed_corpus_total_matches_real_file_count(self):
        # 1 legacy file (2 rows) + 2 ordinary files (1 row each) = 3 real files
        ents = [
            {"file_name": "a.xls", "status": "completed"},
            {"file_name": "a.xlsx", "status": "completed"},
            {"file_name": "b.pdf", "status": "completed"},
            {"file_name": "c.pptx", "status": "pending"},
        ]
        self.assertEqual(len(store._collapse_legacy_pairs(ents)), 3)


class LiveProcessing(unittest.TestCase):
    """The 'worker 1 processing file 1, retry 3, 40s' panel."""

    def test_only_processing_rows_included(self):
        now = datetime.now(timezone.utc)
        ents = [
            {"file_name": "a.pdf", "status": "processing", "worker_instance": "w1",
             "attempt_count": 3, "started_at": now - timedelta(seconds=40)},
            {"file_name": "b.pdf", "status": "completed", "worker_instance": "w1",
             "attempt_count": 1, "started_at": now},
        ]
        out = store._live_processing(ents)
        self.assertEqual(len(out), 1)
        row = out[0]
        self.assertEqual(row["file_name"], "a.pdf")
        self.assertEqual(row["worker_instance"], "w1")
        self.assertEqual(row["attempt_count"], 3)
        self.assertGreaterEqual(row["elapsed_s"], 39)

    def test_sorted_longest_running_first(self):
        now = datetime.now(timezone.utc)
        ents = [
            {"file_name": "short.pdf", "status": "processing", "started_at": now - timedelta(seconds=5)},
            {"file_name": "long.pdf", "status": "processing", "started_at": now - timedelta(seconds=500)},
        ]
        out = store._live_processing(ents)
        self.assertEqual([r["file_name"] for r in out], ["long.pdf", "short.pdf"])


class JobMetricsCollapsesLegacyPairs(unittest.TestCase):
    """End-to-end through job_metrics() with a monkeypatched _entities_for -- no Azure needed
    (same monkeypatch style as ArchiveGuardsReset). Proves the KPI fix and the new live-processing
    panel both reach the Monitor payload."""

    def setUp(self):
        self._orig = store._entities_for

    def tearDown(self):
        store._entities_for = self._orig

    def test_total_and_completed_match_real_file_count(self):
        now = datetime.now(timezone.utc)
        ents = [
            {"PartitionKey": "J", "RowKey": "r1", "file_name": "report.doc", "status": "completed",
             "attempt_count": 1, "started_at": now, "completed_at": now, "enqueued_at": now},
            {"PartitionKey": "J", "RowKey": "r2", "file_name": "report.docx", "status": "completed",
             "attempt_count": 1, "started_at": now, "completed_at": now, "enqueued_at": now},
            {"PartitionKey": "J", "RowKey": "r3", "file_name": "a.pdf", "status": "completed",
             "attempt_count": 1, "started_at": now, "completed_at": now, "enqueued_at": now},
            {"PartitionKey": "J", "RowKey": "r4", "file_name": "b.pdf", "status": "pending",
             "attempt_count": 1, "started_at": now, "enqueued_at": now},
        ]
        store._entities_for = lambda job_id: ents
        m = store.job_metrics("J")
        self.assertEqual(m["total"], 3)          # not 4 -- the legacy pair collapses to 1 real file
        self.assertEqual(m["files_completed"], 2)
        self.assertEqual(m["files_pending"], 1)

    def test_processing_tasks_surface_worker_attempt_elapsed(self):
        now = datetime.now(timezone.utc)
        ents = [
            {"PartitionKey": "J", "RowKey": "r1", "file_name": "a.pdf", "status": "processing",
             "worker_instance": "worker-1", "attempt_count": 3,
             "started_at": now - timedelta(seconds=40), "enqueued_at": now},
        ]
        store._entities_for = lambda job_id: ents
        m = store.job_metrics("J")
        self.assertEqual(len(m["processing_tasks"]), 1)
        row = m["processing_tasks"][0]
        self.assertEqual(row["file_name"], "a.pdf")
        self.assertEqual(row["worker_instance"], "worker-1")
        self.assertEqual(row["attempt_count"], 3)
        self.assertGreaterEqual(row["elapsed_s"], 39)


class EnvReport(unittest.TestCase):
    def test_storage_endpoint_ok_logic(self):
        old = dict(os.environ)
        try:
            for k in ("AZURE_STORAGE_CONNECTION_STRING", "AZURE_STORAGE_TABLE_URL",
                      "AZURE_STORAGE_QUEUE_URL"):
                os.environ.pop(k, None)
            self.assertFalse(store.env_report()["storage_endpoint_ok"])
            os.environ["AZURE_STORAGE_CONNECTION_STRING"] = "x"
            self.assertTrue(store.env_report()["storage_endpoint_ok"])
            del os.environ["AZURE_STORAGE_CONNECTION_STRING"]
            os.environ["AZURE_STORAGE_TABLE_URL"] = "t"       # one URL alone is not enough
            self.assertFalse(store.env_report()["storage_endpoint_ok"])
            os.environ["AZURE_STORAGE_QUEUE_URL"] = "q"       # both URLs -> ok
            self.assertTrue(store.env_report()["storage_endpoint_ok"])
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_worker_side_vars_not_in_ops_required(self):
        # A worker-side var flagged missing on the ops VM is a false alarm (findings §7).
        req = {x["key"] for x in store.env_report()["ops_required"]}
        worker = {x["key"] for x in store.env_report()["worker_side"]}
        self.assertIn("USE_LLM", worker)
        self.assertIn("AZURE_OPENAI_ENDPOINT", worker)
        self.assertNotIn("USE_LLM", req)
        self.assertNotIn("AZURE_OPENAI_ENDPOINT", req)


class RescanMirror(unittest.TestCase):
    @unittest.skipUnless(os.path.isfile(SCALING_1), "historical outputs/scaling-1 not present")
    def test_rescan_count_matches_load_filter_set(self):
        # notes #11: the preview MUST equal what enqueue.py enqueues. Prove our in-process mirror
        # equals pii_triage.runner.load_filter_set on a real inventory, for a couple lane sets.
        if ui.PII_PKG not in sys.path:
            sys.path.insert(0, ui.PII_PKG)
        try:
            from pii_triage.runner import load_filter_set
        except Exception as exc:
            self.skipTest(f"pii_triage not importable here: {exc}")
        for excl in ({"likely_non_responsive"}, {"likely_non_responsive", "standard"}, set()):
            real = len(load_filter_set(SCALING_1, excl))
            mine = ui.rescan_keep_count(SCALING_1, excl)
            self.assertEqual(mine, real, f"mirror disagreed for exclude={excl}")


class ArchiveGuardsReset(unittest.TestCase):
    """§6.8 / §8: reset must ARCHIVE-and-verify first, and must REFUSE to clear the table if the
    archive did not write. Injected failure — no Azure needed (the store is monkeypatched)."""

    def setUp(self):
        self.rid = "__test_archive_guard__"
        self.d = ui._run_dir(self.rid)
        inv = os.path.join(self.d, "inventory.csv")
        with open(inv, "w", encoding="utf-8") as fh:
            fh.write("rel_path\nx\n")
        ui.jdump(ui.meta_path(self.rid), {"id": self.rid, "job_id": "J", "inventory": inv})
        self._orig = (store.archive_job, store.list_jobs, store.run_reset)
        self.reset_called = {"v": False}
        store.list_jobs = lambda: [{"job_id": "J", "total": 1}]
        store.run_reset = lambda: (self.reset_called.__setitem__("v", True) or {"deleted": 1})

    def tearDown(self):
        store.archive_job, store.list_jobs, store.run_reset = self._orig
        shutil.rmtree(self.d, ignore_errors=True)

    def test_refuses_reset_when_archive_unverified(self):
        store.archive_job = lambda job_id, dest: {"job_id": job_id, "count": 1, "verified": False,
                                                  "detail": "disk full (injected)",
                                                  "rows_path": dest, "metrics_path": dest}
        r = ui.archive_and_reset(self.rid, override=True, typed="J")
        self.assertFalse(r["ok"])
        self.assertIn("did not verify", r["why"])
        self.assertFalse(self.reset_called["v"], "reset MUST NOT run when the archive did not verify")

    def test_proceeds_when_archive_verified(self):
        store.archive_job = lambda job_id, dest: {"job_id": job_id, "count": 1, "verified": True,
                                                  "detail": "ok", "rows_path": dest, "metrics_path": dest}
        r = ui.archive_and_reset(self.rid, override=True, typed="J")
        self.assertTrue(r["ok"])
        self.assertTrue(self.reset_called["v"])

    def test_stops_any_live_watcher_before_archiving(self):
        # Nothing should still be appending to this run's inventory once it's about to be cleared.
        store.archive_job = lambda job_id, dest: {"job_id": job_id, "count": 1, "verified": True,
                                                  "detail": "ok", "rows_path": dest, "metrics_path": dest}
        orig_stop = ui.stop_watch
        calls = []
        ui.stop_watch = lambda rid: calls.append(rid)
        try:
            ui.archive_and_reset(self.rid, override=True, typed="J")
        finally:
            ui.stop_watch = orig_stop
        self.assertEqual(calls, [self.rid])


class InputMountCheck(unittest.TestCase):
    def _under(self, mount, path):
        old = os.environ.get("INPUT_MOUNT")
        os.environ["INPUT_MOUNT"] = mount
        try:
            return ui._under_input_mount(path)
        finally:
            if old is None:
                os.environ.pop("INPUT_MOUNT", None)
            else:
                os.environ["INPUT_MOUNT"] = old

    def test_subpath_and_equal(self):
        self.assertTrue(self._under("/mnt/input", "/mnt/input/CNG-10"))
        self.assertTrue(self._under("/mnt/input", "/mnt/input"))

    def test_root_mount_regression(self):
        # The reported bug: a ROOT mount. POSIX '/' is the analogue of Windows 'I:\', whose abspath
        # keeps a trailing separator; the old string-prefix check produced '//' and wrongly rejected
        # a real child. commonpath handles it.
        self.assertTrue(self._under("/", "/tmp/CNG-10"))

    def test_sibling_prefix_is_not_under(self):
        self.assertFalse(self._under("/mnt/data", "/mnt/database"))   # shares a prefix but not a child
        self.assertFalse(self._under("/mnt/input", "/somewhere/else"))


class BuildPreflight(unittest.TestCase):
    # Build/deploy execution was removed from the UI (done from the CLI); only the readiness
    # preflight remains. Its structure is still worth pinning.
    def test_docker_is_not_a_gate(self):
        # §8.1 correction: acr-build runs in ACR's cloud; Docker must never block the build.
        pf = ui.build_preflight()
        docker = next(r for r in pf["checks"] if r["key"] == "Docker")
        self.assertEqual(docker["state"], "ok")
        self.assertIn("not required", docker["val"])

    def test_preflight_reports_targets_and_readiness_flags(self):
        pf = ui.build_preflight()
        for key in ("checks", "targets", "can_build", "can_deploy", "sha"):
            self.assertIn(key, pf)


class BenchmarkAbsentMeans(unittest.TestCase):
    """The new 'assume zero entities on files not in the review' scoring option, end to end."""

    def _score(self, absent):
        import tempfile
        if ui.PII_PKG not in sys.path:
            sys.path.insert(0, ui.PII_PKG)
        try:
            from pii_triage.benchmark import run_benchmark
        except Exception as exc:
            self.skipTest(f"pii_triage not importable here: {exc}")
        d = tempfile.mkdtemp()
        inv, gold = os.path.join(d, "inv.csv"), os.path.join(d, "gold.csv")
        with open(inv, "w", encoding="utf-8") as fh:
            fh.write("rel_path,file_name,suggested_lane,is_bde,entities_found,"
                     "llm_consulted,llm_responsive,is_structured\n")
            fh.write("CNG/a.txt,a.txt,standard,False,Name,False,,False\n")               # resp, in gold
            fh.write("CNG/b.txt,b.txt,likely_non_responsive,False,,False,,False\n")        # NR, in gold
            fh.write("CNG/c.txt,c.txt,standard,False,Email,True,yes,False\n")              # resp, NOT in gold
            fh.write("CNG/d.txt,d.txt,likely_non_responsive,False,,False,,False\n")        # NR, NOT in gold
        with open(gold, "w", encoding="utf-8") as fh:
            fh.write("file,responsive\na.txt,Responsive\nb.txt,Non-Responsive\n")
        import io as _io
        import contextlib
        with contextlib.redirect_stderr(_io.StringIO()):
            r = run_benchmark(inv, gold, absent_means=absent)
        shutil.rmtree(d, ignore_errors=True)
        return r

    def test_unreviewed_skips_absent_files(self):
        r = self._score("unreviewed")
        self.assertEqual((r["tp"], r["fp"], r["fn"], r["tn"]), (1, 0, 0, 1))

    def test_zero_scores_absent_files_as_nonresponsive(self):
        r = self._score("zero")
        # c.txt (tool flagged responsive, not reviewed) -> FP; d.txt (cleared) -> TN
        self.assertEqual((r["tp"], r["fp"], r["fn"], r["tn"]), (1, 1, 0, 2))
        self.assertEqual(r["assumed_nr"], 2)


class BenchmarkBdeThreshold(unittest.TestCase):
    """Re-scoring the BDE flag at a chosen threshold from the recorded entity counts."""

    def _score(self, bde_threshold):
        import tempfile
        import io as _io
        import contextlib
        if ui.PII_PKG not in sys.path:
            sys.path.insert(0, ui.PII_PKG)
        try:
            from pii_triage.benchmark import run_benchmark
        except Exception as exc:
            self.skipTest(f"pii_triage not importable here: {exc}")
        d = tempfile.mkdtemp()
        inv, gold = os.path.join(d, "inv.csv"), os.path.join(d, "gold.csv")
        with open(inv, "w", encoding="utf-8") as fh:
            fh.write("rel_path,file_name,suggested_lane,is_bde,estimated_entities,"
                     "entities_found,llm_consulted,llm_responsive,is_structured\n")
            # scanned at 51 -> is_bde False; but the file really has 10 entities
            fh.write("CNG/a.txt,a.txt,standard,False,10,Name,False,,False\n")
        with open(gold, "w", encoding="utf-8") as fh:
            fh.write("file,responsive,bde\na.txt,Responsive,yes\n")
        with contextlib.redirect_stderr(_io.StringIO()):
            r = run_benchmark(inv, gold, bde_threshold=bde_threshold)
        shutil.rmtree(d, ignore_errors=True)
        return r

    def test_rescore_flips_bde_accuracy(self):
        # gold says BDE=yes. At 51: 10>=51 is False -> disagree (0.0). At 7: 10>=7 True -> agree (1.0).
        self.assertEqual(self._score(51)["bde_accuracy"], 0.0)
        self.assertEqual(self._score(7)["bde_accuracy"], 1.0)


class BenchmarkCountExport(unittest.TestCase):
    """Scoring against an entity-COUNT review sheet: responsive = count>0, BDE = count>=threshold."""

    def test_count_columns_score(self):
        import tempfile
        import io as _io
        import contextlib
        if ui.PII_PKG not in sys.path:
            sys.path.insert(0, ui.PII_PKG)
        try:
            from pii_triage.benchmark import run_benchmark
        except Exception as exc:
            self.skipTest(f"pii_triage not importable here: {exc}")
        d = tempfile.mkdtemp()
        inv, gold = os.path.join(d, "inv.csv"), os.path.join(d, "gold.csv")
        with open(inv, "w", encoding="utf-8") as fh:
            fh.write("rel_path,file_name,suggested_lane,is_bde,estimated_entities,"
                     "entities_found,llm_consulted,llm_responsive,is_structured\n")
            fh.write("CNG/a.txt,a.txt,standard,False,100,Name,False,,False\n")
            fh.write("CNG/b.txt,b.txt,likely_non_responsive,False,0,,False,,False\n")
            fh.write("CNG/c.txt,c.txt,standard,False,60,Name,False,,False\n")
            fh.write("CNG/d.txt,d.txt,standard,False,3,Name,False,,False\n")
        with open(gold, "w", encoding="utf-8") as fh:
            fh.write("Control ID,total entities\na.txt,100\nb.txt,0\nc.txt,60\nd.txt,3\n")
        with contextlib.redirect_stderr(_io.StringIO()):
            r = run_benchmark(inv, gold, id_col="Control ID", responsive_col="total entities",
                              bde_col="total entities", bde_threshold=51)
        shutil.rmtree(d, ignore_errors=True)
        # responsive: a,c,d have >0 entities and tool flagged them -> TP; b has 0 and tool cleared -> TN
        self.assertEqual((r["tp"], r["fp"], r["fn"], r["tn"]), (3, 0, 0, 1))
        # BDE @51: a(100)/c(60) BDE both sides, b(0)/d(3) not BDE both sides -> all agree
        self.assertEqual(r["bde_accuracy"], 1.0)


class ScoreSummaryParser(unittest.TestCase):
    PIPE = (
        "NR/R -- PIPELINE (sequential — what a reviewer receives)\n"
        "==========\n"
        "  scored 1,964    (undetermined, excluded: 36)\n"
        "  TP=361  FP=150  FN=110  TN=1,343\n"
        "  recall      0.7665    <- misses: truly responsive, tool cleared\n"
        "  precision   0.7065\n"
        "  accuracy    0.8676    F1 0.7352\n"
        "  NR accuracy 0.0757    of files CLEARED (target < 0.05)\n"
        "  R accuracy  0.2935    of files FLAGGED (target < 0.50)\n"
        "  flagged     0.2602    over-call 0.0764   under-call 0.2335\n"
        "==========\nNR/R -- UNION\n  recall 0.9999\n")

    def test_parses_pipeline_block(self):
        s = ui.parse_score_summary(self.PIPE)
        self.assertEqual(s["scored"], "1964")
        self.assertEqual((s["tp"], s["fp"], s["fn"], s["tn"]), ("361", "150", "110", "1343"))
        self.assertEqual(s["recall"], "0.7665")     # PIPELINE's, not the UNION's 0.9999
        self.assertEqual(s["precision"], "0.7065")
        self.assertEqual(s["accuracy"], "0.8676")
        self.assertEqual(s["f1"], "0.7352")
        self.assertEqual(s["nr_accuracy"], "0.0757")
        self.assertEqual(s["r_accuracy"], "0.2935")
        self.assertEqual(s["flagged"], "0.2602")
        self.assertEqual(s["over_call"], "0.0764")
        self.assertEqual(s["under_call"], "0.2335")

    def test_missing_block_is_empty(self):
        self.assertEqual(ui.parse_score_summary("nothing here"), {})
        self.assertEqual(ui.parse_score_summary(""), {})


class LiveCollectorWatcher(unittest.TestCase):
    """start_watch/stop_watch/watch_status -- the background --watch process the UI now spawns
    right after submit, instead of only collecting once the whole batch drains. Uses trivial
    dummy subprocesses (not a real collect_outputs.py run) so this needs no Azure access."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.inv = os.path.join(self.d, "inventory.csv")
        self._orig_watchers = dict(ui.WATCHERS)
        ui.WATCHERS.clear()

    def tearDown(self):
        for entry in list(ui.WATCHERS.values()):
            try:
                entry["proc"].terminate()
                entry["proc"].wait(timeout=2)
            except Exception:
                pass
        ui.WATCHERS.clear()
        ui.WATCHERS.update(self._orig_watchers)
        shutil.rmtree(self.d, ignore_errors=True)

    def test_watch_status_when_nothing_was_ever_started(self):
        st = ui.watch_status("nope", self.inv)
        self.assertFalse(st["watching"])
        self.assertEqual(st["rows"], 0)
        self.assertFalse(st["exists"])

    def test_watch_status_reflects_a_live_process(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        ui.WATCHERS["R1"] = {"proc": proc, "log": "x", "out": self.inv, "started_at": ui.now_iso()}
        try:
            st = ui.watch_status("R1", self.inv)
            self.assertTrue(st["watching"])
            self.assertEqual(st["pid"], proc.pid)
        finally:
            proc.terminate(); proc.wait(timeout=2)

    def test_start_watch_reports_already_running_instead_of_duplicating(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        ui.WATCHERS["R2"] = {"proc": proc, "log": "x", "out": self.inv, "started_at": ui.now_iso()}
        try:
            res = ui.start_watch("R2", self.inv)
            self.assertTrue(res["already_running"])
            self.assertEqual(res["pid"], proc.pid)
        finally:
            proc.terminate(); proc.wait(timeout=2)

    def test_stop_watch_terminates_and_removes_from_registry(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        ui.WATCHERS["R3"] = {"proc": proc, "log": "x", "out": self.inv, "started_at": ui.now_iso()}
        ui.stop_watch("R3")
        self.assertNotIn("R3", ui.WATCHERS)
        self.assertIsNotNone(proc.poll())   # actually terminated, not just forgotten

    def test_stop_watch_on_an_unknown_run_is_a_safe_no_op(self):
        ui.stop_watch("no-such-run")   # must not raise

    def test_watch_status_reports_exit_code_once_the_process_ends(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        ui.WATCHERS["R4"] = {"proc": proc, "log": "x", "out": self.inv, "started_at": ui.now_iso()}
        st = ui.watch_status("R4", self.inv)
        self.assertFalse(st["watching"])
        self.assertEqual(st["exit_code"], 0)

    def test_watch_status_rows_reads_from_disk_not_memory(self):
        with open(self.inv, "w", encoding="utf-8", newline="") as fh:
            fh.write("rel_path\na.pdf\nb.pdf\n")
        st = ui.watch_status("nothing-tracked", self.inv)
        self.assertEqual(st["rows"], 2)


class SubmitRunStartsLiveCollector(unittest.TestCase):
    """submit_run() must start the live watcher -- the actual fix for 'no output until the whole
    batch completes'. enqueue.py/az calls are stubbed; nothing here touches Azure."""

    def setUp(self):
        self.mount = tempfile.mkdtemp()
        self.job_dir = os.path.join(self.mount, "job1")
        os.makedirs(os.path.join(self.job_dir, "files"))
        with open(os.path.join(self.job_dir, "files", "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("x")
        self._orig_input_mount = os.environ.get("INPUT_MOUNT")
        os.environ["INPUT_MOUNT"] = self.mount
        self._orig_run_tool = ui.run_tool
        self._orig_active_run = ui.active_run
        self._orig_start_watch = ui.start_watch
        ui.run_tool = lambda argv, timeout=None: {"ok": True, "exit": 0, "out": ""}
        ui.active_run = lambda: None
        self.watch_calls = []
        ui.start_watch = lambda rid, inv: (self.watch_calls.append((rid, inv)),
                                           {"ok": True, "started": True})[1]
        ui.COUNT_CACHE.clear()
        self.run_id = None

    def tearDown(self):
        if self._orig_input_mount is None:
            os.environ.pop("INPUT_MOUNT", None)
        else:
            os.environ["INPUT_MOUNT"] = self._orig_input_mount
        ui.run_tool = self._orig_run_tool
        ui.active_run = self._orig_active_run
        ui.start_watch = self._orig_start_watch
        shutil.rmtree(self.mount, ignore_errors=True)
        if self.run_id:
            shutil.rmtree(ui._run_dir(self.run_id), ignore_errors=True)
        try:
            os.remove(ui.LOCK_PATH)
        except OSError:
            pass

    def test_submit_run_starts_a_watcher_for_the_new_run(self):
        res = ui.submit_run(self.job_dir)
        self.run_id = res.get("id")
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(self.watch_calls), 1)
        rid, inv = self.watch_calls[0]
        self.assertEqual(rid, res["id"])
        self.assertTrue(inv.endswith("inventory.csv"))


class LoopbackOnly(unittest.TestCase):
    def test_server_binds_loopback(self):
        srv = ui.Server(("127.0.0.1", 0), ui.H)
        try:
            self.assertEqual(srv.server_address[0], "127.0.0.1")
        finally:
            srv.server_close()

    def test_source_never_binds_all_interfaces(self):
        with open(os.path.join(HERE, "hwe_scaled_ui.py"), "r", encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("0.0.0.0", src)


if __name__ == "__main__":
    unittest.main()
