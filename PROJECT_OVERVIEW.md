# pii_triage — Project Overview (Detailed)

**Package:** `pii_triage` v3.0.0 — the **HWE Bucketing & Tagging** stage.
**Repo:** `New-HWE/` (branch `combined-scaling`).

pii_triage scans a corpus of files for personally identifiable information (PII),
produces one **inventory row per file** (entity-type *counts* and a routing *label*),
and feeds two downstream deliverables:

- **Table 1** — the searchable-file inventory (per-file detail).
- **Table 2** — the non-searchable population, sampled + reviewed + extrapolated.

**The one hard guarantee that shapes everything:** *no PII value is ever stored, logged, or
output.* Detection counts occurrences into a local set that is discarded on return; only entity
**type counts** and **routing labels** ever leave a function or land in the CSV.

Version 3.0.0 adds a **second LLM stage** — a graded responsiveness overview — that reuses the
same extraction pass as stage 1, so no file is parsed twice.

---

## 1. What the pipeline does, end to end

Every file flows through one decision tree. Early exits assign a lane and stop; later stages only
run if the file survives to them.

```
file_path
  │  size > MAX_BYTES ─────────────────────────► manual_oversize
  ▼
extractors.py  (extract text + metadata, read-only)
  │  unsupported extension ────────────────────► needs_parser
  │  extraction error ─────────────────────────► review_error
  │  container (zip/pst/ost/nsf/mbox) ─────────► container_expand
  │  legacy Office (.ppt/.ods) ────────────────► convert_lane
  ▼
image_only?  ── YES + USE_OCR ─► Document Intelligence OCR ─► (continue)
             └─ YES + no OCR ───────────────────► nonsearchable_sample
  ▼
detection.py  (count entities by type; values discarded)
  ▼
routing.py — ambiguity classification
  │  clear_non_responsive (no signals) ────────► likely_non_responsive
  │  clear_responsive (strong id, e.g. SSN) ───► skip LLM → routing
  │  ambiguous (weak signals only) ── USE_LLM ─► Azure OpenAI classify → routing
  ▼
routing.py — final lane
  │  spreadsheet + entity rows ≥ BDE_THRESHOLD ─► structured_bde
  │  entities ≥ BDE_THRESHOLD ──────────────────► bde
  └─ entities < BDE_THRESHOLD ──────────────────► standard
```

**Ambiguity gate** decides whether the LLM is consulted at all:
- `clear_responsive` — a strong identifier (SSN, etc.) is present → **no LLM needed**.
- `clear_non_responsive` — zero signals → **no LLM needed**.
- `ambiguous` — weak signals only (keyword hit, name only, money only) → **LLM consulted if enabled**.

**LLM output** (when consulted): responsiveness level (`clear_yes` / `likely_yes` / `borderline` /
`likely_no` / `clear_no`), a person count, and a one-sentence rationale. It is stored in the
`FileRecord` and *overrides* the rules-based ambiguity result.

### The ten lanes

| Lane | Condition |
|---|---|
| `standard` | responsive, searchable, `< BDE_THRESHOLD` entities |
| `bde` | responsive, searchable, `≥ BDE_THRESHOLD` (default 51) |
| `structured_bde` | responsive spreadsheet, `≥ 51` entity rows |
| `likely_non_responsive` | no signals detected |
| `nonsearchable_sample` | image / OCR-needed → manual sample (Table 2) |
| `container_expand` | zip / pst / ost / nsf / mbox |
| `convert_lane` | legacy `.ppt` / `.ods` |
| `needs_parser` | unsupported extension |
| `manual_oversize` | exceeds size cap |
| `review_error` | extraction failure |

---

## 2. Core library — `pii_triage_merged/pii_triage/`

