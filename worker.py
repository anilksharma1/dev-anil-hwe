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
import csv

from dotenv import load_dotenv
from scaling_lib.worker import Worker
from scaling_lib.log import setup_logging

load_dotenv()
setup_logging(ai_level=logging.INFO)

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
        # Per-file timeout in seconds (extraction / processing helper limits).
        timeout_s=int(os.environ.get("FILE_TIMEOUT_S", 150)),
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
    with _bde_cache_lock:
        _bde_cache[key] = val
    return val


_ERROR_STATUSES = {"error", "timeout"}
_WARN_STATUSES = {"no_parser", "skipped_too_large"}


_LEGACY_EXTS = (".doc", ".xls", ".ppt")


def _forward_to_linux_queue(job_id: str, converted_path: Path) -> None:
    """Enqueue the converted file on the Linux queue, under the same job_id."""
    from scaling_lib.status import init_task
    from scaling_lib.queue import _build_message, _get_queue_service

    job_type = os.environ["JOB_TYPE"]
    init_task(job_id, job_type, converted_path.name)
    stored_path = converted_path.relative_to(Path(_cfg.root))
    client = _get_queue_service().get_queue_client(os.environ["AZURE_QUEUE_NAME"])
    client.send_message(_build_message(stored_path, job_id, job_type, posix=True))


def _convert_and_forward(file_path: str, output_dir: Path, task) -> None:
    """Windows-only: convert a legacy Office file, then hand detection off to a Linux worker.

    Keeps the Windows worker's per-task holding time down to just the COM call --
    the actual detection/OCR/LLM pipeline runs on the horizontally-scalable Linux
    fleet instead of serialized behind one COM instance.
    """
    from pii_triage.conversion import convert_legacy_office

    orig = Path(file_path)
    if task is None:
        raise RuntimeError("no active task context — cannot forward converted file")

    # Written under INPUT_MOUNT (not OUTPUT_MOUNT) so the relative path in the
    # forwarded queue message resolves against a Linux worker's own INPUT_MOUNT.
    dest_dir = Path(_cfg.root) / "_converted" / task.job_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    with task.checkpoint("convert"):
        converted = convert_legacy_office(file_path, dest_dir, _cfg.timeout_s)
    if not converted:
        raise RuntimeError(f"conversion failed for {file_path}")

    Path(str(converted) + ".orig.json").write_text(json.dumps({
        "orig_rel_path": os.path.relpath(file_path, _cfg.root),
        "orig_file_name": orig.name,
        "orig_ext": orig.suffix.lower(),
    }))
    _forward_to_linux_queue(task.job_id, converted)
    (output_dir / "forwarded.json").write_text(json.dumps({"forwarded_to_file": converted.name}))
    logging.info("converted+forwarded  %s -> %s", orig.name, converted.name)


def process(file_path: str, output_dir: Path) -> None:
    global _worker_completions
    from scaling_lib.metrics import current_task
    try:
        orig = Path(file_path)
        task = current_task()

        if os.name == "nt" and orig.suffix.lower() in _LEGACY_EXTS:
            _convert_and_forward(file_path, output_dir, task)
            return

        # Files forwarded from the Windows leg carry a sidecar with the original
        # (pre-conversion) identity, so the record reports the corpus file, not
        # the converted intermediate.
        orig_meta = None
        sidecar = Path(file_path + ".orig.json")
        if sidecar.exists():
            orig_meta = json.loads(sidecar.read_text(encoding="utf-8"))

        # Look up the protocol under the file's own job dir -- use the pre-conversion
        # path for forwarded files, since the converted intermediate lives under
        # INPUT_MOUNT/_converted/<job_id>/, not the original job's files/ folder.
        protocol_lookup_path = (
            str(Path(_cfg.root) / orig_meta["orig_rel_path"]) if orig_meta else file_path
        )
        protocol_text = _protocol_text_for(protocol_lookup_path)
        job_bde_threshold = _bde_threshold_for(protocol_lookup_path)

        rec = process_file(file_path, checkpoint=task.checkpoint if task else None,
                            protocol_text=protocol_text, bde_threshold=job_bde_threshold)

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
        # Also append this result to a per-job inventory CSV so the job has
        # a continuously-updating inventory as workers finish files.
        try:
            from pii_triage.routing import FIELDNAMES
            inventory_csv = output_dir.parent / "inventory.csv"
            write_header = (not inventory_csv.exists()) or (inventory_csv.stat().st_size == 0)
            with open(inventory_csv, "a", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=FIELDNAMES, extrasaction="ignore", restval="")
                if write_header:
                    writer.writeheader()
                writer.writerow(rec)
        except Exception:
            logging.debug("could not append per-job inventory.csv", exc_info=True)
    finally:
        with _worker_completions_lock:
            _worker_completions += 1


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

    Worker().run(process)


if __name__ == "__main__":
    main()
