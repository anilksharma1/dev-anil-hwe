# pii_triage — Tool Overview

A start-here overview of the **pii_triage** tool: what it is, what it does, how to run it, how to
track its tests, what it reports, and the common failure scenarios with their workarounds.

For depth, see the companion docs:

| Doc | Covers |
|---|---|
| [`README.md`](README.md) | Scaled deployment, dependencies, environment variables |
| [`RUNBOOK.md`](RUNBOOK.md) | Operator runbook — deploy, rollback, incident response |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Workflow diagrams, HLD & LLD per initiative |
| [`PROJECT_OVERVIEW.md`](PROJECT_OVERVIEW.md) | Detailed project/pipeline walkthrough |
| [`UI_OVERVIEW.md`](UI_OVERVIEW.md) | Detailed operator-UI reference |
| [`pii_triage_merged/README.md`](pii_triage_merged/README.md) | The library and local CLI |

---

## 1. What it is

**pii_triage** (v3.0.0) is the **HWE Bucketing & Tagging** stage of a breach-notification review
pipeline. It is a read-only, parallel, crash-safe scanner that inspects a corpus of files for
personally identifiable information (PII) and produces **one inventory row per file** — entity
*type* counts and a routing *label*, never the values themselves.

It ships in two forms from one codebase:

- **A local CLI** (`python -m pii_triage`) for single-machine runs — the triage engine lives in
  `pii_triage_merged/`.
- **A scaled fleet** — Docker workers on Azure Container Apps that pull files off a queue, driven
  by a local **operator UI** (`hwe_scaled_ui.py`). This is the repo root.

**The one guarantee that shapes everything:** *no PII value is ever stored, logged, or output.*
Detection counts occurrences into a local set that is discarded on return; only entity **type
counts** and **routing labels** ever leave a function or land in the CSV.

## 2. What it does

For every file, in one shared pass (extract → OCR → detect), then two decision stages:

1. **Extract** text + metadata (25+ formats, read-only).
2. **OCR** image-only files and embedded PDF images via Azure Document Intelligence (opt-in).
3. **Detect** entity types (Name, SSN, Address, Email, …) — counts only, values discarded.
4. **Stage 1 — NR removal:** classify ambiguity, consult the LLM on ambiguous files
   (responsiveness), run a separate BDE person-count, and assign one of **11 lanes**. This is the
   decision that clears non-responsive files.
5. **Stage 2 — dataset overview:** for files that survived stage 1, a graded LLM call
   (`clear_yes` … `clear_no`) describes what's there without ever overturning stage 1.

The outputs feed the two HWE deliverables — **Table 1** (searchable files, per-file) and **Table
2** (non-searchable files, sampled → reviewed → extrapolated). See
[`ARCHITECTURE.md`](ARCHITECTURE.md) for the full workflow diagram and design.

## 3. How to run

### 3.1 Local CLI (single machine)

```bash
pip install -r pii_triage_merged/requirements.txt          # optional parsers + Pillow

# Full scan (one pass, both stages)
python -m pii_triage scan /corpus --out inventory.csv --workers 16 \
    --ocr --llm --bde-threshold 7 --protocol protocol.pdf

# Deliverables
python -m pii_triage report   inventory.csv --out table1_searchable.csv
python -m pii_triage sample   inventory.csv --out sample.csv --rate 0.05
#   reviewers fill gold_responsive / gold_bde, then:
python -m pii_triage estimate inventory.csv sample.csv --out table2_nonsearchable.csv
python -m pii_triage benchmark inventory.csv gold.xlsx
```

`--ocr`/`--llm` are opt-in; without them the run is rules-only and makes **no network calls**.
Resume is automatic — rerun the same `scan` command; the CSV is the progress record. Full flag
reference: [`pii_triage_merged/README.md`](pii_triage_merged/README.md).

### 3.2 Scaled fleet (distributed)

The normal path is: build/deploy once from the CLI, then drive everything else from the app.

```powershell
scaling-lib acr-release                 # build + deploy the worker image (CLI only)
./HWE_Scaled.cmd                        # launch the operator UI — do the rest here
```

