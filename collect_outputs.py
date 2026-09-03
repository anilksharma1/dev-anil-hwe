"""Collect completed scaling-lib worker outputs into a single inventory.csv.

Run after the queue is fully drained (scaling-lib status shows 100% complete):
    python collect_outputs.py
    python collect_outputs.py --out my_inventory.csv

Then proceed with the normal pii_triage pipeline:
    pii_triage report inventory.csv
    pii_triage sample inventory.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import os
import subprocess
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "pii_triage_merged"))


def _read_completed_entity(entity: dict) -> tuple:
    """Read one completed entity's result.json.

    Returns (record, None) on success, (None, None) for an expected Windows-leg
    conversion stub (forwarded.json present -- the real result lands under a
    separate forwarded task), or (None, reason) when the output is genuinely
    missing/unparseable. No shared state is touched, so this is safe to run
    concurrently from a thread pool -- each call only touches its own files.
    """
    from scaling_lib.storage import get_output_dir

    output_dir = get_output_dir(entity["PartitionKey"], entity["file_name"])
    if (output_dir / "forwarded.json").exists():
        return None, None
    result_file = output_dir / "result.json"
    if not result_file.exists():
        return None, str(result_file)
    raw = result_file.read_text(encoding="utf-8").lstrip()
    try:
        data, _ = json.JSONDecoder().raw_decode(raw)
    except json.JSONDecodeError as exc:
        return None, f"{result_file} (parse error: {exc})"
    return data, None


def _fetch_worker_config() -> dict | None:
    """Query Container Apps Management API + Azure Retail Prices API for resource config.

    Reads AZURE_CONTAINER_APP and AZURE_RESOURCE_GROUP from env (already in .env).
    Subscription ID is read from AZURE_SUBSCRIPTION_ID if set, otherwise resolved
    via the ARM subscriptions list (fails loudly if there are multiple subscriptions
    and the env var is not set).

    Returns a dict with vcpu, gb, pricing, and location — or None on any failure.
    Failures are non-fatal: timing snapshot is still written without this section.
    """
    import urllib.request, urllib.parse, json as _j

    app_name = os.environ.get("AZURE_CONTAINER_APP", "").strip()
    rg       = os.environ.get("AZURE_RESOURCE_GROUP", "").strip()
    if not app_name or not rg:
        sys.stderr.write("worker config: AZURE_CONTAINER_APP / AZURE_RESOURCE_GROUP not set\n")
        return None

    try:
        cred_type = os.environ.get("AZURE_CREDENTIAL_TYPE", "").strip().lower()
        if cred_type == "cli":
            from azure.identity import AzureCliCredential
            cred = AzureCliCredential()
        else:
            from azure.identity import DefaultAzureCredential
            cred = DefaultAzureCredential()
        token = cred.get_token("https://management.azure.com/.default").token
    except Exception as exc:
        sys.stderr.write(f"worker config: credential error: {type(exc).__name__}: {exc}\n")
        return None

    headers = {"Authorization": f"Bearer {token}"}

    def _arm_get(url):
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            return _j.loads(r.read())

    # Subscription ID
    sub_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
    if not sub_id:
        try:
            items = _arm_get(
                "https://management.azure.com/subscriptions?api-version=2020-01-01"
            ).get("value", [])
            if len(items) == 1:
                sub_id = items[0]["subscriptionId"]
            elif not items:
                sys.stderr.write("worker config: no Azure subscriptions found\n")
                return None
            else:
                sys.stderr.write(
                    f"worker config: {len(items)} subscriptions found — "
                    "set AZURE_SUBSCRIPTION_ID in .env to select one\n"
                )
                return None
        except Exception as exc:
            sys.stderr.write(f"worker config: subscription lookup failed: {exc}\n")
            return None

    # Container App definition
    try:
        app = _arm_get(
            f"https://management.azure.com/subscriptions/{sub_id}"
            f"/resourceGroups/{rg}/providers/Microsoft.App/containerApps/{app_name}"
            f"?api-version=2024-03-01"
        )
    except Exception as exc:
        sys.stderr.write(f"worker config: Container App fetch failed: {exc}\n")
        return None

    location = app.get("location", "")
    props     = app.get("properties", {})
    containers = props.get("template", {}).get("containers", [])

    vcpu = gb = 0.0
    if containers:
        res = containers[0].get("resources", {})
        try:
            vcpu = float(res.get("cpu", 0) or 0)
        except (TypeError, ValueError):
            pass
        mem_str = str(res.get("memory", "0Gi") or "0Gi").strip()
        try:
            if   mem_str.endswith("Gi"): gb = float(mem_str[:-2])
            elif mem_str.endswith("Mi"): gb = float(mem_str[:-2]) / 1024
            elif mem_str.endswith("G"):  gb = float(mem_str[:-1])
            else:                        gb = float(mem_str)
        except ValueError:
            pass

    workload_profile = (props.get("workloadProfileName") or "Consumption").strip()
    is_dedicated     = workload_profile.lower() not in ("consumption", "")

    # Managed Environment → workload profile type → GPU count
    # The profile type (e.g. "NC8as-T4") lives on the environment, not the container.
    # All current ACA GPU profiles have 1 GPU per replica regardless of vCPU size.
    gpu            = 0
    profile_type   = ""
    env_id = props.get("managedEnvironmentId", "")
    if env_id and is_dedicated:
        try:
            # env_id is a full ARM resource ID; use it directly as the API path base
            env = _arm_get(
                f"https://management.azure.com{env_id}?api-version=2024-03-01"
            )
            for wp in (env.get("properties", {}).get("workloadProfiles") or []):
                if wp.get("name", "").lower() == workload_profile.lower():
                    profile_type = wp.get("workloadProfileType", "")
                    break
            # Detect GPU from profile type name — all current ACA GPU profiles are 1 GPU/replica
            if any(x in profile_type.upper() for x in ("T4", "A100", "A10", "V100")):
                gpu = 1
        except Exception as exc:
            sys.stderr.write(f"worker config: managed environment fetch failed: {exc}\n")

    # Azure Retail Prices API (public — no auth needed).
    # Query without priceType filter — dedicated workload profiles are not priceType=Consumption.
    # Try both common service name variants; log all returned meter names if nothing matched
    # so the caller can see what IS in the API for this region.
    price_vcpu_hr = price_gb_hr = price_gpu_hr = None
    try:
        arm_region = location.lower().replace(" ", "")
        all_items = []
        for svc in ("Azure Container Apps", "Container Apps"):
            filt = urllib.parse.quote(
                f"serviceName eq '{svc}' and armRegionName eq '{arm_region}'"
            )
            req = urllib.request.Request(
                f"https://prices.azure.com/api/retail/prices"
                f"?api-version=2023-01-01-preview&$filter={filt}"
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                all_items = _j.loads(r.read()).get("Items", [])
            if all_items:
                break

        for item in all_items:
            meter = item.get("meterName", "")
            unit  = item.get("unitOfMeasure", "").lower()
            price = float(item.get("retailPrice", 0))
            price_hr = price * 3600 if "second" in unit else price

            if meter == "Standard vCPU Active Usage" and price_vcpu_hr is None:
                price_vcpu_hr = price_hr
            elif meter == "Standard Memory Active Usage" and price_gb_hr is None:
                price_gb_hr = price_hr
            elif gpu and "GPU Usage" in meter and price_gpu_hr is None:
                # Matches "Standard NC T4 v3 GPU Usage" and future GPU profile meters
                price_gpu_hr = price_hr

        if all_items and (price_vcpu_hr is None or price_gb_hr is None
                          or (gpu and price_gpu_hr is None)):
            found = sorted({item.get("meterName", "") for item in all_items})
            sys.stderr.write(
                f"worker config: some prices not matched. Meters returned by API:\n"
                + "".join(f"  {m}\n" for m in found)
            )
    except Exception as exc:
        sys.stderr.write(f"worker config: retail prices unavailable: {exc}\n")

    return {
        "app_name":         app_name,
        "resource_group":   rg,
        "location":         location,
        "vcpu":             vcpu,
        "gb":               gb,
        "gpu":              gpu,
        "profile_type":     profile_type,
        "workload_profile": workload_profile,
        "price_vcpu_hr":    price_vcpu_hr,
        "price_gb_hr":      price_gb_hr,
        "price_gpu_hr":     price_gpu_hr,
    }


def dump_timing(out_path: str) -> bool:
    """Serialize the current run's scaling-lib metrics to <out_path_stem>_timing.json.

    Call immediately after collect() while the job is still the most recent one.
    Returns True on success, False if run_metrics() is unavailable.
    """
    import pathlib

    try:
        from scaling_lib.metrics import run_metrics
        m = run_metrics()
    except Exception as exc:
        sys.stderr.write(f"timing dump skipped: {type(exc).__name__}: {exc}\n")
        return False

    def _dt(v):
        return v.isoformat() if v is not None else None

    tasks_data = []
    for t in m.tasks:
        tasks_data.append({
            "file_name":       t.file_name or "",
            "status":          t.status or "",
            "worker_instance": t.worker_instance or "",
            "started_at":      _dt(t.started_at),
            "completed_at":    _dt(t.completed_at),
            "processing_s":    t.processing_s,
            "attempt_count":   t.attempt_count or 0,
            "tokens_in":       t.tokens_in or 0,
            "tokens_out":      t.tokens_out or 0,
            "checkpoints": [
                {"label": cp.label, "duration_s": cp.duration_s,
                 "metadata": dict(cp.metadata or {})}
                for cp in (t.checkpoints or [])
            ],
        })

    sys.stderr.write("fetching worker resource config... ")
    worker_config = _fetch_worker_config()
    sys.stderr.write("ok\n" if worker_config else "skipped\n")

    snap = {
        "job_id":           getattr(m, "job_id", "") or "",
        "total_tokens_in":  getattr(m, "total_tokens_in",  0) or 0,
        "total_tokens_out": getattr(m, "total_tokens_out", 0) or 0,
        "files_completed":  getattr(m, "files_completed",  0) or 0,
        "files_failed":     getattr(m, "files_failed",     0) or 0,
        "files_retried":    getattr(m, "files_retried",    0) or 0,
        "wall_clock_s":     getattr(m, "wall_clock_s",  None),
        "worker_count":     getattr(m, "worker_count",    0) or 0,
        "total_bytes":      getattr(m, "total_bytes",     0) or 0,
        "worker_config":    worker_config,
        "tasks":            tasks_data,
    }

    snap_path = pathlib.Path(out_path).stem
    snap_path = pathlib.Path(out_path).parent / (snap_path + "_timing.json")
    snap_path.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    sys.stderr.write(f"timing snapshot  -> {snap_path}  ({len(tasks_data)} tasks)\n")
    return True


def collect(out_path: str = "inventory.csv", concurrency: int = 32) -> int:
    """Collect all currently-completed outputs into out_path in one pass.

    Meant to be run after the queue is fully drained.

    Reads result.json files concurrently: each one is a small file on
    (typically) a network-mounted volume, so wall time is dominated by
    per-file round-trip latency, not CPU -- a thread pool cuts that down by
    roughly the concurrency factor since the reads aren't CPU-bound.
    """
    from concurrent.futures import ThreadPoolExecutor
    from scaling_lib.status import _fetch_entities
    from pii_triage.routing import FIELDNAMES

    t0 = time.monotonic()
    entities = list(_fetch_entities(status_filter="completed"))
    missing = []
    rows = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for data, reason in pool.map(_read_completed_entity, entities):
            if data is not None:
                rows.append(data)
            elif reason is not None:
                missing.append(reason)

    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES,
                                extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(rows)

    if missing:
        sys.stderr.write(f"warning: {len(missing)} output(s) not found:\n")
        for m in missing[:10]:
            sys.stderr.write(f"  {m}\n")
        if len(missing) > 10:
            sys.stderr.write(f"  ... and {len(missing) - 10} more\n")

    elapsed_s = time.monotonic() - t0
    sys.stderr.write(f"collected {len(rows)} records -> {out_path}  ({elapsed_s:.1f}s)\n")
    sys.stderr.write(f"next: pii_triage report {out_path}\n")
    return len(rows)


# --------------------------------------------------------------------------------- #
# --watch: append newly-completed results as they land, instead of waiting for the
# whole queue to drain. A file is available the moment its worker writes result.json
# to OUTPUT_MOUNT (already a shared Azure File Share -- no separate transfer needed);
# this just polls the status table on an interval and appends what's new.
# --------------------------------------------------------------------------------- #

def _row_key(entity: dict) -> str:
    return f"{entity['PartitionKey']}/{entity['RowKey']}"


def _watch_state_path(out_path: str) -> str:
    return out_path + ".watch_state.json"


def _load_watch_state(state_path: str) -> set:
    if not os.path.exists(state_path):
        return set()
    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            return set(json.load(fh).get("seen_row_keys", []))
    except (OSError, json.JSONDecodeError):
        return set()


def _save_watch_state(state_path: str, seen: set) -> None:
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"seen_row_keys": sorted(seen)}, fh)
    os.replace(tmp, state_path)


def _watch_pid_path(out_path: str) -> str:
    return out_path + ".watch.pid"


def _pid_alive(pid: int) -> bool:
    """Best-effort, cross-platform 'is this PID still running'. On any doubt, says yes --
    refusing to start a second watcher is the safe failure mode; a false "already running"
    is a one-line --restart, but two writers racing on the same CSV is silent corruption."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=10)
            return str(pid) in (out.stdout or "")
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def _is_drained() -> bool:
    """True once no row anywhere in the table is pending/processing. Table-wide, matching
    collect()'s own table-wide scope (there is no --job-id on collect)."""
    from scaling_lib.status import _fetch_entities
    for e in _fetch_entities():
        if e.get("status") in ("pending", "processing"):
            return False
    return True


