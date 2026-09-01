"""OCR and LLM enrichment orchestration.

These functions are deliberately pure: they take the OCR / LLM callables as
arguments rather than importing Azure directly, so the wiring can be tested with
fakes and runs unchanged whether or not Azure is configured. The real Azure
implementations live in azure_clients.py; the runner injects them (or None).

Contracts:
  ocr_fn(path, cfg) -> (text, meta)         meta like an extractor's
  llm_fn(text, cfg) -> {"responsive": bool, "names": [str], "person_count": int,
                        "reasoning": str}
"""
from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


def apply_ocr(path, text, meta, cfg, ocr_fn):
    """OCR a non-searchable file so it can flow through normal detection.

    Only fires on image-only files when an OCR function is available. On failure
    the original (non-searchable) text/meta are returned with a detail note, so a
    bad OCR call degrades to the old sampling path rather than dropping the file.

    3.0.0 -- COST ACCOUNTING. This function REPLACES meta on success, which rewrites
    text_extractable from "image_only" to "text". That is why the inventory's
    text_extractable column counts OCR *failures* rather than OCR *runs*, and why OCR
    volume was previously unmeasurable from the tool's own output. Two keys are now
    always set so the attempt survives the meta replacement:

        ocr_attempted : bool  -- a billable DI call was made for this file
        ocr_pages     : int   -- pages DI reported (0 if the call failed)

    `ocr` (success) is unchanged and still only set on success.
    """
    if ocr_fn is None or meta.get("text_extractable") != "image_only":
        return text, meta
    try:
        otext, ometa = ocr_fn(path, cfg)
    except Exception as exc:  # noqa: BLE001 -- never let one file kill the run
        _log.warning("OCR failed for %s: %s", path, exc, exc_info=True)
        meta = dict(meta)
        meta["detail"] = f"ocr_failed:{type(exc).__name__}"
        meta["ocr_attempted"] = True      # billable even though it failed
        meta["ocr_pages"] = 0
        return text, meta
    ometa = dict(ometa)
    ometa.setdefault("text_extractable", "text")
    ometa["ocr"] = True
    ometa["ocr_attempted"] = True
    ometa["ocr_pages"] = int(ometa.get("page_or_sheet_count", 0) or 0)
    return otext or "", ometa


def apply_image_ocr(text, meta, cfg, ocr_fn):
    """OCR content-sized images embedded in a text PDF and append their text.

    Only fires when OCR is enabled and x_pdf found qualifying images in meta["content_images"]
    (list of (page_1indexed, jpeg_bytes) pairs). Each image is written to a temp JPEG, passed
    to Azure DI, then deleted. Failures for individual images are logged and skipped.

    Returns (text, summary_str | None, stats). `stats` is ALWAYS a dict:

        {"qualifying": int,   # images x_pdf found (== len(content_images))
         "calls": int,        # DI calls actually attempted (excludes the <1KB pre-call skips)
         "ok": int,           # calls that returned text
         "failed": int,       # calls that raised
         "skipped_small": int,# qualifying images never sent (under 1KB)
         "chars": int}        # characters appended

    3.0.0 -- COST ACCOUNTING FIX. This function used to return only (text, None) when no
    image yielded text, so a PDF whose images all OCR'd to nothing consumed DI calls and left
    NO trace anywhere: not in the CSV, not in the manifest. Embedded-image OCR is the single
    largest OCR cost centre (24,223 of ~25,473 DI attempts on the 208k CNG corpus), so it was
    also the least observable. `stats` is now returned unconditionally, and the caller records
    it in dedicated columns. Note "qualifying" vs "calls": the legacy `detail` note reported
    len(content_images), i.e. images FOUND, which over-counts actual DI calls by the number of
    sub-1KB skips. Both are recorded so the two can be reconciled.
    """
    stats = {"qualifying": 0, "calls": 0, "ok": 0, "failed": 0, "skipped_small": 0, "chars": 0}
    content_images = meta.get("content_images")
    if not content_images or ocr_fn is None or not getattr(cfg, "use_image_ocr", True):
        return text, None, stats
    import os, tempfile
    stats["qualifying"] = len(content_images)
    extra_parts = []
    ocrd_pages = []
    for page_num, img_bytes in content_images:
        if len(img_bytes) < 1024:
            _log.debug("content image p%d skipped: %d bytes (too small for DI)", page_num, len(img_bytes))
            stats["skipped_small"] += 1
            continue
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
                f.write(img_bytes)
                tmp_path = f.name
            stats["calls"] += 1          # count the ATTEMPT: it is billable either way
            img_text, _ = ocr_fn(tmp_path, cfg)
            if img_text:
                stats["ok"] += 1
                extra_parts.append(img_text)
                ocrd_pages.append(page_num)
                _log.debug("content image p%d: %d chars from OCR", page_num, len(img_text))
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            _log.warning("content image OCR failed (p%d): %s: %s", page_num, type(exc).__name__, exc, exc_info=True)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
    if not extra_parts:
        # Still return the stats: this is the case that used to vanish.
        if stats["calls"]:
            return text, f"img_ocr:none({stats['calls']} calls,0 chars)", stats
        return text, None, stats
    chars_added = sum(len(p) for p in extra_parts)
    stats["chars"] = chars_added
    combined = text + "\n" + "\n".join(extra_parts)
    total = len(content_images)
    pages_str = ",".join(f"p{p}" for p in ocrd_pages)
    summary = f"img_ocr:{pages_str}({len(ocrd_pages)}/{total},+{chars_added}chars)"
    return combined[: cfg.max_scan_chars], summary, stats


