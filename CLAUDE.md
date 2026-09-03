# Project: pii_triage

**pii_triage** (v3.0.0) is the HWE Bucketing & Tagging stage. It scans a corpus of files for PII, produces per-file inventory rows, and feeds downstream Table 1 (searchable files) and Table 2 (non-searchable, sampled + extrapolated). Version 3.0.0 adds a second LLM stage (graded responsiveness overview) that reuses the same extraction pass as stage 1 — no double-parsing. **Key guarantee: no PII values are ever stored, logged, or output — only entity type counts and routing labels.**

## Directory layout

```
New-HWE/
├── pii_triage_merged/         # Merged 3.0.0 package — the core library
│   ├── pii_triage/           # Python package
│   │   ├── cli.py            # CLI entry point & command parser
│   │   ├── config.py         # Master List / rulepack loading
│   │   ├── detection.py      # Entity detection (regex, keyword, name, address, SSN, money)
│   │   ├── extractors.py     # Text extraction for 25+ file formats
│   │   ├── routing.py        # FileRecord schema, bucketing, lane routing
│   │   ├── runner.py         # Parallel scan orchestrator, crash-safe resume
│   │   ├── azure_clients.py  # Azure OpenAI & Document Intelligence wrappers
│   │   ├── enrich.py         # OCR / LLM orchestration per file
│   │   ├── report.py         # Table 1 builder (searchable files)
│   │   ├── sampling.py       # Table 2: sample drawing & extrapolation
│   │   ├── benchmark.py      # Accuracy scoring vs. gold-standard results
│   │   ├── conversion.py     # Legacy Office → OOXML via LibreOffice headless (cross-platform)
│   │   └── legacy_pairs.py   # Shared helper: collapse a legacy file's two-Table-row artifact (historical runs)
│   ├── rulepacks/default.yaml  # Built-in Master List (entity definitions, not values)
│   └── tests/                  # unit tests
├── worker.py                 # scaling-lib worker entry point (distributed)
├── collect_outputs.py        # Gathers result.json files into inventory.csv
├── enqueue.py                # Worker-fleet equivalent of `scaling-lib enqueue`, with streaming walk + optional inventory filtering
├── Dockerfile                # Multi-stage build for scaled workers
└── .env.example              # Environment variable template
```

## Core modules

**`detection.py`** — Counts entity occurrences; never stores values (held in a local set, discarded on return). Eight detection methods:

| Method | Entity examples |
|---|---|
| `regex` | EMAIL, PHONE, PAYMENT_CARD (+ Luhn) |
| `keyword` | HEALTH_INFO, BIOMETRIC |
| `name` | NAME (title/salutation heuristic, or spaCy NER) |
| `address` | ADDRESS (street or City, State ZIP) |
| `ssn` | SSN (validates area/group/serial) |
| `labeled_value` | PASSPORT, DRIVER_LICENSE, DOB |
| `money` | MONEY (currency symbols, formatted amounts) |

**`extractors.py`** — Read-only format parsers returning `(text, metadata)`. Natively handles txt/csv/html/xml/rtf/eml; with optional libs: docx, pptx, xlsx, pdf, msg, xls. Flags images, zips, legacy Office, and PST/OST for downstream routing.

**`routing.py`** — Defines `FileRecord` (one CSV row per file) and assigns one of 10 **lanes**:

| Lane | Condition |
|---|---|
| `standard` | responsive, searchable, < BDE threshold entities |
| `bde` | responsive, searchable, ≥ BDE threshold (default 51) |
| `structured_bde` | responsive, spreadsheet, ≥ 51 entity rows |
| `likely_non_responsive` | no signals detected |
| `nonsearchable_sample` | image/OCR-needed, goes to manual sample |
| `container_expand` | zip/pst/ost/nsf/mbox |
| `convert_lane` | legacy .ppt/.ods |
| `needs_parser` | unsupported extension |
| `manual_oversize` | exceeds size cap |
| `review_error` | extraction failure |

