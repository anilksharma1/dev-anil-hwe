# pii_triage 3.0.0 — merged single-pass build

Anna's v2.10.2 and Daniel's v2.9.9 combined into one tool that extracts, OCRs and detects
**once**, then runs both decision stages over that single shared read.

```
discover
  └─ per file, one worker:
     SHARED PASS (once)      extract → apply_ocr → apply_image_ocr → detect → estimate
     STAGE 1 (Anna's)        classify_ambiguity → apply_llm → apply_bde_count → choose_lane
                             └─ nr_stage1 = (lane == "likely_non_responsive")
     STAGE 2 (Daniel's)      runs ONLY if nr_stage1 is False
                             graded LLM → 5 levels → s2_lane → s2_nr
     one CSV row, 49 columns
```

## Decisions taken

| # | Decision |
|---|---|
| 1 | Built on Anna's `azure_clients.py`. **`scaling_lib` dropped entirely** — it was undeclared, absent, and its two guard probes made Daniel's OCR and LLM silently return `None`. Anna's `_retry` / `_hard_timeout` / `_is_transient` layer is retained (13 failures in 187,690 calls vs 49-in-64 without it). |
| 2 | Stage 2 gates on stage 1, matching the workflow already in production. |
| 3 | OCR is instrumented first-class — see below. |
| 4 | **`llm_reasoning` removed outright**, not flag-gated. It is dropped inside `llm_classify_graded` so it cannot reach the CSV even by accident. |
| 5 | Test suite repaired before anything else. |
| 6 | **Pillow declared and probed** (new). |

## Stage 1 is a verified behavioural no-op

- `tools/check_nr_frozen.py check` **passes**. `detection.py`, `enrich.apply_llm`,
  `azure_clients.llm_classify` and `_SYSTEM_PROMPT` are byte-identical to 2.10.2.
- `routing.py` drifted once, for the schema addition only. All six routing functions
  (`choose_lane`, `classify_ambiguity`, `roster_entity_estimate`, `estimate_entities`,
  `bucket_of`, `complexity_bucket`) were verified **AST-identical** before re-capture, and
  the golden records why plus a `reviewer: PENDING` field. The pre-merge golden is kept at
  `tools/nr_frozen_golden.v2_10_2_base.json`.
- Same corpus through 2.10.2 and 3.0.0: **0 differences across 9 files × 27 legacy columns.**

## OCR accounting — the numbers that were previously invisible

Three defects fixed:

1. `apply_ocr` **replaces** meta on success, rewriting `text_extractable` from
   `image_only` to `text`. So that column counted OCR *failures*, and a successfully OCR'd
   file was identifiable nowhere. `ocr_attempted` / `ocr` / `ocr_pages` now survive it.
2. `apply_image_ocr` returned `(text, None)` whenever no image yielded text, so a PDF whose
   images all OCR'd to nothing burned billable DI calls and left **no trace anywhere**. It
   is ~95% of DI calls on the CNG corpus and was the least observable part of the tool. It
   now returns a stats dict unconditionally.
3. `_pdf_extract_content_images` needs Pillow for `img.image`, inside a bare
   `except Exception: continue`. **With no Pillow every image is silently skipped and
   embedded-image OCR becomes a no-op** — which is what happened in Daniel's run: 0 embedded
   calls against Anna's **14,441 on the 99,514 files both builds processed** (Anna's figure over
   the full 208k corpus is 24,223). Pillow is now in
   `requirements.txt`, reported by `optional_dependency_report()`, warned about at startup
   when `--ocr` is set, and decode failures are counted per file.

New columns (9, counting `elapsed_s`): `ocr_attempted`, `ocr`, `ocr_pages`,
`img_ocr_qualifying`, `img_ocr_calls`, `img_ocr_ok`, `img_decode_failed`, `di_calls`,
`elapsed_s`. Manifest gains `ocr_stats`, `stage2_stats`
and a `cost` block. **Sum `di_calls` over the CSV to get the billable call total** — verified
against the actual call count in the smoke run.

`img_ocr_qualifying` vs `img_ocr_calls` matters: the legacy `detail` note reported images
*found*, which over-counts calls by the number of sub-1KB pre-call skips. Both are recorded
so the 24,223 historical figure can be reconciled.

