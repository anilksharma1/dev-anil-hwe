# Backlog — Known Issues & Planned Updates

A living backlog for **pii_triage**: known issues, limitations, documentation drift, deferred
features, planned enhancements, and technical debt.

> **How to read this.** Every item is grounded in the code or the project's own docs, with a
> pointer to the evidence. **Severity/Priority is a suggested assessment, not an official
> roadmap** — triage with the team before committing. Compiled from the `combined-scaling`
> branch; keep it current as items are resolved.
>
> Severity: 🔴 high · 🟠 medium · 🟡 low. Status: `open` unless noted.

---

## A. Known issues & bugs

| ID | Item | Evidence | Sev | Suggested action |
|---|---|---|---|---|
| A1 | **`NameError` in embedded-image OCR error path.** `_pdf_extract_content_images(reader)` logs `… of %s …, path` in its outer `except`, but `path` is not a parameter of the function — so when per-page `page.images` iteration itself raises, the handler raises `NameError` instead of logging-and-continuing. | `pii_triage_merged/pii_triage/extractors.py:374-375` | 🟡 | Pass the pdf path into the function, or drop `path` from the log call. Only fires on the rare page-image-iteration failure. |
| A2 | **scaling-lib pinned inconsistently.** The Dockerfile installs `scaling-lib.git@dev` (a moving branch); `requirements-local.txt` installs `scaling-lib` with **no ref** (default branch). Two environments can resolve different versions, and a moving branch is non-reproducible. | `Dockerfile:10` vs `pii_triage_merged/requirements-local.txt:2` | 🟠 | Pin both to the same immutable tag/SHA. |
| A3 | **Undeclared runtime dependencies.** `python-dotenv` and `textual` are imported but not declared in any requirements file — they rely on transitive/optional install. If `scaling-lib` stops pulling `python-dotenv`, `worker.py`/`enqueue.py`/`collect_outputs.py` break at import. | `worker.py`, `enqueue.py` (dotenv); `worker_status.py:140` (textual); see `DEPENDENCIES.md` §1.5 | 🟠 | Declare `python-dotenv`; declare `textual` as an optional/extra for the TUI. |

## B. Known limitations

| ID | Item | Evidence | Sev | Notes |
|---|---|---|---|---|
| B1 | **UI "not measured" metrics.** Token in/out split, cached tokens, rate-limit wait, replica series, and two-pass token attribution live only in the Azure Table (or nowhere), so inventory-only UI views render them "not measured." | `hwe_scaled_ui.py::summarize_inventory` | 🟡 | Enhancement: wire `store.job_metrics()` (Table) data into those views instead of showing "—". |
| B2 | **Windows leg is a manual native worker.** Legacy `.doc/.xls/.ppt` conversion runs as a hand-started `python worker.py` on a Windows VM — not containerized or autoscaled. | `worker.py` Windows leg; `RUNBOOK.md` §3.5 | 🟠 | Operational SPOF; document ownership/monitoring, or containerize with a Windows base image later. |
| B3 | **LLM cost is a range, not a point.** The API returns only `total_tokens`; without a timing snapshot the in/out split is unknown, so cost is reported as bounds. | `runner.py::_cost_summary`; `score_combined.py` | 🟡 | Inherent; exact cost needs the `_timing.json` split. |
| B4 | **Test deps ship in the worker image.** `pytest` and `reportlab` are in `requirements.txt`, which the Dockerfile installs — so they land in the production image. | `pii_triage_merged/requirements.txt`; `Dockerfile` | 🟡 | Split test-only deps into `requirements-test.txt` to slim the image. |

## C. Documentation drift

| ID | Item | Evidence | Sev | Suggested action |
|---|---|---|---|---|
| C1 | **Library README has stale Azure facts.** API-version default listed as `2024-10-21` (code: `2024-12-01-preview`); lists only `AZURE_DOCUMENTINTELLIGENCE_ENDPOINT` (code also accepts `AZURE_DI_ENDPOINT`); shows `gpt-4.5-nano` as the only fallback (code also reads `AZURE_OPENAI_DEPLOYMENT_GPT_5_4_NANO`). The root README was already corrected. | `pii_triage_merged/README.md:184,189,192` vs `azure_clients.py:198-206` | 🟡 | Sync the library README with the code. |
| C2 | **`FileRecord` field count.** README/CLAUDE say "49 columns"; the dataclass has **50** fields (`FIELDNAMES`). | `routing.py` `FIELDNAMES`; `README.md`, `CLAUDE.md` | 🟡 | Reconcile the "49" wording (or cite `FIELDNAMES` as source of truth). |
| C3 | **Lane count.** CLAUDE lists 10 lanes; `choose_lane` can emit an 11th, `structured_unreadable`. | `routing.py::choose_lane`; `CLAUDE.md` | 🟡 | Add `structured_unreadable` to the docs table. |
| C4 | **Detection-method count.** CLAUDE's table lists 7 methods; `detect()` has 8 dispatch branches. | `detection.py::detect`; `CLAUDE.md` | 🟡 | Update the methods table. |

## D. Deferred / removed features (from the 3.0.0 merge)

Explicitly excluded during the merge and recorded in the README's *What changed* section —
candidates to revisit, each "in its own change."

