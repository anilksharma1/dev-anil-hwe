# How HWE Runner — Scaled was built, and what cost time

The counterpart to `UI_BUILD_NOTES.md`, for the distributed (Azure Storage Queue + Container Apps)
build. Same product, same rules; a different source of truth. Part 1 is what exists. Part 2 is the
decisions and the bugs that cost real time — the part worth reading if you touch this next.

**What was built:** `hwe_scaled_ui.py` (server + orchestration), `hwe_scaled_store.py` (all Azure
access, through scaling_lib's own helpers), one `hwe_scaled_ui.html` (inline CSS/JS, lifted from
the local build so the two are visibly one product), two launchers, and `test_hwe_scaled_ui.py`
(**34 tests**). No dependencies beyond the standard library and the Azure SDK packages the project
already uses. No build step. Binds `127.0.0.1` on a random free port.

The Phase-0 investigation that everything rests on is `SCALED_UI_FINDINGS.md` — read it first; it
has the verified `--help` surface, the Table schema, the queue-message schema, the inventory
schema, the auth model, and (crucially) the list of what the pipeline does and doesn't record.

---

# Part 1 — what exists

## 1.1 The organising principle, carried over verbatim

> Every screen is a view over artefacts the CLI and the store already write. The UI builds a
> command, shows it before it runs, spawns it, and reads results back. It never computes a number
> the pipeline doesn't compute, and it never holds authoritative state.

Locally the thing being read was "files off disk." Here it's the **Azure Table and the queues** for
live truth, plus the same on-disk `runs/<id>/` for the UI's own provenance. The three properties
still hold: the UI's numbers can't disagree with the CLI's (they're the same numbers); you can close
the browser mid-run and reopen (progress lives in the store); delete the UI and every run is still
readable from its artefacts.

The one in-memory thing the server holds is live `subprocess.Popen` handles for processes **it**
started — enqueue, collect, report, sample, benchmark, and build/deploy. Everything else is re-read
from the Table, the queues, or disk.

## 1.2 The nine screens

