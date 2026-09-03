"""scaling-lib worker entry point for pii_triage."""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

# Make pii_triage importable from the repo root when not installed via pip.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "pii_triage_merged"))

import argparse

from dotenv import load_dotenv
from scaling_lib.worker import Worker
from scaling_lib.log import setup_logging


def _resolve_log_level(name: str) -> int:
    """Map a LOG_LEVEL env value ('DEBUG'/'INFO'/'WARNING'/...) to a logging level constant,
    defaulting to INFO for anything unset or unrecognised (never raises on a typo)."""
    return getattr(logging, (name or "").strip().upper(), logging.INFO)


load_dotenv()
# LOG_LEVEL (default INFO) controls stdout + the local rotating file; ai_level (App Insights
# export) stays fixed at INFO regardless -- DEBUG-level per-file detail is useful for a live
# troubleshooting session on the box, not worth the ingestion cost of exporting it fleet-wide.
setup_logging(level=_resolve_log_level(os.environ.get("LOG_LEVEL", "")), ai_level=logging.INFO)

from pii_triage.config import Config, load_rulepack
from pii_triage.detection import CompiledRules
from pii_triage.extractors import quiet_noisy_libraries, get_extractor
from pii_triage.azure_clients import get_ocr_fn, get_llm_fn, get_bde_count_fn, get_stage2_fn
from pii_triage.runner import process_file
import pii_triage.runner as _runner

_cfg: Config | None = None

# DLQ circuit-breaker (fleet path; inactive when AZURE_DEAD_LETTER_QUEUE_NAME is unset)
_DLQ_CHECK_INTERVAL_S = int(os.environ.get("DLQ_CHECK_INTERVAL_S", "60"))
_DLQ_FAILURE_RATE = float(os.environ.get("DLQ_FAILURE_RATE", "0.05"))
_DLQ_MIN_COMPLETIONS = int(os.environ.get("DLQ_MIN_COMPLETIONS", "100"))
# Dead-letters accrue from ALL containers but _worker_completions counts only this one.
# Set DLQ_WORKER_COUNT to the fleet size so the rate denominator reflects total throughput.
_DLQ_WORKER_COUNT = int(os.environ.get("DLQ_WORKER_COUNT", "1"))

_worker_completions: int = 0
_worker_completions_lock = threading.Lock()


def _write_event(prefix: str, **fields) -> None:
    """Durable, UI-visible marker under OUTPUT_MOUNT/_events/<prefix>_<host>_<ts_ms>.json.

    OUTPUT_MOUNT is already the one thing every worker AND the ops-VM UI can both read, so
    this needs no new Azure resource. Used for anything worth surfacing on Monitor that
    logging.critical() alone would leave invisible to an operator -- worker logs are
    deliberately not tailed in this UI (a Log Analytics query across every replica), so
    without a durable marker like this, "why did the run stall / why do containers keep
    restarting" has no answer short of digging through logs by hand. Best-effort by design:
    every caller wraps this so a failure to WRITE the marker never blocks or masks the real
    event it's describing.
    """
    import socket
    events_dir = Path(os.environ.get("OUTPUT_MOUNT", ".")) / "_events"
    events_dir.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname()
    ts_ms = int(time.time() * 1000)
    event = {"at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
             "worker_instance": host, "pid": os.getpid(), **fields}
    path = events_dir / f"{prefix}_{host}_{ts_ms}.json"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(event), encoding="utf-8")
    tmp.replace(path)


def _write_dlq_trip_event(dlq_growth: int, done: int, rate: float) -> None:
    """Durable, UI-visible record of a circuit-breaker trip -- see _write_event."""
    _write_event("dlq_trip", type="dlq_circuit_breaker",
                 dlq_growth=dlq_growth, completions=done,
                 rate=round(dlq_growth / done, 4) if done else None, threshold=_DLQ_FAILURE_RATE)