def apply_llm(rec, text, cfg, llm_fn):
    """Consult the LLM ONLY on ambiguous files; let its call decide responsiveness.

    Mutates `rec` in place: records the decision, and if the LLM found more people
    than the rules did, raises the entity estimate (caller re-derives bucket/BDE).
    A failed call leaves the rules' result intact with a detail note.
    """
    if llm_fn is None or rec.ambiguity != "ambiguous":
        return
    try:
        result = llm_fn(text, cfg) or {}
    except Exception as exc:  # noqa: BLE001
        _log.warning("LLM call failed for %s: %s", getattr(rec, "file_name", "?"), exc, exc_info=True)
        rec.detail = (f"{rec.detail} " if rec.detail else "") + f"llm_failed:{type(exc).__name__}"
        return
    rec.llm_consulted = True
    rec.llm_tokens = int(result.get("tokens") or 0)
    rec.llm_responsive = "yes" if result.get("responsive") else "no"
    names = result.get("names") or []
    try:
        person_count = int(result.get("person_count"))
    except (TypeError, ValueError):
        person_count = 0
    if person_count <= 0:
        person_count = len(names)
    if person_count > rec.estimated_entities:
        rec.estimated_entities = person_count
    if names and "Name" not in (rec.entities_found or ""):
        rec.entities_found = "Name | " + rec.entities_found if rec.entities_found else "Name"


def apply_bde_count(rec, text, cfg, bde_count_fn):
    """Authoritative BDE person-count -- INDEPENDENT of the responsiveness (NR) call.

    Asks a COUNT-ONLY LLM (see _BDE_COUNT_PROMPT) how many DISTINCT people have PII beyond a
    bare name, per the matter protocol, and records it in rec.bde_person_count. The caller
    decides the BDE tier from this count, so a 1-person ledger with hundreds of PII tokens
    reads as 1, and a 10-person roster reads as ~10.

    Fires on any searchable file with readable text whose candidate-entity estimate reaches
    cfg.bde_count_min_entities (a file with fewer PII tokens than that cannot hold that many
    distinct people -- so this prunes the calls that can only ever be non-BDE without missing a
    real one). No file-type restriction and NO 'already over threshold' skip: an inflated
    estimate is exactly what we want the LLM to correct.

    NR-safety: never reads or writes rec.llm_responsive or rec.estimated_entities -- it only
    writes the separate rec.bde_person_count. Returns True iff the LLM count actually ran.
    """
    if bde_count_fn is None or not rec.searchable:
        return False, False
    if not (text or "").strip():
        return False, False
    floor = int(getattr(cfg, "bde_count_min_entities", 7) or 7)
    # Eligible = any structured file (an under-read roster reads as ~0 rows but may hold many
    # people -> always ask), OR any file whose candidate-entity estimate reaches the floor
    # (a file with fewer PII tokens cannot hold `floor`+ distinct people).
    if not (rec.is_structured or int(rec.estimated_entities or 0) >= floor):
        return False, False
    try:
        # Give the counter the file's approximate size so it can tell it is seeing a partial
        # sample of a large roster (the row parser / token estimate knows the magnitude even
        # when the LLM only sees the first chunk of text).
        approx = int(rec.estimated_entities or 0)
        hint = (f"CONTEXT: this file has approximately {approx} data rows/records; the text below "
                "may be only a partial sample. If it is a per-person roster, the true number of "
                "data subjects is about that many.\n\n") if approx >= floor else ""
        result = bde_count_fn(hint + (text or ""), cfg) or {}
    except Exception as exc:  # noqa: BLE001
        _log.warning("BDE count call failed for %s: %s", getattr(rec, "file_name", "?"), exc, exc_info=True)
        rec.detail = (f"{rec.detail} " if rec.detail else "") + f"bde_count_failed:{type(exc).__name__}"
        return False, False
    rec.llm_tokens = int(rec.llm_tokens or 0) + int(result.get("tokens") or 0)
    try:
        person_count = int(result.get("person_count"))
    except (TypeError, ValueError):
        person_count = 0
    is_roster = bool(result.get("is_roster"))
    # The LLM count is authoritative for the BDE tier (it may be LOWER than the token
    # estimate -- that is the point). Writes only bde_person_count; NR is untouched.
    rec.bde_person_count = max(0, person_count)
    rec.detail = (f"{rec.detail} " if rec.detail else "") + ("bde_llm_count_roster" if is_roster else "bde_llm_count")
    return True, is_roster