From the app: submit a corpus, monitor progress, collect results, report, sample, score, compare.
The CLI equivalents (`enqueue.py`, `collect_outputs.py`, `python -m pii_triage …`) exist for
automation. Full procedure: [`RUNBOOK.md`](RUNBOOK.md).

## 4. Tracking tests

The test suite is the contract. It runs **offline** — OCR and LLM are injected as fake callables,
so no Azure and no corpus are needed.

### 4.1 Running the tests

```bash
# Library engine — 16 modules, 300+ tests (config in pii_triage_merged/pytest.ini)
cd pii_triage_merged && python -m pytest tests/ -q      # README reports: 273 passed, 4 skipped

# Operator UI — 46 tests, at the repo root
python -m pytest test_hwe_scaled_ui.py -q
```

### 4.2 What the suites pin

| Suite | Guards |
|---|---|
| `test_csv_contract.py` | the inventory columns, pinned by name **and order** |
| `test_stage_split.py` | the shared pass runs exactly once; stage 2 is gated and isolated |
| `test_stage2_levels.py` | the five stage-2 levels and the recall-safe defaults |
| `test_ocr_accounting.py` | DI call/page accounting, including the missing-Pillow case |
| `test_integration.py` | end-to-end over a real directory, incl. **no-PII-in-output** |
| `test_frozen_check.py` | the frozen-NR checker (CRLF-tolerant; catches real edits) |
| `test_triage.py`, `test_money.py`, `test_roster_*`, `test_router_bde.py`, `test_edge_cases.py` | detection, bucketing, lane routing, extrapolation |

### 4.3 The frozen NR gate — run this after every change

Clearing a document that contains real PII is the one unrecoverable error, so the stage-1 decision
surface is pinned by hash. Run the check after any edit:

```bash
cd pii_triage_merged && python tools/check_nr_frozen.py check   # exit 0 clean, 1 on drift, 2 if golden missing
```

A clean run is the proof an edit didn't touch NR behaviour. Adding new code (as stage 2 does) does
not drift it; *modifying* `detection.py`, `routing.py`, `enrich.apply_llm`,
`azure_clients.llm_classify`, or `_SYSTEM_PROMPT` does. `capture` re-blesses the golden and is a
reviewed action, not a fix.

### 4.4 Before-you-ship checklist

1. `pytest tests/ -q` (library) and `pytest test_hwe_scaled_ui.py -q` (UI) both green.
2. `check_nr_frozen.py check` exits 0 (or the drift is intended and re-captured with review).
3. **Known skips (4):** `test_scanned_bde.py`, `test_bde_tier_distinct.py`, and two in
   `test_pdf_open_bypass.py` describe a partial-coverage-OCR design not built in this version — they
   skip with reasons rather than fail. If your count of skips changes, investigate.

## 5. Reporting (as applicable)

Everything the tool reports is labels, counts, and routing decisions — never PII values.

| Report | Produced by | Contents |
|---|---|---|
| **`inventory.csv`** | `scan` / `collect_outputs.py` | one row per file — the full `FileRecord` (searchable, entity types, categories, estimate, lane, stage-2 verdict, OCR/DI accounting) |
| **Run manifest** `<out>.manifest.json` | `scan` | totals, status/lane counts, LLM & OCR stats, stage-2 stats, and a `cost` block (money only when price flags are supplied); rebuilt correctly across resumes |
| **HWE Table 1** | `report` | per-file inventory for searchable files |
| **HWE Table 2** | `sample` → reviewers → `estimate` | non-searchable population, per-bucket %s extrapolated to the full corpus |
| **Benchmark** | `benchmark` | precision / recall / F1 vs a yes/no gold sheet, **misses first** |
| **Scorecard** `scorecard_*.xlsx` | `tools/score_combined.py` | NR/R accuracy (stage 1 / stage 2 / pipeline), BDE accuracy, OCR + LLM cost, per-file-type breakdowns, against an entities export |
| **Timing snapshot** `*_timing.json` | `collect_outputs.py` | tokens, per-task checkpoints, worker-hours cost (scaled runs) |
| **Live monitor** | Operator UI `/api/monitor` | authoritative per-state counts + throughput/ETA, read from the Azure status Table |