def _dlq_monitor(baseline_dlq: int) -> None:
    """Daemon thread: exit if dead-letter growth rate exceeds the threshold.

    Compares DLQ growth since startup against this worker's completions scaled by
    DLQ_WORKER_COUNT. os._exit(1) terminates the whole process — scaling-lib's
    visibility heartbeat will time out and the in-flight task will be requeued.
    """
    while True:
        time.sleep(_DLQ_CHECK_INTERVAL_S)
        try:
            from scaling_lib.queue import queue_status
            counts = queue_status()
        except Exception as exc:
            logging.warning("DLQ monitor: failed to read queue counts: %s", exc, exc_info=True)
            continue
        dlq_growth = max(0, counts["dead_letter_count"] - baseline_dlq)
        with _worker_completions_lock:
            done = _worker_completions * _DLQ_WORKER_COUNT
        if done >= _DLQ_MIN_COMPLETIONS and dlq_growth / done > _DLQ_FAILURE_RATE:
            logging.critical(
                "DLQ circuit breaker: %d new dead-letters over ~%d completions (%.1f%%) "
                "exceeds %.0f%% threshold — stopping worker",
                dlq_growth, done, 100.0 * dlq_growth / done, 100.0 * _DLQ_FAILURE_RATE,
            )
            try:
                _write_dlq_trip_event(dlq_growth, done, _DLQ_FAILURE_RATE)
            except Exception:
                logging.warning("DLQ monitor: failed to write the trip event file", exc_info=True)
            os._exit(1)


def _build_config(ocr: bool = False, llm: bool = False, ner: bool = False) -> Config:
    """Build Config from env vars, with optional flag overrides."""
    def _flag(env: str, override: bool) -> bool:
        return override or os.environ.get(env, "").lower() in ("1", "true")

    pack = load_rulepack(os.environ.get("RULEPACK_PATH"))
    return Config(
        root=os.environ["INPUT_MOUNT"],
        rulepack=pack,
        bde_threshold=int(os.environ.get("BDE_THRESHOLD", pack.get("bde_threshold", 51))),
        use_ner=_flag("USE_NER", ner),
        use_ocr=_flag("USE_OCR", ocr),
        use_llm=_flag("USE_LLM", llm),
        llm_deployment=os.environ.get("AZURE_OPENAI_DEPLOYMENT")
                      or os.environ.get("AZURE_OPENAI_DEPLOYMENT_GPT_5_4_NANO", ""),
        timeout_s=int(os.environ.get("FILE_TIMEOUT_S", 60)),
        max_bytes=int(os.environ.get("MAX_BYTES", 1 << 30)),
        max_scan_chars=int(os.environ.get("MAX_SCAN_CHARS", 5_000_000)),
        max_scan_rows=int(os.environ.get("MAX_SCAN_ROWS", 200_000)),
        jurisdiction=os.environ.get("DEFAULT_JURISDICTION", ""),
        llm_input_chars=int(os.environ.get("LLM_INPUT_CHARS", 24_000)),
        log_prompts=_flag("LOG_LLM_PROMPTS", False),
    )


# Job layout on INPUT_MOUNT: <job_dir>/protocol.<ext> alongside <job_dir>/files/
# (the corpus, enqueued by pointing `scaling-lib enqueue` at the files/ subfolder).
# Looked up per file rather than once at worker startup, since one container's
# worker can process files from several concurrently-running jobs/matters.
_PROTOCOL_EXTS = (".pdf", ".docx", ".doc", ".txt", ".rtf")
_protocol_cache: dict[str, str] = {}
_protocol_cache_lock = threading.Lock()


def _job_dir_for(file_path: str) -> Path | None:
    for parent in Path(file_path.replace("\\", "/")).parents:
        if parent.name == "files":
            return parent.parent
    return None


def _find_protocol_file(job_dir: Path) -> Path | None:
    for ext in _PROTOCOL_EXTS:
        candidate = job_dir / f"protocol{ext}"
        if candidate.is_file():
            return candidate
    # Case-insensitive fallback (Linux workers have case-sensitive filesystems; a
    # protocol file uploaded from Windows with an uppercase extension won't match above).
    try:
        for p in job_dir.iterdir():
            if p.name.lower() in {f"protocol{e}" for e in _PROTOCOL_EXTS}:
                return p
    except OSError:
        pass
    return None


