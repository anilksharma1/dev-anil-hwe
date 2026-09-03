"""Enqueue a job's files/ folder for processing, optionally filtered to a subset.

    python enqueue.py /mnt/input/<job_dir>/files
    python enqueue.py /mnt/input/<job_dir>/files --inventory inventory.csv

Without --inventory, enqueues every file under files_dir. With --inventory,
enqueues only the files whose `suggested_lane` (from a prior inventory.csv,
via collect_outputs.py) isn't in --exclude-lanes -- e.g. to rescan the
responsive/unresolved subset with USE_LLM/USE_OCR on, instead of resubmitting
the whole corpus. Workers only ever see what's already on the queue, so this
filtering has to happen at enqueue time, not inside worker.py's process().

scaling_lib's own enqueue() mints a fresh random job_id for each single file,
which would scatter a filtered subset across many one-file "batches" in
`scaling-lib status`. This instead builds the queue messages directly (the
same building blocks worker.py's Windows-leg forwarding uses) under one
shared job_id, so the whole run shows as a single batch -- and streams the
directory walk instead of materializing it, so enqueueing starts on the first
file instead of waiting out a full walk of a few-hundred-thousand-file corpus.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "pii_triage_merged"))


def _iter_files(root: str):
    """Yield file paths as os.walk discovers them.

    Unlike pii_triage.runner.discover_files, this doesn't materialize the
    whole tree before returning -- for a few-hundred-thousand-file corpus on
    a network mount, that full walk is itself slow, and nothing would get
    enqueued until it finished. Streaming lets filtering/sending start on the
    first matched file instead of waiting on the whole walk.
    """
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            yield os.path.join(dirpath, name)


def _chunked(iterable, size: int):
    """Group a streamed iterable into lists of up to `size`, without materializing the
    whole thing -- keeps enqueue's memory bounded on a few-hundred-thousand-file corpus
    while still letting each chunk submit concurrently."""
    chunk = []
    for item in iterable:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _enqueue_one(f: str, input_mount: str, keep, job_id: str, job_type: str,
                 main_client, win_client) -> bool:
    """Filter + submit ONE file's init_task + send_message. Returns True if it was actually
    enqueued (False if filtered out by --inventory/--exclude-lanes). Raises on a genuine
    Table/Queue failure -- the caller isolates that to this one file, not the whole run."""
    from scaling_lib.status import init_task
    from scaling_lib.queue import _build_message, _is_windows_file

    rel = Path(f).relative_to(input_mount)
    if keep is not None and rel.as_posix() not in keep:
        return False
    init_task(job_id, job_type, os.path.basename(f))
    is_win = bool(win_client and _is_windows_file(Path(f)))
    target = win_client if is_win else main_client
    target.send_message(_build_message(rel, job_id, job_type, posix=not is_win))
    return True


def enqueue(files_dir: str, inventory_csv: str | None = None, exclude_lanes: set | None = None,
            job_id: str | None = None, bde_threshold: int | None = None,
            concurrency: int = 32, batch_size: int = 500) -> int:
    """Enqueue every matching file, concurrently, in bounded batches.

    A serial loop here means TWO network round-trips per file (init_task, send_message) --
    for a few-hundred-thousand-file corpus that dominates enqueue wall time far more than the
    directory walk itself. Chunking the streamed walk into batches and submitting each batch
    through a thread pool (I/O-bound work, same reasoning as collect_outputs.py's own
    ThreadPoolExecutor) cuts that down by roughly `concurrency`, while still bounding memory
    to one batch at a time and reporting progress as it goes -- rather than materializing the
    whole corpus list upfront, which would also defeat the point of the streaming walk below.
    One bad file's Table/Queue error is isolated to that file (logged, skipped) rather than
    aborting the entire enqueue.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from scaling_lib.queue import _ensure_queues, _ensure_table, _get_queue_service

    _ensure_queues()
    _ensure_table()

    files_dir = os.path.abspath(files_dir)
    if Path(files_dir).name != "files":
        sys.stderr.write(f"warning: {files_dir} is not named 'files' -- "
                          "the worker's protocol lookup expects <job_dir>/files/.\n")

    # Per-job BDE threshold: write it as a sibling of files/ (never enqueued), where the worker
    # reads it per job dir. Omitting --bde-threshold leaves any existing file and the workers fall
    # back to their BDE_THRESHOLD env default.
    if bde_threshold is not None:
        job_dir = Path(files_dir).parent
        try:
            (job_dir / "pii_job.json").write_text(
                json.dumps({"bde_threshold": int(bde_threshold)}), encoding="utf-8")
            sys.stderr.write(f"wrote {job_dir / 'pii_job.json'} (bde_threshold={int(bde_threshold)})\n")
        except OSError as exc:
            sys.stderr.write(f"warning: could not write per-job config ({exc}); "
                             "workers will use the BDE_THRESHOLD env default\n")

    input_mount = os.environ["INPUT_MOUNT"]
    job_type = os.environ["JOB_TYPE"]
    win_q_name = os.environ.get("AZURE_WINDOWS_QUEUE_NAME")

    keep = None
    if inventory_csv:
        from pii_triage.runner import load_filter_set
        keep = load_filter_set(inventory_csv, exclude_lanes or set())

    suffix = "rescan" if keep is not None else "job"
    job_id = job_id or f"{Path(files_dir).parent.name}-{suffix}-{uuid.uuid4().hex[:8]}"

    service = _get_queue_service()
    main_client = service.get_queue_client(os.environ["AZURE_QUEUE_NAME"])
    win_client = service.get_queue_client(win_q_name) if win_q_name else None

    seen = 0
    matched = 0
    failed = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for batch in _chunked(_iter_files(files_dir), batch_size):
            seen += len(batch)
            futures = {
                pool.submit(_enqueue_one, f, input_mount, keep, job_id, job_type,
                           main_client, win_client): f
                for f in batch
            }
            for fut in as_completed(futures):
                f = futures[fut]
                try:
                    if fut.result():
                        matched += 1
                except Exception as exc:
                    failed.append((f, exc))
            sys.stderr.write(f"  ...{seen} discovered, {matched} enqueued so far\n")

    if failed:
        sys.stderr.write(f"warning: {len(failed)} file(s) failed to enqueue:\n")
        for f, exc in failed[:10]:
            sys.stderr.write(f"  {f}: {type(exc).__name__}: {exc}\n")
        if len(failed) > 10:
            sys.stderr.write(f"  ... and {len(failed) - 10} more\n")

    if matched == 0:
        sys.stderr.write("no files matched the filter -- nothing enqueued.\n")
        return 0

    if keep is not None:
        sys.stderr.write(
            f"enqueued {matched}/{seen} file(s) discovered "
            f"(excluding lanes {sorted(exclude_lanes or set())}) under job_id={job_id}\n")
    else:
        sys.stderr.write(f"enqueued {matched} file(s) under job_id={job_id}\n")
    return matched


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Enqueue a job's files/ folder for processing, optionally filtered to a subset.")
    p.add_argument("files_dir", help="Path to the job's files/ folder (under INPUT_MOUNT)")
    p.add_argument("--inventory", dest="inventory_csv", default=None,
                   help="Prior inventory CSV (from collect_outputs.py) to filter against. "
                        "Omit to enqueue every file under files_dir.")
    p.add_argument("--exclude-lanes", default="likely_non_responsive",
                   help="Comma-separated suggested_lane values to skip (only applies with --inventory; "
                        "default: likely_non_responsive)")
    p.add_argument("--job-id", default=None,
                   help="Override the shared batch job_id (default: <job_dir>-rescan|job-<random8>)")
    p.add_argument("--bde-threshold", type=int, default=None,
                   help="Per-job BDE entity threshold for the workers (writes <job_dir>/pii_job.json). "
                        "Omit to use the workers' BDE_THRESHOLD env default.")
    p.add_argument("--concurrency", type=int, default=32,
                   help="Parallel init_task+send_message calls (default: 32) -- raise this on a "
                        "large corpus")
    p.add_argument("--batch-size", type=int, default=500,
                   help="Files per chunk submitted to the thread pool at once (default: 500) -- "
                        "bounds memory on a very large corpus while still overlapping I/O")
    p.add_argument("--env-file", default=".env")
    a = p.parse_args()

    from dotenv import load_dotenv
    load_dotenv(a.env_file)

    exclude = {v.strip() for v in a.exclude_lanes.split(",") if v.strip()}
    enqueue(a.files_dir, a.inventory_csv, exclude, a.job_id, a.bde_threshold,
           a.concurrency, a.batch_size)
