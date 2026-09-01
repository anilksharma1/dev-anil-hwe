# HWE Scaled — pii_triage distributed deployment

This repository root is the **scaled deployment** of `pii_triage` 3.0.0: a fleet of Docker
workers that pull files off an Azure Storage Queue, run the full read-only PII triage pipeline
(extract → detect → enrich → route) on each, and write one `result.json` per file to an output
file share. A local operator UI drives runs and reads results back. It never stores PII values —
only entity *type* counts and routing labels.

The triage engine itself — detection, extraction, routing, scoring, and the single-machine
`python -m pii_triage` CLI — lives in **[`pii_triage_merged/`](pii_triage_merged/README.md)**.
Read that README for how the pipeline decides things (the two LLM stages, the frozen NR path, the
inventory CSV schema, the Master List / rulepacks). This README covers only the deployment layer
that wraps it.

**Documentation**

Start with [`TOOL_OVERVIEW.md`](TOOL_OVERVIEW.md), then reach for the doc that matches your task:

| Doc | Purpose |
|---|---|
| [`TOOL_OVERVIEW.md`](TOOL_OVERVIEW.md) | **start here** — what it is, how to run, tests, reporting, failure scenarios |
| **this file** (`README.md`) | deployment overview, setup, dependencies, environment variables |
| [`RUNBOOK.md`](RUNBOOK.md) | operator runbook — deploy, rollback, incident response |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | workflow diagrams, HLD & LLD per initiative |
| [`DEPENDENCIES.md`](DEPENDENCIES.md) | dependency, license & paid-service register |
| [`SOURCES_AND_CREDENTIALS.md`](SOURCES_AND_CREDENTIALS.md) | information sources the tool uses + credentials/auth |
| [`TESTING_RESULTS.md`](TESTING_RESULTS.md) | validation log per initiative — version, hypothesis, result, learning |
| [`BENCHMARKS.md`](BENCHMARKS.md) | ground-truth benchmarks + measured accuracy/cost, with cross-run comparison |
| [`BACKLOG.md`](BACKLOG.md) | known issues, deferred features, planned updates |
| [`PROJECT_OVERVIEW.md`](PROJECT_OVERVIEW.md) · [`UI_OVERVIEW.md`](UI_OVERVIEW.md) | detailed pipeline / operator-UI walkthroughs |
| [`pii_triage_merged/README.md`](pii_triage_merged/README.md) | the triage library and local CLI |
| [`pii_triage_merged/RUNNING.md`](pii_triage_merged/RUNNING.md) | library-side running notes |
| [`pii_triage_merged/tools/SCORING.md`](pii_triage_merged/tools/SCORING.md) | scoring a run against an entities export |
| `CLAUDE.md` | architecture reference for the whole repo |

---

## What runs where

```
                 enqueue.py  ──►  Azure Storage Queue (AZURE_QUEUE_NAME)
                     │                     │  poll
                     │                     ▼
                     │           Linux workers (Docker on Azure Container Apps)
                     │           worker.py → process() → result.json → OUTPUT_MOUNT
                     │
                     └──►  Windows queue (AZURE_WINDOWS_QUEUE_NAME)  ── legacy .doc/.xls/.ppt
                                           │  poll
                                           ▼
                                 Windows VM worker (native python worker.py)
                                 Win32 COM convert → forward to Linux queue

collect_outputs.py ──►  reads every result.json (paths from the status table) ──►  inventory.csv
hwe_scaled_ui.py   ──►  local operator UI (loopback only) over runs/ + inventories
```

- **Linux workers** are Docker containers on Azure Container Apps. They poll `AZURE_QUEUE_NAME`,
  and the Container App scales the replica count up and down with queue depth.
- **The Windows leg** exists only because legacy `.doc`/`.xls`/`.ppt` need Win32 COM conversion,
  which is Windows-only. `enqueue.py` routes those extensions to `AZURE_WINDOWS_QUEUE_NAME`
  automatically; a native `python worker.py` on a Windows VM converts them and forwards the
  result to the Linux queue.
