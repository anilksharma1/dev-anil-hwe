#!/usr/bin/env python3
"""HWE Runner — Scaled. Operator UI for the distributed (queue + Container Apps) pii_triage
pipeline.

WHAT THIS IS
    The counterpart to the local HWE Runner, for the scaled build. Same product, same rules:
    every screen is a view over artefacts the CLI/store already writes; the UI builds a command,
    shows it before it runs, and reads results back. It never computes a number the pipeline does
    not compute, and it never holds authoritative state.

    Start it:   python hwe_scaled_ui.py          (or double-click HWE_Scaled.cmd on Windows)
    It binds 127.0.0.1 on a free port, prints the URL, opens a browser. Loopback only.

RULES IT IS BUILT AROUND  (carried over verbatim from the local build)
    1. No PII in the UI, ever. It renders inventory-CSV columns and store/metric fields only —
       labels and counts, never values. `entities_found` is labels ("Name | SSN | Address").
       There is deliberately no document preview anywhere.
    2. Read-only on the corpus. The UI never writes into the documents folder. (The only writer of
       corpus-adjacent state is the opt-in job-directory staging in a later phase, which copies.)
    3. Distinguish "not measured" from zero, everywhere. A metric the pipeline did not record is
       rendered "not measured" / "—", never a 0 we did not read from a real record. See measured().
    4. Status is measured, never assumed. Progress comes from the Azure Table and the queues, never
       from the fact that the UI sent a request or from log text.

THIS FILE — PHASE 2 (read-only over existing artefacts)
    Runs list, run detail, Results, vs-manual-review, Compare. No submit, no cancel, no reset, no
    build/deploy — those are the control plane and come in later phases. Everything here works
    against inventories that already exist on disk (runs/<id>/ and the historical outputs/*/), and
    needs no Azure connection at all, so it is most of the value at zero risk.

    Verified facts this screen set relies on (see SCALED_UI_FINDINGS.md):
      - the inventory schema is pii_triage.routing.FIELDNAMES (49 columns, no PII);
      - a row is *model-decided* iff llm_consulted or s2_llm_consulted is true; everything else is
        rules-decided and must be byte-identical between two runs (Compare invariant, §6.7);
      - DI billing lives in the inventory (ocr_pages, di_calls, img_ocr_calls), NOT the store;
      - the in/out token split, cached tokens, rate-limit wait, replica series and two-pass token
        attribution live in the Azure Table (or nowhere) — for an inventory-only view they are
        "not measured".
"""
from __future__ import annotations

import csv
import http.server
import io
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
import zipfile
from collections import Counter
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(ROOT, "runs")                       # UI-owned run workspace
OUTPUTS = os.path.join(ROOT, "outputs")                 # historical CLI runs (external)
PII_PKG = os.path.join(ROOT, "pii_triage_merged")       # so `python -m pii_triage` resolves
UI_HTML = os.path.join(ROOT, "hwe_scaled_ui.html")
IS_WIN = os.name == "nt"

# Raise the CSV field-size ceiling: entities_found / detail can exceed the 128 KB default on a
# wide roster, and the C reader raises rather than truncating. This is labels, never values.
csv.field_size_limit(16 * 1024 * 1024)

# Display-only prices, pre-filled at the right magnitude. The pipeline's flags are per THOUSAND
# tokens; the vendor quotes per MILLION ($0.30/M -> 0.0003 here) — a 1000x trap met once, not twice.
DEFAULTS = {
    "price_per_1k_pages": 9.50,
    "price_per_1k_in": 0.0003,
    "price_per_1k_out": 0.0003,
}

# Columns whose value is produced by the model (or is per-run timing), so they are ALLOWED to differ
# between two runs. Everything else in a rules-decided row must match byte-for-byte (§6.7).
_MODEL_OR_TIMING_COLS = {
    "llm_consulted", "llm_responsive", "llm_tokens",
    "s2_ran", "s2_skip_reason", "s2_llm_consulted", "s2_llm_responsiveness",
    "s2_llm_responsive", "s2_llm_tokens", "s2_is_bde", "s2_lane", "s2_nr", "s2_detail",
    "elapsed_s", "llm_tokens_total",
    # OCR is a network call whose page yield can vary; treat its accounting as non-deterministic.
    "ocr_attempted", "ocr", "ocr_pages", "img_ocr_qualifying", "img_ocr_calls",
    "img_ocr_ok", "img_decode_failed", "di_calls",
}

# The deterministic DETECTOR decisions — the columns the rules pass actually decides. On a
# rules-decided row these MUST be identical between runs (the real §6.7 invariant). A difference
# here is a bug or a rulepack/detector-build change. Separated from input/environment columns
# (size_bytes, status, text_extractable, …) because a .DOC re-saved by the Windows conversion leg
# legitimately changes size_bytes without the detectors having behaved differently — that is an
# input difference to note, not a detector regression to alarm on.
_DECISION_COLS = {
    "suggested_lane", "is_bde", "bde_stage1", "nr_stage1", "entities_found", "value_signal",
    "pi_categories", "entity_bucket", "estimated_entities", "estimate_truncated", "ambiguity",
    "complexity_bucket", "bde_person_count", "bde_confirmed",
}


# ------------------------------------------------------------------ small helpers
def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def whoami() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