_no_job_dir_warned: set[str] = set()
_no_job_dir_warned_lock = threading.Lock()


def _protocol_text_for(file_path: str) -> str:
    job_dir = _job_dir_for(file_path)
    if job_dir is None:
        with _no_job_dir_warned_lock:
            if file_path not in _no_job_dir_warned:
                _no_job_dir_warned.add(file_path)
                logging.warning(
                    "no 'files/' ancestor found for %s — cannot locate protocol document; "
                    "proceeding without it",
                    file_path,
                )
        return ""
    key = str(job_dir)
    with _protocol_cache_lock:
        if key in _protocol_cache:
            logging.debug("protocol cache hit for %s", job_dir)
            return _protocol_cache[key]

    text = ""
    protocol_file = _find_protocol_file(job_dir)
    if protocol_file is not None:
        pe = get_extractor(protocol_file.suffix.lower())
        if pe is None:
            logging.warning(
                "unsupported protocol file extension '%s' for %s; proceeding without protocol",
                protocol_file.suffix, protocol_file,
            )
        else:
            try:
                text = pe(str(protocol_file), _cfg, _runner._RULES)[0]
                logging.debug("loaded protocol %s (%d chars) for job dir %s",
                             protocol_file.name, len(text), job_dir)
            except Exception:
                logging.warning("could not read protocol file %s; proceeding without it",
                                protocol_file, exc_info=True)
    else:
        logging.warning(
            "no protocol document found in %s (looked for protocol.pdf/.docx/.doc/.txt/.rtf); "
            "proceeding without it",
            job_dir,
        )

    with _protocol_cache_lock:
        # setdefault: the first thread to finish wins; a later write of "" from a
        # failed read in a concurrent thread cannot overwrite a successful result.
        _protocol_cache.setdefault(key, text)
        return _protocol_cache[key]


# Per-job overrides live in <job_dir>/pii_job.json (a sibling of files/, so it is never enqueued),
# written by enqueue.py --bde-threshold. Looked up per job dir and cached, exactly like the protocol,
# so one worker fleet can run several concurrent jobs each at its own BDE threshold. Falls back to
# the worker-global BDE_THRESHOLD env when absent.
_JOB_CONFIG_NAME = "pii_job.json"
_bde_cache: dict[str, int | None] = {}
_bde_cache_lock = threading.Lock()


def _bde_threshold_for(file_path: str) -> int | None:
    job_dir = _job_dir_for(file_path)
    if job_dir is None:
        return None
    key = str(job_dir)
    with _bde_cache_lock:
        if key in _bde_cache:
            return _bde_cache[key]
    val = None
    cfg_file = job_dir / _JOB_CONFIG_NAME
    if cfg_file.is_file():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("bde_threshold") is not None:
                val = int(data["bde_threshold"])
        except Exception:
            logging.warning("could not read %s; using the worker default BDE threshold",
                            cfg_file, exc_info=True)
    logging.debug("bde_threshold for job dir %s: %s", job_dir,
                  val if val is not None else "(worker default)")
    with _bde_cache_lock:
        _bde_cache[key] = val
    return val


_ERROR_STATUSES = {"error", "timeout"}
_WARN_STATUSES = {"no_parser", "skipped_too_large"}


