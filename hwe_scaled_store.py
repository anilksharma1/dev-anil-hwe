"""Store-access layer for the scaled HWE Runner.

Reads the Azure Table + queues through scaling_lib's OWN helpers — it never re-implements table or
queue access, so the UI's numbers cannot drift from `scaling-lib status` / `collect_outputs.py`
(the same reuse discipline as the local build's peek_columns-inside-the-scorer rule). scaling_lib
is imported lazily inside each function, so the read-only screens run even where scaling_lib / the
Azure SDK are absent; nothing here touches Azure until a function is actually called.

What is and isn't reconstructable is fixed by SCALED_UI_FINDINGS.md:
  - per-file rows carry status, timestamps, worker_instance, attempt_count, tokens_in/out, and a
    checkpoints JSON (so rate-limit wait and a stage split ARE available);
  - there are NO per-call timestamps, so concurrency is a TASK-level sweep, never per-call;
  - run_metrics() only ever targets the latest job, so for any specific run we query the partition
    (PartitionKey == job_id) ourselves and reuse scaling_lib's RunMetrics dataclass to aggregate.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone

# During local dev scaling_lib may not be pip-installed; point SCALING_LIB_SRC at a checkout.
_SL_SRC = os.environ.get("SCALING_LIB_SRC")
if _SL_SRC and os.path.isdir(_SL_SRC) and _SL_SRC not in sys.path:
    sys.path.insert(0, _SL_SRC)


# ── env-var specification for the Setup screen ────────────────────────────────
# Split by WHO reads it (findings §7): the ops-VM UI host and the worker containers need different
# sets. Flagging a worker-only var as "missing" on the ops VM would be a false alarm, so those are
# marked informational. Values are never shown — only set/not-set.
ENV_SPEC = {
    "ops_required": [
        ("INPUT_MOUNT", "Where corpora live; enqueue paths are resolved against it."),
        ("OUTPUT_MOUNT", "Where workers write result.json; collect reads from here."),
        ("JOB_TYPE", "Stable job identifier baked into the image; part of every task's RowKey."),
        ("AZURE_TABLE_NAME", "The status table the whole UI reads."),
        ("AZURE_QUEUE_NAME", "The Linux work queue."),
        ("AZURE_DEAD_LETTER_QUEUE_NAME", "Where exhausted-retry tasks land; the failures view reads it."),
    ],
    "ops_storage_endpoint": [
        ("AZURE_STORAGE_CONNECTION_STRING", "Local/Azurite auth (use this OR the two URLs below)."),
        ("AZURE_STORAGE_TABLE_URL", "Production table endpoint (managed identity)."),
        ("AZURE_STORAGE_QUEUE_URL", "Production queue endpoint (managed identity)."),
    ],
    "ops_optional": [
        ("AZURE_WINDOWS_QUEUE_NAME", "Windows conversion queue; without it .doc/.xls/.ppt stay on the main queue."),
        ("WINDOWS_FILE_EXTENSIONS", "Overrides which extensions route to Windows (default .doc,.xls,.ppt)."),
        ("AZURE_CREDENTIAL_TYPE", "Set to 'cli' to force AzureCliCredential for local dev."),
    ],
    "build_deploy": [
        ("ACR_REGISTRY", "Azure Container Registry name."),
        ("ACR_IMAGE", "Image name."),
        ("AZURE_CONTAINER_APP", "Container App to deploy to."),
        ("AZURE_RESOURCE_GROUP", "Resource group of the Container App."),
    ],
    "worker_side": [   # informational on the ops VM — injected into the Container App at runtime
        ("USE_LLM", "Worker: enable Azure OpenAI classification."),
        ("USE_OCR", "Worker: enable Document Intelligence OCR."),
        ("USE_NER", "Worker: enable spaCy NER."),
        ("AZURE_OPENAI_ENDPOINT", "Worker: Azure OpenAI endpoint."),
        ("AZURE_OPENAI_DEPLOYMENT", "Worker: Azure OpenAI deployment."),
        ("AZURE_DI_ENDPOINT", "Worker: Document Intelligence endpoint."),
        ("BDE_THRESHOLD", "Worker: entity count for BDE routing."),
        ("RULEPACK_PATH", "Worker: custom Master List (unset = built-in)."),
    ],
}


def _isset(name: str) -> bool:
    return bool(os.environ.get(name))


def env_report() -> dict:
    """set/not-set for every variable the code actually reads, grouped by host. Never values."""
    def group(items):
        return [{"key": k, "set": _isset(k), "purpose": p} for k, p in items]
    storage_ok = _isset("AZURE_STORAGE_CONNECTION_STRING") or (
        _isset("AZURE_STORAGE_TABLE_URL") and _isset("AZURE_STORAGE_QUEUE_URL"))
    return {
        "ops_required": group(ENV_SPEC["ops_required"]),
        "ops_storage_endpoint": group(ENV_SPEC["ops_storage_endpoint"]),
        "storage_endpoint_ok": storage_ok,
        "ops_optional": group(ENV_SPEC["ops_optional"]),
        "build_deploy": group(ENV_SPEC["build_deploy"]),
        "worker_side": group(ENV_SPEC["worker_side"]),
    }


# ── scaling_lib availability / storage mode ───────────────────────────────────
def scaling_lib_status() -> dict:
    try:
        import scaling_lib  # noqa: F401
        from scaling_lib._version import get_commit_hash
        try:
            commit = get_commit_hash()
        except Exception:
            commit = None
        return {"ok": True, "detail": f"import OK (commit {commit})" if commit else "import OK"}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


def storage_mode() -> str | None:
    if _isset("AZURE_STORAGE_CONNECTION_STRING"):
        return "connection-string (local / Azurite)"
    if _isset("AZURE_STORAGE_TABLE_URL") and _isset("AZURE_STORAGE_QUEUE_URL"):
        return "managed-identity (DefaultAzureCredential)"
    return None


def credential_probe() -> dict:
    """Report whether we can authenticate to storage. Connection-string mode is self-evidently fine;
    managed-identity mode attempts a real token acquisition (this is the scaled 'az login valid?')."""
    mode = storage_mode()
    if mode is None:
        return {"ok": False, "mode": None, "detail": "no storage endpoint configured (§ Setup)"}
    if mode.startswith("connection-string"):
        return {"ok": True, "mode": mode, "detail": "using a storage connection string; no login required"}
    try:
        from scaling_lib._config import _credential
        cred = _credential()
        cred.get_token("https://storage.azure.com/.default")
        return {"ok": True, "mode": mode, "detail": "acquired a storage token"}
    except Exception as exc:
        return {"ok": False, "mode": mode,
                "detail": f"token acquisition failed — is `az login` valid? {type(exc).__name__}: {exc}"}


def check_table() -> dict:
    try:
        from scaling_lib.status import _get_table_client
        client = _get_table_client()
        next(iter(client.list_entities(results_per_page=1)), None)   # a cheap round-trip
        return {"ok": True, "detail": f"reachable ({os.environ.get('AZURE_TABLE_NAME','?')})"}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


def check_queues() -> dict:
    try:
        counts = queue_counts()["counts"]
        parts = [f"main={counts.get('queue_count')}", f"dlq={counts.get('dead_letter_count')}"]
        if "windows_queue_count" in counts:
            parts.append(f"windows={counts['windows_queue_count']}")
        return {"ok": True, "detail": " · ".join(parts)}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


def check_mounts() -> dict:
    out = {}
    for key in ("INPUT_MOUNT", "OUTPUT_MOUNT"):
        path = os.environ.get(key, "")
        if not path:
            out[key] = {"ok": False, "detail": "not set"}
        elif not os.path.isdir(path):
            out[key] = {"ok": False, "detail": f"not a readable directory: {path}"}
        else:
            writable = os.access(path, os.W_OK)
            out[key] = {"ok": True, "writable": writable,
                        "detail": f"{path} ({'writable' if writable else 'read-only'})"}
    return out


# ── queue + table reads ───────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def queue_counts() -> dict:
    """scaling_lib.queue.queue_status(), stamped with the reading time. Counts are APPROXIMATE and
    exclude in-flight (invisible) messages — the table is the precise progress signal."""
    from scaling_lib.queue import queue_status
    return {"counts": queue_status(), "at": _now_iso(), "approximate": True}


def _entities_for(job_id: str) -> list:
    from scaling_lib.status import _get_table_client
    return list(_get_table_client().query_entities(f"PartitionKey eq '{job_id}'"))


def list_jobs() -> list:
    """Every job (PartitionKey) in the table, with per-status counts. This is how the Runs screen
    learns about runs the UI didn't start (findings §4.2: jobs ARE isolated by job_id)."""
    from scaling_lib.status import _get_table_client
    from collections import Counter
    client = _get_table_client()
    jobs: dict[str, dict] = {}
    for e in client.list_entities(select=["PartitionKey", "status", "enqueued_at"]):
        jid = e.get("PartitionKey", "")
        j = jobs.setdefault(jid, {"job_id": jid, "total": 0, "counts": Counter(),
                                  "enqueued_min": None, "enqueued_max": None})
        j["total"] += 1
        j["counts"][e.get("status", "")] += 1
        eq = e.get("enqueued_at")
        if eq is not None:
            j["enqueued_min"] = eq if j["enqueued_min"] is None else min(j["enqueued_min"], eq)
            j["enqueued_max"] = eq if j["enqueued_max"] is None else max(j["enqueued_max"], eq)
    out = []
    for j in jobs.values():
        j["counts"] = dict(j["counts"])
        for k in ("enqueued_min", "enqueued_max"):
            v = j[k]
            j[k] = v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)
        out.append(j)
    out.sort(key=lambda x: x["enqueued_max"] or "", reverse=True)
    return out


