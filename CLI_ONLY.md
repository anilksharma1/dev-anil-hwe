# What you can't do from HWE Runner — Scaled (and exactly what to type instead)

Written for the operator. HWE Runner — Scaled covers the normal run: check a corpus, submit it,
watch it, collect and score the results, compare runs, archive-and-reset, and build/deploy the
workers. A few things it deliberately does **not** do — either because they can't be done safely
from a localhost web page, or because they belong on a different machine or a one-time setup. This
is the list, why, and the command to run in a terminal instead.

If a command below starts with `scaling-lib`, run it from the repo folder on the **ops VM** (the
machine that has `az login`, the `.env`, and the mounted file shares). If you'd rather not open a
terminal, in HWE Runner's prompt box you can type `! <command>` to run it in-session.

---

## Cannot be done from the UI

| Task | Why | What to do instead |
|---|---|---|
| **`az login`** | Interactive device sign-in, often with MFA — it needs a real terminal and a browser prompt, and it's a once-per-machine thing. | On the ops VM: `az login`. The Setup screen's "Credential / login" row goes green once it's valid. |
| **Start the Windows worker** | It runs on a **different machine** — the Windows VM — because converting legacy `.doc/.xls/.ppt` needs Win32 COM, which only exists on Windows. A localhost page on the ops VM cannot start a process on another host. | On the Windows VM, in the repo folder: `python worker.py`. Leave it polling; it stops itself when the Windows queue is empty. New run's Check tells you how many files need it, and Monitor's Windows panel shows the queue. |
| **Cancel a run midway** | scaling-lib has **no graceful per-job cancel** — no signal that stops the fleet cleanly. See "Stopping a run" below for the honest options. | Stop feeding the queue and let in-flight files finish (Archive & reset), or scale the Container App to 0 replicas. |
| **Provision the infrastructure** | Storage account, queues, table, Container App, managed identity, and the `INPUT_MOUNT`/`OUTPUT_MOUNT` file-share mounts are a one-time setup done by an admin, outside this tool. | Your Azure admin sets these up once. The runbook and `scaling_lib/docs/acr.md` cover it. |
| **Edit secrets in `.env`** | The UI reports every setting as set / not-set and **never shows or writes a value** — that's the no-leak guarantee. | Edit `.env` in a text editor on the ops VM. Setup re-checks it. For Container App secrets: the Azure Portal, or `az containerapp secret set`. |
| **Change the detection rules (rulepack)** | Requires editing YAML and either baking it into the image or mounting it, then a rebuild — an engineering change, not an operator toggle. | Copy `pii_triage_merged/rulepacks/default.yaml`, edit it, set `RULEPACK_PATH`, and rebuild (Build & deploy). Leave `RULEPACK_PATH` unset to use the built-in Master List. |
| **The manual review itself** | Deciding `gold_responsive` / `gold_bde` for the sampled non-searchable files is human judgement done in a spreadsheet. The UI ingests the result; it doesn't host the review. | A reviewer fills those columns in the sample sheet. Then use **vs manual review** to score, or `python -m pii_triage estimate inventory.csv sample.csv --out table2.csv`. |
| **Read worker logs across replicas** | With ~21 workers there's no single log file to tail — it's a query across all of them in Azure. The UI never pretends to tail worker output. | In the Portal, open your Container App's Log Analytics workspace (or Application Insights if `APPLICATIONINSIGHTS_CONNECTION_STRING` is set) and filter by the run's `job_id` and, if needed, a worker hostname. The UI's Monitor shows the failure reason (`error_message`) for failed files without needing this. |

---

## Stopping a run (there is no clean "cancel")

scaling-lib workers pull one file at a time and there is no external stop signal. So "cancel" is
really "stop feeding new work, and decide what happens to what's already in flight." Your options,
honestly:

1. **Archive & reset (from the Runs screen).** This purges every queue and clears the status table.
   - **Queued-but-unclaimed files are dropped** — they never get processed.
   - **A file a worker is *actively* processing at that instant finishes** and writes its
     `result.json` — it is **not** re-queued and **not** force-killed. Its status row was just
     cleared, so that output becomes an orphan under `OUTPUT_MOUNT` (harmless, just uncollected).
   - Because it's destructive and table-wide, the UI archives the run's rows (and any other job's
     rows) to disk and makes you type the `job_id` first.
2. **Scale the workers to zero** (stops the fleet entirely, ops action):
   ```
   az containerapp update --name <AZURE_CONTAINER_APP> --resource-group <AZURE_RESOURCE_GROUP> \
       --min-replicas 0 --max-replicas 0
   ```
   A worker terminated mid-file doesn't finish that file; its queue message reappears after the
   visibility timeout (`QUEUE_VISIBILITY_TIMEOUT_SECONDS`, default 300s) — with no worker running,
   it simply waits. Scale back up (or reset) when ready.

The practical recipe: if you want to abandon a run, **Archive & reset** it (in-flight finishes,
queue drops) — you keep the timing/token archive and start clean. Use scale-to-zero only when you
need the compute stopped immediately.

---

## Possible, but deliberately kept out of the UI

Not oversights — decisions, so the next person knows.

| Left out | Reason |
|---|---|
| `scaling-lib enqueue` (the library's own single-file enqueue) | The repo's `enqueue.py` is what the UI runs instead: it shares one `job_id` across the whole batch (so `status` shows one run, not hundreds of one-file jobs), streams the directory walk, and supports the rescan filter. The New run screen wraps it. |
| `scaling-lib status` (the terminal dashboard) | It needs a TTY and only ever shows the **latest** job. The UI's Monitor reads the same table directly, for **any** run, keyed on `job_id` — strictly better here. `scaling_lib/README.md` notes it needs the `[cli]` extra installed. |
| `scaling-lib queue-status` | Surfaced inside Setup and Monitor (main / Windows / dead-letter counts, with the reading time). |
| Worker tuning (`WORKER_MAX_ATTEMPTS`, `QUEUE_VISIBILITY_TIMEOUT_SECONDS`, `WORKER_IDLE_EXIT_SECONDS`, `WORKER_CONCURRENCY`) | One-time `.env` tuning, not per-run controls. Setup lists them (worker-side). |
| Deep inspection of a single failure | The UI shows each failure's `error_message` (truncated to 500 chars by scaling-lib) and error class — no document text, no PII, ever. Anything deeper is a Log Analytics / table query. |
| Advanced `benchmark` / `estimate` options beyond the vs-manual-review screen | The screen exposes the reviewed-file picker plus the scorer's own column auto-detect (with optional overrides). For anything more, run `python -m pii_triage benchmark …` / `estimate …` directly. |
| `scaling-lib init` | One-time scaffolding of a `Dockerfile` / `.env.example`; already done for this repo. |