def collect_incremental(out_path: str, seen: set, concurrency: int = 32) -> tuple:
    """One incremental pass: append any newly-completed rows not already in `seen`,
    flushed to disk immediately. Returns (new_row_count, updated seen set).

    A row is added to `seen` once its outcome is known either way: a real result.json
    (written to out_path) or a confirmed Windows-leg conversion stub (forwarded.json --
    never produces its own result.json, so there is nothing further to wait for). A
    genuinely missing/unparseable output is left OUT of `seen` so it's retried next
    pass -- result.json may simply still be mid-write on a slow network share.
    """
    from concurrent.futures import ThreadPoolExecutor
    from scaling_lib.status import _fetch_entities
    from pii_triage.routing import FIELDNAMES

    entities = [e for e in _fetch_entities(status_filter="completed") if _row_key(e) not in seen]
    if not entities:
        return 0, seen

    rows = []
    missing = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for entity, (data, reason) in zip(entities, pool.map(_read_completed_entity, entities)):
            if data is not None:
                rows.append(data)
                seen.add(_row_key(entity))
            elif reason is None:
                seen.add(_row_key(entity))   # windows-leg stub -- nothing more will ever land here
            else:
                missing.append(reason)

    if rows:
        file_exists = os.path.exists(out_path) and os.path.getsize(out_path) > 0
        with open(out_path, "a", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDNAMES, extrasaction="ignore", restval="")
            if not file_exists:
                writer.writeheader()
            writer.writerows(rows)
            fh.flush()
            os.fsync(fh.fileno())

    if missing:
        sys.stderr.write(f"  ({len(missing)} output(s) not yet readable, will retry next pass"
                         + (f": {missing[0]}" if len(missing) == 1 else "") + ")\n")

    return len(rows), seen