def latest_job_id() -> str | None:
    jobs = list_jobs()
    return jobs[0]["job_id"] if jobs else None


def _epoch(dt) -> float | None:
    if dt is None:
        return None
    if hasattr(dt, "timestamp"):
        return dt.timestamp()
    try:
        return datetime.fromisoformat(str(dt).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# A legacy Windows-leg file (worker.py's _convert_and_forward) produces TWO Table rows
# under the same job_id: the pre-conversion row (file_name="report.doc") and, once COM
# conversion succeeds, a second post-conversion row forwarded to the Linux queue
# (file_name="report.docx") that carries the real processing outcome/tokens/timing.
# Counting both inflates "total files" / "completed" by one extra row per legacy file.
_LEGACY_EXT_MAP = {".doc": ".docx", ".xls": ".xlsx", ".ppt": ".pptx"}


def _collapse_legacy_pairs(ents: list) -> list:
    """Collapse a Windows-leg pre-conversion row into its post-conversion counterpart so
    per-state counts reflect actual input files, not Table rows (findings §6: "a Windows
    .doc/.xls/.ppt produces two Table rows"). Table-only (no filesystem read, so this stays
    cheap on every ~3s Monitor poll): the converted file_name is deterministic (stem +
    docx/xlsx/pptx, matching conversion.py's _EXT_MAP), so a pair is recognised purely from
    file_name -- if a legacy row's converted-name counterpart is ALSO present in this job,
    the legacy row is dropped (the converted row is that file's true current state: still
    pending/processing on the Linux leg, or completed/failed). A legacy row with NO
    converted counterpart yet (conversion hasn't forwarded, or never will) is kept as-is --
    it IS the file's current state.

    Heuristic, like the "approximate" queue counts elsewhere in this module: a corpus that
    happens to contain BOTH "x.doc" and an unrelated, genuinely distinct "x.docx" as separate
    original files would see the ".doc" row collapsed away too. This trades a rare
    false-collapse for a cheap, no-I/O check on every live poll; collect_outputs.py's
    inventory (the deliverable) reconciles this authoritatively via the on-disk
    forwarded.json/.orig.json sidecars instead.
    """
    file_names = {e.get("file_name", "") for e in ents}
    out = []
    for e in ents:
        name = e.get("file_name", "")
        stem, ext = os.path.splitext(name)
        converted_ext = _LEGACY_EXT_MAP.get(ext.lower())
        if converted_ext and (stem + converted_ext) in file_names:
            continue  # this file's true state is the converted row, counted separately
        out.append(e)
    return out


def _live_processing(ents: list) -> list:
    """Every task currently claimed by a worker (status == 'processing'), with which
    worker, which attempt, and how long it's been running -- e.g. "worker-abc123 ·
    report.pdf · attempt 3 · 40s". Complements stuck_from_entities (which only surfaces
    tasks OVER the visibility timeout) with the full live picture; longest-running first."""
    now = datetime.now(timezone.utc).timestamp()
    out = []
    for e in ents:
        if e.get("status") != "processing":
            continue
        started = _epoch(e.get("started_at"))
        out.append({
            "file_name": e.get("file_name", ""),
            "worker_instance": e.get("worker_instance", ""),
            "attempt_count": int(e.get("attempt_count") or 1),
            "elapsed_s": int(now - started) if started is not None else None,
        })
    out.sort(key=lambda r: (r["elapsed_s"] is None, -(r["elapsed_s"] or 0)))
    return out


def job_metrics(job_id: str, visibility_timeout: int | None = None) -> dict:
    """Aggregate one job by querying its partition and reusing scaling_lib's RunMetrics dataclass —
    the same aggregation `scaling-lib status` uses, but scoped to a chosen job_id instead of only
    the latest. Returns a JSON-safe dict for the monitor screen.
    """
    import json as _json
    from scaling_lib.metrics import RunMetrics, TaskRecord, CheckpointRecord

    ents = _collapse_legacy_pairs(_entities_for(job_id))
    tasks = []
    for e in ents:
        cps = []
        raw = e.get("checkpoints")
        if raw:
            try:
                for cp in _json.loads(raw):
                    cps.append(CheckpointRecord(cp["label"], cp["duration_s"], cp.get("metadata", {})))
            except Exception:
                pass
        tasks.append(TaskRecord(
            file_name=e.get("file_name", ""), job_id=job_id,
            file_size_bytes=e.get("file_size_bytes"),
            started_at=e.get("started_at") or datetime.now(timezone.utc),
            status=e.get("status", ""), processing_s=float(e.get("processing_s") or 0.0),
            attempt_count=int(e.get("attempt_count") or 1),
            worker_instance=e.get("worker_instance", ""),
            enqueued_at=e.get("enqueued_at"), completed_at=e.get("completed_at"),
            checkpoints=cps,
        ))
    m = RunMetrics(tasks=tasks)

    # status breakdown straight off the rows (authoritative, unlike approximate queue counts)
    from collections import Counter
    status_counts = Counter(e.get("status", "") for e in ents)

    checkpoints = {lab: {"count": cp.count, "total_s": round(cp.total_s, 2), "avg_s": round(cp.avg_s, 3),
                         "metadata": cp.metadata} for lab, cp in m.checkpoints.items()}
    rlw = m.checkpoints.get("azure_openai_rate_limit_wait")

    # task-level concurrency series (no per-call timestamps exist — findings Q8)
    intervals = []
    for t in tasks:
        s, e2 = _epoch(t.started_at), _epoch(t.completed_at)
        if s is not None and e2 is not None and e2 >= s:
            intervals.append((s, e2))
    series = concurrency_series(intervals)
    peak_concurrency = max((c for _, c in series), default=0)

    # Throughput over a TRAILING WINDOW (findings §6.4), not done/total-wall-clock — the latter reads
    # as absurd early on and hides a slowdown. Guarded so a run with no recent completions (or too
    # little elapsed time to measure) reports None -> "not measured", never a false rate.
    import time as _time
    now = _time.time()
    comp = sorted(x for x in (_epoch(t.completed_at) for t in tasks if t.status == "completed")
                  if x is not None)
    throughput_fpm = None
    eta_range_min = None
    remaining = m.files_pending + m.files_processing
    if comp:
        window_start = max(comp[0], now - 300)          # last 5 min, or since first completion
        span_s = now - window_start
        in_window = sum(1 for c in comp if c >= window_start)
        if span_s >= 10 and in_window >= 1:
            fpm = in_window / (span_s / 60.0)
            throughput_fpm = round(fpm, 1)
            if remaining and fpm > 0:
                eta = remaining / fpm
                eta_range_min = [round(eta * 0.7), round(eta * 1.4)]   # a range, not a point

    stuck = stuck_from_entities(ents, visibility_timeout) if visibility_timeout else []
    processing = _live_processing(ents)

    return {
        "job_id": job_id,
        "total": len(ents),
        "status_counts": dict(status_counts),
        "processing_tasks": processing,
        "files_completed": m.files_completed, "files_processing": m.files_processing,
        "files_pending": m.files_pending, "files_failed": m.files_failed,
        "files_retried": m.files_retried, "total_extra_attempts": m.total_extra_attempts,
        "total_bytes": m.total_bytes, "avg_processing_s": m.avg_processing_s,
        "wall_clock_s": m.wall_clock_s, "worker_count": m.worker_count,
        "workers": sorted({t.worker_instance for t in tasks if t.worker_instance}),
        "tokens_in": m.total_tokens_in, "tokens_out": m.total_tokens_out,
        "checkpoints": checkpoints,
        "rate_limit_wait_s": round(rlw.total_s, 1) if rlw else None,   # None => "not measured"
        "peak_concurrency": peak_concurrency,
        "throughput_fpm": throughput_fpm, "eta_range_min": eta_range_min,
        "stuck_count": len(stuck), "stuck": stuck,
        "at": _now_iso(),
    }


def stuck_from_entities(ents, visibility_timeout: int) -> list:
    """Tasks claimed (processing) but not completed for longer than the visibility timeout — a
    crashed worker's claim that must expire, or those documents are lost silently (§6.4)."""
    import time
    now = time.time()
    out = []
    for e in ents:
        if e.get("status") != "processing":
            continue
        started = _epoch(e.get("started_at"))
        if started is not None and (now - started) > visibility_timeout:
            out.append({"file_name": e.get("file_name", ""),
                        "age_s": int(now - started),
                        "worker_instance": e.get("worker_instance", "")})
    return out


def failures(job_id: str) -> list:
    """Failed / dead-lettered rows — file id + error class only, never document text or PII (§6.4)."""
    out = []
    for e in _entities_for(job_id):
        if e.get("status") in ("failed", "dead_lettered"):
            out.append({"file_name": e.get("file_name", ""), "status": e.get("status"),
                        "attempt_count": int(e.get("attempt_count") or 1),
                        "error_message": e.get("error_message", "")})
    return out


# ── pure helper: task-level concurrency sweep ─────────────────────────────────
def concurrency_series(intervals) -> list:
    """Sweep [start,end) intervals into a (t, active) series. CLOSE BEFORE OPEN at equal timestamps
    (findings §7): back-to-back tasks (end_i == start_{i+1}) then read as sequential, not concurrent
    — otherwise six sub-millisecond calls in one recorded instant inflate to six 'concurrent'. Each
    interval must have start < end (real durations); a zero-length interval contributes nothing.
    """
    events = []
    for s, e in intervals:
        if e <= s:
            continue
        events.append((s, 1))     # open
        events.append((e, -1))    # close
    # -1 sorts before +1 at equal t, so a close is applied before an open at the same instant
    events.sort(key=lambda x: (x[0], x[1]))
    cur, series = 0, []
    for t, d in events:
        cur += d
        series.append((t, cur))
    return series


# ── archive (for the archive-before-reset guard, §6.8) ────────────────────────
def archive_job(job_id: str, dest_dir: str) -> dict:
    """Snapshot every table row for a job (and a metrics aggregate) to local disk, then VERIFY the
    snapshot is readable. This is the durable record `scaling-lib reset` is about to destroy, so the
    caller must not proceed to reset unless this returns verified=True.
    """
    import json as _json
    os.makedirs(dest_dir, exist_ok=True)
    ents = _entities_for(job_id)
    rows_path = os.path.join(dest_dir, "status_rows.jsonl")
    tmp = rows_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for e in ents:
            fh.write(_json.dumps({k: (v.isoformat() if hasattr(v, "isoformat") else v)
                                  for k, v in dict(e).items()}) + "\n")
    os.replace(tmp, rows_path)

    metrics_path = os.path.join(dest_dir, "metrics_snapshot.json")
    try:
        m = job_metrics(job_id)
    except Exception as exc:
        m = {"error": f"{type(exc).__name__}: {exc}"}
    with open(metrics_path, "w", encoding="utf-8") as fh:
        _json.dump(m, fh, indent=1)

    # verify: re-read the rows file and confirm the count matches what we wrote
    verified, verify_detail = False, ""
    try:
        with open(rows_path, "r", encoding="utf-8") as fh:
            read_back = sum(1 for _ in fh)
        verified = (read_back == len(ents))
        verify_detail = f"wrote {len(ents)} rows, re-read {read_back}"
    except OSError as exc:
        verify_detail = f"could not re-read snapshot: {exc}"
    return {"job_id": job_id, "rows_path": rows_path, "metrics_path": metrics_path,
            "count": len(ents), "verified": verified, "detail": verify_detail}


# ── one-run lock support + reset ──────────────────────────────────────────────
def job_open_count(job_id: str) -> int:
    """Pending + processing rows for a job — the MEASURED signal for 'is this run still active'
    (findings §4.1: status is measured, never assumed). A light select-only query."""
    from scaling_lib.status import _get_table_client
    n = 0
    for e in _get_table_client().query_entities(f"PartitionKey eq '{job_id}'", select=["status"]):
        if e.get("status") in ("pending", "processing"):
            n += 1
    return n


def run_reset() -> dict:
    """Clear the status table and purge every queue — the exact code path `scaling-lib reset` runs
    (clear_all_tasks + clear_all_queues). DESTRUCTIVE and table-wide; the caller MUST have archived
    and verified first (§6.8). Returns the deleted row count."""
    from scaling_lib.status import clear_all_tasks
    from scaling_lib.queue import clear_all_queues
    deleted = clear_all_tasks()
    clear_all_queues()
    return {"deleted": deleted}


# ── provenance inputs the ops VM can answer ───────────────────────────────────
def run_cli(cmd: list, timeout: int = 60):
    """Run an external CLI, capturing output. On Windows, tools installed as .cmd/.bat batch files
    — notably `az` — can't be launched from a bare argv list (CreateProcess only appends .exe), so
    run them through the shell there, exactly as scaling_lib's own _run does. POSIX runs the list
    directly (no shell, no quoting surprises)."""
    if sys.platform == "win32":
        return subprocess.run(subprocess.list2cmdline(cmd), shell=True,
                              capture_output=True, text=True, timeout=timeout)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def git_sha() -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)))
        return r.stdout.strip() if r.returncode == 0 else None
    except FileNotFoundError:
        return None


def deployed_image_tag(app: str | None = None, resource_group: str | None = None) -> dict:
    """The image tag currently running on the Container App, via `az`. The single most useful Setup
    readout — 'the code in this tree is not what the workers are running.' Needs `az` (ops VM only)."""
    app = app or os.environ.get("AZURE_CONTAINER_APP")
    rg = resource_group or os.environ.get("AZURE_RESOURCE_GROUP")
    if not (app and rg):
        return {"ok": False, "detail": "AZURE_CONTAINER_APP / AZURE_RESOURCE_GROUP not set"}
    try:
        r = run_cli(
            ["az", "containerapp", "show", "--name", app, "--resource-group", rg,
             "--query", "properties.template.containers[0].image", "-o", "tsv"], timeout=60)
    except FileNotFoundError:
        return {"ok": False, "detail": "az CLI not found on this host (ops VM only)"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "az timed out"}
    if r.returncode != 0:
        return {"ok": False, "detail": (r.stderr or "az error").strip()[:300]}
    image = r.stdout.strip()
    tag = image.rsplit(":", 1)[-1] if ":" in image else image
    return {"ok": True, "image": image, "tag": tag}