| ID | Item | Evidence | Notes |
|---|---|---|---|
| D1 | **Table-aware detection.** `detect(tables=…)` and `detect_labeled_value_cells()` were removed as dead code (no call sites, no rulepack declared the method), along with the `pdfplumber` per-PDF table pass. | `README.md` *Deliberately excluded* | "A real idea" — reintroduce with call sites + before/after if wanted. |
| D2 | **`route` label-widening.** Emitting extra labels for `labeled_value` entities whose label/value are non-adjacent — changes `entities_found`/`pi_categories` and widens LLM volume. | `README.md` *Deliberately excluded* | Belongs in its own change with measured before/after. |
| D3 | **`--filter-inventory` / `--filter-exclude-lanes`.** The local resume/rescan flags were removed once both stages ran in one pass. The scaled path keeps the equivalent via `enqueue.py --inventory`. | `README.md`; `enqueue.py` | Restore the local flags "on request." |
| D4 | **`--debug-log`.** Removed deliberately — it wrote full extracted text, OCR text, and LLM prompts/responses to a plaintext file, contradicting the no-PII guarantee. | `README.md` | Do **not** restore as-is; see F2. |

## E. Not-yet-ported / planned investigations

| ID | Item | Evidence | Notes |
|---|---|---|---|
| E1 | **Per-project layer not ported.** `projects.py`, `ingest.py`, `score_bde.py`, the `ingest`/`score` subcommands, and per-project settings. Genuinely orthogonal (the regex layer is project-agnostic); what it makes per-project is the injected protocol text plus a few numeric settings. | `README.md` *Not yet ported* | Port later at no extra cost. |
| E2 | **Partial-coverage OCR / scanned-BDE design.** 4 tests skip because they import `apply_scanned_bde`, `structured_subject_count`, `_first_n_pages_pdf` and expect a `meta["ocr_sample_text"]` contract that exists in no available version — a coherent spec (OCR a page sample of a large scan, keep it non-searchable, extrapolate a BDE count) that is **not built**. | `test_scanned_bde.py`, `test_bde_tier_distinct.py`, `test_pdf_open_bypass.py` (skipped); `README.md` *Tests* | **Open question:** does this code exist elsewhere, or was it never built? Relevant to OCR cost. |
| E3 | **Measure the value of embedded-image OCR.** Nothing has established how often text recovered from embedded images changes a routing decision — the bulk of DI spend on some corpora. | `README.md` *Controlling OCR cost* | Run a slice with `--no-image-ocr`, diff lanes; if the delta is small, tighten the qualifying-image threshold (a ~one-line change). |

## F. Technical debt & risks

| ID | Item | Evidence | Sev | Suggested action |
|---|---|---|---|---|
| F1 | **Coupling to scaling-lib private APIs.** Repo code calls underscore-prefixed / internal helpers: `enqueue.py` (`_build_message`, `_get_queue_service`, `_ensure_queues/_ensure_table`), `worker.py` (`_build_message`, `_get_queue_service`), `worker_status.py` (`_fetch_entities`, `_eta_string`, `_STATUS_DISPLAY`, `tui._progress_bar`), `hwe_scaled_store.py` (`status._get_table_client`, `_config._credential`). These can break on any scaling-lib upgrade. | those files | 🟠 | Pin scaling-lib (A2) and/or request public APIs for the helpers relied on. |
| F2 | **DEBUG logging can leak PII.** `runner.py` carries `logger.debug` calls that dump full extracted text and OCR text. No CLI path enables DEBUG, so a normal run logs nothing — but attaching a DEBUG handler to `pii_triage.*` writes that text wherever the handler points. | `runner.py`; `README.md` *Safety guarantees* | 🟠 | Keep DEBUG off in prod; consider redacting or gating those debug lines behind an explicit opt-in. |
| F3 | **Frozen-NR `capture` has no guardrail.** `check_nr_frozen.py capture` re-blesses the golden with no diff and no confirmation, so anyone can silence a real NR regression with it. | `pii_triage_merged/tools/check_nr_frozen.py::capture` | 🟠 | Require a reviewed PR for any golden re-capture; treat `capture` as a change, not a fix. |

## G. Compliance & operational actions

| ID | Item | Evidence | Sev | Action |
|---|---|---|---|---|
| G1 | **`extract-msg` (GPL-3.0) is baked into the worker image.** GPL obligations attach on *distribution* of the image; internal use does not. | `DEPENDENCIES.md` §1.1/§4 | 🟠 | Don't ship the image outside the org without a license review; isolate/replace `.msg` handling if external distribution becomes a requirement. |
| G2 | **Secret/token renewals aren't tracked in-repo.** Service-principal secret (`AZURE_CLIENT_SECRET`) and the build `GITHUB_TOKEN` PAT both expire. | `DEPENDENCIES.md` §5 | 🟡 | Keep `DEPENDENCIES.md` §5 current; review quarterly for approaching expiry. |

---

## Quick triage view

| Priority | Items |
|---|---|
| **Do soon** | A2 (pin scaling-lib), A3 (declare deps), F1 (private-API coupling), F3 (capture guardrail), F2 (DEBUG PII) |
| **Fix opportunistically** | A1 (NameError), C1–C4 (doc drift), B4 (image slimming), G1/G2 (compliance) |
| **Investigate / decide** | E2 (scanned-BDE — built or not?), E3 (embedded-image OCR value), B1 (UI metrics), B2 (Windows leg) |
| **Deferred (revisit on request)** | D1–D3 |
| **Won't restore as-is** | D4 (`--debug-log`) |

*This backlog is compiled from the code and existing docs, not a formal issue tracker. As items
are picked up, move them to your tracker (or annotate here with a status and owner).*