## Schema: 49 columns

The **27 legacy columns keep their names, meanings and order**, so `report.build_table1`,
`benchmark.run_benchmark` and the offline BDE scorer all work unchanged — verified by running
all three. Appended: 9 OCR accounting, 2 derived (`nr_stage1`, `bde_stage1`), 10 `s2_*`,
1 rollup (`llm_tokens_total`). `tests/test_csv_contract.py` pins the exact list and order.

## The asymmetry, on purpose

Stage 1's prompt rounds genuine uncertainty **up** to responsive. Stage 2's expresses it as
`borderline`, which routing then treats as non-responsive. **So stage 2 clears on uncertainty
where stage 1 flags**, which is exactly why stage 1 owns NR removal and stage 2 only describes
what survived it. Pinned by `test_borderline_clears_at_stage2_but_stage1_is_untouched`.

Recall-safe defaults: an **unrecognised** level never clears (`test_unknown_level_does_not_clear`);
a stage-2 LLM failure leaves all 27 stage-1 fields untouched; with no stage-2 LLM available the
stage falls back to the same recall-first rules path stage 1 uses.

## New CLI flags

```
--no-stage2              stage 1 only (reproduce a 2.10.2 inventory exactly)
--stage2-on-all          also grade files stage 1 cleared (ungated; costs extra LLM calls)
--s2-bde-threshold N     stage-2 BDE threshold (default: same as --bde-threshold)
--jurisdiction us|non-us wired into the stage-2 prompt (Config.jurisdiction was dead before)
--ocr-max-pages N        0 = whole document. Was a hardcoded 15 with no way to change it.
--no-image-ocr           skip embedded-image OCR — use this to measure what it contributes
--price-per-1k-in/-out   Azure OpenAI pricing for the cost summary
--price-per-1k-pages     Document Intelligence pricing
```

`--ocr-max-pages` defaults to the existing 15 so the merge stays a no-op. Changing it is a
deliberate, separately-validated behaviour change.

## Tests: 273 passing, 4 skipped

New files: `test_stage_split.py` (14), `test_stage2_levels.py` (13),
`test_ocr_accounting.py` (16), `test_csv_contract.py` (10), `test_integration.py` (10).
Plus `pytest.ini` and `conftest.py` — there was no test config in any prior tree, and a bare
`pytest tests/` used to *abort* on collection rather than report.

`test_integration.py` is the first end-to-end test in any version: it runs `runner.run()` over
a real directory with counting fakes and asserts one extract + one OCR + one detect per file,
correct gating, idempotent resume, and — for the first time — the **no-PII-in-output guarantee**
over a produced CSV and manifest using sentinel values.

### 4 skipped, deliberately

`test_scanned_bde.py`, `test_bde_tier_distinct.py` and 2 tests in `test_pdf_open_bypass.py`
import `apply_scanned_bde`, `structured_subject_count`, `_first_n_pages_pdf` and expect a
`meta["ocr_sample_text"]` contract. **None of these exist in any of the three versions.** They
describe a coherent 2.9.17–2.9.19 *partial-coverage OCR* design: OCR a page sample of a large
scan, keep the file non-searchable so the sampled text never reaches the NR decision, and
extrapolate a BDE count from sample + page ratio. That is directly relevant to OCR cost, but
it is not this merge and it would change the OCR contract. Skipped with pointed reasons rather
than deleted, so the specification survives. **This is the open question (Q-2).**

## Not done

- `--filter-inventory` / `--filter-exclude-lanes` (Daniel's) are **not** carried over. Once
  both stages run in one pass they are no longer the workflow; if you want them back as a
  resume path, say so.
- Daniel's table-aware detection is **deliberately excluded**: `detect()` accepted a `tables`
  argument and never read it, `detect_labeled_value_cells()` had zero call sites, and no
  rulepack declares that method. The `pdfplumber` dependency and its per-PDF table pass went
  with it.
- Daniel's `route` label-widening in `detection.py` is **not** adopted — it changes
  `entities_found` / `pi_categories` for all files with `labeled_value` entities and widens LLM
  volume. It belongs in its own change with its own before/after.
- New Anna's project-rules layer (`projects.py`, `ingest.py`, `score_bde.py`) — Phase 4.
