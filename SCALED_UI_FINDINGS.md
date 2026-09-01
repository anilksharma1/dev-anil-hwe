# SCALED_UI_FINDINGS.md — Phase 0

Investigation for the operator UI of the **scaled** (queue + Azure Container Apps) `pii_triage` /
HWE pipeline. **No application code has been written.** This is the read-before-build report the
brief mandates. Where the brief, the runbook, or the transcript disagree with the repository, the
repository wins and the disagreement is called out.

Everything below is cited to a file and line I actually read, or to real `--help` output I
actually ran. Anything I could **not** verify on this machine is labelled as such.

---

## 0. Environment caveat (read this first — it changes what "verified" means)

This is a **macOS developer box**, not the ops VM. Consequences I had to work around:

- **`scaling-lib` is not installed here** and its source was nowhere on disk. I cloned the exact
  ref the image is built from — `git+https://…/ldmglobal-com/scaling-lib.git@dev`
  (`Dockerfile:10`) — to `/tmp/scaling-lib-src`, and read/ran it there. It is the same code the
  workers run.
- I installed `python-dotenv` + the four Azure client libs (`azure-core`, `azure-data-tables`,
  `azure-storage-queue`, `azure-identity`) into user-site **only** so `--help` would import. This
  did not touch the repo, the corpus, or any Azure resource.
- **There is no queue, table, file share, `az login`, or `.env` on this box.** So I could run
  every `--help` and read every schema, but I **cannot** run the pipeline end-to-end here. The
  brief's §8 "watch the 10-file example complete" must happen on the ops VM, or against a local
  **Azurite** stack (scaling-lib ships Azurite pytest fixtures — `scaling_lib/testing.py`,
  `docs/testing.md`). **Flagging now: I will need either ops-VM access or an Azurite setup to
  satisfy §8's live test.** Everything else I can build and unit-test locally.

---

## 1. Verified command surface (real `--help`)

### 1.1 `scaling-lib` (from the cloned `@dev` source)

```
scaling-lib [-h] {status,enqueue,queue-status,reset,init,acr-login,acr-build,acr-deploy,acr-release} ...
    status         Show progress for the most recently enqueued job
    enqueue        Enqueue a file or folder for processing
    queue-status   Show approximate message counts for the work and dead-letter queues
    reset          Clear the status table and all queues to start a fresh run
    init           Scaffold a Dockerfile and .env.example in the current directory
    acr-login      Authenticate Docker to an Azure Container Registry
    acr-build      Build and push a Docker image to ACR
    acr-deploy     Deploy an image to Azure Container Apps
    acr-release    Build, push, and deploy in one step
```

```
scaling-lib status [-h] [--interval SECONDS] [--once]
scaling-lib enqueue [-h] [--limit LIMIT] path
scaling-lib queue-status [-h]
scaling-lib reset [-h]
scaling-lib acr-build   [-h] [--registry R] [--image I] [--tag T] [--job-type J] [--github-token G]
scaling-lib acr-deploy  [-h] [--registry R] [--image I] [--tag T] [--app A] [--resource-group RG]
scaling-lib acr-release [-h] [--registry R] [--image I] [--tag T] [--job-type J] [--github-token G] [--app A] [--resource-group RG]
scaling-lib acr-login   [-h] [--registry R]
scaling-lib init [-h]
```

All spellings are **hyphenated** (`acr-build`, not "ACR build"; `queue-status`; `reset`). The
transcript's spacing is transcription noise. `status` and the ACR commands require the CLI extra:
`pip install "scaling-lib[cli]"` (or `[dev]`), per `scaling_lib/README.md:19`. The core Docker
install has neither.

> **The UI will not shell out to `scaling-lib status`.** It reads the Table/queues directly with
> the same helpers `worker_status.py` already uses (see §3). `scaling-lib status` also only ever
> shows the *latest* job (§4, Q8) and needs a TTY — both wrong for our purposes.

### 1.2 Repo scripts (real `--help`, ran on this box)

```
enqueue.py [-h] [--inventory INVENTORY_CSV] [--exclude-lanes EXCLUDE_LANES] [--job-id JOB_ID] [--env-file ENV_FILE] files_dir
collect_outputs.py [-h] [--out OUT] [--env-file ENV_FILE] [--concurrency CONCURRENCY]
worker.py [-h] [--no-ocr] [--no-llm] [--no-ner]
worker_status.py [-h] [--filter STATUS] [--since DATETIME] [--interval SECONDS] [--once]
```

`enqueue.py` gained flags the runbook never mentions: **`--job-id`** (supply your own batch id) and
**`--exclude-lanes`** (`enqueue.py:106-114`). `worker_status.py` is a whole script the brief did
not know about — see §3.