| Module | Responsibility |
|---|---|
| `cli.py` / `__main__.py` | CLI entry point & command parser (`scan`, `report`, `sample`, `estimate`, `benchmark`). |
| `config.py` | Loads the **Master List** / rulepack from YAML/JSON or the built-in default. 11 PI-Type categories (Contact, Government ID, Birth, Financial, Access, Health, Biometric, Family, Demographic, Student, Work). Custom rulepacks inherit unspecified keys from the default. |
| `detection.py` | Counts entity occurrences; never stores values. Eight detection methods (see below). |
| `extractors.py` | Read-only parsers for 25+ formats, returning `(text, metadata)`. |
| `routing.py` | Defines `FileRecord` (one CSV row per file), ambiguity classification, and lane assignment. |
| `runner.py` | Parallel scan orchestrator (multiprocessing); crash-safe CSV resume. |
| `azure_clients.py` | Wrappers over Azure OpenAI & Document Intelligence (via scaling-lib clients). |
| `enrich.py` | Per-file OCR / LLM orchestration; called only when flags are on and only on ambiguous files. |
| `report.py` | Builds **Table 1** (searchable files). |
| `sampling.py` | Builds **Table 2**: `draw_sample` → reviewers code → `estimate` extrapolates. |
| `benchmark.py` | Scores an inventory against a gold-standard sheet (precision / recall / F1; misses first). |
| `conversion.py` | Legacy Office → OOXML via Win32 COM (Windows only). |

### Detection methods (`detection.py`)

| Method | Entity examples |
|---|---|
| `regex` | EMAIL, PHONE, PAYMENT_CARD (+ Luhn check) |
| `keyword` | HEALTH_INFO, BIOMETRIC |
| `name` | NAME (title/salutation heuristic, or spaCy NER when `--ner`) |
| `address` | ADDRESS (street, or "City, State ZIP") |
| `ssn` | SSN (validates area/group/serial) |
| `labeled_value` | PASSPORT, DRIVER_LICENSE, DOB |
| `money` | MONEY (currency symbols, formatted amounts) |

### Extraction (`extractors.py`)

Natively handles txt / csv / html / xml / rtf / eml. With optional libraries: docx, pptx, xlsx,
pdf, msg, xls. Flags images, zips, legacy Office, and PST/OST for downstream routing rather than
attempting to parse them inline.

### Table 2 workflow (`sampling.py`)

```
nonsearchable_sample lane
  ▼ draw_sample — stratified by complexity bucket
    (1–4 / 5–10 / 11–50 / 51+ pages; default 5% rate; fixed seed)
  ▼ manual review — coders fill gold_responsive + gold_bde
  ▼ estimate — per-bucket % extrapolated to the full population → Table 2
```

---

## 3. Two ways to run it

### 3a. Local / single machine (CLI)

```bash
python -m pii_triage scan /corpus --out inventory.csv --workers 16 \
    [--rulepack custom.yaml] [--ner] [--ocr] [--llm] [--protocol protocol.pdf]

python -m pii_triage report   inventory.csv --out table1.csv
python -m pii_triage sample   inventory.csv --out sample.csv --rate 0.05 --seed 12345
python -m pii_triage estimate inventory.csv sample.csv --out table2.csv
python -m pii_triage benchmark inventory.csv gold.xlsx
```

The scan is multiprocess. The **output CSV is the durable progress record**: on resume, complete
rows are skipped and a partial trailing line is dropped. Per file: extract → (OCR) → detect → (LLM)
→ route → append row.

### 3b. Scaled / distributed (Azure Storage Queue + Container Apps)

Multiple Linux Docker containers poll `AZURE_QUEUE_NAME` in parallel; each picks up one file, runs
the full pipeline, and writes a `result.json` to `OUTPUT_MOUNT/job_id/file_stem/`. The Container App
autoscales replica count with queue depth. scaling-lib handles polling, retries, rate limiting,
dead-lettering, output-dir creation, and status tracking.

**Repo files that make this work:**

