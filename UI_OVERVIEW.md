# HWE Runner — Scaled: Operator UI (Detailed)

A localhost operator console for the **scaled** (Azure Storage Queue + Container Apps) `pii_triage`
pipeline. It lets an operator check a corpus, submit it, watch it run, collect and score the
results, compare runs, archive-and-reset, and prepare a build/deploy — all from a browser, without
touching a terminal for the normal path.

**Files**

| File | Lines | Role |
|---|---|---|
| `hwe_scaled_ui.py` | ~1,500 | HTTP server, all routes, orchestration, subprocess tools |
| `hwe_scaled_store.py` | ~470 | All Azure Table + Queue access, through scaling-lib's own helpers |
| `hwe_scaled_ui.html` | ~770 | Single-page app — inline CSS + vanilla JS, no build step |
| `test_hwe_scaled_ui.py` | — | 46 unit tests |
| `HWE_Scaled.sh` / `HWE_Scaled.cmd` | — | launchers |

---

## 1. Architecture

### The organising principle (carried verbatim from the local build)

> Every screen is a **view over artefacts the CLI and the store already write**. The UI builds a
> command, shows it before it runs, spawns it, and reads the results back. It never computes a
> number the pipeline doesn't compute, and it never holds authoritative state.

Consequences that hold by construction:
- The UI's numbers **can't disagree** with `scaling-lib status` / `collect_outputs.py` — they *are*
  the same numbers.
- You can **close the browser mid-run and reopen** — progress lives in the Azure Table and queues.
- **Delete the UI** and every run is still readable from its artefacts on disk.

The only in-memory state the server holds is live `subprocess.Popen` handles for processes **it**
started (enqueue, collect, report, sample, benchmark). Everything else is re-read from the Table,
the queues, or disk on each request.

### Server

- **Pure Python stdlib** — `http.server.BaseHTTPRequestHandler` on a
  `ThreadingMixIn` HTTP server. No Flask/Django, no dependencies beyond the Azure SDK the project
  already uses.
- Binds **`127.0.0.1` on a random free port** (loopback only — never network-exposed), loads `.env`,
  and opens a browser at `http://127.0.0.1:<port>/`.
- Serves the single `hwe_scaled_ui.html`; all data goes over `/api/*` JSON endpoints.
- Failures are returned as **data** (`{ok: false, ...}`), not exceptions — the server never 500s on
  a tool failure.

### `hwe_scaled_store.py` — the Azure access layer

A separate, unit-testable module that reaches the Table and queues **only through scaling-lib's own
functions** (`_fetch_entities`, `queue_status`, `RunMetrics`/`TaskRecord`, `clear_all_tasks`,
`clear_all_queues`, `get_output_dir`), so the UI's metrics can't drift from the CLI's. scaling-lib
is **lazily imported inside each function**, so the read-only screens (Setup, Build, Results from a
saved inventory) still work on a machine with no Azure SDK or credentials.

Key functions:

| Function | Returns |
|---|---|
| `env_report()` | Env vars grouped by who reads them — **set/not-set only, never values**. |
| `scaling_lib_status()` | scaling-lib importable? + commit hash. |
| `storage_mode()` | `"connection-string"` (Azurite) or `"managed-identity"` (prod) or `None`. |
| `credential_probe()` / `check_table()` / `check_queues()` / `check_mounts()` | Cheap reachability probes for Setup. |
| `queue_counts()` | Approximate main / Windows / dead-letter depths, timestamped. |
| `list_jobs()` / `latest_job_id()` | Jobs in the table grouped by `job_id`, newest first. |
| `job_metrics(job_id, visibility_timeout)` | The full Monitor aggregate (status counts, throughput, ETA, concurrency, tokens, checkpoints, stuck items). |
| `concurrency_series(intervals)` | Task-level sweep — **close-before-open at equal timestamps** so back-to-back sub-ms tasks read as 1, not N. |
| `archive_job(job_id, dest)` | Snapshot rows → `status_rows.jsonl` + `metrics_snapshot.json`, **re-read and verify count**. |
| `run_reset()` | `clear_all_tasks()` + `clear_all_queues()` (table-wide). |
| `job_open_count(job_id)` | pending + processing count — the load-bearing input to the lock. |
| `git_sha()` / `deployed_image_tag()` | Provenance: local SHA vs what the Container App actually runs. |