**`runner.py`** — Parallel scan (multiprocessing). The output CSV is the durable progress record — on resume, complete rows are skipped and partial trailing lines are dropped. Per-file: extract → (OCR) → detect → (LLM) → route → append row.

**`enrich.py` / `azure_clients.py`** — Optional enrichment gated by `--ocr` / `--llm` flags. OCR uses Azure Document Intelligence (`prebuilt-layout`). LLM uses Azure OpenAI and is called **only on ambiguous files** (some signals but no strong identifier). LLM returns: responsiveness level, person count, one-sentence reasoning. Both degrade gracefully on failure. Use scaling-lib's `AzureOpenAIClient` and `DocumentIntelligenceClient` wrappers (rate limiting, retry, context window management built in).

**`config.py`** — Loads Master List from YAML/JSON (or built-in default). 11 PI-Type categories: Contact, Government ID, Birth, Financial, Access, Health, Biometric, Family, Demographic, Student, Work. Custom rulepacks inherit unspecified keys from default.

**`report.py` / `sampling.py`** — Build Table 1 (per-file, searchable) and Table 2 (non-searchable, bucketed). Table 2 workflow: `draw_sample` → reviewers code `gold_responsive`/`gold_bde` columns → `estimate` extrapolates percentages to the full population.

**`benchmark.py`** — Scores inventory against gold-standard xlsx/csv. Reports precision, recall, F1; lists misses first (missed responsives are the critical failure mode).

## Per-file processing pipeline

Every file passes through this decision tree. Early exits write a lane and stop; later stages only run if the file survives to them.

```
file_path received
│
├─ size > MAX_BYTES ──────────────────────────────► manual_oversize
│
▼
extractors.py — extract text + metadata
│
├─ unsupported extension ─────────────────────────► needs_parser
├─ extraction error ──────────────────────────────► review_error
├─ container (zip/pst/ost/nsf/mbox) ─────────────► container_expand
├─ legacy format (.ppt/.ods) ────────────────────► convert_lane
│
▼
text_extractable == "image_only"?
│
├─ YES + USE_OCR=true ──► Document Intelligence OCR ──► (continue below)
└─ YES + no OCR ──────────────────────────────────► nonsearchable_sample
│
▼
detection.py — count entities by type (values discarded)
│
▼
routing.py — classify ambiguity
│
├─ clear_non_responsive  (no signals at all)
│   └──────────────────────────────────────────────► likely_non_responsive
│
├─ clear_responsive  (strong identifier present, e.g. SSN)
│   └── skip LLM ──► (routing below)
│
└─ ambiguous  (some signals, no strong identifier)
    ├─ USE_LLM=true ──► Azure OpenAI classify ──► (routing below)
    └─ no LLM ────────────────────────────────► (routing below, rules result)
│
▼
routing.py — assign final lane
│
├─ structured (spreadsheet) + entity rows ≥ BDE_THRESHOLD ──► structured_bde
├─ entities ≥ BDE_THRESHOLD ─────────────────────────────── ► bde
└─ entities < BDE_THRESHOLD ─────────────────────────────── ► standard
```

**Ambiguity classification** (before LLM decision):
- `clear_responsive` — SSN or other strong identifier found → no LLM needed
- `clear_non_responsive` — zero signals → no LLM needed
- `ambiguous` — weak signals (keyword hits, name only, money only) → LLM consulted if enabled

**LLM output** (when consulted): responsiveness level (`clear_yes` / `likely_yes` / `borderline` / `likely_no` / `clear_no`), person count, one-sentence reasoning. Stored in `FileRecord`; overrides rules-based ambiguity result.

**Non-searchable sample flow** (Table 2 path):
```
nonsearchable_sample lane
│
▼
sampling.py draw_sample — stratified by complexity bucket
(1–4 pages / 5–10 / 11–50 / 51+ pages, default 5% rate, fixed seed)
│
▼
Manual review — coders fill gold_responsive + gold_bde columns
│
▼
sampling.py estimate — per-bucket % extrapolated to full population → Table 2
```