def jdump(path: str, obj) -> None:
    """Atomic write, so a crash mid-write never leaves an unreadable json."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1)
    os.replace(tmp, path)


def jload(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def measured(value):
    """The one wording rule that matters most: keep 'not measured' distinct from a real zero.

    Returns the value unchanged when it is a real, recorded number (including 0), and None when the
    pipeline never recorded it. The front end renders None as 'not measured', a real 0 as '0'. Pass
    a genuine count straight through; pass None (never 0) when the metric was not captured.
    """
    if value is None:
        return None
    if isinstance(value, bool):        # bool is an int subclass; a flag is not a measurement
        return value
    if isinstance(value, (int, float)):
        return value
    return value


def as_bool(v) -> bool:
    """Parse a CSV cell to bool. CSV gives strings; empty / 'false' / '0' are False."""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes")


def as_int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def as_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ inventory reading
def read_inventory(path: str, cap: int = 2_000_000):
    """Read an inventory CSV into a list of dict rows. Capped so a wrong path cannot exhaust memory.

    Returns (rows, fieldnames, truncated). Never raises on a malformed row — the inventory is the
    progress record and a half-written trailing line must not blank the whole screen.
    """
    rows, truncated = [], False
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
            reader = csv.DictReader(fh)
            fieldnames = reader.fieldnames or []
            for row in reader:
                rows.append(row)
                if len(rows) >= cap:
                    truncated = True
                    break
    except OSError:
        return [], [], False
    return rows, fieldnames, truncated


def _entity_label_counts(rows) -> list:
    """Count entity *types* across the corpus (labels only, never values). entities_found looks
    like 'Name | SSN | Address'."""
    c = Counter()
    for r in rows:
        raw = (r.get("entities_found") or "").strip()
        if not raw:
            continue
        for lab in raw.split("|"):
            lab = lab.strip()
            if lab:
                c[lab] += 1
    return c.most_common(20)


def summarize_inventory(rows, prices=None) -> dict:
    """Reduce an inventory to what the Results screen shows. Every number here is summed straight
    from inventory columns — the UI is never a second source of truth. Store-only metrics (token
    in/out split, cached tokens, rate-limit wait, replica series, two-pass token attribution) are
    reported as None -> 'not measured', because an inventory cannot carry them.
    """
    prices = prices or DEFAULTS
    n = len(rows)

    lane_counts = Counter((r.get("suggested_lane") or "").strip() or "(blank)" for r in rows)
    s2_lane_counts = Counter((r.get("s2_lane") or "").strip() for r in rows if (r.get("s2_lane") or "").strip())
    status_counts = Counter((r.get("status") or "").strip() or "(blank)" for r in rows)
    grade_counts = Counter((r.get("s2_llm_responsiveness") or "").strip()
                           for r in rows if (r.get("s2_llm_responsiveness") or "").strip())

    nr_stage1 = sum(1 for r in rows if as_bool(r.get("nr_stage1")))
    is_bde = sum(1 for r in rows if as_bool(r.get("is_bde")))
    s2_is_bde = sum(1 for r in rows if as_bool(r.get("s2_is_bde")))
    s2_nr = sum(1 for r in rows if as_bool(r.get("s2_nr")))
    llm_consulted = sum(1 for r in rows if as_bool(r.get("llm_consulted")))
    s2_consulted = sum(1 for r in rows if as_bool(r.get("s2_llm_consulted")))
    rules_decided = sum(1 for r in rows if not as_bool(r.get("llm_consulted"))
                        and not as_bool(r.get("s2_llm_consulted")))

    # DI billable accounting — the whole point of the cost panel: units, not call counts.
    ocr_pages = sum(as_int(r.get("ocr_pages")) for r in rows)
    img_calls = sum(as_int(r.get("img_ocr_calls")) for r in rows)
    di_calls = sum(as_int(r.get("di_calls")) for r in rows)
    ocr_attempted = sum(1 for r in rows if as_bool(r.get("ocr_attempted")))
    di_pages_billable = ocr_pages + img_calls        # each embedded image ~ 1 page

    # Tokens: the inventory records TOTALS only (in/out split is store-only). Keep them distinct.
    tok_total = sum(as_int(r.get("llm_tokens_total")) for r in rows)
    tok_stage1 = sum(as_int(r.get("llm_tokens")) for r in rows)
    tok_stage2 = sum(as_int(r.get("s2_llm_tokens")) for r in rows)
    if tok_total == 0 and (tok_stage1 or tok_stage2):
        tok_total = tok_stage1 + tok_stage2

    di_cost = round(di_pages_billable / 1000.0 * float(prices["price_per_1k_pages"]), 2)
    # in/out split is not in the inventory, so cost is a total-token estimate at the input rate.
    llm_cost = round(tok_total / 1000.0 * float(prices["price_per_1k_in"]), 2)

    return {
        "files": n,
        "lanes": dict(lane_counts),
        "s2_lanes": dict(s2_lane_counts),
        "status_counts": dict(status_counts),
        "grades": dict(grade_counts),
        "entity_labels": _entity_label_counts(rows),
        "decision": {
            "nr_stage1": nr_stage1, "responsive_stage1": n - nr_stage1,
            "is_bde": is_bde, "s2_is_bde": s2_is_bde, "s2_nr": s2_nr,
            "llm_consulted": llm_consulted, "s2_consulted": s2_consulted,
            "rules_decided": rules_decided, "model_decided": n - rules_decided,
        },
        "ocr": {
            "di_calls": di_calls, "ocr_attempted": ocr_attempted,
            "ocr_pages": ocr_pages, "img_ocr_calls": img_calls,
            "di_pages_billable": di_pages_billable,
        },
        "tokens": {
            "total": tok_total, "stage1": tok_stage1, "stage2": tok_stage2,
            # split / cached are not in the inventory — the Table has the split, nothing has cached.
            "input": measured(None), "output": measured(None), "cached": measured(None),
        },
        "cost": {"di_usd": di_cost, "llm_usd": llm_cost, "total_usd": round(di_cost + llm_cost, 2),
                 "note": "DI cost is billable pages x rate; LLM cost is total tokens x input rate "
                         "(the inventory does not carry the in/out split, so it is an estimate, "
                         "not a stated figure)."},
        # These require the Azure Table (or are unrecorded entirely). For an inventory-only view
        # they are honestly not measured — never rendered as a clean zero.
        "not_measured": {
            "token_split_in_out": True, "cached_tokens": True, "rate_limit_wait_s": True,
            "replica_series": True, "two_pass_documents": True, "retries": True,
        },
    }


# ------------------------------------------------------------------ run resolution
def _external_runs() -> list:
    """Reconstruct runs the UI did not start, from historical outputs/*/inventory.csv. Marked
    external and read-only. This is the scaled analogue of the local build handling orphan runs."""
    out = []
    if not os.path.isdir(OUTPUTS):
        return out
    for name in sorted(os.listdir(OUTPUTS)):
        inv = os.path.join(OUTPUTS, name, "inventory.csv")
        if not os.path.isfile(inv):
            continue
        try:
            when = datetime.fromtimestamp(os.path.getmtime(inv), timezone.utc).astimezone().isoformat(timespec="minutes")
        except OSError:
            when = ""
        scores = [f for f in os.listdir(os.path.join(OUTPUTS, name))
                  if f.startswith("scorecard")]
        out.append({
            "id": "ext:" + name, "name": name, "external": True, "status": "external",
            "created": when, "user": "—", "inventory": inv, "source_dir": os.path.join(OUTPUTS, name),
            "scorecards": scores, "argv": None,
        })
    return out


def _ui_runs() -> list:
    out = []
    if not os.path.isdir(RUNS):
        return out
    for rid in sorted(os.listdir(RUNS), reverse=True):
        meta = jload(os.path.join(RUNS, rid, "run.json"))
        if not meta:
            continue
        out.append(meta)
    return out


def all_runs() -> list:
    """UI runs first (newest first), then external historical runs."""
    return _ui_runs() + _external_runs()


def resolve_run(rid: str) -> dict | None:
    """Return a run record with a usable `inventory` path, for either a UI run or an external one."""
    if rid.startswith("ext:"):
        for r in _external_runs():
            if r["id"] == rid:
                return r
        return None
    meta = jload(os.path.join(RUNS, rid, "run.json"))
    if not meta:
        return None
    meta.setdefault("inventory", os.path.join(RUNS, rid, "inventory.csv"))
    meta.setdefault("external", False)
    meta.setdefault("source_dir", os.path.join(RUNS, rid))
    return meta


def _row_count(inv: str) -> int:
    try:
        with open(inv, "r", encoding="utf-8", errors="replace", newline="") as fh:
            return max(0, sum(1 for _ in fh) - 1)
    except OSError:
        return 0


# ------------------------------------------------------------------ compare (Q for §6.7)
def compare_rules_decided(rows_a, rows_b) -> dict:
    """The Compare invariant: a row decided WITHOUT any model call must be byte-identical between
    two runs; a model-decided row may differ. Join on rel_path (the stable id that survives even
    format conversion — see findings §6). For every rel_path that is rules-decided in BOTH runs,
    every non-model, non-timing column must match. Any mismatch is a finding, not noise.
    """
    def rules_decided(r):
        return not as_bool(r.get("llm_consulted")) and not as_bool(r.get("s2_llm_consulted"))

    idx_a = {r.get("rel_path"): r for r in rows_a}
    idx_b = {r.get("rel_path"): r for r in rows_b}
    common = [k for k in idx_a if k in idx_b]

    both_rules = 0
    moved = []           # a DETECTOR decision changed on a rules-decided row -> the real invariant break
    input_changed = []   # only input/environment columns (size_bytes, status, …) differ -> informational
    for key in common:
        ra, rb = idx_a[key], idx_b[key]
        if not (rules_decided(ra) and rules_decided(rb)):
            continue
        both_rules += 1
        dec_diffs = sorted(c for c in _DECISION_COLS if (ra.get(c) or "") != (rb.get(c) or ""))
        other_cols = (set(ra) | set(rb)) - _MODEL_OR_TIMING_COLS - _DECISION_COLS
        inp_diffs = sorted(c for c in other_cols if (ra.get(c) or "") != (rb.get(c) or ""))
        if dec_diffs:
            moved.append({"rel_path": key, "columns": dec_diffs[:8],
                          "example": {c: [ra.get(c), rb.get(c)] for c in dec_diffs[:3]}})
        elif inp_diffs:
            input_changed.append({"rel_path": key, "columns": inp_diffs[:8]})

    return {
        "files_a": len(rows_a), "files_b": len(rows_b),
        "common": len(common),
        "only_in_a": len(idx_a) - len(common), "only_in_b": len(idx_b) - len(common),
        "both_rules_decided": both_rules,
        "moved_count": len(moved),
        "moved": moved[:50],
        "input_changed_count": len(input_changed),
        "input_changed": input_changed[:50],
        "verdict": "clean" if not moved else "rules-decided row moved",
    }


def provenance_diff(meta_a: dict | None, meta_b: dict | None) -> list:
    """Diff the provenance blocks of two runs — a change here (image tag, model, api version,
    rulepack hash) is usually the explanation for a results difference. Returns a list of
    {key, a, b} for keys that differ. Empty when either run has no provenance (external runs)."""
    pa = (meta_a or {}).get("provenance") or {}
    pb = (meta_b or {}).get("provenance") or {}
    keys = sorted(set(pa) | set(pb))
    return [{"key": k, "a": pa.get(k), "b": pb.get(k)} for k in keys if pa.get(k) != pb.get(k)]


# ------------------------------------------------------------------ subprocess tools
def _tool_env() -> dict:
    env = dict(os.environ)
    parts = [PII_PKG]
    sl = env.get("SCALING_LIB_SRC")     # local dev: scaling_lib from a checkout (unset on the ops VM)
    if sl and os.path.isdir(sl):
        parts.append(sl)
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    # Force UTF-8 for child stdio. On Windows, a child (notably `az acr build`, reached via
    # scaling-lib) defaults to the cp1252 'charmap' codec when its output is redirected into our
    # pipe/log, and crashes with UnicodeEncodeError on any non-cp1252 character in the build log.
    # This propagates through scaling-lib down to az. POSIX is UTF-8 already, so this is a no-op there.
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_tool(argv: list, timeout: int = 1800) -> dict:
    """Run a pii_triage subcommand and capture its output. Never raises — a tool failure is data."""
    try:
        # decode the child's UTF-8 output explicitly (it now writes UTF-8; the parent's default
        # decode is cp1252 on Windows, which would mojibake or fail on non-ASCII output)
        r = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout, env=_tool_env())
        return {"ok": r.returncode == 0, "exit": r.returncode,
                "out": (r.stdout or "") + (r.stderr or "")}
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit": -1, "out": f"timed out after {timeout}s"}
    except Exception as exc:
        return {"ok": False, "exit": -1, "out": f"{type(exc).__name__}: {exc}"}


def _q(s) -> str:
    s = str(s)
    return f'"{s}"' if (" " in s or "\t" in s) else s


def build_collect_argv(out_csv: str) -> list:
    """collect_outputs.py gathers each worker's result.json (across the drained table) into one
    inventory CSV — the 'save this run' step. Written into the run's own folder."""
    return [sys.executable, os.path.join(ROOT, "collect_outputs.py"), "--out", out_csv]


def build_collect_watch_argv(out_csv: str, interval: float = 15.0) -> list:
    return [sys.executable, os.path.join(ROOT, "collect_outputs.py"),
            "--out", out_csv, "--watch", "--interval", str(interval)]


# ------------------------------------------------------------------ live collection (--watch)
# Auto-started right after submit, so results accumulate into runs/<id>/inventory.csv as files
# complete -- instead of the old "nothing until the whole batch drains" behaviour, where a bad
# run late in a long batch meant zero partial output and a full rework. One background
# collect_outputs.py --watch subprocess per run_id; the UI only ever holds the live Popen handle
# for it (same "in-memory state = only processes we started" rule as everything else here). The
# subprocess itself is protected against a second writer (its own <out>.watch.pid lock, in
# collect_outputs.py) -- load-bearing here specifically because a UI restart loses this dict but
# NOT the still-running background process, so a naive re-start-on-every-submit-check would
# otherwise race two writers on the same file.
WATCHERS: dict = {}
_WATCH_LOCK = threading.Lock()


def start_watch(run_id: str, out_csv: str) -> dict:
    """Start a background --watch collector for this run, or report one is already running."""
    with _WATCH_LOCK:
        existing = WATCHERS.get(run_id)
        if existing and existing["proc"].poll() is None:
            return {"ok": True, "started": False, "already_running": True, "pid": existing["proc"].pid}
        os.makedirs(os.path.dirname(out_csv), exist_ok=True)
        log_path = os.path.join(os.path.dirname(out_csv), "collect_watch.log")
        argv = build_collect_watch_argv(out_csv)
        try:
            log_fh = open(log_path, "a", encoding="utf-8")
            log_fh.write(f"\n--- watch started {now_iso()} by {whoami()} ---\n{' '.join(_q(x) for x in argv)}\n")
            log_fh.flush()
            proc = subprocess.Popen(argv, cwd=ROOT, env=_tool_env(),
                                    stdout=log_fh, stderr=subprocess.STDOUT)
        except Exception as exc:
            return {"ok": False, "why": f"{type(exc).__name__}: {exc}"}
        WATCHERS[run_id] = {"proc": proc, "log": log_path, "out": out_csv, "started_at": now_iso()}
        return {"ok": True, "started": True, "pid": proc.pid}


def stop_watch(run_id: str, timeout: float = 5.0) -> None:
    """Best-effort stop of a run's background watcher -- e.g. before archiving/resetting it, so
    nothing keeps appending to a run directory that is about to be cleared."""
    with _WATCH_LOCK:
        entry = WATCHERS.pop(run_id, None)
    if not entry:
        return
    proc = entry["proc"]
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=timeout)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def watch_status(run_id: str, inv: str) -> dict:
    """Live status for the Results screen: is a watcher running, how many rows so far, and (once
    it exits) how it ended. Row count is read straight from disk, so this stays correct even
    across a UI restart that lost the in-memory WATCHERS entry."""
    rows = _row_count(inv) if os.path.isfile(inv) else 0
    entry = WATCHERS.get(run_id)
    if not entry:
        return {"watching": False, "rows": rows, "exists": os.path.isfile(inv)}
    rc = entry["proc"].poll()
    status = {"watching": rc is None, "rows": rows, "exists": os.path.isfile(inv),
              "pid": entry["proc"].pid, "started_at": entry["started_at"], "log": entry["log"]}
    if rc is not None:
        status["exit_code"] = rc
    return status


def build_report_argv(inventory: str, out_csv: str) -> list:
    return [sys.executable, "-m", "pii_triage", "report", inventory, "--out", out_csv]


def build_sample_argv(inventory: str, out_csv: str, rate, seed) -> list:
    return [sys.executable, "-m", "pii_triage", "sample", inventory,
            "--out", out_csv, "--rate", str(rate), "--seed", str(int(seed))]


def build_benchmark_argv(inventory: str, gold: str, id_col=None, responsive_col=None,
                         bde_col=None, sheet=None, absent_means="unreviewed",
                         bde_threshold=None) -> list:
    a = [sys.executable, "-m", "pii_triage", "benchmark", inventory, gold]
    if id_col:
        a += ["--id-col", id_col]
    if responsive_col:
        a += ["--responsive-col", responsive_col]
    if bde_col:
        a += ["--bde-col", bde_col]
    if sheet:
        a += ["--sheet", sheet]
    if absent_means == "zero":     # opt-in; default 'unreviewed' matches the CLI default
        a += ["--absent-means", "zero"]
    if bde_threshold not in (None, ""):
        a += ["--bde-threshold", str(int(bde_threshold))]
    return a


def build_score_argv(inventory: str, entities: str, out_dir: str, id_col=None, count_col=None,
                     entities_sheet=None, bde_threshold=None, absent_means="auto", prices=None) -> list:
    """tools/score_combined.py — the full "vs manual review" scorecard the local build used. Reads
    the CNG entities export (Control ID + Total Entities), deriving responsive = count>0 and
    BDE = count > threshold, plus cost and stage-2. NOTE: score_combined uses '> threshold', while
    the pipeline uses '>= N', so an N+ threshold is passed as N-1 (matches the local UI)."""
    a = [sys.executable, os.path.join(PII_PKG, "tools", "score_combined.py"),
         "--inventory", inventory, "--entities", entities, "--out-dir", out_dir]
    if id_col:
        a += ["--id-col", id_col]
    if count_col:
        a += ["--count-col", count_col]
    if entities_sheet:
        a += ["--entities-sheet", entities_sheet]
    if bde_threshold not in (None, ""):
        a += ["--bde-threshold", str(int(bde_threshold) - 1)]
    if absent_means and absent_means != "auto":
        a += ["--absent-means", absent_means]
    prices = prices or DEFAULTS
    a += ["--price-per-1k-pages", str(prices["price_per_1k_pages"]),
          "--price-per-1k-in", str(prices["price_per_1k_in"]),
          "--price-per-1k-out", str(prices["price_per_1k_out"])]
    return a


def parse_score_summary(out: str) -> dict:
    """Pull the headline PIPELINE metrics ('what a reviewer receives') out of score_combined's
    stdout so the UI can show a summary card, not just the raw terminal text. Best-effort — returns
    {} if the block isn't found (the full text is always shown too), so a format change degrades the
    summary rather than breaking the screen."""
    import re
    if not out:
        return {}
    i = out.find("PIPELINE")
    if i == -1:
        return {}
    rest = out[i:]
    j = rest.find("NR/R --", 8)                 # stop before the next section (UNION), if any
    block = rest[:j] if j != -1 else rest

    def g(pat):
        m = re.search(pat, block)
        return m.group(1).replace(",", "") if m else None

    s = {
        "scored": g(r"(?m)^\s*scored\s+([\d,]+)"),
        "excluded": g(r"excluded:\s*([\d,]+)"),
        "recall": g(r"(?m)^\s*recall\s+([\d.]+)"),
        "precision": g(r"(?m)^\s*precision\s+([\d.]+)"),
        "accuracy": g(r"(?m)^\s*accuracy\s+([\d.]+)"),
        "f1": g(r"\bF1\s+([\d.]+)"),
        "nr_accuracy": g(r"(?m)^\s*NR accuracy\s+([\d.]+)"),
        "r_accuracy": g(r"(?m)^\s*R accuracy\s+([\d.]+)"),
        "flagged": g(r"(?m)^\s*flagged\s+([\d.]+)"),
        "over_call": g(r"over-call\s+([\d.]+)"),
        "under_call": g(r"under-call\s+([\d.]+)"),
    }
    tp = re.search(r"TP=([\d,]+)\s+FP=([\d,]+)\s+FN=([\d,]+)\s+TN=([\d,]+)", block)
    if tp:
        s["tp"], s["fp"], s["fn"], s["tn"] = [x.replace(",", "") for x in tp.groups()]
    return {k: v for k, v in s.items() if v is not None}


def _run_dir(rid: str) -> str:
    """A UI-owned working dir for derived artefacts. For an external run this is a sibling under
    runs/ so we never write into outputs/ (read-only on historical artefacts)."""
    safe = rid.replace("ext:", "external__").replace("/", "_").replace("\\", "_")
    d = os.path.join(RUNS, safe)
    os.makedirs(d, exist_ok=True)
    return d


def read_table_csv(path: str, cap: int = 5000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
            reader = csv.reader(fh)
            rows = []
            for i, row in enumerate(reader):
                rows.append(row)
                if i >= cap:
                    break
            return rows
    except OSError:
        return []


# ------------------------------------------------------------------ store-backed (Phase 3+)
# scaling_lib + the Azure store are reached only through hwe_scaled_store, imported lazily so the
# read-only screens keep working where scaling_lib / Azure config are absent.
ENV_LOADED = 0


def load_env() -> int:
    """Load ROOT/.env into the process (once) so the store sees the same config the CLI would.
    Returns the number of settings loaded, or 0 if python-dotenv or the file is absent."""
    global ENV_LOADED
    path = os.path.join(ROOT, ".env")
    try:
        from dotenv import load_dotenv
    except Exception:
        return 0
    if os.path.isfile(path):
        load_dotenv(path)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                ENV_LOADED = sum(1 for ln in fh if "=" in ln and not ln.strip().startswith("#"))
        except OSError:
            ENV_LOADED = 0
    return ENV_LOADED


def _store():
    import hwe_scaled_store as store   # lazy: only when a store-backed screen is used
    return store


def setup_checks() -> list:
    """The scaled preflight. Each row: {state: ok|warn|bad, key, val, detail?}. Every check is
    measured (a real store/credential/az reading), never assumed. Worker-side env vars are NOT
    flagged missing on the ops VM (findings §7). The headline is deployed-tag vs local-SHA."""
    rows = []

    def add(state, key, val, detail=None):
        r = {"state": state, "key": key, "val": val}
        if detail:
            r["detail"] = detail
        rows.append(r)

    add("ok", "Python (UI host)", sys.version.split()[0])

    # Re-read .env on every Setup check, so a file you JUST added is picked up without restarting
    # the UI — and diagnose the common reasons it isn't found (wrong folder, Windows '.env.txt',
    # missing python-dotenv) instead of a bare "not loaded".
    load_env()
    env_file = os.path.join(ROOT, ".env")
    try:
        import dotenv  # noqa: F401
        have_dotenv = True
    except Exception:
        have_dotenv = False
    if not have_dotenv:
        add("bad", ".env", "python-dotenv is not installed in this Python",
            "pip install python-dotenv — it's the same one enqueue.py / worker.py use")
    elif not os.path.isfile(env_file):
        add("bad", ".env", "no .env found in this folder",
            f"looked for exactly: {env_file}\n"
            "• it must sit next to hwe_scaled_ui.py (not in a subfolder)\n"
            "• on Windows, confirm it isn't really '.env.txt' — Explorer hides the extension "
            "(turn on View ▸ File name extensions, or save from Notepad with type 'All Files')\n"
            "values are never shown, only counted")
    elif not ENV_LOADED:
        add("warn", ".env", "found the file but read 0 settings",
            "each line must be KEY=VALUE (not all comments or blank)")
    else:
        add("ok", ".env", f"loaded · {ENV_LOADED} setting(s)")

    try:
        store = _store()
    except Exception as exc:
        add("bad", "scaling_lib", f"UI store module failed to import: {exc}")
        return rows

    sl = store.scaling_lib_status()
    add("ok" if sl["ok"] else "bad", "scaling_lib", sl["detail"],
        None if sl["ok"] else "workers, enqueue, and the store all need this importable")

    mode = store.storage_mode()
    add("ok" if mode else "bad", "Storage mode", mode or "no storage endpoint configured",
        None if mode else "set AZURE_STORAGE_CONNECTION_STRING, or the TABLE_URL + QUEUE_URL pair")

    if sl["ok"] and mode:
        cred = store.credential_probe()
        add("ok" if cred["ok"] else "bad", "Credential / login", cred["detail"])
        tbl = store.check_table()
        add("ok" if tbl["ok"] else "bad", "Status table", tbl["detail"])
        q = store.check_queues()
        add("ok" if q["ok"] else "bad", "Queues", q["detail"])
    else:
        add("warn", "Storage reachability", "skipped — scaling_lib or storage endpoint missing")

    for key, r in store.check_mounts().items():
        add("ok" if r["ok"] else "bad", key, r["detail"])

    # env completeness — required ops-VM vars only; worker-side vars are informational
    er = store.env_report()
    missing = [x["key"] for x in er["ops_required"] if not x["set"]]
    if not er["storage_endpoint_ok"]:
        missing.append("storage endpoint")
    add("ok" if not missing else "bad", "Required ops-VM settings",
        "all present" if not missing else f"missing: {', '.join(missing)}")

    # THE headline: is this working tree what the workers are actually running?
    sha = store.git_sha()
    dep = store.deployed_image_tag()
    if not dep["ok"]:
        add("warn", "Deployed image vs local code",
            f"cannot determine — {dep['detail']}",
            f"local git SHA is {sha or 'unknown'}; this needs `az` on the ops VM to compare")
    else:
        deployed_tag = dep["tag"]
        if sha and deployed_tag == sha:
            add("ok", "Deployed image vs local code", f"workers run {deployed_tag} = this tree")
        else:
            add("bad", "Deployed image vs local code",
                f"workers run {deployed_tag}, this tree is {sha or 'unknown'}",
                "the code in this working tree is NOT what the workers are running — build & deploy, "
                "or check out the deployed SHA, before trusting a run's provenance")
    return rows


def monitor_payload(job_id: str) -> dict:
    """Everything the Running/monitor screen shows for one job: authoritative per-state counts from
    the table, approximate queue depths (stamped), throughput/ETA range, task-level replica series,
    rate-limit wait, stuck items, failures. All measured; unrecorded metrics come back None."""
    store = _store()
    vis = int(os.environ.get("QUEUE_VISIBILITY_TIMEOUT_SECONDS") or 300)
    m = store.job_metrics(job_id, visibility_timeout=vis)
    try:
        m["queues"] = store.queue_counts()
    except Exception as exc:
        m["queues"] = {"error": f"{type(exc).__name__}: {exc}"}
    m["failures"] = store.failures(job_id)
    m["visibility_timeout_s"] = vis
    try:
        m["dlq_events"] = store.dlq_events()
    except Exception:
        m["dlq_events"] = []
    try:
        m["preflight_events"] = store.preflight_events()
    except Exception:
        m["preflight_events"] = []
    # throughput_fpm + eta_range_min are computed in job_metrics (trailing window, guarded).
    return m


# ------------------------------------------------------------------ New run / submit (Phase 4 write)
LOCK_PATH = os.path.join(RUNS, "_active_run.json")
AUDIT_PATH = os.path.join(RUNS, "_audit.jsonl")
_PROTOCOL_EXTS = (".pdf", ".docx", ".doc", ".txt", ".rtf")   # exactly what worker.py accepts
COUNT_CACHE: dict = {}


def _under_input_mount(path: str) -> bool:
    """True if `path` is at or under INPUT_MOUNT. Uses commonpath (component-aware, case-insensitive
    on Windows) rather than a string prefix — the prefix approach broke when INPUT_MOUNT is a drive
    root like 'I:\\', whose abspath keeps a trailing separator and never prefix-matches 'I:\\CNG-10'."""
    inp = os.environ.get("INPUT_MOUNT", "")
    if not inp:
        return False
    try:
        a = os.path.normcase(os.path.abspath(inp))
        b = os.path.normcase(os.path.abspath(path))
        return os.path.commonpath([a, b]) == os.path.commonpath([a])
    except ValueError:
        return False   # e.g. different drives on Windows -> not under the mount


def windows_exts() -> set:
    """Extensions routed to the Windows queue — but ONLY when AZURE_WINDOWS_QUEUE_NAME is set, which
    mirrors queue._is_windows_file exactly (findings Q). Otherwise everything goes to the main queue."""
    if not os.environ.get("AZURE_WINDOWS_QUEUE_NAME"):
        return set()
    raw = os.environ.get("WINDOWS_FILE_EXTENSIONS")
    if raw:
        return {e.strip().lower() for e in raw.split(",") if e.strip()}
    return {".doc", ".xls", ".ppt"}


def _normalize_job_dir_input(job_dir: str) -> tuple[str, str]:
    """Accept both the job directory itself and the common files/ path form used by enqueue.py.
    Returns normalized (job_dir, files_dir)."""
    raw = (job_dir or "").strip().strip('"')
    if not raw:
        return "", ""
    abspath = os.path.abspath(raw)
    name = os.path.basename(abspath)
    if name.lower() == "files":
        return os.path.dirname(abspath), abspath
    if os.path.isfile(abspath):
        parent = os.path.dirname(abspath)
        if os.path.basename(parent).lower() == "files":
            return os.path.dirname(parent), parent
    return abspath, os.path.join(abspath, "files")


def validate_job_dir(job_dir: str) -> dict:
    """Validate the layout enqueue.py + worker.py require: a directory UNDER INPUT_MOUNT, containing
    a folder named exactly 'files', with an optional sibling protocol.<ext>. Reports precisely what
    is wrong (findings Q3/Q4). Accepts either the parent job directory or the direct `files` path."""
    problems = []
    if not job_dir:
        return {"ok": False, "problems": ["no folder given"]}
    job_dir, files_dir = _normalize_job_dir_input(job_dir)
    inp = os.environ.get("INPUT_MOUNT", "")
    if not inp:
        problems.append("INPUT_MOUNT is not set — cannot resolve the corpus location")
    elif not _under_input_mount(job_dir):
        problems.append(f"must be under INPUT_MOUNT ({inp}); enqueue resolves paths relative to it")
    if not os.path.isdir(job_dir):
        problems.append("that path is not a directory")
        return {"ok": False, "job_dir": job_dir, "problems": problems}
    has_files = os.path.isdir(files_dir)
    if not has_files:
        problems.append("no 'files/' folder inside (the folder name must be exactly 'files')")
    protocol = None
    for ext in _PROTOCOL_EXTS:
        cand = os.path.join(job_dir, "protocol" + ext)
        if os.path.isfile(cand):
            protocol = cand
            break
    return {"ok": not problems, "job_dir": job_dir,
            "files_dir": files_dir if has_files else None, "protocol": protocol,
            "problems": problems}


def check_corpus(files_dir: str, use_cache: bool = True) -> dict:
    """Walk files/ once: total, by-extension, Windows-queue count, total bytes. Cached against the
    path so toggling a form option rebuilds only the command, never re-walks a network share
    (local build's no_count optimisation, notes #6)."""
    if use_cache and files_dir in COUNT_CACHE:
        return COUNT_CACHE[files_dir]
    n, exts, win, total_bytes, truncated = 0, {}, 0, 0, False
    wexts = windows_exts()
    for dp, _d, fs in os.walk(files_dir):
        for f in fs:
            ext = os.path.splitext(f)[1].lower()
            key = ext or "(none)"
            exts[key] = exts.get(key, 0) + 1
            n += 1
            if ext in wexts:
                win += 1
            if n >= 400_000:
                truncated = True
                break
        if truncated:
            break
    res = {"files": n, "exts": sorted(exts.items(), key=lambda kv: -kv[1])[:12],
           "windows_count": win, "total_bytes": total_bytes, "truncated": truncated}
    COUNT_CACHE[files_dir] = res
    return res


def rescan_keep_count(inventory: str, exclude_lanes: set):
    """Mirror pii_triage.runner.load_filter_set EXACTLY (a unit test proves agreement): the number
    of files enqueue.py --inventory will actually enqueue is the rows whose suggested_lane is not in
    exclude_lanes. Kept in-process so the preview equals reality (notes #11)."""
    keep = set()
    try:
        with open(inventory, "r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("suggested_lane") not in exclude_lanes:
                    keep.add(row["rel_path"])
    except OSError:
        return None
    return len(keep)


def build_enqueue_argv(files_dir: str, job_id: str, inventory: str | None = None,
                       exclude_lanes=None, bde_threshold=None) -> list:
    """The single place the enqueue command is built. Takes explicit params only — UI-only form
    fields (name, mode, no_count) cannot reach argv (§1.4 #12; asserted by a test)."""
    a = [sys.executable, os.path.join(ROOT, "enqueue.py"), files_dir, "--job-id", job_id]
    if inventory:
        a += ["--inventory", inventory]
        if exclude_lanes:
            a += ["--exclude-lanes", ",".join(exclude_lanes)]
    if bde_threshold is not None:
        a += ["--bde-threshold", str(int(bde_threshold))]
    return a


def provenance_block() -> dict:
    """Everything knowable at submit time and unrecoverable afterwards (§7). Model/deployment/flags
    are worker-side (injected into the Container App), so they are best-effort here and labelled."""
    sha = dep = None
    try:
        store = _store()
        sha = store.git_sha()
        d = store.deployed_image_tag()
        dep = d if d.get("ok") else {"ok": False, "detail": d.get("detail")}
    except Exception:
        pass
    return {
        "git_sha": sha,
        "deployed_image": (dep or {}).get("image"),
        "deployed_tag": (dep or {}).get("tag"),
        "deployed_tag_detail": None if (dep or {}).get("ok") else (dep or {}).get("detail"),
        "job_type": os.environ.get("JOB_TYPE"),
        "table": os.environ.get("AZURE_TABLE_NAME"),
        "main_queue": os.environ.get("AZURE_QUEUE_NAME"),
        "windows_queue": os.environ.get("AZURE_WINDOWS_QUEUE_NAME"),
        "rulepack": os.environ.get("RULEPACK_PATH") or "built-in default Master List",
        "model_deployment": os.environ.get("AZURE_OPENAI_DEPLOYMENT")
                            or os.environ.get("AZURE_OPENAI_DEPLOYMENT_GPT_5_4_NANO")
                            or "(worker-side — see deployed image)",
        "api_version": os.environ.get("AZURE_OPENAI_API_VERSION") or "(worker-side)",
        "use_llm": os.environ.get("USE_LLM") or "(worker-side)",
        "use_ocr": os.environ.get("USE_OCR") or "(worker-side)",
        "captured_at": now_iso(),
    }


# -- one-run lock (§4.1/§4.2) -----------------------------------------------
def read_lock():
    return jload(LOCK_PATH)


def active_run():
    """The lock, enriched with a MEASURED open-count (pending+processing) for its job. 'active' is
    read from the store, never assumed from the lock's mere existence (findings §4.1)."""
    lock = read_lock()
    if not lock:
        return None
    lock = dict(lock)
    lock["open_count"], lock["measured"], lock["drained"] = None, False, False
    try:
        lock["open_count"] = _store().job_open_count(lock.get("job_id", ""))
        lock["measured"] = True
        lock["drained"] = lock["open_count"] == 0
    except Exception:
        pass
    return lock


def acquire_lock(run_id: str, job_id: str, corpus: str):
    jdump(LOCK_PATH, {"run_id": run_id, "job_id": job_id, "user": whoami(),
                      "started_at": now_iso(), "corpus": corpus})


def release_lock():
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


def read_audit(limit: int = 200) -> list:
    """Most recent audit entries, newest first -- who ran what, when, and its outcome, across
    every action this UI took (submit, collect, report, sample, benchmark, score, compare,
    stage, reset). Written but never previously surfaced anywhere; this is what makes the
    audit trail actually readable rather than just a file nobody opens. Best-effort: an
    unreadable/malformed line is skipped, never raised."""
    if not os.path.isfile(AUDIT_PATH):
        return []
    try:
        with open(AUDIT_PATH, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    out.reverse()
    return out


def audit(action: str, **fields) -> dict:
    rec = {"action": action, "user": whoami(), "at": now_iso(), **fields}
    os.makedirs(RUNS, exist_ok=True)
    try:
        with open(AUDIT_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass
    return rec


# -- staging: COPY a corpus into <job_dir>/files/ (the chosen option, §6.3) --
STAGE: dict = {}
_STAGE_LOCK = threading.Lock()


def stage_start(src: str, dest_job_dir: str, protocol_src: str | None = None) -> dict:
    src, dest_job_dir = os.path.abspath(src), os.path.abspath(dest_job_dir)
    problems = []
    if not os.path.isdir(src):
        problems.append("source folder does not exist")
    if not os.environ.get("INPUT_MOUNT"):
        problems.append("INPUT_MOUNT is not set")
    elif not _under_input_mount(dest_job_dir):
        problems.append(f"destination must be under INPUT_MOUNT ({os.environ.get('INPUT_MOUNT')})")
    if os.path.exists(os.path.join(dest_job_dir, "files")):
        problems.append("destination already has a files/ folder — choose a fresh job-directory name")
    if problems:
        return {"ok": False, "problems": problems}
    total, total_bytes = 0, 0
    for dp, _d, fs in os.walk(src):
        for f in fs:
            total += 1
            try:
                total_bytes += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    sid = datetime.now().strftime("%Y%m%d-%H%M%S-") + str(len(STAGE))
    STAGE[sid] = {"total": total, "total_bytes": total_bytes, "copied": 0, "bytes": 0,
                  "done": False, "error": None, "dest": dest_job_dir,
                  "files_dir": os.path.join(dest_job_dir, "files"), "protocol": None}
    threading.Thread(target=_stage_worker, args=(sid, src, dest_job_dir, protocol_src),
                     daemon=True).start()
    audit("stage_start", stage_id=sid, files=total, total_bytes=total_bytes)
    return {"ok": True, "id": sid, "total": total, "total_bytes": total_bytes,
            "files_dir": os.path.join(dest_job_dir, "files")}


def _stage_worker(sid, src, dest_job_dir, protocol_src):
    st = STAGE[sid]
    try:
        files_dir = os.path.join(dest_job_dir, "files")
        os.makedirs(files_dir, exist_ok=True)
        for dp, _d, fs in os.walk(src):
            rel = os.path.relpath(dp, src)
            outdir = files_dir if rel == "." else os.path.join(files_dir, rel)
            os.makedirs(outdir, exist_ok=True)
            for f in fs:
                shutil.copy2(os.path.join(dp, f), os.path.join(outdir, f))   # COPY — never move/delete
                st["copied"] += 1
                try:
                    st["bytes"] += os.path.getsize(os.path.join(outdir, f))
                except OSError:
                    pass
        if protocol_src and os.path.isfile(protocol_src):
            ext = os.path.splitext(protocol_src)[1].lower()
            if ext in _PROTOCOL_EXTS:
                shutil.copy2(protocol_src, os.path.join(dest_job_dir, "protocol" + ext))
                st["protocol"] = "protocol" + ext
        st["done"] = True
    except Exception as exc:
        st["error"] = f"{type(exc).__name__}: {exc}"
        st["done"] = True


def stage_status(sid):
    return STAGE.get(sid)


# -- submit -----------------------------------------------------------------
def submit_run(job_dir: str, mode: str = "job", name: str = "",
               rescan_run_id=None, exclude_lanes=None, bde_threshold=None) -> dict:
    v = validate_job_dir(job_dir)
    if not v.get("ok") or not v.get("files_dir"):
        return {"ok": False, "why": "; ".join(v.get("problems") or ["invalid job directory"])}
    act = active_run()
    if act:
        tail = ("; it is drained — Archive & reset it before starting another"
                if act.get("drained") else "; wait for it to finish or reset it")
        return {"ok": False, "active": act,
                "why": f"a run is already active — {act.get('run_id')} (job {act.get('job_id')}), "
                       f"started by {act.get('user')} at {act.get('started_at')}" + tail}
    files_dir = v["files_dir"]
    run_id = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    job_id = f"{os.path.basename(v['job_dir'])}-{run_id}"
    inventory, excl = None, None
    if mode == "rescan":
        prior = resolve_run(rescan_run_id or "")
        if not (prior and prior.get("inventory") and os.path.isfile(prior["inventory"])):
            return {"ok": False, "why": "pick a prior run that has an inventory to rescan"}
        inventory = prior["inventory"]
        excl = exclude_lanes or ["likely_non_responsive"]
    bde = int(bde_threshold) if bde_threshold not in (None, "") else None
    argv = build_enqueue_argv(files_dir, job_id, inventory, excl, bde_threshold=bde)
    res = run_tool(argv, timeout=3600)
    argv_str = " ".join(_q(x) for x in argv)
    if not res["ok"]:
        return {"ok": False, "why": "enqueue.py failed", "out": res["out"], "argv_str": argv_str}
    chk = check_corpus(files_dir)
    rundir = _run_dir(run_id)
    meta = {
        "id": run_id, "job_id": job_id, "name": name or run_id, "external": False,
        "corpus": v["job_dir"], "files_dir": files_dir, "protocol": v.get("protocol"),
        "mode": mode, "argv": argv, "argv_str": argv_str, "user": whoami(),
        "created": now_iso(), "status": "submitted", "file_count": chk["files"],
        "windows_count": chk["windows_count"], "exts": chk["exts"],
        "bde_threshold": bde,
        "rescan_of": rescan_run_id if mode == "rescan" else None,
        "provenance": provenance_block(),
        "inventory": os.path.join(rundir, "inventory.csv"),
    }
    jdump(os.path.join(rundir, "run.json"), meta)
    acquire_lock(run_id, job_id, v["job_dir"])
    audit("submit", run_id=run_id, job_id=job_id, files=chk["files"], windows=chk["windows_count"], mode=mode)
    watch_res = start_watch(run_id, meta["inventory"])
    if not watch_res.get("ok"):
        # Never let the convenience live-collector block a real submission -- the manual
        # /api/collect fallback still works once the run drains.
        audit("watch_start_failed", run_id=run_id, job_id=job_id, why=watch_res.get("why"))
    return {"ok": True, "id": run_id, "job_id": job_id, "windows_count": chk["windows_count"],
            "argv_str": argv_str, "out": res["out"]}


# -- archive-and-reset (§6.8, the crown jewel) ------------------------------
def archive_and_reset(run_id: str, override: bool = False, typed: str = "") -> dict:
    """Reset is only reachable here. It (1) refuses unless collect ran (override required),
    (2) requires typing the job_id, (3) ARCHIVES + verifies the table rows first — for the run AND
    any other jobs present, since reset is table-wide — and (4) only then clears, releasing the lock
    and writing an audit entry. If the archive does not verify, it refuses to reset."""
    stop_watch(run_id)   # nothing should still be appending to this run's inventory once we archive it
    meta = resolve_run(run_id)
    if not meta:
        # run.json was deleted manually, but the active-run lock still holds this run's
        # job_id — which is all archive-and-reset actually needs to clear the table. Rebuild
        # a minimal meta from the lock so a stuck run can still be archived and reset.
        lock = read_lock()
        if lock and lock.get("run_id") == run_id and lock.get("job_id"):
            meta = {"id": run_id, "job_id": lock["job_id"], "external": False,
                    "inventory": os.path.join(_run_dir(run_id), "inventory.csv")}
        else:
            return {"ok": False, "why": "unknown run"}
    job_id = meta.get("job_id")
    if not job_id:
        return {"ok": False, "why": "this is an external/historical run with no live job_id — "
                "there is nothing in the status table for it to archive or reset"}
    inv = meta.get("inventory")
    collected = bool(inv and os.path.isfile(inv))
    if not collected and not override:
        return {"ok": False, "needs_override": True,
                "why": "collect has not been run for this run (no inventory.csv). Reset would discard "
                       "the only record of timing, tokens, and attempts. Collect first — or override."}
    try:
        store = _store()
        jobs = store.list_jobs()
    except Exception as exc:
        return {"ok": False, "why": f"cannot read the status table to size the reset: {exc}"}
    total_rows = sum(j["total"] for j in jobs)
    other_jobs = [j["job_id"] for j in jobs if j["job_id"] != job_id]
    if typed.strip() != job_id:
        return {"ok": False, "needs_typed": True, "confirm_token": job_id,
                "total_rows": total_rows, "other_jobs": other_jobs,
                "why": f"type the job_id ({job_id}) to confirm discarding {total_rows} status row(s)"
                       + (f" across {len(jobs)} job(s)" if other_jobs else "")}
    archive_dir = os.path.join(_run_dir(run_id), "archive")
    arch = store.archive_job(job_id, archive_dir)
    others = [store.archive_job(oj, os.path.join(archive_dir, "other__" + oj)) for oj in other_jobs]
    if not arch["verified"]:
        return {"ok": False, "archive": arch,
                "why": f"archive did not verify ({arch['detail']}) — refusing to reset"}
    for o in others:
        if not o["verified"]:
            return {"ok": False, "why": f"archive of co-resident job {o['job_id']} did not verify — "
                    "refusing to reset"}
    r = store.run_reset()
    release_lock()
    audit("archive_and_reset", run_id=run_id, job_id=job_id, archived=arch["count"],
          other_jobs=other_jobs, deleted=r["deleted"])
    m = jload(meta_path(run_id)) or {}
    if m:
        m.update({"status": "archived_reset", "archived_at": now_iso(), "archive": arch})
        jdump(meta_path(run_id), m)
    return {"ok": True, "archived": arch["count"], "deleted": r["deleted"],
            "archive_dir": archive_dir, "other_jobs": other_jobs}


# ------------------------------------------------------------------ Build & deploy (§6.9)
# READ-ONLY status. Building/deploying is done from the CLI (`scaling-lib acr-*`) — it changes what
# every worker in the environment runs, so it is deliberately NOT triggerable from this UI. This
# screen only reports whether the environment is ready and what the target image / app are.


def build_preflight() -> dict:
    """Each check icon+word+reason. NOTE: Docker is NOT a prerequisite — acr-build runs in ACR's
    cloud via `az acr build` (findings §8.1). The gating checks are az+login, git, targets, and
    (for build/release) a GitHub token source."""
    rows = []

    def add(state, key, val, detail=None):
        r = {"state": state, "key": key, "val": val}
        if detail:
            r["detail"] = detail
        rows.append(r)

    az = shutil.which("az")
    logged = False
    az_detail = "not found on PATH — install the Azure CLI (this is an ops-VM action)"
    if az:
        try:
            r = _store().run_cli(["az", "account", "show"], timeout=30)   # shell=True on Windows (az is a .cmd)
            logged = r.returncode == 0
            az_detail = "present and logged in" if logged else "present but not logged in — run `az login`"
        except Exception as exc:
            az_detail = f"present; login check failed: {exc}"
    add("ok" if (az and logged) else "bad", "az CLI", az_detail)

    git = shutil.which("git")
    sha = tree_clean = None
    if git:
        try:
            sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                                 text=True, cwd=ROOT).stdout.strip() or None
            porcelain = subprocess.run(["git", "status", "--porcelain"], capture_output=True,
                                       text=True, cwd=ROOT).stdout.strip()
            tree_clean = (porcelain == "")
        except Exception:
            pass
    add("ok" if git else "bad", "git", (f"present · HEAD {sha}" if sha else "present") if git else "not found")
    add("ok" if tree_clean else "warn", "Working tree",
        "clean" if tree_clean else "uncommitted changes present",
        None if tree_clean else "a build uploads the working tree but tags it with the committed SHA — "
        "commit first for a truthful tag, or pass an explicit --tag")
    add("ok", "Docker", "not required — acr-build runs in ACR's cloud (az acr build)")

    tok = bool(os.environ.get("GITHUB_TOKEN"))
    add("ok" if tok else "warn", "GITHUB_TOKEN",
        "set" if tok else "not set — acr-build/release fall back to your git HTTPS credentials",
        None if tok else "fine if you can `git push` to the repo over HTTPS on this host")

    targets = {k: os.environ.get(k) for k in
               ("ACR_REGISTRY", "ACR_IMAGE", "AZURE_CONTAINER_APP", "AZURE_RESOURCE_GROUP", "JOB_TYPE")}
    miss = [k for k, v in targets.items() if not v]
    add("ok" if not miss else "bad", "Targets (.env)", "all set" if not miss else "missing: " + ", ".join(miss),
        " · ".join(f"{k}={v}" for k, v in targets.items() if v) or None)

    can_build = bool(az and logged and git and targets["ACR_REGISTRY"]
                     and targets["ACR_IMAGE"] and targets["JOB_TYPE"])
    can_deploy = bool(az and logged and targets["ACR_REGISTRY"] and targets["ACR_IMAGE"]
                      and targets["AZURE_CONTAINER_APP"] and targets["AZURE_RESOURCE_GROUP"])
    return {"checks": rows, "sha": sha, "tree_clean": tree_clean, "targets": targets,
            "can_build": can_build, "can_deploy": can_deploy, "active_run": active_run()}


# ------------------------------------------------------------------ http
class H(http.server.BaseHTTPRequestHandler):
    server_version = "HWE-Scaled/2"

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # -- GET --------------------------------------------------------------
    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        route, q = p.path, urllib.parse.parse_qs(p.query)
        if route in ("/", "/index.html"):
            try:
                with open(UI_HTML, "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            except OSError:
                return self._send(500, b"hwe_scaled_ui.html is missing next to hwe_scaled_ui.py",
                                  "text/plain; charset=utf-8")
        if route == "/api/context":
            return self._json({"defaults": DEFAULTS, "root": ROOT,
                               "platform": "windows" if IS_WIN else "posix",
                               "env_loaded": ENV_LOADED, "user": whoami()})
        if route == "/api/setup":
            try:
                store = _store()
                return self._json({"checks": setup_checks(), "env": store.env_report()})
            except Exception as exc:
                return self._json({"checks": [{"state": "bad", "key": "Setup",
                                   "val": f"{type(exc).__name__}: {exc}"}], "env": None})
        if route == "/api/jobs":
            # store jobs (from the Table) — how the monitor learns of the live run
            try:
                return self._json({"jobs": _store().list_jobs()})
            except Exception as exc:
                return self._json({"jobs": [], "error": f"{type(exc).__name__}: {exc}"})
        if route == "/api/monitor":
            jid = (q.get("id") or [""])[0]
            if not jid:
                try:
                    jid = _store().latest_job_id()
                except Exception:
                    jid = None
            if not jid:
                return self._json({"error": "no job in the status table"}, 404)
            try:
                return self._json(monitor_payload(jid))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        if route == "/api/lock":
            return self._json({"active": active_run()})
        if route == "/api/newrun/validate":
            d = (q.get("dir") or [""])[0].strip().strip('"')
            return self._json(validate_job_dir(d))
        if route == "/api/stage/status":
            st = stage_status((q.get("id") or [""])[0])
            return self._json(st or {"error": "unknown staging job"})
        if route == "/api/collect/status":
            # Polled from the Results screen while a run's background --watch collector is
            # (or was) live, so a growing inventory shows up without waiting for a manual Collect.
            rid = (q.get("id") or [""])[0]
            m0 = resolve_run(rid)
            if not m0:
                return self._json({"error": "unknown run"}, 404)
            inv = m0.get("inventory") or os.path.join(_run_dir(rid), "inventory.csv")
            live = watch_status(rid, inv)
            if not live["watching"] and "exit_code" in live and m0.get("status") != "collected":
                # the watcher finished on its own (queue drained) since we last checked -- record it
                m = jload(meta_path(rid)) or {}
                if m:
                    m.update({"status": "collected", "collected_at": now_iso()})
                    jdump(meta_path(rid), m)
            return self._json(live)
        if route == "/api/build/preflight":
            return self._json(build_preflight())
        if route == "/api/audit":
            limit = int((q.get("limit") or ["200"])[0])
            return self._json({"entries": read_audit(limit)})
        if route == "/api/runs":
            runs = []
            for r in all_runs():
                inv = r.get("inventory") or os.path.join(RUNS, r.get("id", ""), "inventory.csv")
                r = dict(r)
                r["done"] = _row_count(inv) if inv and os.path.isfile(inv) else 0
                r["has_inventory"] = bool(inv and os.path.isfile(inv))
                runs.append(r)
            return self._json({"runs": runs})
        if route == "/api/run":
            rid = (q.get("id") or [""])[0]
            meta = resolve_run(rid)
            if not meta:
                return self._json({"error": "unknown run"}, 404)
            inv = meta.get("inventory")
            resp = {"meta": meta}
            if inv and os.path.isfile(inv):
                rows, fields, trunc = read_inventory(inv)
                resp["summary"] = summarize_inventory(rows, DEFAULTS)
                resp["summary"]["truncated"] = trunc
                resp["has_inventory"] = True
            else:
                resp["has_inventory"] = False
            # surface any table1 / sample already built for this run
            d = _run_dir(rid)
            resp["artefacts"] = {name: os.path.isfile(os.path.join(d, name))
                                 for name in ("table1.csv", "sample.csv", "benchmark.txt")}
            return self._json(resp)
        if route == "/api/table":
            rid = (q.get("id") or [""])[0]
            which = (q.get("which") or ["table1.csv"])[0]
            if which not in ("table1.csv", "sample.csv"):
                return self._json({"error": "unknown table"}, 400)
            path = os.path.join(_run_dir(rid), which)
            if not os.path.isfile(path):
                return self._json({"rows": [], "built": False})
            return self._json({"rows": read_table_csv(path), "built": True})
        if route == "/api/export":
            rid = (q.get("id") or [""])[0]
            return self._export(rid)
        return self._json({"error": "not found"}, 404)

    # -- POST -------------------------------------------------------------
    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        b = self._body()
        rid = b.get("id") or ""
        meta = resolve_run(rid)
        if route == "/api/collect":
            # "Save this run": gather the workers' result.json into the run's inventory. A live
            # --watch collector is normally already running for this run (started at submit) and
            # keeps the inventory current as files complete -- if so, report its status instead of
            # launching a second, conflicting writer. Only fall back to the old one-shot path (gated
            # on the run being drained -- findings Q9: collecting on a live queue yields a silently-
            # partial file) when no watcher is running: an external run, a submit-time watch-start
            # failure, or a UI restart after the watcher already finished.
            if not meta:
                return self._json({"ok": False, "out": "unknown run"}, 404)
            if meta.get("external"):
                return self._json({"ok": False, "out": "this is a historical run — already collected"})
            job_id = meta.get("job_id")
            inv = meta.get("inventory") or os.path.join(_run_dir(rid), "inventory.csv")
            live = watch_status(rid, inv)
            if live["watching"]:
                return self._json({"ok": True, "out": f"collecting live — {live['rows']} record(s) so far",
                                   "watching": True, "rows_written": live["rows"]})
            try:
                open_n = _store().job_open_count(job_id) if job_id else 0
            except Exception as exc:
                return self._json({"ok": False, "out": f"could not confirm the run is drained: {exc}"})
            if open_n > 0:
                return self._json({"ok": False, "out": f"{open_n} file(s) are still pending/processing — "
                                   "collect once Monitor shows 100% (the queue fully drained)."})
            os.makedirs(_run_dir(rid), exist_ok=True)
            argv = build_collect_argv(inv)
            res = run_tool(argv, timeout=3600)
            res["argv_str"] = " ".join(_q(x) for x in argv)
            if res.get("ok"):
                res["rows_written"] = _row_count(inv) if os.path.isfile(inv) else 0
                m = jload(meta_path(rid)) or {}
                if m:
                    m.update({"status": "collected", "collected_at": now_iso()})
                    jdump(meta_path(rid), m)
            audit("collect", run_id=rid, ok=res.get("ok"), rows_written=res.get("rows_written"))
            return self._json(res)

        if route in ("/api/report", "/api/sample", "/api/benchmark"):
            if not meta:
                return self._json({"ok": False, "out": "unknown run"}, 404)
            inv = meta.get("inventory")
            if not (inv and os.path.isfile(inv)):
                return self._json({"ok": False, "out": "this run has no inventory yet — Collect it first"})
            d = _run_dir(rid)

        if route == "/api/report":
            out_csv = os.path.join(d, "table1.csv")
            argv = build_report_argv(inv, out_csv)
            res = run_tool(argv)
            res["argv_str"] = " ".join(_q(x) for x in argv)
            res["rows"] = read_table_csv(out_csv) if os.path.isfile(out_csv) else []
            audit("report", run_id=rid, ok=res.get("ok"), rows=len(res["rows"]))
            return self._json(res)

        if route == "/api/sample":
            out_csv = os.path.join(d, "sample.csv")
            rate = b.get("rate", 0.05)
            seed = b.get("seed", 12345)
            argv = build_sample_argv(inv, out_csv, rate, seed)
            res = run_tool(argv)
            res["argv_str"] = " ".join(_q(x) for x in argv)
            res["rows"] = read_table_csv(out_csv) if os.path.isfile(out_csv) else []
            # the seed makes the sample reproducible — record it with the run
            if not meta.get("external"):
                m = jload(meta_path(rid)) or {}
                m["sample"] = {"rate": rate, "seed": seed, "at": now_iso(), "by": whoami()}
                jdump(meta_path(rid), m)
            audit("sample", run_id=rid, ok=res.get("ok"), rate=rate, seed=seed, rows=len(res["rows"]))
            return self._json(res)

        if route == "/api/benchmark":
            gold = (b.get("gold") or "").strip().strip('"')
            if not os.path.isfile(gold):
                return self._json({"ok": False, "out": "reviewed-truth file not found: " + gold})
            argv = build_benchmark_argv(inv, gold, b.get("id_col"), b.get("responsive_col"),
                                        b.get("bde_col"), b.get("sheet"),
                                        absent_means=(b.get("absent_means") or "unreviewed"),
                                        bde_threshold=b.get("bde_threshold"))
            res = run_tool(argv)
            res["argv_str"] = " ".join(_q(x) for x in argv)
            try:
                with open(os.path.join(d, "benchmark.txt"), "w", encoding="utf-8") as fh:
                    fh.write(res.get("out", ""))
            except OSError:
                pass
            audit("benchmark", run_id=rid, ok=res.get("ok"))
            return self._json(res)

        if route == "/api/score":
            # The real "vs manual review": tools/score_combined.py against the CNG entities export.
            if not meta:
                return self._json({"ok": False, "out": "unknown run"}, 404)
            inv = meta.get("inventory")
            if not (inv and os.path.isfile(inv)):
                return self._json({"ok": False, "out": "this run has no inventory yet — Collect it first"})
            entities = (b.get("entities") or "").strip().strip('"')
            if not os.path.isfile(entities):
                return self._json({"ok": False, "out": "entities export not found: " + entities})
            score_dir = os.path.join(_run_dir(rid), "score")
            os.makedirs(score_dir, exist_ok=True)
            bde = b.get("bde_threshold") or meta.get("bde_threshold")
            argv = build_score_argv(inv, entities, score_dir,
                                    id_col=(b.get("id_col") or None), count_col=(b.get("count_col") or None),
                                    entities_sheet=(b.get("sheet") or None), bde_threshold=bde,
                                    absent_means=(b.get("absent_means") or "auto"))
            res = run_tool(argv, timeout=3600)
            res["argv_str"] = " ".join(_q(x) for x in argv)
            res["summary"] = parse_score_summary(res.get("out", ""))
            try:
                cards = sorted(f for f in os.listdir(score_dir)
                               if f.startswith("scorecard") and f.endswith(".xlsx"))
                res["scorecard"] = os.path.join(score_dir, cards[-1]) if cards else None
            except OSError:
                res["scorecard"] = None
            audit("score", run_id=rid, ok=res.get("ok"),
                 recall=res["summary"].get("recall"), precision=res["summary"].get("precision"))
            return self._json(res)

        if route == "/api/compare":
            ra, rb = resolve_run(b.get("a") or ""), resolve_run(b.get("b") or "")
            if not (ra and rb):
                return self._json({"ok": False, "out": "pick two runs"})
            ia, ib = ra.get("inventory"), rb.get("inventory")
            if not (ia and os.path.isfile(ia) and ib and os.path.isfile(ib)):
                return self._json({"ok": False, "out": "both runs need an inventory"})
            rows_a, _f, _t = read_inventory(ia)
            rows_b, _f, _t = read_inventory(ib)
            res = compare_rules_decided(rows_a, rows_b)
            res["provenance_diff"] = provenance_diff(ra, rb)
            res["ok"] = True
            res["a_name"] = ra.get("name")
            res["b_name"] = rb.get("name")
            audit("compare", run_a=ra.get("id"), run_b=rb.get("id"), moved=res.get("moved_count"))
            return self._json(res)

        if route == "/api/newrun/check":
            job_dir = (b.get("job_dir") or "").strip().strip('"')
            v = validate_job_dir(job_dir)
            resp = {"validate": v}
            if v.get("files_dir"):
                try:
                    bde = int(b.get("bde_threshold")) if str(b.get("bde_threshold") or "").strip() else None
                except (TypeError, ValueError):
                    bde = None
                job_id_preview = f"{os.path.basename(v['job_dir'])}-<runstamp>"
                try:
                    if b.get("mode") == "rescan":
                        prior = resolve_run(b.get("run_id") or "")
                        prior_inv = prior.get("inventory") if prior else None
                        excl = [x.strip() for x in (b.get("exclude_lanes") or "likely_non_responsive").split(",") if x.strip()]
                        keep = (rescan_keep_count(prior_inv, set(excl))
                                if prior_inv and os.path.isfile(prior_inv) else None)
                        argv = build_enqueue_argv(v["files_dir"], job_id_preview, prior_inv, excl, bde_threshold=bde)
                        resp["rescan"] = {"keep": keep, "inventory": prior_inv, "exclude_lanes": excl,
                                          "prior_name": (prior or {}).get("name")}
                    else:
                        check_result = check_corpus(v["files_dir"], use_cache=not b.get("no_count", False))
                        resp["check"] = check_result
                        argv = build_enqueue_argv(v["files_dir"], job_id_preview, bde_threshold=bde)
                    resp["argv_str"] = " ".join(_q(x) for x in argv)
                except Exception as e:
                    import traceback
                    sys.stderr.write(f"[ERROR] /api/newrun/check: {type(e).__name__}: {e}\n")
                    traceback.print_exc(file=sys.stderr)
                    resp["error"] = f"{type(e).__name__}: {e}"
            return self._json(resp)

        if route == "/api/newrun/submit":
            excl = [x.strip() for x in (b.get("exclude_lanes") or "").split(",") if x.strip()]
            return self._json(submit_run(
                (b.get("job_dir") or "").strip().strip('"'), mode=b.get("mode", "job"),
                name=(b.get("name") or "").strip(), rescan_run_id=b.get("run_id"),
                exclude_lanes=excl or None, bde_threshold=b.get("bde_threshold")))

        if route == "/api/stage/start":
            return self._json(stage_start(
                (b.get("src") or "").strip().strip('"'),
                (b.get("dest_job_dir") or "").strip().strip('"'),
                (b.get("protocol_src") or "").strip().strip('"') or None))

        if route == "/api/reset":
            return self._json(archive_and_reset(rid, override=bool(b.get("override")),
                                                typed=b.get("typed") or ""))

        return self._json({"error": "not found"}, 404)

    def _export(self, rid: str):
        meta = resolve_run(rid)
        if not meta:
            return self._json({"error": "unknown run"}, 404)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("run.json", json.dumps(meta, indent=1))
            inv = meta.get("inventory")
            if inv and os.path.isfile(inv):
                rows, _f, _t = read_inventory(inv)
                z.writestr("metrics_snapshot.json", json.dumps(summarize_inventory(rows), indent=1))
                # inventory is labels/counts only (no PII), safe to include
                with open(inv, "rb") as fh:
                    z.writestr("inventory.csv", fh.read())
            d = _run_dir(rid)
            for name in ("table1.csv", "sample.csv", "benchmark.txt"):
                path = os.path.join(d, name)
                if os.path.isfile(path):
                    with open(path, "rb") as fh:
                        z.writestr(name, fh.read())
        body = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{rid.replace(":", "_")}_export.zip"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def meta_path(rid: str) -> str:
    return os.path.join(RUNS, rid, "run.json")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    ports = [a for a in argv if a.isdigit()]
    port = int(ports[0]) if ports else free_port()
    no_open = "--no-browser" in argv

    if not os.path.isfile(UI_HTML):
        sys.stderr.write(f"ERROR: hwe_scaled_ui.html not found next to this script ({ROOT}).\n")
        return 2
    os.makedirs(RUNS, exist_ok=True)
    n = load_env()      # so the store sees the same .env the CLI would

    url = f"http://127.0.0.1:{port}/"
    srv = Server(("127.0.0.1", port), H)      # loopback only, never the network
    sys.stderr.write(f"HWE Runner — Scaled — {url}\n  serving from {ROOT}\n"
                     f"  {'loaded ' + str(n) + ' setting(s) from .env' if n else 'no .env loaded'}"
                     f" · Ctrl-C to stop\n")
    if not no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nstopping.\n")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
