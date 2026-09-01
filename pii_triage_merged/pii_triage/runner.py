"""Parallel run loop with crash-safe resume, progress, and a run manifest.

Resume model: the output CSV *is* the durable progress record. A file is "done"
iff it has a complete row in the CSV. On resume the CSV is read, any partial
trailing line (from a hard crash mid-write) is dropped via an atomic rewrite,
already-done files are skipped, and new rows are appended. At most the in-flight
batch is re-scanned, which is idempotent.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import sys
import time
import queue
import signal
from collections import deque
import datetime
import multiprocessing as mp
from contextlib import nullcontext
from dataclasses import asdict, replace

from . import __version__
from .config import Config
from .detection import CompiledRules, detect, value_signal
from .enrich import apply_ocr, apply_image_ocr, apply_llm, apply_bde_count
from .extractors import get_extractor, time_limit, TimeoutError_, quiet_noisy_libraries
from .routing import (FileRecord, FIELDNAMES, NR_LANE, bucket_of, complexity_bucket,
                      classify_ambiguity, estimate_entities, roster_entity_estimate,
                      choose_lane)

logger = logging.getLogger(__name__)

# Default per-file hard-timeout for the watchdog, in seconds. On by default (see run());
# override per-run with env PII_WATCHDOG_S ("0" disables) or cfg.hard_timeout_s.
DEFAULT_WATCHDOG_S = 120.0
_FAILURE_STATUSES = frozenset({"error", "timeout"})

# Worker globals (set once per worker; avoids pickling compiled regex per task).
_CFG: Config | None = None
_RULES: CompiledRules | None = None
_OCR_FN = None
_LLM_FN = None
_BDE_FN = None
_S2_FN = None


def _init_worker(cfg: Config) -> None:
    global _CFG, _RULES, _OCR_FN, _LLM_FN, _BDE_FN, _S2_FN
    _CFG = cfg
    _RULES = CompiledRules.from_pack(cfg.rulepack, use_ner=cfg.use_ner)
    from .azure_clients import (get_ocr_fn, get_llm_fn, get_bde_count_fn,
                                get_stage2_fn)
    _OCR_FN = get_ocr_fn(cfg)   # None unless OCR enabled + Azure available
    _LLM_FN = get_llm_fn(cfg)   # None unless LLM enabled + Azure available
    _BDE_FN = get_bde_count_fn(cfg)  # separate BDE-only counter; independent of the NR call
    _S2_FN = get_stage2_fn(cfg)      # stage-2 graded responsiveness; None unless stage 2 on
    quiet_noisy_libraries()
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # parent owns Ctrl-C


def process_file(path: str, checkpoint=None, protocol_text: str | None = None,
                 bde_threshold: int | None = None) -> dict:
    """Scan one file: ONE extraction + ONE OCR + ONE detection pass, then stage 1
    (Anna's NR/BDE decision) and -- only if stage 1 did not clear the file -- stage 2
    (Daniel's graded overview).

    This is the whole point of 3.0.0. The previous workflow ran the tool twice, so every
    surviving file was parsed twice and OCR'd twice. Here the expensive shared work happens
    once and both decision stages read the same text.

    checkpoint(label) -> context manager, e.g. a scaling-lib task's .checkpoint method.
    Optional: defaults to a no-op so pii_triage stays usable standalone (CLI / tests)
    without a scaling-lib task in scope.

    protocol_text, if given, overrides the worker-global Config's protocol_text for this
    call only (via a local copy, not a mutation) -- lets a worker fleet processing files
    from multiple concurrent jobs apply each job's own matter protocol without racing on
    shared state. bde_threshold overrides the worker-global BDE entity threshold the same
    way, so each job can use its own (e.g. 7+ vs 51+) without a redeploy.
    """
    t0 = time.monotonic()
    rec = _process_file(path, checkpoint=checkpoint, protocol_text=protocol_text,
                        bde_threshold=bde_threshold)
    rec["elapsed_s"] = round(time.monotonic() - t0, 3)
    return rec


def _finalize_early(rec, reason: str) -> dict:
    """Close out a file that never reached the decision stages -- unsupported extension,
    oversize, timeout, or a parser error.

    These paths return before stage 1 has any text to work with, so they must still stamp
    the derived columns and say WHY stage 2 did not run. Without this the row carries a
    blank s2_skip_reason, which is indistinguishable from a bug in the gate.
    """
    rec.suggested_lane = choose_lane(rec)
    rec.nr_stage1 = rec.suggested_lane == NR_LANE
    rec.bde_stage1 = bool(rec.is_bde)
    rec.s2_skip_reason = reason
    rec.llm_tokens_total = int(rec.llm_tokens or 0) + int(rec.s2_llm_tokens or 0)
    return asdict(rec)


def _process_file(path: str, checkpoint=None, protocol_text: str | None = None,
                  bde_threshold: int | None = None) -> dict:
    checkpoint = checkpoint or (lambda label: nullcontext())
    cfg, rules = _CFG, _RULES
    assert cfg is not None and rules is not None
    if protocol_text:
        cfg = replace(cfg, protocol_text=protocol_text)
    if bde_threshold is not None:
        # Per-job override of the BDE threshold (worker fleet); a local copy, never a global
        # mutation, so it flows through roster recovery, is_bde, and the stage-2 threshold alike.
        cfg = replace(cfg, bde_threshold=int(bde_threshold))
    ext = os.path.splitext(path)[1].lower()
    rel = os.path.relpath(path, cfg.root)
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    rec = FileRecord(rel_path=rel, file_name=os.path.basename(path), ext=ext, size_bytes=size)

    # On Windows, convert legacy Office formats to OOXML before extraction so that
    # .doc/.xls/.ppt flow through the normal extractor path rather than convert_lane.
    # The worker does its own conversion (with a task checkpoint) before calling
    # process_file(), so by the time the worker reaches here the ext is already modern.
    tmp_dir = None
    if os.name == "nt" and ext in (".doc", ".xls", ".ppt"):
        import tempfile
        from pathlib import Path as _Path
        from .conversion import convert_legacy_office
        # Keep the temp dir on the same drive as the corpus to avoid cross-drive
        # relpath errors on Windows.
        _drive = _Path(cfg.root).drive
        tmp_dir = tempfile.mkdtemp(dir=(_drive + "\\") if _drive else None)
        try:
            converted = convert_legacy_office(path, _Path(tmp_dir), cfg.timeout_s)
            if converted:
                path = str(converted)
                ext = os.path.splitext(path)[1].lower()
        except Exception as exc:
            logger.warning("legacy Office conversion failed for %s, continuing with original: %s",
                           os.path.basename(path), exc, exc_info=True)

    try:
        extractor = get_extractor(ext)
        if extractor is None:
            rec.status, rec.text_extractable, rec.detail = "no_parser", "unknown", "unsupported_ext"
            return _finalize_early(rec, "no_parser")
        if size > cfg.max_bytes:
            rec.status, rec.detail = "skipped_too_large", f"size>{cfg.max_bytes}"
            return _finalize_early(rec, "skipped_too_large")

        try:
            with checkpoint("extract"), time_limit(cfg.timeout_s):
                text, meta = extractor(path, cfg, rules)
        except TimeoutError_:
            rec.status, rec.detail = "timeout", "per_file_timeout"
            return _finalize_early(rec, "timeout")
        except Exception as exc:
            logger.warning("extraction failed for %s: %s", rec.file_name, exc, exc_info=True)
            rec.status, rec.detail = "error", type(exc).__name__  # class only; no PII
            return _finalize_early(rec, "extract_error")

        logger.debug("\n=== %s ===", rec.file_name)
        logger.debug("extracted: text_extractable=%s  size=%d bytes",
                     meta.get("text_extractable", "?"), size)
        logger.debug("--- extracted text (%d chars) ---\n%s", len(text), text)

        with checkpoint("ocr"):
            # OCR turns a non-searchable (image-only) file into text so it flows through
            # normal detection instead of the sampling lane. No-op unless OCR is enabled.
            text, meta = apply_ocr(path, text, meta, cfg, _OCR_FN)

            if meta.get("ocr"):
                logger.debug("--- OCR text (%d chars) ---\n%s", len(text), text)

            # For text PDFs with embedded content images (e.g. image-header tables), OCR
            # those images and append the text so detection can read what pypdf couldn't.
            text, img_ocr_summary, img_stats = apply_image_ocr(text, meta, cfg, _OCR_FN)
            if img_ocr_summary:
                rec.detail = (f"{rec.detail} " if rec.detail else "") + img_ocr_summary

            # ---- OCR / DI accounting (3.0.0) ----------------------------------- #
            # Recorded unconditionally so the CSV can be summed to a billable-call total.
            rec.ocr_attempted = bool(meta.get("ocr_attempted", False))
            rec.ocr = bool(meta.get("ocr", False))
            rec.ocr_pages = int(meta.get("ocr_pages", 0) or 0)
            rec.img_ocr_qualifying = int(img_stats.get("qualifying", 0) or 0)
            rec.img_ocr_calls = int(img_stats.get("calls", 0) or 0)
            rec.img_ocr_ok = int(img_stats.get("ok", 0) or 0)
            rec.img_decode_failed = int(meta.get("pdf_images_decode_failed", 0) or 0)
            rec.di_calls = (1 if rec.ocr_attempted else 0) + rec.img_ocr_calls
            if rec.img_decode_failed and not rec.img_ocr_qualifying:
                # pypdf saw images it could not decode and produced no candidates. Almost
                # always a missing Pillow, which silently disables embedded-image OCR.
                rec.detail = ((f"{rec.detail} " if rec.detail else "")
                              + f"img_decode_failed:{rec.img_decode_failed}(pillow?)")

        rec.text_extractable = meta.get("text_extractable", "text")
        rec.is_structured = meta.get("is_structured", False)
        rec.page_or_sheet_count = meta.get("page_or_sheet_count", 0)
        rec.attachment_count = meta.get("attachment_count", 0)
        rec.estimate_truncated = meta.get("estimate_truncated", False)
        if meta.get("status"):
            rec.status = meta["status"]
        if meta.get("detail"):
            rec.detail = (f"{rec.detail} " if rec.detail else "") + meta["detail"]

        rec.searchable = rec.text_extractable == "text"
        rec.programmatic = bool(rec.is_structured)

        edges = cfg.rulepack.get("bucket_edges", None)
        # =================== STAGE 1 (Anna's) ================================ #
        # Authoritative for NR removal. This block is byte-for-byte the 2.10.2 logic and
        # writes ONLY the 27 legacy columns. Nothing below the stage-1 marker may change
        # it -- that invariant is what tools/check_nr_frozen.py exists to prove.
        if rec.searchable:
            with checkpoint("detect"):
                counts, labels, categories = detect(text, rules)
                rec.entities_found = " | ".join(labels)
                rec.value_signal = value_signal(counts, rules)
                rec.pi_categories = " | ".join(categories)

            with checkpoint("classify"):
                rec.estimated_entities = estimate_entities(meta, counts, labels, rules.per_person_keys)
                # Recover unrecognized rosters: a structured file the identifier rules read as
                # 0 entities but with many rows is likely a roster (the '51+ entities' Excels
                # manual review flags but the tool was clearing pre-AI). Bump it to its row count
                # so it reaches the LLM / flags in fallback rather than being silently cleared.
                try:
                    review_rows = int(os.environ.get("PII_STRUCTURED_REVIEW_ROWS", "")
                                      or getattr(cfg, "structured_review_rows", 0) or cfg.bde_threshold)
                except (TypeError, ValueError):
                    review_rows = cfg.bde_threshold
                bumped = roster_entity_estimate(meta, rec.estimated_entities, review_rows)
                if bumped != rec.estimated_entities:
                    rec.estimated_entities = bumped
                    rec.detail = (f"{rec.detail} " if rec.detail else "") + "roster_by_rowcount"
                rec.ambiguity = classify_ambiguity(
                    counts, labels, rec.is_structured,
                    structured_rows=rec.estimated_entities if rec.is_structured else 0,
                    structured_total_rows=int(meta.get("structured_total_rows", 0) or 0)
                    if rec.is_structured else 0)

            # LLM is consulted only on ambiguous files; it may revise the entity
            # estimate and decides responsiveness (choose_lane honours its call).
            with checkpoint("llm"):
                apply_llm(rec, text, cfg, _LLM_FN)

            # BDE TIER = LLM person-count (distinct people with PII beyond a bare name).
            # Fires on any responsive file that could plausibly be >= threshold people; the
            # count is authoritative and may correct an inflated token estimate DOWN. Writes
            # only bde_person_count -- NR (apply_llm above / estimated_entities) is untouched.
            with checkpoint("bde_count"):
                counted, is_roster = apply_bde_count(rec, text, cfg, _BDE_FN)
                if counted:
                    if is_roster:
                        # per-person roster: the LLM only saw a sample -> take the magnitude from
                        # structure (row/token count), which is reliable for real rosters.
                        tier_count = max(int(rec.bde_person_count or 0), int(rec.estimated_entities or 0))
                    else:
                        # single/few-subject file: trust the LLM count (corrects token over-counts down)
                        tier_count = int(rec.bde_person_count or 0)
                    rec.is_bde = tier_count >= cfg.bde_threshold
                else:
                    # LLM off or below the floor -> fall back to the token estimate
                    rec.is_bde = rec.estimated_entities >= cfg.bde_threshold
                rec.bde_confirmed = rec.is_bde
                rec.entity_bucket = bucket_of(rec.estimated_entities, edges) if edges \
                    else bucket_of(rec.estimated_entities)
        else:
            # Non-searchable: no text to search -> route to complexity-bucketed sampling.
            rec.complexity_bucket = complexity_bucket(rec.page_or_sheet_count or 1)

        with checkpoint("route"):
            rec.suggested_lane = choose_lane(rec)
            # ---- derived stage-1 answers (no new decisions, just named columns) --- #
            rec.nr_stage1 = rec.suggested_lane == NR_LANE
            rec.bde_stage1 = bool(rec.is_bde)
        # =================== END OF STAGE 1 ================================== #

        # =================== STAGE 2 (Daniel's) ============================== #
        # Runs ONLY on files stage 1 did not clear. Reads the SAME text stage 1 read --
        # that shared read is the cost saving. Writes only s2_* fields; it can never
        # change a stage-1 answer, so it cannot lower NR recall.
        with checkpoint("stage2"):
            _stage2(rec, text, cfg, _S2_FN)
        rec.llm_tokens_total = int(rec.llm_tokens or 0) + int(rec.s2_llm_tokens or 0)
        return asdict(rec)
    finally:
        if tmp_dir is not None:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _stage2(rec, text, cfg, s2_fn) -> None:
    """Stage 2: graded responsiveness for the dataset overview.

    Gated on stage 1: a file stage 1 routed to `likely_non_responsive` is never shown to
    stage 2, which is the workflow you already run -- this just removes the second
    extraction and OCR pass that used to pay for it.

    Writes ONLY the s2_* block (routing.STAGE2_FIELDNAMES). It reads rec.suggested_lane,
    rec.searchable, rec.is_structured and rec.estimated_entities, and mutates none of them.

    The five graded levels are collapsed with DANIEL'S rule -- clear_yes/likely_yes are
    responsive, everything else is not. Note this differs from stage 1's uncertainty
    policy on purpose: stage 1's prompt rounds genuine uncertainty UP to responsive,
    stage 2's expresses it as `borderline` and clears it. That is exactly why stage 1 owns
    NR removal and stage 2 only describes what survived.

    An UNRECOGNISED level is treated as responsive, not cleared -- recall-safe default.
    """
    if not getattr(cfg, "use_stage2", True):
        rec.s2_skip_reason = "stage2_disabled"
        return
    if rec.nr_stage1 and not getattr(cfg, "stage2_on_all", False):
        rec.s2_skip_reason = "stage1_nr"
        return
    if not rec.searchable:
        # No text was ever recovered, so there is nothing to grade. Stage 1 has already
        # routed this to the sampling lane.
        rec.s2_skip_reason = "not_searchable"
        return
    if not (text or "").strip():
        rec.s2_skip_reason = "no_text"
        return

    rec.s2_ran = True
    s2_threshold = int(getattr(cfg, "s2_bde_threshold", 0) or cfg.bde_threshold)

    if s2_fn is not None:
        try:
            result = s2_fn(text, cfg) or {}
        except Exception as exc:  # noqa: BLE001 -- one bad file must not kill the run
            logger.warning("stage2 LLM failed for %s: %s: %s",
                           rec.file_name, type(exc).__name__, exc, exc_info=True)
            rec.s2_detail = f"llm_failed:{type(exc).__name__}"
            result = None
        if result is not None:
            rec.s2_llm_consulted = True
            rec.s2_llm_tokens = int(result.get("tokens") or 0)
            level = str(result.get("responsiveness") or "").strip()
            rec.s2_llm_responsiveness = level
            if level in ("clear_yes", "likely_yes"):
                responsive = True
            elif level in ("borderline", "likely_no", "clear_no"):
                responsive = False
            elif level:
                # A level we do not recognise: do NOT clear on it.
                responsive = True
                rec.s2_detail = ((f"{rec.s2_detail} " if rec.s2_detail else "")
                                 + f"unknown_level:{level[:24]}")
            else:
                # Legacy boolean shape (older deployments return `responsive`).
                responsive = bool(result.get("responsive"))
                rec.s2_detail = ((f"{rec.s2_detail} " if rec.s2_detail else "")
                                 + "legacy_boolean")
            rec.s2_llm_responsive = "yes" if responsive else "no"
        else:
            responsive = _s2_rules_fallback(rec)
            rec.s2_llm_responsive = "yes" if responsive else "no"
    else:
        # Stage 2 enabled but no LLM available -> recall-first rules fallback, same shape
        # stage 1 uses when its own LLM is unavailable.
        responsive = _s2_rules_fallback(rec)
        rec.s2_llm_responsive = "yes" if responsive else "no"
        rec.s2_detail = ((f"{rec.s2_detail} " if rec.s2_detail else "") + "no_llm:rules_fallback")

    rec.s2_is_bde = int(rec.estimated_entities or 0) >= s2_threshold
    if not responsive:
        rec.s2_lane = NR_LANE
    elif rec.is_structured and rec.s2_is_bde:
        rec.s2_lane = "structured_bde"
    elif rec.s2_is_bde:
        rec.s2_lane = "bde"
    else:
        rec.s2_lane = "standard"
    rec.s2_nr = rec.s2_lane == NR_LANE


def _s2_rules_fallback(rec) -> bool:
    """Recall-first fallback when the stage-2 LLM is unavailable or failed. Mirrors the
    rules path stage 1 uses (routing.choose_lane): any real value, or structured
    identifier rows, counts as responsive."""
    return bool(rec.value_signal) or (rec.is_structured and int(rec.estimated_entities or 0) > 0)


def discover_files(root: str) -> list[str]:
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            out.append(os.path.join(dirpath, name))
    return out


def load_filter_set(inventory_csv: str, exclude_lanes: set) -> set:
    """Read a prior inventory CSV and return the rel_paths whose `suggested_lane`
    is NOT in `exclude_lanes` (default: just likely_non_responsive). Used by
    `enqueue.py --inventory` to re-enqueue only the responsive-or-unresolved subset
    (e.g. with USE_LLM/USE_OCR on) instead of the whole corpus."""
    keep = set()
    with open(inventory_csv, "r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("suggested_lane") not in exclude_lanes:
                keep.add(row["rel_path"])
    return keep


def _fmt(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def _load_done_and_repair(out_path: str, counters: dict, lanes: dict, llm: dict,
                          ocr: dict, s2: dict) -> set:
    """Read an existing CSV, drop any partial trailing row via atomic rewrite,
    return the set of already-done rel_paths, and tally prior results."""
    done: set = set()
    valid_rows = []
    with open(out_path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if any(row.get(k) is None for k in FIELDNAMES):
                continue  # incomplete/partial line -> drop
            valid_rows.append(row)
            done.add(row["rel_path"])
            _tally(row, counters, lanes, llm, ocr, s2)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(valid_rows)
    os.replace(tmp, out_path)
    return done


def _true(v) -> bool:
    """CSV round-trips booleans as the strings "True"/"False"."""
    return str(v) in ("True", "true", "1")


def _tally(rec: dict, counters: dict, lanes: dict, llm: dict,
           ocr: dict | None = None, s2: dict | None = None) -> None:
    counters[rec["status"]] = counters.get(rec["status"], 0) + 1
    if rec.get("text_extractable") == "image_only":
        # NB this counts OCR *failures* and files where OCR was off -- apply_ocr rewrites
        # text_extractable to "text" on success. Kept for continuity; use the ocr dict below
        # for actual OCR volume.
        counters["image_only"] = counters.get("image_only", 0) + 1
    if _true(rec.get("is_bde")):
        counters["bde"] = counters.get("bde", 0) + 1
    lane = rec.get("suggested_lane", "")
    lanes[lane] = lanes.get(lane, 0) + 1

    # ---- OCR / Document Intelligence accounting (3.0.0) --------------------- #
    # Previously there was none: a successfully OCR'd file was counted nowhere, because
    # the image_only counter above reads a field apply_ocr has already overwritten.
    if ocr is not None:
        def _n(k):
            try:
                return int(rec.get(k) or 0)
            except (TypeError, ValueError):
                return 0
        if _true(rec.get("ocr_attempted")):
            ocr["full_file_calls"] = ocr.get("full_file_calls", 0) + 1
            if _true(rec.get("ocr")):
                ocr["full_file_ok"] = ocr.get("full_file_ok", 0) + 1
            else:
                ocr["full_file_failed"] = ocr.get("full_file_failed", 0) + 1
        ocr["pages"] = ocr.get("pages", 0) + _n("ocr_pages")
        ocr["img_qualifying"] = ocr.get("img_qualifying", 0) + _n("img_ocr_qualifying")
        ocr["img_calls"] = ocr.get("img_calls", 0) + _n("img_ocr_calls")
        ocr["img_ok"] = ocr.get("img_ok", 0) + _n("img_ocr_ok")
        ocr["img_decode_failed"] = ocr.get("img_decode_failed", 0) + _n("img_decode_failed")
        ocr["di_calls"] = ocr.get("di_calls", 0) + _n("di_calls")
        if _n("di_calls"):
            ocr["files_with_di"] = ocr.get("files_with_di", 0) + 1

    # ---- stage-2 accounting (3.0.0) ---------------------------------------- #
    if s2 is not None:
        if _true(rec.get("s2_ran")):
            s2["ran"] = s2.get("ran", 0) + 1
            if _true(rec.get("s2_llm_consulted")):
                s2["consulted"] = s2.get("consulted", 0) + 1
            lvl = rec.get("s2_llm_responsiveness") or "(none)"
            s2["level:" + lvl] = s2.get("level:" + lvl, 0) + 1
            s2["nr" if _true(rec.get("s2_nr")) else "responsive"] = \
                s2.get("nr" if _true(rec.get("s2_nr")) else "responsive", 0) + 1
        else:
            reason = rec.get("s2_skip_reason") or "(unset)"
            s2["skip:" + reason] = s2.get("skip:" + reason, 0) + 1
        try:
            s2["tokens"] = s2.get("tokens", 0) + int(rec.get("s2_llm_tokens") or 0)
        except (TypeError, ValueError):
            pass
    # Total Azure OpenAI tokens spent this run (summed from every file; rebuilt from
    # the CSV on resume, so the figure is complete even across restarts).
    llm["tokens"] = llm.get("tokens", 0) + int(rec.get("llm_tokens") or 0)
    # LLM visibility: of the files that needed the AI's judgment (ambiguous), how many
    # actually reached it, what it decided, and -- crucially -- how many silently FAILED
    # and fell back to the recall-first rules flag. A high fail count is the hidden
    # source of over-calls: those files were flagged by the fallback, not by the AI.
    if rec.get("ambiguity") == "ambiguous":
        llm["ambiguous"] = llm.get("ambiguous", 0) + 1
        if _true(rec.get("llm_consulted")):
            llm["consulted"] = llm.get("consulted", 0) + 1
            decided = "responsive" if rec.get("llm_responsive") == "yes" else "cleared"
            llm[decided] = llm.get(decided, 0) + 1
        else:
            m = re.search(r"llm_failed:(\w+)", rec.get("detail") or "")
            cls = ("fail:" + m.group(1)) if m else "not_run(off?)"
            llm["failed"] = llm.get("failed", 0) + 1
            llm[cls] = llm.get(cls, 0) + 1


def _cost_summary(cfg, llm: dict, ocr: dict, s2: dict) -> dict:
    """Turn the run's measured units into a cost summary.

    Units are always reported. Money only appears when the operator supplied prices
    (--price-per-1k-in / --price-per-1k-out / --price-per-1k-pages), because list pricing
    varies by region and agreement and a wrong number is worse than none.

    Note on DI pages vs calls: Document Intelligence bills per page. A full-file call bills
    the pages it read (ocr_pages); an embedded content image is a single page. `di_pages` is
    the billable figure; `di_calls` is the request count.
    """
    tokens = int(llm.get("tokens", 0) or 0) + int(s2.get("tokens", 0) or 0)
    di_calls = int(ocr.get("di_calls", 0) or 0)
    di_pages = int(ocr.get("pages", 0) or 0) + int(ocr.get("img_calls", 0) or 0)
    out = {
        "llm_tokens_total": tokens,
        "llm_tokens_stage1": int(llm.get("tokens", 0) or 0),
        "llm_tokens_stage2": int(s2.get("tokens", 0) or 0),
        "di_calls": di_calls,
        "di_pages_billable": di_pages,
    }
    # We do not know the in/out token split (the API returns only total_tokens), so if the
    # two prices differ we report a range rather than inventing a ratio.
    p_in = float(getattr(cfg, "price_per_1k_in", 0) or 0)
    p_out = float(getattr(cfg, "price_per_1k_out", 0) or 0)
    p_pages = float(getattr(cfg, "price_per_1k_pages", 0) or 0)
    if p_in or p_out:
        lo, hi = sorted((p_in, p_out)) if (p_in and p_out) else (p_in or p_out, p_in or p_out)
        out["llm_cost_low"] = round(tokens / 1000.0 * lo, 2)
        out["llm_cost_high"] = round(tokens / 1000.0 * hi, 2)
        out["llm_cost_note"] = ("total_tokens only -- in/out split unknown, so this is a range"
                                if lo != hi else "single price applied")
    if p_pages:
        out["di_cost"] = round(di_pages / 1000.0 * p_pages, 2)
    if (p_in or p_out) and p_pages:
        out["di_share_of_spend_pct"] = round(
            100.0 * out["di_cost"] / max(out["di_cost"] + out.get("llm_cost_high", 0), 1e-9), 1)
    return out


def _write_manifest(path, cfg, summary):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(summary | {"config": cfg.to_manifest(), "tool_version": __version__},
                  fh, indent=2)


# --------------------------------------------------------------------------- #
# Resilient (watchdog) execution path
#
# The default Pool.imap_unordered path cannot interrupt a worker stuck in a
# CPU-bound C call (e.g. a parser spinning on a malformed file). On Windows the
# extractor's signal-based time_limit is a no-op (no SIGALRM), so such a file
# wedges its worker forever; once every worker is wedged the whole run stalls
# with zero completions and steady CPU -- which is exactly what large runs hit.
#
# This path runs PERSISTENT workers (initialised once, so Azure clients/regex are
# not rebuilt per file). Each worker sends a "start" heartbeat before each file; a
# parent watchdog terminates and replaces any worker whose current file exceeds the
# per-file deadline, records that file as `timeout` (an undetermined lane -> excluded
# from scoring, never silently dropped), and continues. Enabled only when a deadline
# is set (env PII_WATCHDOG_S, or cfg.hard_timeout_s); otherwise the default path runs
# unchanged.
# --------------------------------------------------------------------------- #

def _stub_record(cfg, path, status, detail):
    """Minimal CSV row for a file a worker never finished (killed or crashed)."""
    ext = os.path.splitext(path)[1].lower()
    try:
        rel = os.path.relpath(path, cfg.root)
    except Exception:
        rel = path
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    rec = FileRecord(rel_path=rel, file_name=os.path.basename(path), ext=ext, size_bytes=size)
    rec.status, rec.detail = status, detail
    return _finalize_early(rec, status or "never_completed")


def _resilient_worker(cfg, task_q, result_q):
    """Persistent worker: init once, then loop pulling paths. Emits a 'start' heartbeat
    (so the parent can time the file) and then a 'done' result for each file."""
    _init_worker(cfg)
    pid = os.getpid()
    while True:
        try:
            path = task_q.get()
        except (EOFError, OSError, KeyboardInterrupt):
            return
        if path is None:
            return
        try:
            result_q.put(("start", pid, path, time.monotonic()))
            rec = process_file(path)
            result_q.put(("done", pid, rec))
        except Exception as exc:                       # process_file already guards;
            logger.error("unhandled worker exception for %s: %s", path, exc, exc_info=True)
            try:
                result_q.put(("fail", pid, path, type(exc).__name__))
            except Exception:
                return


def _run_resilient(cfg, todo, deadline_s, workers, on_record, progress,
                   stop_fn=None, worker_target=None):
    """Drive persistent workers with a per-file watchdog. Calls on_record(rec) for
    every completed or timed-out file (unordered), then progress(). Returns once all
    of `todo` are accounted for. A stuck worker is terminated, its file recorded as a
    timeout, and a replacement spawned while work remains."""
    worker_target = worker_target or _resilient_worker
    ctx = mp.get_context("spawn")
    task_q = ctx.Queue()
    result_q = ctx.Queue()
    for p in todo:
        task_q.put(p)
    total = len(todo)
    target_workers = max(1, workers)
    wd_interval = max(1.0, min(deadline_s / 4.0, 15.0))

    procs: dict = {}
    inflight: dict = {}          # pid -> (path, start_monotonic)

    def spawn():
        pr = ctx.Process(target=worker_target, args=(cfg, task_q, result_q), daemon=True)
        pr.start()
        procs[pr.pid] = pr

    for _ in range(target_workers):
        spawn()

    accounted = 0
    last_wd = time.monotonic()
    try:
        while accounted < total:
            if stop_fn is not None and stop_fn():
                break
            try:
                msg = result_q.get(timeout=1.0)
            except queue.Empty:
                msg = None
            if msg is not None:
                kind = msg[0]
                if kind == "start":
                    _, pid, path, start = msg
                    inflight[pid] = (path, start)
                elif kind == "done":
                    _, pid, rec = msg
                    inflight.pop(pid, None)
                    on_record(rec); accounted += 1; progress()
                elif kind == "fail":
                    _, pid, path, ename = msg
                    inflight.pop(pid, None)
                    on_record(_stub_record(cfg, path, "error", f"worker_error:{ename}"))
                    accounted += 1; progress()

            now = time.monotonic()
            if now - last_wd >= wd_interval:
                last_wd = now
                for pid, (path, start) in list(inflight.items()):
                    if now - start > deadline_s:
                        pr = procs.pop(pid, None)
                        if pr is not None:
                            try:
                                pr.terminate()
                            except Exception:
                                pass
                        inflight.pop(pid, None)
                        on_record(_stub_record(cfg, path, "timeout", "watchdog_killed"))
                        accounted += 1; progress()
                # keep the pool topped up while files remain to process
                while len(procs) < target_workers and (total - accounted - len(inflight)) > 0:
                    spawn()
                if not procs and accounted < total:     # everyone died, work remains
                    spawn()
    finally:
        for pr in list(procs.values()):
            try:
                pr.terminate()
            except Exception:
                pass
        for pr in list(procs.values()):
            try:
                pr.join(timeout=2.0)
            except Exception:
                pass


def run(cfg: Config, paths: list[str], out_path: str, workers: int,
        progress_interval: float, chunksize: int, restart: bool) -> dict:
    manifest_path = out_path + ".manifest.json"
    counters: dict = {}
    lanes: dict = {}
    llm: dict = {}
    ocr_stats: dict = {}
    s2_stats: dict = {}
    resumed = False
    quiet_noisy_libraries()  # parent path (e.g. workers=1 inline) stays quiet too

    def _check_locked(path, exc):
        # Windows raises PermissionError when the CSV is open in another program
        # (almost always Excel). Give a plain instruction instead of a traceback.
        sys.stderr.write(
            f"\nERROR: '{os.path.basename(path)}' is open in another program "
            f"(usually Excel).\n       Close it and run the scan again.\n")
        raise SystemExit(2)

    if restart:
        for p in (out_path, manifest_path):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except PermissionError as exc:
                    _check_locked(p, exc)

    done: set = set()
    if os.path.exists(out_path):
        done = _load_done_and_repair(out_path, counters, lanes, llm, ocr_stats, s2_stats)
        resumed = True
        sys.stderr.write(f"resuming: {len(done):,} files already complete\n")

    todo = [p for p in paths if os.path.relpath(p, cfg.root) not in done]
    grand_total = len(paths)
    start = time.monotonic()
    started_at = datetime.datetime.now().isoformat(timespec="seconds")   # real start, not finish
    last_print = 0.0
    new_done = 0

    # Per-file hard timeout (watchdog). Default ON at 120s so one stuck file can't wedge
    # the whole run -- no need to set anything. Override with env PII_WATCHDOG_S (set it to
    # "0" to disable, or any number of seconds), or cfg.hard_timeout_s. Only active with
    # multiple workers, since it needs a worker to kill.
    _env_wd = os.environ.get("PII_WATCHDOG_S", "")
    try:
        if _env_wd != "":
            watchdog_s = float(_env_wd)               # explicit override, incl. "0" = off
        else:
            watchdog_s = float(getattr(cfg, "hard_timeout_s", 0) or 0) or DEFAULT_WATCHDOG_S
    except (TypeError, ValueError):
        watchdog_s = DEFAULT_WATCHDOG_S
    if watchdog_s > 0 and workers > 1:
        sys.stderr.write(
            f"watchdog: per-file hard timeout {watchdog_s:.0f}s "
            f"(a file that exceeds it is killed and recorded as 'timeout'; "
            f"set PII_WATCHDOG_S=0 to disable)\n")

    try:
        fh = open(out_path, "a", encoding="utf-8", newline="")
    except PermissionError as exc:
        _check_locked(out_path, exc)
    writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
    if not resumed:
        writer.writeheader()

    def progress(force=False):
        nonlocal last_print
        now = time.monotonic()
        if not force and (now - last_print) < progress_interval:
            return
        last_print = now
        completed = len(done) + new_done
        elapsed = now - start
        rate = new_done / elapsed if elapsed > 0 else 0.0
        eta = (len(todo) - new_done) / rate if rate > 0 else 0.0
        pct = (completed / grand_total * 100.0) if grand_total else 100.0
        errs = (counters.get("error", 0) + counters.get("no_parser", 0)
                + counters.get("timeout", 0))
        sys.stderr.write(
            f"\r[{completed:,}/{grand_total:,}] {pct:5.1f}% | {rate:6.1f} f/s | "
            f"elapsed {_fmt(elapsed)} | ETA {_fmt(eta)} | err {errs:,} | "
            f"BDE {counters.get('bde', 0):,}    ")
        sys.stderr.flush()

    failure_window: deque = deque(maxlen=1000)
    _auto_paused = False

    def _check_rate(rec: dict) -> bool:
        nonlocal _auto_paused
        failure_window.append(rec.get("status") in _FAILURE_STATUSES)
        if len(failure_window) < 1000:
            return False
        rate = sum(failure_window) / 1000
        if rate > 0.05:
            _auto_paused = True
            sys.stderr.write(
                f"\n[auto-paused] rolling failure rate {rate:.1%} over last 1,000 files "
                f"(error+timeout); rerun the same command to resume\n"
            )
            return True
        return False

    pool = None
    interrupted = False
    try:
        if workers <= 1:
            _init_worker(cfg)
            results = (process_file(p) for p in todo)
            for rec in results:
                writer.writerow(rec); _tally(rec, counters, lanes, llm, ocr_stats, s2_stats); new_done += 1
                if (new_done % 200) == 0:
                    fh.flush()
                if _check_rate(rec):
                    interrupted = True
                    break
                progress()
        else:
            if watchdog_s > 0:
                def on_record(rec):
                    nonlocal new_done
                    writer.writerow(rec); _tally(rec, counters, lanes, llm, ocr_stats, s2_stats); new_done += 1
                    if (new_done % 200) == 0:
                        fh.flush()
                    _check_rate(rec)
                _run_resilient(cfg, todo, watchdog_s, workers, on_record, progress,
                               stop_fn=lambda: _auto_paused)
                if _auto_paused:
                    interrupted = True
            else:
                ctx = mp.get_context("spawn")
                pool = ctx.Pool(workers, initializer=_init_worker, initargs=(cfg,))
                for rec in pool.imap_unordered(process_file, todo, chunksize=chunksize):
                    writer.writerow(rec); _tally(rec, counters, lanes, llm, ocr_stats, s2_stats); new_done += 1
                    if (new_done % 200) == 0:
                        fh.flush()
                    if _check_rate(rec):
                        pool.terminate()
                        interrupted = True
                        break
                    progress()
    except KeyboardInterrupt:
        interrupted = True
        sys.stderr.write("\n[interrupted] progress saved; rerun the same command to resume\n")
        if pool is not None:
            pool.terminate()
    finally:
        if pool is not None:
            pool.close(); pool.join()
        fh.flush(); fh.close()
        progress(force=True)
        sys.stderr.write("\n")

    summary = {
        "started_at": started_at,
        "finished_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "root": cfg.root,
        "grand_total_files": grand_total,
        "completed": len(done) + new_done,
        "newly_scanned": new_done,
        "resumed": resumed,
        "interrupted": interrupted,
        "elapsed_s": round(time.monotonic() - start, 1),
        "status_counts": counters,
        "lane_counts": lanes,
        "llm_stats": llm,
        # 3.0.0 -- OCR/DI spend is now recorded rather than inferred from the Azure portal.
        "ocr_stats": ocr_stats,
        "stage2_stats": s2_stats,
        "cost": _cost_summary(cfg, llm, ocr_stats, s2_stats),
    }
    _write_manifest(manifest_path, cfg, summary)

    # ---- OCR / DI summary (3.0.0) ------------------------------------------ #
    # Previously invisible. This is the figure to reconcile against the Azure portal.
    if ocr_stats.get("di_calls") or ocr_stats.get("img_decode_failed"):
        c = summary["cost"]
        sys.stderr.write(
            f"OCR: {ocr_stats.get('di_calls', 0):,} Document Intelligence calls over "
            f"{ocr_stats.get('files_with_di', 0):,} files "
            f"({c['di_pages_billable']:,} billable pages)\n"
            f"  full-file : {ocr_stats.get('full_file_calls', 0):,} calls "
            f"({ocr_stats.get('full_file_ok', 0):,} ok, "
            f"{ocr_stats.get('full_file_failed', 0):,} failed), "
            f"{ocr_stats.get('pages', 0):,} pages\n"
            f"  embedded  : {ocr_stats.get('img_calls', 0):,} calls "
            f"({ocr_stats.get('img_ok', 0):,} returned text) from "
            f"{ocr_stats.get('img_qualifying', 0):,} qualifying images\n")
        if "di_cost" in c:
            sys.stderr.write(f"  DI cost   : {c['di_cost']:,.2f}\n")
        if ocr_stats.get("img_decode_failed"):
            sys.stderr.write(
                f"  WARNING: {ocr_stats['img_decode_failed']:,} embedded image(s) could not be "
                f"decoded by pypdf.\n"
                f"           This is almost always a missing Pillow, which SILENTLY disables OCR of\n"
                f"           content images in text PDFs -- normally the bulk of the OCR workload.\n"
                f"           Run: pip install Pillow\n")
    elif cfg.use_ocr:
        sys.stderr.write("OCR: enabled but 0 Document Intelligence calls were made.\n")

    # ---- stage-2 summary --------------------------------------------------- #
    if s2_stats:
        skipped = {k[5:]: v for k, v in s2_stats.items() if k.startswith("skip:")}
        levels = {k[6:]: v for k, v in s2_stats.items() if k.startswith("level:")}
        sys.stderr.write(
            f"stage 2: graded {s2_stats.get('ran', 0):,} file(s) "
            f"(responsive {s2_stats.get('responsive', 0):,}, NR {s2_stats.get('nr', 0):,}); "
            f"{s2_stats.get('tokens', 0):,} tokens\n")
        if levels:
            sys.stderr.write("  levels: "
                             + ", ".join(f"{k}={v:,}" for k, v in sorted(levels.items())) + "\n")
        if skipped:
            sys.stderr.write("  skipped: "
                             + ", ".join(f"{k}={v:,}" for k, v in sorted(skipped.items())) + "\n")

    if cfg.use_llm and llm.get("ambiguous"):
        amb = llm.get("ambiguous", 0)
        ok = llm.get("consulted", 0)
        failed = llm.get("failed", 0)
        sys.stderr.write(
            f"LLM: {ok}/{amb} ambiguous files judged by the AI "
            f"(cleared {llm.get('cleared', 0)}, responsive {llm.get('responsive', 0)}); "
            f"{failed} fell back to the rules flag.\n")
        sys.stderr.write(
            f"  tokens consumed: {llm.get('tokens', 0):,} "
            f"(total Azure OpenAI tokens for this run -- enter in the tracker)\n")
        if failed:
            classes = ", ".join(f"{k}={v}" for k, v in sorted(llm.items())
                                if k.startswith("fail:") or k.startswith("not_run"))
            sys.stderr.write(
                f"  WARNING: {failed} ambiguous file(s) did NOT reach the AI and were "
                f"flagged by the recall-first fallback -- a likely over-call source.\n"
                f"  failure breakdown: {classes or 'n/a'}\n")
    return summary