## scaling-lib integration

**`worker.py`** — Runs a startup preflight (scaling_lib import, mounts, storage reachability;
LLM/OCR credential check if enabled) before ever polling the queue, then initialises compiled
rules, OCR fn, and LLM fn once per worker, and calls `Worker(concurrency=...).run(process)`
(concurrency sized off the container's actual cgroup CPU quota unless `WORKER_CONCURRENCY`
overrides it). The `process(file_path, output_dir)` function:
1. Calls `process_file()` (extract → detect → enrich → route) — legacy `.doc`/`.xls`/`.ppt`
   convert to OOXML inline here, via LibreOffice headless (`conversion.py`), the same on every
   worker; there is no Windows-only step any more.
2. Writes `result.json` to `output_dir`

**`collect_outputs.py`** — After the queue is fully drained, walks all `output_dir` paths (retrieved via scaling-lib status table), reads each `result.json`, and writes a single `inventory.csv`. This feeds the `report`, `sample`, and `estimate` commands.

**`enqueue.py`** -- Worker-fleet equivalent of `scaling-lib enqueue`, given a job's `files/` folder. Streams the directory walk (`os.walk` generator) rather than materializing it first, so enqueueing starts on the first file instead of stalling on a full listing of a few-hundred-thousand-file corpus. Filtering is optional: with `--inventory <inventory.csv>`, it's also the worker equivalent of `scan --filter-inventory` -- enqueues only the files whose `suggested_lane` isn't in `--exclude-lanes` (default `likely_non_responsive`), e.g. to rescan the responsive/unresolved subset with `USE_LLM`/`USE_OCR` on without resubmitting the whole corpus. Filtering happens here, at enqueue time, since a queued task always runs once a worker picks it up -- `process()` has no "skip this" signal. Builds queue messages directly (bypassing scaling-lib's `enqueue()`, which would mint a fresh random `job_id` per file) so the whole run shares one `job_id` and shows as a single batch in `scaling-lib status`.

`enqueue.py` also chunks the streamed walk into batches and submits each batch through a thread pool (`--concurrency`, default 32; `--batch-size`, default 500), instead of one `init_task`+`send_message` round-trip per file serially.

No more Windows queue routing: `.doc`/`.xls`/`.ppt` files go to the same main queue as everything else — `AZURE_WINDOWS_QUEUE_NAME` should be left unset. (It used to route them to a separate Windows-only worker for Win32 COM conversion; that leg is gone — see `conversion.py` and `ARCHITECTURE.md` §7.)

Matter protocol: each job's corpus lives at `<job_dir>/files/` under `INPUT_MOUNT`, with the matter protocol doc as a sibling — `<job_dir>/protocol.pdf` (or `.docx`/`.doc`/`.txt`/`.rtf`). `scaling-lib enqueue` must be pointed at the `files/` subfolder itself (it only lists immediate files in the given path, not recursive), so the protocol file is never enqueued as a work item. `worker.py::process()` walks up from each file's path to find its `files/` ancestor, resolves the sibling protocol doc, extracts its text, and passes it into `process_file(..., protocol_text=...)` — looked up per file (and cached per job dir within a worker process) rather than read once at startup, since one worker fleet can process several concurrently-running jobs.

## CLI (local / single-machine)

```bash
# Full scan
python -m pii_triage scan /corpus --out inventory.csv --workers 16 \
    [--rulepack custom.yaml] [--ner] [--ocr] [--llm] [--protocol protocol.pdf]

# Outputs
python -m pii_triage report inventory.csv --out table1.csv
python -m pii_triage sample inventory.csv --out sample.csv --rate 0.05 --seed 12345
python -m pii_triage estimate inventory.csv sample.csv --out table2.csv
python -m pii_triage benchmark inventory.csv gold.xlsx
```

## Key environment variables