**Two storage modes:** connection-string (`AZURE_STORAGE_CONNECTION_STRING`, local/Azurite) or
managed-identity (`AZURE_STORAGE_TABLE_URL` + `AZURE_STORAGE_QUEUE_URL`, production).

### Who owns what

| Concern | Owner | UI behaviour |
|---|---|---|
| Whether a file is done | Azure Table | reads it; never counts CSV rows to infer progress |
| Queue depth / in-flight / dead-letter | Queues | reads them; stamps every reading; labels it "approximate" |
| Job identity (`job_id`) | `enqueue.py` (UI supplies `--job-id`) | deterministic; everything keyed on it |
| Job spec / argv / provenance / submitter | `runs/<id>/run.json` | the UI is the **only** writer |
| Collected inventory + tables | `collect_outputs.py` / `pii_triage` | the UI runs them, reads them back |
| Worker health | each worker (hostname on its row) | the UI never reports its own host as a worker's |
| Console output | processes the UI spawned only | the ~21 replicas' logs are a Log Analytics query, never a tail |

---

## 2. The eight screens

Navigation is a fixed sidebar; no client-side router. Global state lives in one `ST` object;
each screen is a function (`S.setup()`, `S.new()`, …) that returns an HTML string, and `draw()`
renders `S[ST.screen]()`.

```js
const NAV = [
  ['setup','Setup'], ['new','New run'], ['runs','Runs'], ['monitor','Monitor'],
  ['results','Results'], ['score','vs manual review'], ['compare','Compare runs'],
  ['build','Build & deploy']
];
```

### 2.1 Setup — *is this machine and this store configured?*

Preflight checks rendered as ✓ / ! / ✕ rows, plus an env-var report grouped into **Required ops-VM**,
**Storage endpoint**, **Optional**, **Build & deploy**, and **Worker-side** sections (each row shows
set/not-set + purpose, never a value).

Checks: Python version; `.env` present/loaded/count; scaling-lib importable + commit; storage mode;
credential/login validity; status table reachable; queues reachable (main + Windows + dead-letter);
mounts readable/writable. **Headline check:** the deployed image tag vs the local git SHA — the
single most useful "are the workers running my code?" signal.

Worker-side vars (`USE_LLM`, `AZURE_OPENAI_*`, …) are injected into the Container App at runtime, so
they're grouped separately and **never red-flagged** when blank on the ops VM.

Endpoint: `GET /api/setup`. JS: `loadSetup()`.

### 2.2 New run — *validate a corpus and submit it*

Two modes:
- **Fresh job** — enqueue every file under `files/`.
- **Rescan** — filter to a prior run's non-excluded lanes (default excludes `likely_non_responsive`),
  e.g. to re-run the responsive/unresolved subset with `USE_LLM`/`USE_OCR` on.

Flow:
1. Enter the **job directory** (must be under `INPUT_MOUNT`, must contain a `files/` folder; an
   optional `protocol.*` sibling is detected) → **Check** → `POST /api/newrun/check`. Returns file
   count, by-extension breakdown, Windows-queue count, protocol status, and the **exact enqueue
   command** that will run.
2. Optionally set a run **name** and a per-job **BDE threshold** (written to `<job dir>/pii_job.json`).
3. **Submit run** → `POST /api/newrun/submit` → spawns `enqueue.py`, writes `runs/<id>/run.json`
   (with a full provenance block), **acquires the one-run lock**, audit-logs, and jumps to Monitor.
   The button is disabled with an explicit reason (`submitWhy()`) until checks pass and no run is active.