| File | Role |
|---|---|
| `worker.py` | scaling-lib worker entry point. Initialises rules / OCR fn / LLM fn once per worker, then `Worker().run(process)`. `process(file_path, output_dir)`: (1) convert legacy Office via Win32 COM if on Windows, (2) call `process_file()` (extract → detect → enrich → route), (3) write `result.json`. Walks up to the file's `files/` ancestor to find and cache the sibling **protocol** doc per job dir. |
| `enqueue.py` | Worker-fleet equivalent of `scaling-lib enqueue`, pointed at a job's `files/` folder. Streams the directory walk (starts on the first file, not after a full listing). Optional `--inventory` filter (rescan only non-excluded lanes). Builds queue messages directly so the whole batch shares one deterministic `job_id`. Routes `.doc/.xls/.ppt` to `AZURE_WINDOWS_QUEUE_NAME`. |
| `collect_outputs.py` | After the queue drains, walks all `output_dir` paths (from the status table), reads each `result.json`, and writes one `inventory.csv`. This feeds `report` / `sample` / `estimate` / scoring. |
| `Dockerfile` | Multi-stage build for the scaled workers. |
| `worker_status.py` | Worker/queue status helpers. |
| `hwe_scaled_ui.py` / `hwe_scaled_store.py` / `hwe_scaled_ui.html` | The **localhost operator UI** for the scaled pipeline (see `UI_OVERVIEW.md`). |

**Matter protocol convention:** each job's corpus lives at `<job_dir>/files/`, with the matter
protocol document as a sibling — `<job_dir>/protocol.pdf` (or `.docx/.doc/.txt/.rtf`). `enqueue.py`
is pointed at `files/` so the protocol is never enqueued as a work item; the worker resolves and
extracts it per file (cached per job dir), then passes its text into `process_file(..., protocol_text=...)`.

**Windows leg:** `.doc/.xls/.ppt` need Win32 COM conversion, which only exists on Windows. They are
routed to `AZURE_WINDOWS_QUEUE_NAME` at enqueue time; a Windows VM runs `python worker.py`
(it detects `sys.platform == "win32"`, polls the Windows queue, converts, and forwards the converted
file to the Linux queue for the rest of the pipeline).

---

## 4. Key environment variables

| Variable | Purpose |
|---|---|
| `AZURE_QUEUE_NAME` | Linux worker queue |
| `AZURE_WINDOWS_QUEUE_NAME` | Windows worker queue (legacy Office) |
| `AZURE_STORAGE_QUEUE_URL` / `AZURE_STORAGE_TABLE_URL` | Azure Storage endpoints (managed-identity mode) |
| `AZURE_STORAGE_CONNECTION_STRING` | Alternative to the URL pair (local/Azurite mode) |
| `USE_OCR` / `USE_LLM` / `USE_NER` | Feature flags (default `false`) |
| `BDE_THRESHOLD` | Entity count for BDE routing (default `51`) |
| `FILE_TIMEOUT_S` | Per-file timeout (default `120`) |
| `MAX_BYTES` | File size cap (default 1 GB) |
| `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_DEPLOYMENT` (falls back to `AZURE_OPENAI_DEPLOYMENT_GPT_5_4_NANO`) | LLM enrichment |
| `AZURE_DI_ENDPOINT` | OCR enrichment |
| `RULEPACK_PATH` | Custom Master List path (unset = built-in default) |
| `INPUT_MOUNT` / `OUTPUT_MOUNT` | Corpus and results file-share mounts |
| `JOB_TYPE`, `AZURE_TABLE_NAME`, `AZURE_DEAD_LETTER_QUEUE_NAME` | scaling-lib job/status/DLQ identifiers |
| `ACR_REGISTRY`, `ACR_IMAGE`, `AZURE_CONTAINER_APP`, `AZURE_RESOURCE_GROUP` | Build & deploy targets |

---

## 5. Design guarantees

- **Read-only** — no file modifications, no code execution on the corpus.
- **No PII leakage** — values discarded after counting; only labels/counts in output.
- **Bounded work** — per-file timeout, size cap, scan character/row caps, zip-bomb guard.
- **Crash-safe resume** — CSV-based progress locally; idempotent MERGE-upsert status rows when scaled.
- **Graceful degrade** — missing optional libraries or Azure failures never abort the run.
- **Deterministic rules pass** — no network calls unless `USE_OCR` / `USE_LLM` are explicitly enabled.
  A row decided *without* any model call must be byte-identical across runs (the Compare invariant).