- **The operator UI** (`hwe_scaled_ui.py`) binds `127.0.0.1` on a free port and opens a browser.
  It is read-only over run artefacts and shows only inventory columns and store/metric fields —
  labels and counts, never PII values.

---

## Repository layout (root)

```
New-HWE/
├── worker.py            # scaling-lib worker entry point (Linux + Windows leg)
├── enqueue.py           # submit a job's files/ folder to the queue under one job_id
├── collect_outputs.py   # after the queue drains, gather every result.json → inventory.csv
├── Dockerfile           # multi-stage build for the Linux worker image
├── hwe_scaled_ui.py     # operator UI server (stdlib only)
├── hwe_scaled_ui.html   # its single-page front end
├── hwe_scaled_store.py  # UI ↔ scaling-lib status/queue access layer
├── worker_status.py     # status helpers
├── HWE_Scaled.cmd/.sh   # double-click launchers for the UI (Windows / macOS-Linux)
├── .env.example         # environment variable template — copy to .env
├── runs/                # UI-owned run workspace (git-ignored)
├── outputs/             # historical CLI run outputs
└── pii_triage_merged/   # the triage library + local CLI (see its own README)
```

---

## Setup

### Prerequisites (deployment)

- An Azure Storage account (queues + table)
- An Azure Container Registry
- An Azure Container App with a managed identity, and `INPUT_MOUNT` / `OUTPUT_MOUNT` mounted as
  Azure File Shares