**Staging helper** (collapsible): "My documents aren't in a job/files layout yet." Point it at a
source folder + a new job dir (+ optional protocol) → **Copy into files/**. Runs a background
copy thread (`shutil.copy2`, metadata preserved) polled every 700 ms via `GET /api/stage/status`.
It **only ever copies** — never moves or deletes the source. (Copy, not symlink, because SMB shares
often reject symlinks.)

If a run is already active, the screen shows a **lock banner** (job id, submitter, drained/open
count) with **Open monitor** and **Archive & reset** buttons instead of letting you submit.

### 2.3 Runs — *every run, and the reset control*

A table of all runs: those the UI started (`runs/<id>/run.json`, newest first) **plus** historical
runs reconstructed read-only from `outputs/*/inventory.csv` (marked "external"). Columns: name/id,
when, who, file count, kind pill, and **Open**. Open on an external run without an inventory is
disabled.

This screen is the home of **Archive & reset** (see §3.3). Endpoint: `GET /api/runs`.

### 2.4 Monitor — *live progress from the store, not from guesses*

The authoritative dashboard for a chosen `job_id`, **auto-polling every 3 s** while on-screen and
stopping at terminal state (0 pending + 0 processing). A stale-data pill turns red if the last read
is ≥15 s old.

- **Hero progress bar** — `files_completed / total` %, straight from the status table.
- **Status tiles** — Pending / Processing / Completed / Failed (+ dead-lettered).
- **Throughput + ETA** — files/min over a **guarded trailing 5-minute window** (too little signal →
  "not measured", never a fake rate), and an ETA **range** (70%–140% of the point estimate).
- **Rate-limit wait** — total seconds blocked awaiting Azure OpenAI (from the checkpoint total).
- **Autoscaling observed** — distinct worker hostnames + **task-level peak concurrency** (labelled
  "no per-call data exists"); the *requested* replica count is "not measured" (needs the scale rule via `az`).
- **Queues** — approximate main / Windows / dead-letter, timestamped.
- **Windows-leg alarm** — if the Windows queue has pending files, warns to run `python worker.py` on
  the Windows VM, and flags the queue as stale if depth hasn't moved in ~180 s.
- **Stuck items** — tasks processing longer than the visibility timeout (a crashed worker's claim).
- **Failures table** — file id, status, tries, error class only (**never** document text or PII).

Endpoint: `GET /api/monitor?id=<job_id>` → `monitor_payload()` → `store.job_metrics()`.

### 2.5 Results — *the collected inventory, summarised*

If the run hasn't been collected yet, shows a **Collect** panel: "each worker wrote a `result.json`;
Collect gathers them into one inventory." → `POST /api/collect` (gated on the run being drained).

Once an inventory exists (`GET /api/run?id=<id>` → `summarize_inventory()`):
- **Cost hero + stacked bar** — total estimated USD split into **Document Intelligence** (blue) and
  **LLM** (orange), with the display prices shown. Costs are display-only estimates; real invoicing
  comes from Azure.
- **Billable units** — DI pages (full-file + embedded), DI calls, LLM tokens (stage-1 + stage-2
  split), tokens in/out (from the table). Cached tokens = "not measured" (recorded nowhere).
- **Where they landed** — stage-1 lane distribution as horizontal bars.
- **Stage-2 graded responsiveness** — `clear_yes → likely_yes → borderline → likely_no → clear_no` bars.
- **Decision breakdown** — rules-decided (no model call) vs model-decided, non-responsive stage-1,
  BDE files (both definitions).
- **Entity types** — chips of `label · count` (labels only, never values).
- **Actions** — Build Table 1 (`/api/report`), Draw sample (`/api/sample`, rate 0.05 / seed 12345),
  Compare to manual review, Compare to another run, Copy row for cost log (TSV to clipboard),
  Export run zip (`/api/export`).
- **Provenance / command** — the enqueue argv exactly as submitted.

### 2.6 vs manual review — *score against a reviewer's export*

Point at a CNG entities export (`.csv`/`.xlsx`) with a **Control ID** column and a **Total Entities**
count. Set the id/count column names, sheet name, BDE threshold, and an **absent-means** strategy
(`auto` / `zero` / `unreviewed` — how to treat files not in the export). **Score this run** →
`POST /api/score` → runs `tools/score_combined.py` (the real scorer).

Derivation: responsive = `Total Entities > 0`; BDE = `count ≥ threshold`. Output:
- **Pipeline summary card** — recall, precision, accuracy, F1, scored count, TP/FP/FN/TN.
- **Key metrics** — *missed notifications* (% of cleared files actually responsive; red if >5%) and
  *over-flagged* (% of flagged files actually NR; red if >50%). "Read the misses first."
- An XLSX scorecard written under `runs/<id>/score/` (Run Info, Cost, Timing, NR/R Accuracy, BDE
  Accuracy, Metrics by Type, Stage-2 Detail, OCR Yield, File Detail, Per-file Timing, Misses).
- The scorer's full terminal output.

### 2.7 Compare runs — *did a rules-decided row move?*

Pick Run A and Run B (both need an inventory) → `POST /api/compare`. Enforces the core invariant: a
row decided **without any model call must be byte-identical** between two runs. Compares only the
`_DECISION_COLS` set, excluding `_MODEL_OR_TIMING_COLS` (llm_consulted, tokens, ocr_*, elapsed_s, …).

Output: a **verdict** (clean, or "N rules-decided rows moved" = a real regression), counts (common /
rules-decided-in-both / only-in-A / only-in-B), an **input-changed** note (rows differing only on
`size_bytes`/`status` — normal for `.doc` files re-saved by the Windows leg, kept separate from the
real alarm), a **provenance diff** (model, deployment, rulepack hash, API version — the usual
explanation), and a sample of moved rows with the exact columns that changed.

### 2.8 Build & deploy — *readiness only, run the actual build from a terminal*

**Read-only** — no build/deploy button. `GET /api/build/preflight` checks `az` presence + login,
`git` + SHA + clean tree, and the required ACR/Container-App env vars. Docker is explicitly **not a
gate** (`az acr build` runs in ACR's cloud). Shows the target image, Container App + resource group,
what the workers run now (deployed tag), and the CLI commands to run on the ops VM:

```
scaling-lib acr-release            # build + push + deploy (most common)
scaling-lib acr-build              # build + push only
scaling-lib acr-deploy --tag <sha> # deploy an image already in ACR
```

---

## 3. Load-bearing mechanisms

### 3.1 The one-run lock

`runs/_active_run.json` records the active run (run_id, job_id, submitter, corpus, started_at).
`active_run()` never trusts the file's mere existence — it enriches the lock with a **measured**
`job_open_count(job_id)` from the table (`drained = open_count == 0`). Submit is refused while a
non-drained run is active. The lock is load-bearing precisely because `collect_outputs.py` and
`reset` are **table-wide**, not job-scoped. `GET /api/lock` exposes it to the UI.

### 3.2 Submit / enqueue

`POST /api/newrun/submit` → `submit_run()`: validate the job dir → refuse if a run is active → build
the enqueue argv (`build_enqueue_argv()`, adding `--inventory`/`--exclude-lanes` for rescan and
`--bde-threshold` if given) → run `enqueue.py` as a child process (with `SCALING_LIB_SRC` added to
the child's `PYTHONPATH` for local dev, and UTF-8 forced for Windows) → write `runs/<id>/run.json`
with the full **provenance block** (git SHA, deployed image tag, job type, table/queue names,
rulepack, model deployment, flags — captured at submit, unrecoverable after) → acquire the lock →
audit-log.

### 3.3 Archive & reset (the destructive control)

`POST /api/reset` → `archive_and_reset(run_id, override, typed)`. Three guards, in order:
1. **Refuse if collect hasn't run** (no inventory) unless `override=true` — otherwise timing/tokens
   in the table are lost forever.
2. Because reset is table-wide, list **every** job in the table and require the operator to **type the
   `job_id`** to confirm (`needs_typed` → `confirm_token`).
3. **Archive every present job** (`archive_job`) to disk and **re-read to verify the row count**.
   If any archive fails to verify, the reset **does not run** (there's a test that injects a failed
   archive and asserts this).

Only then: `run_reset()` (clear table + all queues), release the lock, mark the run
`archived_reset`, audit-log. Note the honest cancel semantics (see `CLI_ONLY.md`): queued-but-unclaimed
files are dropped; a file a worker is *actively* processing finishes and writes an orphan
`result.json`; nothing is force-killed or re-queued.

### 3.4 Subprocess tools

Collect / report / sample / benchmark / score all run via `run_tool(argv)` (UTF-8 forced,
`SCALING_LIB_SRC` on `PYTHONPATH`) and every screen **shows the argv before/with the result** for
reproducibility. Collect is gated on the run being drained.

---

## 4. HTTP endpoints

**GET:** `/` (the page), `/api/context`, `/api/setup`, `/api/jobs`, `/api/runs`, `/api/run?id=`,
`/api/table?id=&which=`, `/api/monitor?id=`, `/api/lock`, `/api/newrun/validate?dir=`,
`/api/stage/status?id=`, `/api/build/preflight`, `/api/export?id=` (zip download).

**POST:** `/api/collect`, `/api/report`, `/api/sample`, `/api/benchmark`, `/api/score`,
`/api/compare`, `/api/newrun/check`, `/api/newrun/submit`, `/api/stage/start`, `/api/reset`.

All JSON; `200` on success, `404`/`400` for missing/invalid, and `{ok:false,...}` for tool failures.

---

## 5. Frontend details

- **Single 51 KB HTML file** — inline CSS + vanilla JS, zero frameworks, zero build step.
- **State:** one global `ST` object (`screen`, `ctx`, `runs`, `runId`, `run`, `out`, `form`, `setup`,
  `monitor`, `monitorJob`, `poll`, `winTrack`, `lock`, `reset`, `build`). Mutate → `draw()`. No
  reducers, no event bus.
- **Rendering:** each screen is a function returning an HTML string via backtick templates and
  `.map().join('')`; helpers include `api()` (fetch wrapper), `fmt()`/`money()`, `esc()` (XSS-safe),
  `tile()`, `renderCheck()`, `renderTable()`, `scoreSummary()`, `activeBanner()`, `resetPanel()`.
- **The "not measured" discipline:** a JS `nm()` renders `null → "not measured"` (dash), and only
  ever shows a `0` that came from a real record — so a missing metric never masquerades as zero.
- **Design tokens (`:root` CSS vars):** surfaces `#f4f5f6`/`#fff`; ink `#0b0b0b`; series-1 blue
  `#2a78d6` (stage-1 / DI), series-2 orange `#eb6834` (stage-2 / LLM); status green `#0ca30c`,
  yellow `#fab219`, red `#d03b3b`. Layout: 216 px dark sidebar + 1fr main (max 1080 px). Components:
  `.card`, `.tile`, `.bar-row`, `.pill`/`.chips`, `.callout` (+ `.alarm`/`.good`/`.muted`), `.hero`,
  `.cmd`/`pre.out` (dark code blocks).
- **Polling:** Monitor every 3 s (cleared on nav away, stopped at terminal state); staging every 700 ms.
- **Destructive-action UX:** typed-`job_id` confirmation for reset; an override path for
  reset-without-collect; submit disabled with an explicit reason string.

---

## 6. Launch

```bash
./HWE_Scaled.sh          # macOS/Linux
HWE_Scaled.cmd           # Windows (ops VM)
```

For local validation without Azure, run an **Azurite** stack (Queue + Table), point `.env` at the
devstore connection string, set `SCALING_LIB_SRC` to a scaling-lib checkout, then launch. See
`SCALED_UI_BUILD_NOTES.md` §2.4 for the exact Azurite command and the 46-test/end-to-end coverage,
`SCALED_UI_FINDINGS.md` for the Phase-0 schema investigation, and `CLI_ONLY.md` for what the UI
deliberately leaves to the terminal (`az login`, cancelling a run, provisioning infra, editing
secrets).