---

## 6. scaling-lib integration

The project depends on [scaling-lib](https://github.com/ldmglobal-com/scaling-lib) (pinned in the
`Dockerfile` to the `@dev` ref) for the distributed worker fleet. It provides the queue polling,
retry/dead-letter machinery, rate-limited AI clients (`AzureOpenAIClient`,
`DocumentIntelligenceClient`), the status table, and the ACR build/deploy CLI.

Docs ship with the package — locate them via:
```python
import scaling_lib, pathlib
print(pathlib.Path(scaling_lib.__file__).parent / "README.md")   # + docs/{acr,ai,cli,docker,logging,status,testing,worker}.md
```

**Notable integration seams** (learned during the scaled build):
- `enqueue.py` supplies its own `--job-id` because scaling-lib prints the job id to stderr only.
- `run_metrics()` targets only the *latest* enqueued job, so the UI queries the table partition
  (`PartitionKey == job_id`) directly and rebuilds a `RunMetrics` scoped to a chosen run.
- There are no per-*call* timestamps — only task-level `started_at` / `completed_at`. Concurrency
  is therefore a task-level sweep, cached tokens are unrecorded, and two-pass (Windows-converted)
  documents' tokens file under the converted name.
- `collect_outputs.py` and `reset` are **table-wide**, not job-scoped — which is why the UI enforces
  one run at a time and archives every present job before any reset.

---

## 7. Tests

**Core library — 111 unit tests:**
```bash
cd pii_triage && python -m unittest discover -s tests -v
```
Cover Luhn validation, entity detection, value-vs-topic-mention signals, bucketing, lane routing,
and sampling/extrapolation.

**Scaled UI — 46 tests** in `test_hwe_scaled_ui.py` (command builders, the Compare invariant, the
concurrency equal-timestamp sweep, the archive-must-verify-before-reset guard, deploy-while-active
refusal, loopback-only binding, and more). Store-dependent flows are validated end-to-end against a
local **Azurite** stack.

**Scoring tools** live in `pii_triage_merged/tools/` — chiefly `score_combined.py` (the full
scorecard used by the UI's "vs manual review" screen), plus `diff_runs.py`, `check_nr_frozen.py`,
and `SCORING.md`.

---

## 8. Repository map

```
New-HWE/
├── pii_triage_merged/          # the core library (v3.0.0) + tests + tools + rulepacks
│   ├── pii_triage/             # the Python package (see §2)
│   ├── rulepacks/default.yaml  # built-in Master List (definitions, not values)
│   ├── tools/                  # score_combined.py, diff_runs.py, SCORING.md, …
│   └── tests/                  # 111 unit tests
├── worker.py                   # scaling-lib worker entry point
├── enqueue.py                  # streaming enqueue + rescan filter (shared job_id)
├── collect_outputs.py          # result.json → inventory.csv
├── worker_status.py            # worker/queue status helpers
├── Dockerfile                  # multi-stage scaled-worker build
├── hwe_scaled_ui.py            # localhost operator UI — server + orchestration
├── hwe_scaled_store.py         # Azure Table/queue access via scaling-lib helpers
├── hwe_scaled_ui.html          # single-page UI (inline CSS/JS)
├── test_hwe_scaled_ui.py       # 46 UI tests
├── RUNBOOK.md                  # step-by-step scaled run instructions
├── CLI_ONLY.md                 # what the UI deliberately can't do (+ commands)
├── SCALED_UI_BUILD_NOTES.md    # how the scaled UI was built + bugs that cost time
├── SCALED_UI_FINDINGS.md       # Phase-0 investigation (schemas, --help, auth model)
├── runs/                       # UI-owned run metadata (runs/<id>/run.json) + audit log
└── outputs/                    # historical scaled runs (each has inventory.csv)
```

See **`UI_OVERVIEW.md`** for the detailed operator-UI documentation, **`RUNBOOK.md`** for the
scaled run procedure, and **`CLI_ONLY.md`** for the operations that live outside the UI.