| Variable | Purpose |
|---|---|
| `AZURE_QUEUE_NAME` | The one worker queue — all formats, all workers |
| `AZURE_WINDOWS_QUEUE_NAME` | Legacy, leave unset — no separate Windows worker queue any more |
| `AZURE_STORAGE_QUEUE_URL` / `AZURE_STORAGE_TABLE_URL` | Azure Storage endpoints |
| `USE_OCR` / `USE_LLM` / `USE_NER` | Feature flags (default `false`) |
| `WORKER_CONCURRENCY` | Files processed at once per container (default: sized off the container's cgroup CPU quota) |
| `CONVERT_TIMEOUT_S` | Per-file legacy-conversion timeout (falls back to `FILE_TIMEOUT_S`) |
| `SOFFICE_PATH` | Override the LibreOffice binary path/name if not `soffice`/`libreoffice` on `PATH` |
| `LOG_LEVEL` | `worker.py`/`enqueue.py`/`collect_outputs.py` verbosity (default `INFO`; set `DEBUG` for detail) |
| `BDE_THRESHOLD` | Entity count for BDE routing (default `51`) |
| `FILE_TIMEOUT_S` | Per-file processing timeout (default `120`) |
| `MAX_BYTES` | File size cap (default `1 GB`) |
| `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_DEPLOYMENT` (falls back to `AZURE_OPENAI_DEPLOYMENT_GPT_5_4_NANO`) | LLM enrichment |
| `AZURE_DI_ENDPOINT` | OCR enrichment |
| `RULEPACK_PATH` | Custom Master List path |

## Design guarantees
- **Read-only**: no file modifications or code execution
- **No PII leakage**: values discarded after counting; only labels/counts in output
- **Bounded work**: per-file timeout, size cap, scan character/row caps, zip-bomb guard
- **Crash-safe resume**: CSV-based progress; idempotent
- **Graceful degrade**: missing optional libs or Azure failures do not abort the run
- **Deterministic rules pass**: no network calls unless `USE_OCR`/`USE_LLM` explicitly enabled

## Tests

```bash
cd pii_triage && python -m unittest discover -s tests -v
```

Covers: Luhn validation, entity detection, value vs. topic-mention signals, bucketing, lane routing, sampling/extrapolation, legacy conversion (LibreOffice-headless wrapper logic + inline handling in `_process_file`).

---

## scaling-lib

**Architecture in brief**: Multiple Linux Docker containers poll `AZURE_QUEUE_NAME` in parallel; each picks up one message, calls `process(file_path, output_dir)`, and writes outputs to `OUTPUT_MOUNT/job_id/file_stem/`. Every format, including legacy `.doc`/`.xls`/`.ppt`, goes through the same queue and the same worker fleet — conversion happens inline (LibreOffice headless), so there is no separate Windows queue/worker leg. The library handles polling, retries, rate limiting, dead-lettering, output directory creation, and status tracking. After processing, each task's `output_path` is stored in the status table. You have a few tasks, including implementing `process()`, switching to use scaling-lib's API clients, and collecting outputs together at the end of the pipeline (if this isn't already implemented). **Read all docs thoroughly before writing code. Do not skim past anything**

This project uses [scaling-lib](https://github.com/ldmglobal-com/scaling-lib) for parallel Docker-based file processing workers backed by Azure Storage Queues.

Documentation is installed with the package. To locate it:

```python
import scaling_lib, pathlib
base = pathlib.Path(scaling_lib.__file__).parent
print(base / "README.md")   # quickstart and overview
print(base / "docs")        # full reference docs
```

Available docs:
- `README.md` — quickstart, how it works, full env var reference
- `docs/acr.md` — building and deploying to Azure Container Apps
- `docs/ai.md` — AI rate limiting, context window management, auth
- `docs/cli.md` — all CLI commands
- `docs/docker.md` — Dockerfile structure and customisation
- `docs/logging.md` — Setting up logging, including to Azure Application Insights
- `docs/status.md` — job status table schema, CLI, and collecting outputs
- `docs/testing.md` — Azurite fixtures for local testing
- `docs/worker.md` — Worker API, retry behaviour, idle timeout