**Cost accounting notes.** Sum `di_calls` across the inventory for your billable Document
Intelligence total. LLM cost is reported as a **range**, not a point — the API returns only
`total_tokens`, so inventing an in/out split would be worse than bounds. Money figures appear only
when the matching `--price-per-1k-*` flag is supplied; nothing is priced at a guessed rate.

## 6. Common failure scenarios and workarounds

### 6.1 Scan / detection (local + worker)

| Symptom | Cause | Workaround |
|---|---|---|
| `--ocr` on but **0 Document Intelligence calls** | Nothing was image-only, **or Pillow is missing** (embedded-image OCR silently no-ops) | Check the startup dependency line and the `img_decode_failed` column; `pip install Pillow`. Never compare a with-Pillow run to a without-Pillow one. |
| OCR volume suddenly jumped between runs | Someone installed Pillow | The higher number is correct; the earlier run was under-reading. |
| Files show `llm_failed:RateLimitError` | Concurrency exceeds the Azure OpenAI deployment's RPM | Lower `--workers` (local) or the Container App max-replica count; retries with backoff are already in place. |
| A run stalls — steady CPU, no completions | A parser is spinning on a malformed file | The watchdog (default 120 s) kills and replaces the worker and records `timeout`. Don't disable it (`PII_WATCHDOG_S=0`). |
| Run auto-pauses with `[auto-paused]` | Rolling failure rate exceeded 5 % | Investigate the error class in the CSV/manifest, fix the cause, then rerun to resume. |
| `ERROR: 'inventory.csv' is open in another program` | Excel holds a Windows file lock | Close Excel and rerun — progress is preserved. |
| Many files land in `needs_parser` / `convert_lane` | Optional parser missing, or legacy Office needs conversion | Install the optional lib (`requirements.txt`); `.doc/.xls/.ppt` convert automatically via LibreOffice headless if `soffice`/`libreoffice` is on PATH (`SOFFICE_PATH` to override). |
| Stage 2 shows `skip:not_searchable` for many files | Those files yielded no text | Decide whether `--ocr` should be on for that corpus. |

### 6.2 Scaled fleet (queue / workers / Azure)

| Symptom | Cause | Workaround |
|---|---|---|
| `scaling-lib`/`az` commands fail with auth errors | Wrong subscription, or stale MSAL cache | `az account show` to confirm prod; delete `~/.azure/msal_*cache.bin` and `az login` again. |
| Dead-letter count rising; replicas restart | Repeated task failures tripped the DLQ circuit-breaker | Read a dead-lettered message's error. Bad build → roll back; dependency outage → pause enqueuing. Tune `DLQ_FAILURE_RATE` / `DLQ_MIN_COMPLETIONS`. |
| Queue depth flat, no completions, replicas idle | Container App not scaling, or a wedged file | Check the *Scale* rule and raise the max-replica ceiling; a single wedged file times out via `FILE_TIMEOUT_S` and dead-letters rather than blocking. |
| Results wrong right after a deploy | Bad image | **Roll back:** `scaling-lib acr-deploy --tag <good-sha>`, and pause enqueuing until the queue drains. |
| Working tree ≠ what workers run | Deployed image tag differs from local git SHA | The UI *Setup* screen flags this; rebuild/redeploy (`acr-release`) before trusting a run. |

Full incident-response playbooks (symptom → diagnose → fix) are in
[`RUNBOOK.md`](RUNBOOK.md#5-incident-response).

### 6.3 Tests / release gate

| Symptom | Cause | Workaround |
|---|---|---|
| `check_nr_frozen.py` reports DRIFT | An edit touched a frozen stage-1 surface — **or** a CRLF-only diff on an old checker | If the change is intended, review and `capture`; if it's line-endings, update to the CRLF-tolerant checker (pinned by `test_frozen_check.py`). |
| Scoring says the ID match rate is low | Control IDs and `file_name` aren't lining up | See `pii_triage_merged/tools/SCORING.md`; note `os.path.splitext` mishandles IDs like `Q00897.01-0000000003`. |
| More than 4 skipped tests | A real dependency/import regressed, or the partial-OCR design changed | Investigate the newly-skipped module rather than ignoring it. |

---

*This overview is intentionally concise. When a section says "see X," that doc is the source of
truth — this file just points you to the right one.*