### 1.3 `pii_triage` (real `--help`)

```
pii_triage [-h] [--version] {scan,report,sample,estimate,benchmark} ...
pii_triage report   [-h] [--out OUT] inventory
pii_triage sample   [-h] [--out OUT] [--rate RATE] [--seed SEED] inventory
pii_triage estimate [-h] [--out OUT] inventory coded_sample
pii_triage benchmark[-h] [--id-col C] [--responsive-col C] [--bde-col C] [--sheet S] inventory gold
```

- **There is no `project` subcommand and `scan` has no `--project` flag** (`cli.py:17-101`). See Q7.
- Default out names differ from the runbook: `report` → `table1_searchable.csv`, `sample` →
  `nonsearchable_sample.csv`, `estimate` → `table2_nonsearchable.csv` (`cli.py:80,84,92`). The UI
  will pass explicit `--out` into `runs/<id>/` and not rely on defaults.
- `benchmark` auto-detects the gold sheet's id/responsive/bde columns unless overridden — this is
  the scaled equivalent of the local build's `peek_columns()` (see Q for §6.6 / notes #10).

---

## 2. Azure Table schema (the store the UI reads)

Table name = `AZURE_TABLE_NAME` (runbook default `DocProcessingStatus`). One row per **file**.
Written by `scaling_lib.status.init_task` / `update_task` and `metrics._task_to_fields`
(`status.py:60-86`, `metrics.py:155-167`), documented at `docs/status.md:56-72` and
`docs/metrics.md:126-148`. Confirmed by reading the code, not just the doc.

| Column | Type | Set when | Meaning |
|---|---|---|---|
| `PartitionKey` | str | enqueue | **Job ID** (batch: `<job_dir>-job|rescan-<rand8>`; single file: UUID4). Everything keys on this. |
| `RowKey` | str | enqueue | Task ID = `uuid5(NAMESPACE_URL, "{job_type}:{job_id}:{file_name}")` — **deterministic per (job,file)**. `status.py:55-57` |
| `file_name` | str | enqueue | Original filename (basename only, not the rel path). |
| `status` | str | every transition | `pending → processing → completed / failed / dead_lettered`. `status.py:15` |
| `enqueued_at` | datetime | enqueue | |
| `started_at` | datetime | processing | worker dequeue time. |
| `completed_at` | datetime | terminal | |
| `worker_instance` | str | processing/terminal | **container hostname** (`socket.gethostname()`). Distinct values = worker/replica count. `worker.py:198` |
| `attempt_count` | int | every transition | number of attempts (starts 1; +1 on requeue). |
| `output_path` | str | completed | `job_id/file_name` (relative). Resolve with `get_output_dir` → `OUTPUT_MOUNT/job_id/file_name`. `worker.py:258`, `storage.py:6-14` |
| `error_message` | str | failed/dead_lettered | exception text truncated to 500 chars (`_MAX_ERROR_LENGTH`). **Error class/message only — no document text, no PII.** `status.py:17,80-81` |
| `file_size_bytes` | int/None | terminal | input size. `metrics.py:159` |
| `processing_s` | float | terminal | dequeue→terminal wall-clock, **final attempt only**. `docs/metrics.md:149` |
| `tokens_in` | int | terminal | Σ prompt tokens over `azure_openai` checkpoints (**split IS recorded**). `metrics.py:35-40`, `ai.py:324-331` |
| `tokens_out` | int | terminal | Σ completion tokens. |
| `checkpoints` | str(JSON) | terminal | array of `{label, duration_s, metadata}` — the per-stage timing breakdown (see §5). `metrics.py:163-166` |

**Reading it:** `_fetch_entities(status_filter=None, since=None)` returns rows across **all**
partitions (`status.py:92-107`). To scope to one run, query the partition directly:
`table_client.query_entities("PartitionKey eq '<job_id>'")` — this is what `run_metrics()` does
internally for the *latest* job (`metrics.py:170-227`); the UI will do the same for **any** job_id.
The `RunMetrics`/`TaskRecord` dataclasses (`metrics.py:20-142`) can be reused for aggregation.

---

## 3. Queue message schema + the `worker_status.py` bonus

**Queue message** (`queue.py:206-212`, built by `enqueue.py:87`), JSON:

```json
{"path": "<rel-to-INPUT_MOUNT>", "job_id": "...", "job_type": "...", "attempt_count": 1}
```

`path` is POSIX for Linux-queue files and native (`str(path)`) for Windows-queue files
(`posix=not is_win`, `enqueue.py:87`). The worker joins it back onto its own `INPUT_MOUNT`
(`worker.py:162`). **Queue messages carry no PII and the UI never needs to read them.**