def process(file_path: str, output_dir: Path) -> None:
    """The per-message callback: extract/detect/classify a file and write result.json.

    Legacy .doc/.xls/.ppt conversion happens INLINE inside process_file() (via
    pii_triage.runner's shared _process_file, using LibreOffice headless -- see
    conversion.py) -- there is no separate Windows leg/queue any more. Every file, whatever
    its extension, goes through this exact same path on whichever worker dequeues it.

    The .orig.json sidecar check below is a one-time backward-compatibility bridge: it only
    matters for a file forwarded by the OLD Windows-leg two-hop conversion (pre-this-change),
    if any such job is still in flight across a rolling deploy. New work never creates one.
    """
    global _worker_completions
    from scaling_lib.metrics import current_task
    try:
        orig = Path(file_path)
        task = current_task()
        logging.debug("process() start: %s (job=%s, attempt=%s)", orig.name,
                      getattr(task, "job_id", None), getattr(task, "attempt_count", None))

        orig_meta = None
        sidecar = Path(file_path + ".orig.json")
        if sidecar.exists():
            orig_meta = json.loads(sidecar.read_text(encoding="utf-8"))
            logging.debug("%s is a forwarded/converted file from a pre-existing Windows-leg "
                          "job (original: %s)", orig.name, orig_meta.get("orig_file_name"))

        # Look up the protocol under the file's own job dir -- use the pre-conversion
        # path for forwarded files, since the converted intermediate lives under
        # INPUT_MOUNT/_converted/<job_id>/, not the original job's files/ folder.
        protocol_lookup_path = (
            str(Path(_cfg.root) / orig_meta["orig_rel_path"]) if orig_meta else file_path
        )
        protocol_text = _protocol_text_for(protocol_lookup_path)
        job_bde_threshold = _bde_threshold_for(protocol_lookup_path)

        t0 = time.monotonic()
        rec = process_file(file_path, checkpoint=task.checkpoint if task else None,
                            protocol_text=protocol_text, bde_threshold=job_bde_threshold)
        logging.debug("process_file(%s) -> status=%s lane=%s in %.1fs", orig.name,
                      rec.get("status"), rec.get("suggested_lane"), time.monotonic() - t0)

        if orig_meta:
            rec["rel_path"] = orig_meta["orig_rel_path"]
            rec["file_name"] = orig_meta["orig_file_name"]
            rec["ext"] = orig_meta["orig_ext"]
            rec["converted_from"] = orig.name

        status = rec.get("status", "")
        detail = rec.get("detail", "")
        if status in _ERROR_STATUSES:
            logging.error("%-12s %s  (%s)", status, file_path, detail)
        elif status in _WARN_STATUSES:
            logging.warning("%-12s %s  (%s)", status, file_path, detail)
        (output_dir / "result.json").write_text(json.dumps(rec))
    finally:
        with _worker_completions_lock:
            _worker_completions += 1


def _preflight_checks(cfg: Config) -> list[dict]:
    """Fail fast and loud on the misconfigurations that, per operator feedback, are the
    frequent, hard-to-diagnose ones: scaling-lib not actually importable in this venv, a
    mount that isn't really there, and Azure auth that's broken -- normally discovered only
    much later, as a per-file 'llm_failed:AuthenticationError' three stages deep, which reads
    like a data problem rather than the systemic auth/config problem it actually is.

    Each check is {name, required, ok, detail}. `required` checks (scaling_lib import, both
    mounts) block startup outright -- there is nothing useful this worker could do without
    them. LLM/OCR credential checks are NOT required (a corpus with USE_LLM/USE_OCR off, or a
    transient network blip, shouldn't block ordinary rules-only processing) but their failure
    is logged CRITICAL and written as a durable, UI-visible event -- exactly the earlier
    "why did every legacy/structured file fail with an auth error" case, caught once at
    startup instead of discovered file-by-file.
    """
    checks = []

    def add(name, required, ok, detail):
        checks.append({"name": name, "required": required, "ok": ok, "detail": detail})

    try:
        import scaling_lib
        from scaling_lib._version import get_commit_hash
        commit = None
        try:
            commit = get_commit_hash()
        except Exception:
            pass
        add("scaling_lib import", True, True,
            f"OK (commit {commit})" if commit else "OK")
    except Exception as exc:
        add("scaling_lib import", True, False, f"{type(exc).__name__}: {exc}")

    for mount_env, need_write in (("INPUT_MOUNT", False), ("OUTPUT_MOUNT", True)):
        path = os.environ.get(mount_env, "")
        if not path:
            add(mount_env, True, False, "not set")
        elif not os.path.isdir(path):
            add(mount_env, True, False, f"not a readable directory: {path}")
        elif need_write and not os.access(path, os.W_OK):
            add(mount_env, True, False, f"not writable: {path}")
        else:
            add(mount_env, True, True, path)

    try:
        from scaling_lib.queue import queue_status
        counts = queue_status()
        add("storage queue/table reachable", True, True,
            f"queue_count={counts.get('queue_count')}")
    except Exception as exc:
        add("storage queue/table reachable", True, False, f"{type(exc).__name__}: {exc}")

    if getattr(cfg, "use_llm", False) or getattr(cfg, "use_ocr", False):
        try:
            from scaling_lib._config import _credential
            cred = _credential()
            cred.get_token("https://cognitiveservices.azure.com/.default")
            add("Azure AI credential (LLM/OCR)", False, True, "acquired a token")
        except Exception as exc:
            add("Azure AI credential (LLM/OCR)", False, False,
                f"{type(exc).__name__}: {exc} -- every USE_LLM/USE_OCR call will fail auth "
                f"until this is fixed (check AZURE_CREDENTIAL_TYPE and the managed identity's "
                f"role on the Azure OpenAI/Document Intelligence resource)")

    return checks