| Screen | What it is a view over |
|---|---|
| **Setup** | `.env` (set/not-set only), scaling_lib import, credential probe, table + queue reachability, mount readability, and the headline: the **deployed image tag vs the local git SHA**. |
| **New run** | Corpus validation + Check (file/ext/**Windows-queue** counts, protocol, exact command), copy-staging, and submit → `enqueue.py`. |
| **Runs** | Every run the UI started + historical runs reconstructed from `outputs/`. Home of **Archive & reset**. |
| **Monitor** | The status table (authoritative per-state counts) + queues (approximate, stamped) + throughput/ETA range + rate-limit wait + task-level replica series + stuck items + failures + the Windows panel. |
| **Results** | The collected inventory: lanes, **billable-unit** cost, stage grades, decision split, entity *types* (labels only). Report / sample / export. |
| **vs manual review** | `pii_triage benchmark` against a reviewed sheet, with the scorer's own column auto-detect. |
| **Compare runs** | Rules-decided rows must be byte-identical; model-decided may differ. Plus a provenance diff. |
| **Build & deploy** | The three ACR actions, guarded. |

Endpoints (GET): `context, setup, runs, run, table, export, monitor, jobs, lock, newrun/validate,
stage/status, build/preflight, build/history, build/status`. (POST): `report, sample, benchmark,
compare, newrun/check, newrun/submit, stage/start, reset, build/start`.

## 1.3 What the store owns vs what the UI owns

| Concern | Owner | UI behaviour |
|---|---|---|
| Whether a file is done | Azure Table | reads it; never counts CSV rows to infer progress |
| Queue depth / in-flight / dead-letter | Queues | reads them; stamps every reading; says "approximate" |
| Job identity | `enqueue.py`'s `job_id` | the UI supplies its own `--job-id` so it's deterministic, and keys everything on it |
| Job spec / exact argv / provenance / who submitted | `runs/<id>/run.json` | the UI is the only writer |
| Collected inventory + tables | `collect_outputs.py` / `pii_triage` | the UI runs them, reads them back |
| Worker health | a worker (its hostname on each row) | the UI never reports its own Python/Pillow as the workers' |
| Console output | processes the UI spawned only | the 21 replicas' logs are a Log Analytics query, never a tail |

---

# Part 2 — decisions and the problems that cost time

## 2.1 Decisions, and why

- **Reuse scaling_lib, never re-implement it.** `hwe_scaled_store.py` reaches the Table and queues
  only through scaling_lib's own functions (`_fetch_entities`, `queue_status`, `run_metrics`'s
  `RunMetrics`/`TaskRecord`, `clear_all_tasks`, `clear_all_queues`, `get_output_dir`). The UI's
  numbers therefore can't drift from `scaling-lib status` / `collect_outputs.py`. Same spirit as the
  local build's "call `peek_columns()` inside the scorer."
- **A second module, `hwe_scaled_store.py`.** The local build was one file; the distributed build
  has enough store logic that a separate, unit-testable layer earned its place. scaling_lib is
  imported **lazily inside each function**, so the read-only screens still run where scaling_lib or
  the Azure SDK are absent.
- **The four operator decisions** (confirmed with the owner): staging = **copy** into `files/` (no
  symlinks — SMB shares often reject them and it couldn't be verified); Windows-worker liveness =
  **infer** from queue/table (no pipeline change); Build & deploy = **in the UI, heavily guarded**;
  concurrency = **one run at a time** (a lock file, everything keyed on `job_id`).
- **Validate the control plane against real infrastructure, not mocks.** A local **Azurite** stack
  (Queue + Table) made it possible to actually submit → watch → collect → archive → reset and see it
  work, per "don't report success on a run you didn't watch complete." The az/ACR pieces (no `az` on
  the dev box) are unit-tested here and confirmed on the ops VM.
- **Distinguish "not measured" from zero, everywhere.** A single `measured()` helper and a JS `nm()`
  render `null → "not measured"` and only ever show a `0` that was read from a real record.

## 2.2 The problems that cost real time

Each looked fine until it wasn't.

**1. scaling-lib isn't installed on the dev box, and its source was nowhere on disk.** It's pinned
in the `Dockerfile` to `github.com/ldmglobal-com/scaling-lib.git@dev`. Cloned that exact ref to read
and run it; installed only `python-dotenv` + the Azure client libs so `--help` could import. The UI
finds a local checkout via `SCALING_LIB_SRC` (unset on the ops VM, where it's pip-installed).

**2. `job_id` is printed to stderr only, not returned.** So the UI supplies its own `--job-id`
(`<jobdir>-<runstamp>`) and keys the run on that — no fragile stderr parsing, and the id is
deterministic and readable.

**3. `run_metrics()` only ever targets the *latest* enqueued job.** Useless for run history. The
store queries the partition directly (`PartitionKey == job_id`) and rebuilds a `RunMetrics` from
those rows — the same aggregation, scoped to a chosen run.

**4. There are no per-call timestamps.** `CheckpointRecord` stores a `duration_s`, not a start/end.
So a per-*call* concurrency sweep is impossible; the Monitor computes a **task-level** series over
`[started_at, completed_at]` — and does it with **close-before-open at equal timestamps**, so six
back-to-back sub-millisecond tasks read as 1 concurrent, not 6. There's a unit test for exactly that.

**5. Throughput read as 14,955 files/min.** Computing `done / wall_clock` on synthetic data whose
timestamps were all ≈ now gave a near-zero denominator. Replaced with a **guarded trailing window**:
completions in the last 5 minutes; too little signal → `null` → "not measured", never a false rate.

**6. Compare flagged `.DOC` files that hadn't really changed.** On the same corpus, two runs differed
only in `size_bytes` of `.doc` files — a two-pass conversion artefact (the Windows leg re-saves the
file). So Compare now separates a **detector-decision drift** (the real §6.7 alarm) from an
**input-only difference** (informational). Same corpus now reads clean, with a note.

**7. Cost has two token stories, and DI billing lives in a third place.** The inventory records
total tokens only (no in/out split) and a DI **billable-units** block (`ocr_pages`, `di_calls`,
`img_ocr_calls`). The status table records the in/out split (`tokens_in`/`tokens_out`) but files
two-pass documents' tokens under the *converted* name — which never appears in the inventory. So the
Results cost panel is built on billable units from the inventory; the in/out split comes from the
store; **cached tokens are recorded nowhere → "not measured."** Per-document token cost for the
~two-pass files is flagged as unattributable, not silently wrong.

**8. `collect_outputs.py` is table-wide, and `reset` wipes the whole table.** Neither is job-scoped.
So the one-run-at-a-time lock is load-bearing, and **Archive & reset archives every job present**
(not just the active one) before clearing — and refuses to clear if any archive doesn't verify.
There's a test that injects a failed archive and asserts the reset never runs.

**9. `_build_message` wants a `Path`, not a `str`.** Cost ten minutes on the Azurite seed script —
a reminder that the queue helpers are internal and unforgiving.

**10. The enqueue subprocess couldn't find scaling_lib.** It runs `enqueue.py` in a child process,
which imports scaling_lib expecting it installed. Fixed by having `_tool_env()` add `SCALING_LIB_SRC`
to the child's `PYTHONPATH` for local dev (a no-op on the ops VM).

**11. Docker is not a build prerequisite.** The brief listed "Docker running" as a Build & deploy
preflight; `acr-build` runs in ACR's cloud via `az acr build`, so Docker is only needed for
`acr-login` (local pulls). The preflight checks **`az` + login**, and the Docker row explicitly says
"not required." A test asserts Docker is never a gate.

**12. Worker-side env vars are not the ops VM's problem.** `USE_LLM`, `AZURE_OPENAI_*`, etc. are
injected into the Container App at runtime; blank on the ops VM is normal. Setup groups them
separately and never red-flags them — a test asserts they're not in the "required" set.

## 2.3 The data rules (§7), and where each stands

| Rule | Status | Source |
|---|---|---|
| A stable document id across conversion | **partial** | inventory keeps the original `rel_path`; the token telemetry files two-pass docs under the converted name — reconcilable only via the `forwarded.json` / `.orig.json` sidecars |
| One row per call attempt | **not recorded** | one row per file, MERGE-upserted; a failed-then-retried attempt's tokens/pages are lost. Retries are *counted* (`attempt_count`, `total_extra_attempts`), not *priced* |
| Timestamps on every call | **task-level only** | `started_at`/`completed_at` per row; no per-call timestamps |
| Token split in / out | **measured** | `tokens_in` / `tokens_out` on the row (from the `azure_openai` checkpoint) |
| Cached tokens | **not measured** | scaling_lib.ai reads only prompt/completion tokens |
| Billable units per attempt (DI pages) | **measured (inventory)** | `ocr_pages`, `di_calls`, `img_ocr_calls` per file; not in the live table |
| Provenance block per run | **written at submit** | git SHA, deployed image tag, job_type, table/queue names, rulepack, model deployment (best-effort; worker-side) |
| Telemetry can't fail a run | **holds** | the worker's completion write is in a `finally`; the UI's own snapshot/audit writes swallow their exceptions |
| Rate-limit wait | **measured** | the `azure_openai_rate_limit_wait` checkpoint total |
| Replica count over time | **observed only** | distinct worker hostnames + task-level peak concurrency; the *requested* replica count needs the Container App scale rule (az) → "not measured" |

## 2.4 Testing, and what is ops-VM-only

- **34 unit tests** cover: the not-measured formatter; every command builder plus the assertions
  that UI-only form fields never reach argv (`report`, `sample`, `benchmark`, `enqueue`,
  `acr-*`); the Compare invariant (incl. the input-only case); the concurrency equal-timestamp
  sweep; the env ops/worker split; the `rescan_keep_count == load_filter_set` proof; the
  archive-must-verify-before-reset guard (injected failure); the deploy-while-active refusal and the
  typed-Container-App confirm; that Docker is never a build gate; and loopback-only binding.
- **Azurite** (local Queue + Table, started via `npm i -g azurite`) validated the store-dependent
  flows end to end: Setup, Monitor, submit → `enqueue.py`, the one-run lock, staging copy, and
  Archive & reset (archiving co-resident jobs, verifying, then clearing).
- **Ops-VM only** (no `az` on the dev box): the deployed-tag-vs-SHA comparison turning green, and
  actually running `acr-build`/`acr-deploy`/`acr-release`. The command construction and every guard
  around them are tested; the execution is one `az`-authenticated machine away.

To run the local Azurite validation yourself: `azurite --skipApiVersionCheck --location <dir>
--queuePort 10001 --tablePort 10002`, point `.env` at the devstore connection string (in
`.env.example`), set `SCALING_LIB_SRC` to a scaling-lib checkout, then `./HWE_Scaled.sh`.