- `az login` on the deploying machine
- [`scaling-lib`](https://github.com/ldmglobal-com/scaling-lib) installed for the `acr-*` and
  `status` commands (`pip install -r pii_triage_merged/requirements-local.txt`)

Running the **operator UI only** needs none of the above — it works offline over inventories
that already exist on disk.

### 1. Install dependencies

Which requirements file you need depends on what you're running:

| You're running | Install | Notes |
|---|---|---|
| The **operator UI** (`hwe_scaled_ui.py`) | Python 3.11+ — **standard library only** | No `pip install` to view runs. For its build/deploy and report/estimate actions it shells out to `scaling-lib` and `python -m pii_triage`, so install those (below) for the full control plane. |
| A **worker locally** or the **Windows VM leg** | `pip install -r pii_triage_merged/requirements-local.txt` | Pulls `scaling-lib[dev]` (which provides `python-dotenv`, used by `worker.py`) and, on Windows, `pywin32` for COM conversion. |
| The **library CLI** (`report`, `sample`, `estimate`, scoring) | `pip install -r pii_triage_merged/requirements.txt` | Optional format parsers + `Pillow`; the Azure SDKs are commented and only needed for `--ocr` / `--llm`. See the library README. |
| The **Linux worker image** | `docker build` (see `Dockerfile`) | Installs `pii_triage_merged/requirements.txt` + `scaling-lib@dev`; needs a `GITHUB_TOKEN` build arg to reach the private repo. Normally built via `scaling-lib acr-build`. |

Requirements files (all under `pii_triage_merged/`):

| File | Contents |
|---|---|
| `requirements.txt` | the library's optional parsers, `Pillow`, `pytest`/`reportlab` for tests; Azure SDKs listed as commented optionals |
| `requirements-windows.txt` | `-r requirements.txt` + `pywin32` (Windows COM conversion) |
| `requirements-local.txt` | `-r requirements-windows.txt` + `scaling-lib[dev]` from git |

> **Pillow is not optional if you use OCR.** `pypdf` needs it to decode images embedded in PDFs;
> without it embedded-image OCR silently becomes a no-op. It's pinned in `requirements.txt`. See
> the library README for the full story.

### 2. Configure `.env`

Copy the template and fill it in:

```bash
cp .env.example .env
```

`worker.py`, `enqueue.py`, and `collect_outputs.py` load `.env` on startup. See the
[environment variables](#environment-variables) reference below. A real shell variable always
wins over the file, and nothing prints secret *values* — only how many settings loaded.

### 3. Build, deploy, enqueue, collect

Follow **[`RUNBOOK.md`](RUNBOOK.md)** for the operational sequence. In brief:

```powershell
scaling-lib acr-release                 # build + push + deploy the worker image
python enqueue.py I:\<job_dir>\files    # submit a corpus (one job_id; Windows files auto-routed)
scaling-lib status                      # watch queue depth + per-job progress
python collect_outputs.py --out inventory.csv   # once the queue drains
```

For the non-searchable (Table 2) path, reviewers code the drawn sample and then
`python -m pii_triage estimate inventory.csv sample.csv --out table2.csv`.

---

## Environment variables

Canonical template is [`.env.example`](.env.example). Grouped reference:

### Azure Storage & credentials

| Variable | Purpose |
|---|---|
| `AZURE_STORAGE_QUEUE_URL` | Queue endpoint (production / managed identity) |
| `AZURE_STORAGE_TABLE_URL` | Table endpoint (production / managed identity) |
| `AZURE_STORAGE_CONNECTION_STRING` | Local Azurite alternative to the two URLs above |
| `AZURE_CREDENTIAL_TYPE` | `cli` forces `AzureCliCredential` (local dev); omit for `DefaultAzureCredential` (production). Read by `scaling-lib` and `collect_outputs.py`. |

### Queues & status table

| Variable | Default | Purpose |
|---|---|---|
| `AZURE_QUEUE_NAME` | `doc-processing` | Linux worker queue |
| `AZURE_WINDOWS_QUEUE_NAME` | `doc-processing-windows` | Windows queue for legacy Office; `enqueue.py` routes `.doc`/`.xls`/`.ppt` here |
| `AZURE_DEAD_LETTER_QUEUE_NAME` | `doc-processing-deadletter` | Dead-letter queue; also arms the worker DLQ circuit-breaker |
| `AZURE_TABLE_NAME` | `DocProcessingStatus` | Job/task status table |
| `WINDOWS_FILE_EXTENSIONS` | `.doc,.xls,.ppt` | Extensions routed to the Windows queue |

### Corpus mounts & job identity

| Variable | Purpose |
|---|---|
| `INPUT_MOUNT` | Corpus file-share root (e.g. `I:\`); each matter lives at `<mount>/<job_dir>/files/` with a sibling protocol doc |
| `OUTPUT_MOUNT` | Results file-share root (e.g. `O:\`); workers write `<job_id>/<file_stem>/result.json` |
| `JOB_TYPE` | Baked into the image via `ARG JOB_TYPE` (default `pii-triage`) |

### Worker tuning

| Variable | Default | Purpose |
|---|---|---|
| `QUEUE_VISIBILITY_TIMEOUT_SECONDS` | `300` | Message lease before requeue |
| `WORKER_MAX_ATTEMPTS` | `2` | Retries before dead-lettering (infra default is 2) |
| `WORKER_IDLE_EXIT_SECONDS` | `120` | Worker exits after this idle span |
| `DLQ_CHECK_INTERVAL_S` | `60` | DLQ circuit-breaker poll interval (`worker.py`) |
| `DLQ_FAILURE_RATE` | `0.05` | Fleet failure rate that trips the breaker |
| `DLQ_MIN_COMPLETIONS` | `100` | Minimum completions before the breaker can trip |
| `DLQ_WORKER_COUNT` | `1` | Fleet size, so the failure-rate denominator reflects total throughput |

The DLQ circuit-breaker is inactive unless `AZURE_DEAD_LETTER_QUEUE_NAME` is set.

### AI services (OCR + LLM enrichment)

| Variable | Default | Purpose |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | — | Azure OpenAI endpoint (`--llm`) |
| `AZURE_OPENAI_DEPLOYMENT` | — | Deployment name; falls back to `AZURE_OPENAI_DEPLOYMENT_GPT_5_4_NANO`, then hardcoded `gpt-4.5-nano` |
| `AZURE_OPENAI_DEPLOYMENT_GPT_5_4_NANO` | — | Fallback deployment name |
| `AZURE_OPENAI_API_VERSION` | `2024-12-01-preview` | OpenAI API version |
| `AZURE_DI_ENDPOINT` | — | Document Intelligence endpoint (`--ocr`); `AZURE_DOCUMENTINTELLIGENCE_ENDPOINT` is also accepted |
| `AZURE_KEY_VAULT_URL` | — | Optional: pull endpoints from Key Vault instead of env |
| `AOAI_CONTEXT_WINDOW` | — | Optional: override the context window for a non-standard deployment |

Endpoints/credentials are never hardcoded; `DefaultAzureCredential` picks up a managed identity
or `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET`.

### pii_triage feature flags & tuning

| Variable | Default | Purpose |
|---|---|---|
| `USE_OCR` | `false` | Enable Document Intelligence OCR |
| `USE_LLM` | `false` | Enable Azure OpenAI enrichment (both stages) |
| `USE_NER` | `false` | Enable spaCy name detection (must be installed) |
| `BDE_THRESHOLD` | `51` | Entity count at/above which a file is a BDE |
| `FILE_TIMEOUT_S` | `120` | Per-file processing timeout |
| `MAX_BYTES` | `1073741824` | File size cap (1 GiB); larger files → `manual_oversize` |
| `MAX_SCAN_CHARS` | `5000000` | Character cap per file |
| `MAX_SCAN_ROWS` | `200000` | Row cap per structured file |
| `RULEPACK_PATH` | built-in | Path to a custom Master List YAML/JSON |
| `DEFAULT_JURISDICTION` | `""` | `us` / `non-us`, applied in the stage-2 prompt (`worker.py`) |
| `LLM_INPUT_CHARS` | `24000` | Characters of extracted text sent to the LLM (`worker.py`) |
| `PII_WATCHDOG_S` | `120` | Per-file hard watchdog timeout; `0` disables |

### Logging & deployment

| Variable | Default | Purpose |
|---|---|---|
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | — | App Insights; inject at runtime, never bake into the image |
| `LOG_FILE_PATH` | `app.log` | Local log file |
| `ACR_REGISTRY` | — | `<registry>.azurecr.io` for `scaling-lib acr-*` |
| `ACR_IMAGE` | `pii_triage` | Image name |
| `AZURE_CONTAINER_APP` | — | Target Container App name |
| `AZURE_RESOURCE_GROUP` | — | Resource group |
| `AZURE_SUBSCRIPTION_ID` | — | Subscription (used by `collect_outputs.py`) |
| `GITHUB_TOKEN` | — | Build-time token to `pip install scaling-lib` from the private repo |
| `SCALING_LIB_SRC` | — | Optional local path override for the `scaling_lib` source (UI/store) |

---

## Design guarantees

Carried through from the library and preserved by the deployment layer:

- **Read-only** on the corpus — no file is modified, moved, or executed.
- **No PII leakage** — values are discarded after counting; only labels and counts reach
  `result.json`, `inventory.csv`, and the UI. There is no document preview anywhere.
- **Crash-safe / idempotent** — a queued task always runs once; `collect_outputs.py` rebuilds the
  inventory from whatever `result.json` files exist.
- **Graceful degrade** — a missing optional parser, a failed OCR call, or a failed LLM call
  degrades that one file's row and never stops the fleet.
- **Deterministic rules pass** — no network calls unless `USE_OCR` / `USE_LLM` are enabled.

---

## Tests

The triage library ships 273 tests (run from `pii_triage_merged/`):

```bash
cd pii_triage_merged && python -m pytest tests/ -q
```

The operator UI has its own suite at the repo root:

```bash
python -m pytest test_hwe_scaled_ui.py -q
```