def watch(out_path: str = "inventory.csv", interval: float = 15.0, concurrency: int = 32,
          restart: bool = False, max_iterations: int | None = None) -> int:
    """Continuously append newly-completed files into out_path as they finish, instead of
    only collecting once the whole queue has drained. Crash-safe and restartable: a
    sidecar <out_path>.watch_state.json tracks which Table rows are already written, the
    same "durable progress record" discipline runner.py's CSV resume already uses.

    Stops automatically once the table fully drains (a doc that finishes between the
    drained check and the final pass is still caught by that last pass), or on Ctrl+C --
    either way, already-written rows and the resume state are safe on disk.
    `max_iterations` is a test hook; leave it None to run until drained/interrupted.
    """
    state_path = _watch_state_path(out_path)
    pid_path = _watch_pid_path(out_path)

    if restart:
        for p in (out_path, state_path, pid_path):
            if os.path.exists(p):
                os.remove(p)

    if os.path.exists(pid_path):
        old_pid = None
        try:
            old_pid = int(open(pid_path, encoding="utf-8").read().strip())
        except (OSError, ValueError):
            pass
        if old_pid and _pid_alive(old_pid):
            sys.stderr.write(
                f"ERROR: a --watch process (pid {old_pid}) already appears to be running against "
                f"'{out_path}' ({pid_path}).\n       Refusing to start a second one -- two writers "
                f"appending to the same CSV would race.\n       If that process is actually gone, "
                f"delete {pid_path} or pass --restart.\n")
            raise SystemExit(2)
        # stale lock (the prior watcher crashed/was killed without cleaning up) -- safe to continue

    if os.path.exists(out_path) and not os.path.exists(state_path):
        sys.stderr.write(
            f"ERROR: '{out_path}' already exists but has no watch-state sidecar ({state_path}).\n"
            f"       It looks like it was written by a one-shot collect() run (or a previous "
            f"--watch run's\n       state file was deleted). Appending here could duplicate rows.\n"
            f"       Pass --restart to start fresh, or point --out at a new file.\n")
        raise SystemExit(2)

    seen = _load_watch_state(state_path)
    total_written = 0
    if os.path.exists(out_path):
        with open(out_path, "r", encoding="utf-8", newline="") as fh:
            total_written = max(sum(1 for _ in fh) - 1, 0)   # rows minus header

    sys.stderr.write(f"watching for newly-completed files -> {out_path} "
                     f"(every {interval:.0f}s; resumed {len(seen)} already-seen row(s), "
                     f"{total_written} already-written record(s))\n")

    with open(pid_path, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))

    iterations = 0
    try:
        while True:
            n, seen = collect_incremental(out_path, seen, concurrency)
            if n:
                total_written += n
                _save_watch_state(state_path, seen)
                sys.stderr.write(f"  +{n} (total {total_written})\n")
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            if _is_drained():
                n, seen = collect_incremental(out_path, seen, concurrency)   # catch any stragglers
                if n:
                    total_written += n
                    _save_watch_state(state_path, seen)
                    sys.stderr.write(f"  +{n} (total {total_written})\n")
                sys.stderr.write(f"queue drained -- stopping. {total_written} record(s) in {out_path}\n")
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        sys.stderr.write(f"\ninterrupted -- {total_written} record(s) in {out_path} so far "
                         f"(state saved; rerun --watch to resume)\n")
    finally:
        try:
            os.remove(pid_path)
        except OSError:
            pass
    return total_written


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Collect scaling-lib worker outputs into inventory.csv.")
    p.add_argument("--out", default="inventory.csv")
    p.add_argument("--env-file", default=".env")
    p.add_argument("--concurrency", type=int, default=32,
                   help="Parallel result.json reads (default: 32) -- raise this on a large corpus")
    p.add_argument("--no-timing", action="store_true",
                   help="Skip writing the timing snapshot JSON alongside inventory.csv")
    p.add_argument("--watch", action="store_true",
                   help="Keep running, appending newly-completed files to --out as they land, "
                        "instead of waiting for the whole queue to drain. Stops automatically once "
                        "drained, or on Ctrl+C (resumable via a <out>.watch_state.json sidecar).")
    p.add_argument("--interval", type=float, default=15.0,
                   help="--watch only: seconds between polls (default: 15)")
    p.add_argument("--restart", action="store_true",
                   help="--watch only: discard an existing --out and its watch-state sidecar, "
                        "and start fresh")
    a = p.parse_args()

    from dotenv import load_dotenv
    load_dotenv(a.env_file)

    if a.watch:
        watch(a.out, a.interval, a.concurrency, a.restart)
    else:
        collect(a.out, a.concurrency)
    if not a.no_timing:
        dump_timing(a.out)