def _run_preflight_or_exit(cfg: Config) -> None:
    checks = _preflight_checks(cfg)
    logging.info("preflight: python=%s venv=%s cwd=%s", sys.version.split()[0], sys.prefix, os.getcwd())
    failed_required, failed_optional = [], []
    for c in checks:
        level = logging.INFO if c["ok"] else (logging.CRITICAL if c["required"] else logging.WARNING)
        logging.log(level, "preflight: %-32s %s -- %s",
                    c["name"], "OK" if c["ok"] else "FAILED", c["detail"])
        if not c["ok"]:
            (failed_required if c["required"] else failed_optional).append(c)
    if failed_optional:
        try:
            _write_event("preflight_warn", type="preflight_warning",
                         checks=[c["name"] for c in failed_optional],
                         detail=[c["detail"] for c in failed_optional])
        except Exception:
            logging.warning("preflight: failed to write the warning event file", exc_info=True)
    if failed_required:
        try:
            _write_event("preflight_fail", type="preflight_failure",
                         checks=[c["name"] for c in failed_required],
                         detail=[c["detail"] for c in failed_required])
        except Exception:
            logging.warning("preflight: failed to write the failure event file", exc_info=True)
        logging.critical("preflight: refusing to start -- %d required check(s) failed: %s",
                         len(failed_required), ", ".join(c["name"] for c in failed_required))
        sys.exit(1)


def _detect_cpu_quota(cgroup_root: str = "/sys/fs/cgroup") -> float | None:
    """Best-effort read of THIS container's actual CPU allocation (vCPUs) straight from the
    cgroup quota, e.g. Container Apps' `cpu=0.5`/`cpu=2` setting -- os.cpu_count() and
    os.sched_getaffinity() both report the underlying NODE's core count, not a fractional
    per-replica allocation, which would size concurrency for hardware this container was
    never actually given a share of. Tries cgroup v2 (cpu.max) first, then v1
    (cpu.cfs_quota_us / cpu.cfs_period_us). Returns None (caller falls back to a fixed
    default) if neither is readable -- e.g. on the bare Windows VM, or outside a container.
    `cgroup_root` is a parameter (not a hardcoded path) purely so this is testable without a
    real cgroup filesystem.
    """
    try:
        with open(os.path.join(cgroup_root, "cpu.max"), encoding="utf-8") as fh:
            quota_str, period_str = fh.read().split()
        if quota_str != "max":
            return int(quota_str) / int(period_str)
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(cgroup_root, "cpu", "cpu.cfs_quota_us"), encoding="utf-8") as fh:
            quota = int(fh.read().strip())
        with open(os.path.join(cgroup_root, "cpu", "cpu.cfs_period_us"), encoding="utf-8") as fh:
            period = int(fh.read().strip())
        if quota > 0:
            return quota / period
    except (OSError, ValueError):
        pass
    return None