**`queue_status()`** (`queue.py:131-145`) returns `{"queue_count", "dead_letter_count",
["windows_queue_count"]}` using `approximate_message_count`. Caveat baked into Azure: this count is
**approximate and excludes in-flight (invisible) messages**, so "queue_count" ≠ "pending tasks".
The **table** is the precise progress signal; the queue count is a secondary readout. (Directly
relevant to notes #5: progress comes from the durable record, not a count that lies.)

**`worker_status.py`** — undocumented in the brief, already in the repo. It is a live TUI over the
**same table**, adding a token column, reusing `scaling_lib.status._fetch_entities`,
`_eta_string`, `_STATUS_DISPLAY`, and `scaling_lib.tui._progress_bar`. It is effectively a
read-only prototype of our monitor screen and confirms every field above. Two things it teaches:
(1) tokens only appear once a file reaches a terminal state (`worker_status.py:20-23`); (2) those
scaling-lib helpers are underscore-prefixed/unstable API (`worker_status.py:14-18`) — I will read
the Table rows myself rather than depend on private helpers where it matters.

---

## 4. The ten questions

**Q1 — Is there a reset command? YES, and it is brutal.**
`scaling-lib reset` (`__main__.py:36,94-99`) calls `clear_all_tasks()` (deletes **every row in the
whole table**, all partitions, batched — `status.py:229-249`) then `clear_all_queues()` (clears
main + dead-letter + Windows via `clear_messages()` — `queue.py:191-203`). Prints `Cleared N status
row(s) and purged all queues.` It is **not** job-scoped and **not** undoable. This is exactly the
"archive-before-reset" hazard (§6.8): reset destroys the only record of timing/tokens/attempts. The
UI must snapshot the target partition first and verify the snapshot before ever calling this.

**Q2 — Exact subcommand spellings.** Verified in §1.1: `status`, `enqueue`, `queue-status`,
`reset`, `init`, `acr-login`, `acr-build`, `acr-deploy`, `acr-release`. Hyphenated. Runbook was
right; transcript spacing was noise.

**Q3 — What path form does `enqueue.py` accept? Whatever matches `INPUT_MOUNT` on the enqueue
host — and the runbook example is internally inconsistent.**
`enqueue.py:55,80` does `os.path.abspath(files_dir)` then `Path(f).relative_to(INPUT_MOUNT)`. So the
path **must be under `INPUT_MOUNT`, in the same form `INPUT_MOUNT` is written**. On the ops VM the
runbook sets `INPUT_MOUNT=I:\` (`RUNBOOK.md:26`) but then shows `python enqueue.py
/mnt/input/<job_dir>/files` (`RUNBOOK.md:97`) — a POSIX path that `Path("/mnt/…").relative_to("I:\\")`
would reject with `ValueError`. **The runbook's enqueue example cannot work with its own
`INPUT_MOUNT`.** The repo `.env.example` uses POSIX mounts (`INPUT_MOUNT=/mnt/input`,
`.env.example:25`), which is the Linux-container convention. **UI implication:** the folder picker
must yield a path under whatever `INPUT_MOUNT` is on the ops VM (Windows → `I:\…\files`), and the
UI should validate `is_relative_to(INPUT_MOUNT)` before enqueue and explain the failure if not.

**Q4 — How does a worker find `protocol.*`? Walk up to the `files/` ancestor; the name `files` is
load-bearing; five extensions.**
`worker.py:105-117`: `_job_dir_for` walks parents until `parent.name == "files"`, returns its
parent as `<job_dir>`; `_find_protocol_file` looks **only** for `<job_dir>/protocol{.pdf,.docx,
.doc,.txt,.rtf}` (`_PROTOCOL_EXTS`, `worker.py:100`). So: the folder **must** be named exactly
`files` (subfolders under it are fine), the protocol **must** be named exactly `protocol.<ext>` and
sit as a sibling of `files/`, and only those five extensions are read. No `files/` ancestor → a
warning and processing continues without protocol (`worker.py:124-135`). This is pure convention in
code, **not configurable** — there is no env var or flag to point at a protocol elsewhere. That
directly bounds the §6.3 staging options (see the staging analysis at the end).

**Q5 — Where does `job_id` come from, and can I set it? `enqueue.py` mints it, prints it to
stderr, and yes you can override it.**
`enqueue.py:70`: `job_id = job_id or f"{Path(files_dir).parent.name}-{suffix}-{uuid4().hex[:8]}"`
where suffix ∈ {`job`,`rescan`}. It is printed to **stderr** (`enqueue.py:96,98`:
`enqueued N file(s) under job_id=<id>`), **not stdout**, and `enqueue()` also returns the matched
count (not the id). **UI implication:** to capture the id, either parse stderr for `job_id=…`, or
(cleaner and race-free) **pass our own `--job-id`** and record it directly — I recommend the
latter, deriving a readable id like `<jobdir>-<runstamp>`.

**Q6 — pii_triage vs HWE vs "hybrid workflow estimator": same pipeline, layered names.**
`README.md:1-5`: *"pii_triage 3.0.0 … Implements the **HWE Bucketing & Tagging** stage and emits the
two HWE deliverables."* HWE = **H**ybrid **W**orkflow **E**stimator (the product / the two
deliverable tables); `pii_triage` = the package that implements its bucketing/tagging stage; the
local UI is branded **"HWE Runner."** The transcript's "hybrid workflow estimator" is HWE.
**UI implication:** brand the scaled UI in the same family (e.g. **"HWE Runner — Scaled"**), say
"pii_triage" only where naming a command.

**Q7 — Project / coverage-gate concept in the scaled repo? NO.**
`pii_triage/cli.py` exposes only `scan/report/sample/estimate/benchmark`; `scan` has no `--project`.
The only `exit/SystemExit(2)` sites are "not a directory" (`cli.py:108`) and **"the CSV is open in
Excel"** (`runner.py:715-721`, `_check_locked`) — neither is a coverage gate. `grep` for
`project`/`coverage`/`project.json` across `pii_triage_merged/pii_triage/*.py` finds nothing. The
`scan --project`/exit-2 coverage gate described in the local notes (#11) belongs to a **different,
project-layer build** (the Downloads `merged 2` tree has `PROJECT_LAYER.md`), **not** this scaled
repo. **UI implication: the "Projects / matters" screen (§6.2) has no scaled counterpart.** There
is nothing to mirror and no gate to enforce. Protocol handling folds into New run (§6.3), exactly
as the brief's fallback says. (Notes #11 "mirror the gate" therefore has no gate to mirror here — I
will document that rather than invent one.)

**Q8 — What per-attempt telemetry exists? A lot at the task level; but NOT per-attempt, NOT cached
tokens, and NOT DI pages *in the store*. DI billing lives in the inventory instead.**
This is the most consequential answer, so it is itemised:

- **Recorded in the Table, per file (final attempt):** `processing_s`, `file_size_bytes`,
  `tokens_in`, `tokens_out` (real in/out split — `ai.py:324-331`), `attempt_count`, and a
  `checkpoints` JSON of `{label, duration_s, metadata}`.
- **`run_metrics()` aggregate** (`docs/metrics.md:5-24`, `metrics.py:62-142`): `files_completed/
  processing/pending/failed`, `files_retried`, `total_extra_attempts`, `total_bytes`,
  `avg_processing_s`, `wall_clock_s`, `worker_count`, `total_tokens_in/out`, and `checkpoints`
  aggregated across tasks. **Caveat: `run_metrics()` only ever targets the single most-recently
  enqueued job** (`metrics.py:170-188`, `docs/metrics.md:147`) — the UI must query by PartitionKey
  for any other run.
- **Rate-limit wait IS measured:** automatic `azure_openai_rate_limit_wait` checkpoint
  (`ai.py:306`, `docs/metrics.md:119`). So the §6.4 headline metric is buildable — as a **total**,
  from `checkpoints["azure_openai_rate_limit_wait"].total_s`.
- **Per-attempt records: NO.** One deterministic RowKey per file, MERGE-upserted. On a failed→
  retried attempt the worker writes only `status=pending, attempt_count+1` and **no metrics/tokens**
  (`worker.py:233-236`); only the final attempt's metrics survive (dead-letter path writes final-
  attempt metrics — `worker.py:241-245`). **So tokens/pages burned on a failed-then-retried attempt
  are invisible.** Retries are *counted* (`attempt_count`, `total_extra_attempts`) but not *priced*.
- **Cached-token split: NOT measured.** `ai.py` reads only `usage.prompt_tokens` /
  `completion_tokens` (`ai.py:324-326`), never `prompt_tokens_details.cached_tokens`. The §6.5 cost
  panel's "cached" bucket = **"not measured."**
- **DI billable pages: not in the store, but IN the inventory.** The `document_intelligence`
  checkpoint carries **no** page metadata (`ai.py:458`, `docs/metrics.md:120`). **However** the
  pii_triage **inventory** records real DI billing per file: `ocr_attempted`, `ocr_pages` (full-file
  DI pages), `img_ocr_qualifying/calls/ok`, `img_decode_failed`, and `di_calls` (billable DI calls)
  — `routing.py:105-113`. So the §6.5 "billable units not call counts" panel **is** buildable — from
  the inventory (after collect), summing `ocr_pages` + `img_ocr_calls` for pages and `di_calls` for
  calls, with the embedded-image split that produces the 16.9%-by-pages vs 95%-of-calls story. It
  is **not** buildable from the live Table mid-run (no page metric there).
- **No `ts_start`/`ts_end` per call.** `CheckpointRecord` stores only `duration_s`
  (`metrics.py:13-17`) — **no absolute per-call timestamps.** A per-*call* concurrency sweep (§7) is
  therefore **impossible** from stored data. A per-*task* sweep over `[started_at, completed_at]`
  across the partition **is** possible and gives the real replica-occupancy series (§6.4). I will
  implement exactly that, with the equal-timestamp "close-before-open" rule and a unit test.
- **Telemetry-fails-a-run safety:** the worker's completion counter is in a `finally`
  (`worker.py:256-258`); the whole scan does not depend on telemetry writes. Good, but I will still
  keep the UI's own snapshot/telemetry writes exception-swallowing per §7.

**Q9 — What must be true before `collect_outputs.py`? Nothing is enforced; it collects whatever is
`completed` across the whole table.**
`collect(...)` calls `_fetch_entities(status_filter="completed")` (all partitions) and reads each
`OUTPUT_MOUNT/job_id/file_name/result.json`; entities with a `forwarded.json` (Windows stubs) are
skipped; genuinely-missing outputs are warned, not fatal (`collect_outputs.py:23-89`). **It does not
check queue depth.** Run while the queue is still draining, you get a **partial** inventory plus
"output not found" warnings for in-flight files — silently incomplete. **Two UI implications:**
(1) the Results screen must gate Collect on drained-ness itself (queue counts == 0 **and** table
pending+processing == 0), naming the outstanding count when it refuses; (2) because collect is
**table-wide, not job-scoped**, the UI must ensure the table holds only the current run's rows
before collecting (i.e. the reset-before-run discipline held), or scope the resulting inventory to
the run's files. There is no `--job-id` on collect.

**Q10 — Dead-letter reader? Only the queue depth + the `error_message` on failed/dead-lettered
rows. No dedicated DLQ message reader.**
`queue_status()` gives `dead_letter_count` (`queue.py:142-144`). Per-failure detail is the
`error_message` (≤500 chars) on the table rows whose `status ∈ {failed, dead_lettered}`
(`status.py:80-81`, surfaced already by `worker_status.py:121-125,245`). There is **no** function
to pop/inspect DLQ message bodies, and we would not want to (a re-driven message body is a queue
message, not PII, but the failure detail we need already lives on the table). **UI failures view =
count from the queue + `{file_name, status, attempt_count, error_message}` rows from the table.**
File identifier + error class only — no document text.

---

## 5. What the store owns vs the UI owns (confirmed against code)

- **Progress / done-ness:** Table rows by status. Never a CSV row count (that was the local model;
  here the CSV doesn't exist until collect).
- **Queue depth / in-flight / DLQ:** `queue_status()` — approximate; timestamp every reading.
- **Job identity:** `enqueue.py` job_id (I'll set it via `--job-id`).
- **Per-run provenance / argv / operator / who-reset:** `runs/<id>/run.json` + an audit log — the
  UI is the only writer (matches local `run.json` model, `hwe_ui.py:275-305`).
- **Inventory / tables / sample:** `collect_outputs.py` + `pii_triage` outputs, copied into
  `runs/<id>/`.
- **Per-stage timing** (for a "stage split" like the local one): the `checkpoints` JSON on each
  row. pii_triage emits `extract, ocr, detect, classify, llm, bde_count, route, stage2`
  (`runner.py:151-284`) plus scaling-lib's automatic `azure_openai`, `azure_openai_rate_limit_wait`,
  `document_intelligence`, and the Windows-leg `convert` (`worker.py:198`). Rich enough for a real
  stage breakdown.
- **Worker health:** only what a worker writes (`worker_instance` hostname, when it processes a
  file). **I will not report the ops-VM's Python/Pillow versions as the workers'** (notes §2.2).

The UI holds only live `subprocess.Popen` handles for processes **it** starts (enqueue, collect,
report, sample, benchmark, estimate, acr-*). Everything else is re-read from Table/queue/disk.

---

## 6. Inventory (result.json / FileRecord) — what a screen may render

`FIELDNAMES = [f.name for f in fields(FileRecord)]` (`routing.py:64-139`). No-PII-safe columns the
UI can show: `rel_path, file_name, ext, size_bytes, status, searchable, programmatic,
text_extractable, is_structured, page_or_sheet_count, estimated_entities, bde_person_count,
entity_bucket, entities_found` (labels like `"Name | SSN | Address"`, never values — `routing.py:82`),
`pi_categories, is_bde, complexity_bucket, ambiguity, llm_consulted, llm_responsive, llm_tokens,
suggested_lane, detail`, the OCR/DI accounting block (`ocr_pages, di_calls, img_ocr_*`, `elapsed_s`),
the stage-2 block (`s2_llm_responsiveness ∈ {clear_yes,likely_yes,borderline,likely_no,clear_no}`,
`s2_is_bde, s2_lane, s2_nr`), and rollups (`llm_tokens_total, llm_tokens` + `s2_llm_tokens`).

Two things this settles:
- **Compare (§6.7) invariant maps cleanly:** a row is *model-decided* iff `llm_consulted` (stage 1)
  or `s2_llm_consulted` (stage 2) is true; everything else is *rules-decided* and must be
  byte-identical between two runs. The scaled repo has no bundled `check_repeatable.py` tool
  (the local build shelled to one), so the UI will implement the diff itself against this rule and
  document it as having to agree with the routing definition.
- **Two-pass documents (§6.5) — the exact failure mode, confirmed.** A Windows `.doc/.xls/.ppt`
  produces **two** Table rows: the original-named row (Windows leg, writes `forwarded.json`, no
  `result.json`, **no tokens**) and a **converted-named** row (Linux leg, has `result.json` +
  tokens). `worker.py:243-247` rewrites the inventory record back to the **original** `rel_path/
  file_name/ext` — but `converted_from` is **not** a FIELDNAME, so `collect`'s `extrasaction=
  "ignore"` (`collect_outputs.py:74`) **drops it**. Net: the inventory row is under the original
  name (good, stable-ish id = `rel_path`), but the **token telemetry in the Table is filed under the
  converted name** and cannot be joined to the inventory by filename. Reconciliation requires the
  `forwarded.json` (`{"forwarded_to_file": …}`) / `.orig.json` (`{"orig_rel_path", …}`) sidecars
  (`worker.py:203-209`). **So "a stable id that survives conversion" is half-present:** the
  inventory keeps original identity; the cost telemetry does not. The UI must (a) key per-document
  views on `rel_path`, and (b) surface the two-pass count by counting `forwarded.json` outputs, and
  explain that per-document *token* cost is unattributable for those files without the sidecars.

---

## 7. Environment variables the code actually reads (for the Setup screen)

Derived from the code, split by **who** reads them — because the ops-VM UI host and the worker
containers need **different** sets, and flagging worker-only vars as "missing" on the ops VM would
be a false alarm (directly serves §6.1).

**Needed on the ops VM (the UI host) — enqueue / collect / reset / status:**
`INPUT_MOUNT`, `OUTPUT_MOUNT`, `JOB_TYPE`, one of {`AZURE_STORAGE_CONNECTION_STRING`} **or**
{`AZURE_STORAGE_QUEUE_URL` + `AZURE_STORAGE_TABLE_URL` (+ platform `AZURE_CLIENT_ID`)},
`AZURE_QUEUE_NAME`, `AZURE_DEAD_LETTER_QUEUE_NAME`, `AZURE_TABLE_NAME`, `AZURE_WINDOWS_QUEUE_NAME`
(optional), `WINDOWS_FILE_EXTENSIONS` (optional). Auth: `_credential()` = `DefaultAzureCredential`,
or `AzureCliCredential` if `AZURE_CREDENTIAL_TYPE=cli` (`_config.py:15-26`).
Sources: `queue.py:22-30,65-74`, `status.py:28-36`, `storage.py:12`, `enqueue.py:60-74`.

**Needed on the ops VM only for Build/Deploy (§6.9):** `ACR_REGISTRY`, `ACR_IMAGE`,
`AZURE_CONTAINER_APP`, `AZURE_RESOURCE_GROUP`, `JOB_TYPE`, and `GITHUB_TOKEN` (or git-credential
fallback) for `acr-build`/`acr-release` (`__main__.py:107-131,192-219`).

**Needed by the WORKERS (Container App runtime / Windows VM), *not* the UI host** — do not
red-flag these on the ops VM: `USE_OCR/USE_LLM/USE_NER`, `AZURE_OPENAI_ENDPOINT`,
`AZURE_OPENAI_DEPLOYMENT` (or `…_GPT_5_4_NANO`), `AZURE_OPENAI_API_VERSION`, `AZURE_DI_ENDPOINT`
(or `AZURE_DOCUMENTINTELLIGENCE_ENDPOINT`), `AZURE_KEY_VAULT_URL`, `RULEPACK_PATH`, `BDE_THRESHOLD`,
`FILE_TIMEOUT_S`, `MAX_BYTES`, `MAX_SCAN_CHARS`, `MAX_SCAN_ROWS`, `DEFAULT_JURISDICTION`,
`LLM_INPUT_CHARS`, `QUEUE_VISIBILITY_TIMEOUT_SECONDS`, `WORKER_MAX_ATTEMPTS`,
`WORKER_IDLE_EXIT_SECONDS`, `WORKER_CONCURRENCY`, the `DLQ_*` breaker vars,
`APPLICATIONINSIGHTS_CONNECTION_STRING`, `LOG_FILE_PATH`.
Sources: `worker.py:34-39,72-93`, `azure_clients.py:26-66,177-195`, `worker.md:46-52`.

The Setup screen will show set/not-set (never values) and label each row by which host needs it and
the consequence if missing. Daniel's promise to "add templates to `.env.example`" is **partly** done
— the repo `.env.example` has the Azurite connection-string shape and API-version/mount examples,
but the production `AZURE_STORAGE_*_URL` lines are blank (`.env.example:6-7`).

---

## 8. Where the brief / runbook / transcript were wrong

1. **§6.9 "Docker running" is not a real prerequisite.** `acr-build` runs the build in ACR's cloud
   via `az acr build` (`__main__.py:227-232`, `docs/acr.md:9`). Docker is only needed for
   `acr-login` (local pull/run). The Build/Deploy preflight should check **`az` + login**, not
   Docker.
2. **Runbook §4 enqueue path is inconsistent with its own `INPUT_MOUNT`** (Q3). POSIX path vs `I:\`.
3. **Runbook default out-names differ from the CLI defaults** (§1.3). Minor; UI passes `--out`.
4. **The brief's §6.2 "Projects" and notes #11 "coverage gate" don't exist in this repo** (Q7).
5. **`entities_found` etc. are safe, but `converted_from` silently doesn't reach the inventory**
   (§6). The two-pass "stable id" is only half-solved (Q8/§6).
6. **`run_metrics()` / `scaling-lib status` only see the latest job** — not a per-run history
   source (Q8). The UI must query by PartitionKey.
7. **No graceful per-job cancel exists** (see §9). The brief's §6.4 cancel must be honest about
   this.
8. **`enqueue.py` has `--job-id` and `--exclude-lanes`** the runbook never documents (§1.2) — these
   make the "supply your own id" and "rescan subset" features clean.

---

## 9. How this changes the plan (screen by screen)

- **Setup (§6.1):** build, with the ops-VM-vs-worker env split above, `DefaultAzureCredential`
  token probe for "az login", queue/table reachability, mount readability, and — the headline —
  **deployed image tag (from `az containerapp show`) vs local `git rev-parse --short HEAD`**. Windows
  worker liveness can only be *inferred* (no heartbeat exists — see the decision list). "Run tests"
  maps to `python -m pytest pii_triage_merged/tests` (111 tests, `CLAUDE.md`).
- **Projects (§6.2):** **omit** (Q7). Fold protocol validation into New run.
- **New run (§6.3):** build. Validate `<job_dir>/files/` (name exactly `files`) + sibling
  `protocol.{pdf,docx,doc,txt,rtf}`; Check walks the corpus (reuse the local `count_files`), reports
  total + by-ext + **Windows-queue count** (`.doc/.xls/.ppt`, or `WINDOWS_FILE_EXTENSIONS`) + the
  protocol found + the exact `enqueue.py` command; cache count vs path with a `no_count` command-only
  rebuild. Rescan mode = `--inventory <prior run's inventory> [--exclude-lanes …]`. Submit runs
  `enqueue.py --job-id <ours>`, writes `runs/<id>/run.json` with the provenance block (§7 of brief).
  Loud Windows-leg warning with the exact `python worker.py` line for the Windows VM.
- **Running / monitor (§6.4):** build. Table-derived per-state counts; `queue_status()` depths
  (main/Windows/DLQ) with a reading timestamp; throughput + ETA **range**; **task-level** replica
  occupancy series (not per-call); rate-limit wait **total** (labelled; "not measured" is not
  needed — it *is* measured, as a total); stuck items = `processing` older than
  `QUEUE_VISIBILITY_TIMEOUT_SECONDS`; failures from DLQ count + failed/dead-lettered rows; Windows
  panel; console tail **only** for UI-spawned processes, explicitly labelled; a copy-able Log
  Analytics note for worker logs (no pretend tail).
- **Results (§6.5):** build. Gate Collect on drained-ness (Q9). Cost panel on **billable units from
  the inventory** (`ocr_pages/di_calls/img_ocr_*`) + token in/out split from the **Table**
  (`tokens_in/out`) + stage-1/stage-2 token split from the inventory; **cached = "not measured."**
  Two-pass count from `forwarded.json`. Report/sample via `pii_triage report|sample` with seed
  recorded.
- **vs manual review (§6.6):** build on `pii_triage benchmark` (id/responsive/bde auto-detect =
  the scaled `peek_columns`) and `estimate`. No second header-finder in the UI.
- **Compare (§6.7):** build; implement the rules-decided-byte-identical diff from the
  `llm_consulted`/`s2_llm_consulted` definition (§6), and diff the provenance blocks.
- **Runs (§6.8) + Archive-and-reset:** build. Reconstruct UI-external runs from the Table (by
  PartitionKey) and from `outputs/*/inventory.csv` (real prior runs already on disk), marked as
  external. Archive = snapshot the run's partition rows + `run_metrics`-style aggregate into
  `runs/<id>/`, **verify readable**, refuse reset if collect/report absent (override required), typed
  confirm naming the row count, audit entry — **then** `scaling-lib reset`.
- **Build/Deploy (§6.9):** buildable (three real actions map 1:1 to `acr-build/deploy/release`),
  but see the decision list — and correct the Docker preflight (§8.1).

---

## 10. Decisions — RESOLVED 2026-08-17

Your answers, now locked into the plan:

1. **Staging (§6.3): copy into `files/` only.** No symlink probe. The UI copies the corpus into
   `<job_dir>/files/` with a size/time estimate + progress, never deleting the source; manual
   staging documented in `CLI_ONLY.md` as the alternative.
2. **Windows liveness (§6.1): infer from queue/table.** No pipeline change; show "likely not
   running" as a labelled inference, never a certainty.
3. **Build/Deploy (§6.9): include, heavily guarded.** Three actions, exact command shown, typed
   Container-App-name confirm, refuse-while-run-active, and the corrected **`az` + login** preflight
   (not Docker, per §8.1).
4. **Concurrency (§4.2): one run at a time.** Lock file naming who holds it + refuse a second
   submit; all history keyed on `job_id`.

The original analysis and options are retained below for the record.

### Original options presented

1. **Staging helper (§6.3).** Given Q4 (the `files` name and `protocol.*` names are hard-coded in
   `worker.py`, no override exists), the options rank as:
   - **(1) point at an existing job root** — *not possible* without a pipeline change (forbidden).
   - **(2) create `<job_dir>/files/` of symlinks/junctions to the corpus in place** — *cheapest
     non-copy*, but Azure Files SMB commonly **rejects symlinks**; I **cannot test that here** (no
     mount). Needs an ops-VM probe before I trust it.
   - **(3) copy into `files/`** — always works, no source deletion, but moves data (your 200k-doc
     objection). I'd show size/time estimate + progress.
   My plan: build the **validator + a one-shot symlink probe** first; if SMB symlinks work, ship
   (2); otherwise offer (3) with the estimate, and document "stage manually" in `CLI_ONLY.md`.
   Confirm that's the approach you want.
2. **Windows-worker liveness (§6.1).** No heartbeat exists. I can **infer** ("Windows queue > 0 and
   static for N min with no recent Windows-hostname task" ⇒ *likely not running*) with **no pipeline
   change**, or I can **propose** a tiny heartbeat file the Windows worker writes (a `worker.py`
   change needing your OK). Infer-only, or shall I draft the heartbeat proposal?
3. **Build/Deploy in the UI (§6.9).** It's technically clean (three commands, env-var fallbacks,
   typed-confirm, refuse-while-run-active). My reservation: it changes what **every** replica runs
   and is the rarest action — the failure blast radius is large for a non-technical user. I lean
   **include it, heavily guarded** (correcting the Docker check), but I'll drop it to `CLI_ONLY.md`
   if you'd rather. Your call.
4. **Concurrency model (§4.2).** Jobs **are** partition-isolated by `job_id`, so the table *can*
   hold several — **but** `collect_outputs.py` (table-wide), `run_metrics()`/`scaling-lib status`
   (latest-only), and `reset` (wipes all) all assume one run. I recommend **enforce one active run
   at a time** (lock file + refuse second submit, naming who holds it) while keying all history on
   `job_id`. Confirm, or do you want true concurrent-job support (more work, fights the tooling)?

---

**Next step:** per the build order, I'll start **Phase 2 (read-only over existing artefacts)** —
Runs list, run detail, Results, Compare, vs-manual-review — which needs none of the four decisions
above and works against the real inventories already in `outputs/`. Awaiting your go-ahead and your
answers to §10.