def _default_concurrency(cgroup_root: str = "/sys/fs/cgroup") -> int:
    """Size WORKER_CONCURRENCY's default to this container's actual CPU allocation, instead
    of a flat number -- "scale up" (more vCPU per replica) and "multi-thread within a
    replica" should move together. More threads with no more CPU just adds contention on the
    CPU-bound stages (regex detection, PDF parsing); a bigger container with the old flat
    default of 4 left most of its extra CPU idle instead of turning into more throughput.

    This workload is I/O-heavy (network waits for OCR/LLM, result.json reads/writes over
    SMB/Azure Files) -- a thread waiting on I/O releases the GIL, so a thread count above raw
    vCPU count still helps, hence the x2 factor rather than 1:1. Still fully overridable via
    WORKER_CONCURRENCY; this is only the fallback when that env var is unset. Clamped to
    [2, 16]: floors at 2 so an unknown/undetected allocation still gets some overlap, caps at
    16 so a large container doesn't silently default to more threads than this I/O-to-CPU
    ratio likely benefits from without an operator actually deciding that.
    """
    quota = _detect_cpu_quota(cgroup_root)
    if quota is None:
        return 4   # unknown allocation (e.g. the bare Windows VM, or unreadable cgroup files)
    return max(2, min(16, round(quota * 2)))


def main() -> None:
    global _cfg

    parser = argparse.ArgumentParser(description="pii_triage scaling-lib worker")
    parser.add_argument("--no-ocr", dest="ocr", action="store_false",
                        help="Disable OCR (overrides USE_OCR env var)")
    parser.add_argument("--no-llm", dest="llm", action="store_false",
                        help="Disable LLM classification (overrides USE_LLM env var)")
    parser.add_argument("--no-ner", dest="ner", action="store_false",
                        help="Disable spaCy NER (overrides USE_NER env var)")
    parser.set_defaults(ocr=True, llm=True, ner=True)
    args = parser.parse_args()

    _cfg = _build_config(ocr=args.ocr, llm=args.llm, ner=args.ner)
    _run_preflight_or_exit(_cfg)

    # Initialise runner globals before Worker starts threads.
    # Done here (not inside process()) to avoid clobbering the main process's SIGINT handler.
    _runner._CFG = _cfg
    _runner._RULES = CompiledRules.from_pack(_cfg.rulepack, use_ner=_cfg.use_ner)
    _runner._OCR_FN = get_ocr_fn(_cfg)
    _runner._LLM_FN = get_llm_fn(_cfg)
    _runner._BDE_FN = get_bde_count_fn(_cfg)
    _runner._S2_FN = get_stage2_fn(_cfg)
    quiet_noisy_libraries()

    if os.environ.get("AZURE_DEAD_LETTER_QUEUE_NAME"):
        try:
            from scaling_lib.queue import queue_status
            baseline_dlq = queue_status()["dead_letter_count"]
        except Exception as exc:
            baseline_dlq = 0
            logging.warning("Could not read initial DLQ depth; DLQ monitor will use 0 as baseline: %s",
                            exc, exc_info=True)
        _t = threading.Thread(target=_dlq_monitor, args=(baseline_dlq,),
                              daemon=True, name="dlq-monitor")
        _t.start()
        logging.info(
            "DLQ monitor started (baseline=%d, threshold=%.0f%%, interval=%ds, worker_count=%d)",
            baseline_dlq, 100.0 * _DLQ_FAILURE_RATE, _DLQ_CHECK_INTERVAL_S, _DLQ_WORKER_COUNT,
        )

    # concurrency = files processed simultaneously (threads) within this one container --
    # I/O-bound work (network reads on OUTPUT_MOUNT/INPUT_MOUNT, OCR/LLM calls) releases the
    # GIL and genuinely overlaps across threads, so this is a real speed lever, not a no-op.
    # scaling-lib's own built-in default is 1 (effectively serial per container) unless
    # WORKER_CONCURRENCY is set; default here instead scales with the container's actual CPU
    # allocation (see _default_concurrency) so a bigger container ("scale up") automatically
    # gets more threads too, rather than leaving the extra vCPU idle behind a flat number.
    # Still fully overridable via WORKER_CONCURRENCY. Size it against your Azure OpenAI /
    # Document Intelligence deployment's RPM -- concurrency times replica count is what
    # actually hits those rate limits.
    concurrency = int(os.environ.get("WORKER_CONCURRENCY") or _default_concurrency())
    logging.info("worker concurrency: %d", concurrency)
    Worker(concurrency=concurrency).run(process)


if __name__ == "__main__":
    main()
