# Testing Results & Validation Log

Testing and validation results per initiative, mapped to code version, with the **hypothesis** each
test/experiment was probing and the **learning** that came out of it.

> **Provenance & honesty note.** This log *consolidates* the project's own recorded validation — the
> README's *What changed from 2.10.2 / 2.9.9* section, [`SCALED_UI_FINDINGS.md`](SCALED_UI_FINDINGS.md)
> (Phase 0, dated **2026-08-17**), and the automated test suites. Specific field metrics (e.g. the
> DI-call and rate-limit figures) are **quoted from those records**, from real runs on the CNG
> corpus — they were not re-measured while writing this doc, and the suite was not re-executed here
> (its optional deps aren't installed in this environment). Reproduce with the commands in each
> section.

## Version context

| Thing | Value |
|---|---|
| Tool version | **`pii_triage` 3.0.0** (`pii_triage/__init__.py`, stamped into every manifest as `tool_version`) |
| Baselines compared against | 2.10.2 (immediate predecessor) and 2.9.9 |
| Repo branch for the scaled layer | `combined-scaling` (the worker fleet + UI track the same 3.0.0 engine; they carry no separate package version) |
| Library test result (current) | **273 passed, 4 skipped** across 16 modules (per the library README). *CLAUDE.md's "111 tests" and the Phase-0 findings' "111" are the pre-merge count — superseded.* |
| UI test result | **46 tests** (`test_hwe_scaled_ui.py`) |
| NR gate | `tools/check_nr_frozen.py check` — exit 0 clean, 1 on drift |

**Reproduce**

```bash
cd pii_triage_merged && python -m pytest tests/ -q      # library engine
python -m pytest test_hwe_scaled_ui.py -q               # operator UI (repo root)
cd pii_triage_merged && python tools/check_nr_frozen.py check   # frozen NR gate
```

---

## Initiative 1 — Core Triage Engine

**Code version:** `pii_triage` 3.0.0 · **Suites:** `test_triage`, `test_money`,
`test_roster_extraction`, `test_roster_bde`, `test_router_bde`, `test_edge_cases`,
`test_csv_contract`, `test_scan_early_exit` · **Result:** passing.

| Hypothesis | Test / evidence | Result | Learning |
|---|---|---|---|
| Luhn-valid numbers alone over-flag invoice/order IDs as payment cards | `test_triage` card cases; `card_valid = luhn_valid AND _card_iin_ok` | ✅ | Add an issuer-prefix/length (IIN) check on top of Luhn — Luhn alone is not sufficient signal |
| A bare name is not responsive (per protocol) | `value_signal` cases in `test_triage`/`test_edge_cases` | ✅ | `NAME` alone → not a value signal; `NAME` + any other signal → responsive |
| A structured roster that parses badly (0 entities) could be silently cleared | `test_roster_*`, `structured_unreadable` routing | ✅ | New lane `structured_unreadable` + `roster_entity_estimate` guard files >6 KB with 0 entities rather than clearing them |
| The inventory column set must stay stable for downstream tools | `test_csv_contract` pins `FIELDNAMES` by **name and order**; legacy 27 frozen | ✅ | The CSV is a contract; the 27 legacy columns are order-pinned so `report`/`benchmark`/external scorers keep working |
| Table-aware detection & `route` label-widening add value | dead-code review — no call sites, no rulepack declared the method | Removed | Both were unreachable; removed (with `pdfplumber`). Real ideas, but deferred to their own change (see `BACKLOG.md` D1/D2) |

---

## Initiative 2 — Enrichment (OCR + two-stage LLM)

**Code version:** `pii_triage` 3.0.0 · **Suites:** `test_stage_split`, `test_stage2_levels`,
`test_ocr_accounting` · **Result:** passing (incl. the missing-Pillow case).

| Hypothesis | Test / evidence | Result | Learning |
|---|---|---|---|
| A missing **Pillow** silently disables embedded-image OCR | Two builds on the *identical* CNG corpus: **24,223** embedded-image DI calls vs **0** | Confirmed | Declare Pillow in `requirements.txt`, surface it in the startup dependency line, and add the `img_decode_failed` column so a broken install shows up per file. **Never compare a with-Pillow run to a without-Pillow one.** |
| The `text_extractable` column measured OCR *success* | `apply_ocr` overwrote `image_only`→`text`, so the column actually counted OCR *failures* | Bug found & fixed | Fixed and rolled into the manifest; `test_ocr_accounting` pins the accounting |
| The shared extract/OCR/detect pass must run **exactly once** | `test_stage_split` | ✅ | The 3.0.0 saving is real — both stages read one extracted `text`; no double-parse |
| Stage 2 must be structurally unable to overturn stage 1 | Adversarial test: stage 2 handed a response with **every stage-1 field name** + garbage values; all 27 legacy columns must be unchanged | ✅ | Stage isolation holds — stage 2 may write only `s2_*` columns |
| Different uncertainty policies make stage 1 the safe gate | `test_stage2_levels` — five levels + recall-safe defaults | ✅ | Stage 1 rounds uncertainty **up**; stage 2 expresses it as `borderline` → routed NR. Stage 2 clears exactly where stage 1 flags |
| Without a retry layer, rate limiting dominates at scale | **77% `RateLimitError`** failure rate without retry vs **13 failures in 187,690 calls** with backoff/jitter | Confirmed | Keep the retry/backoff/hard-timeout layer (in `scaling_lib.ai`); size `--workers` to deployment RPM |
| Model reasoning could leak a PII value into the CSV | `reasoning` dropped inside the stage-2 client; stage-1 callers never read it; `test_integration` sentinel test | ✅ | The model's prose has no column to land in — verified end-to-end |

---

## Initiative 3 — Local CLI & Runner

**Code version:** `pii_triage` 3.0.0 · **Suites:** `test_integration`, `test_scan_early_exit`,
`test_frozen_check` · **Result:** passing.

| Hypothesis | Test / evidence | Result | Learning |
|---|---|---|---|
| The stage split changed no stage-1 output vs 2.10.2 | `--no-stage2` reproduces 2.10.2's 27 legacy column **values** — **verified zero differences on a shared corpus** | ✅ | Adding stage 2 is additive; the NR gate is byte-stable against the predecessor |
| A hard crash mid-write leaves a partial trailing row that must not be trusted | resume drops any row with a `None` field via an atomic rewrite (`_load_done_and_repair`) | ✅ | The CSV *is* the progress record; resume is idempotent |
| A malformed file could wedge a worker and stall the run | watchdog kills+replaces the worker, records `timeout`; auto-pause at >5% rolling failure | ✅ | Bounded work: a single bad file times out rather than blocking the queue |
| No output should ever contain a PII value | `test_integration::TestNoPiiInOutput` plants sentinel values, asserts they appear in neither the CSV nor the manifest | ✅ | The no-PII guarantee is tested, not just asserted |
| The stage-1 NR decision surface must not drift | `test_frozen_check` pins the checker (CRLF-tolerant; real one-token edits still caught); `check_nr_frozen.py check` | ✅ | Adding code (stage 2) doesn't drift the gate; modifying the five frozen surfaces does |
| The pre-merge suite was trustworthy | It had **2 collection errors + 8 failures**, 6 of which asserted v2.9 semantics against v2.10 code | Was broken → repaired | Fixed to 273 passed / 4 skipped; a bare `pytest` now runs instead of aborting on collection |

---

## Initiative 4 — Scaled Distributed Platform

**Code version:** `combined-scaling` branch (engine 3.0.0) · **Validation:** operational (the CNG
fleet run) + Phase-0 findings. *No automated fleet-integration test is in the pytest suite — the
queue/worker path is exercised operationally.*

| Hypothesis | Test / evidence | Result | Learning |
|---|---|---|---|
| All files in one submission should share one `job_id` | `enqueue.py` builds queue messages directly, bypassing `scaling_lib.enqueue()` (which mints a random per-file id) | ✅ | One batch shows in `scaling-lib status`; the trade-off is coupling to scaling-lib internals (`BACKLOG.md` F1) |
| Legacy Office files can be converted on Windows and finished on Linux without losing identity | `.orig.json` sidecar + `converted_from`; forwarded to the Linux queue under the same `job_id` | Partial | Works — **but** `converted_from` silently doesn't reach the inventory; the two-pass stable-id is only half-solved (Findings §6/§8, open in `BACKLOG.md`) |
| `run_metrics()` / `scaling-lib status` give per-run history | Findings §8: they only ever see the **latest** job | Disproven | The store must query the Table **by `PartitionKey`** — this shaped `hwe_scaled_store.py` |
| Rising dead-letters should back-pressure the fleet | `_dlq_monitor` daemon `os._exit(1)` above `DLQ_FAILURE_RATE` | ✅ | Intentional recycle, not a crash; tune via `DLQ_*` env knobs |
| "Docker running" is a deploy prerequisite | Findings §8.1: `acr-build` runs in ACR's cloud via `az acr build` | Disproven | The preflight checks **`az` + login**, not Docker |

---

## Initiative 5 — Operator UI (HWE Scaled)

**Code version:** `combined-scaling` branch; Phase-0 findings **2026-08-17** · **Suite:**
`test_hwe_scaled_ui.py` (**46 tests**) · **Result:** passing.

| Hypothesis | Test / evidence | Result | Learning |
|---|---|---|---|
| A rules-decided row must be byte-identical between two runs | `compare_rules_decided` + the §6.7 invariant (`_DECISION_COLS` must match; a diff is a `moved` finding) | ✅ | Only model/timing columns may differ; the deterministic detector must not — that's the Compare contract |
| UI form fields could smuggle unvalidated flags into a command | `build_enqueue_argv` takes **explicit params only**; asserted by a test | ✅ | The UI can't inject arbitrary argv; every action shows the exact command first |
| "Not measured" must be distinguishable from a real zero | `measured()` helper; tested | ✅ | Never render a `0` the pipeline didn't actually record; render `None` as "—" |
| Reset is safe to trigger from the UI | `archive_and_reset` is table-wide → archives & **verifies** every co-resident job, requires typing the exact `job_id` | ✅ | Reset is destructive and cross-job; heavily gated, audit-logged, one path only |
| The original brief's assumptions were correct | Findings §8 corrected several: Docker prereq, a "Projects" screen, a graceful cancel, per-run `run_metrics` — none existed | Disproven | Build to what the code/Table actually expose; decisions locked 2026-08-17 (one run at a time, copy-only staging, infer Windows liveness, guarded build/deploy) |

---

## Initiative 6 — Reporting / Sampling / Scoring

**Code version:** `pii_triage` 3.0.0 · **Validation:** unit coverage (bucketing/extrapolation in the
engine suites) + `tools/score_combined.py` run against the CNG entities export.

| Hypothesis | Test / evidence | Result | Learning |
|---|---|---|---|
| One entities export can serve as both responsive and BDE ground truth | `score_combined`: responsive ⇔ `count > 0`, BDE ⇔ `count > threshold` | ✅ | One export does double duty; no separate BDE sheet needed |
| Score's `>` and the pipeline's `≥` will silently disagree | the UI passes `bde_threshold - 1` to `build_score_argv` | Reconciled | An off-by-one that must be handled explicitly, not assumed away |
| Benchmark should score the tool's **actual lane** (reflecting any AI override), misses first | `_pred_from_lane`; misses-first report | ✅ | A missed responsive = a missed notification = the critical failure mode; surface those first |
| Whether an absent file means "0 entities" or "unreviewed" inverts every NR figure | `--absent-means {auto,zero,unreviewed}`; auto-detects and prints which way it went | ✅ | The absent-means choice must be explicit and reported, or NR accuracy is meaningless |
| Non-searchable sampling must be reproducible | `draw_sample(rate=0.05, seed=12345)`, stratified by complexity bucket | ✅ | Deterministic given the seed; extrapolation is per-bucket |

---

## Cross-cutting result — the 4 deliberate skips

`test_scanned_bde.py`, `test_bde_tier_distinct.py`, and two in `test_pdf_open_bypass.py` **skip**
because they import `apply_scanned_bde`, `structured_subject_count`, `_first_n_pages_pdf` and expect
a `meta["ocr_sample_text"]` contract that exists in no available version.

- **Result:** 4 skipped (not failed) — kept with reasons rather than deleted, so the *specification*
  isn't lost.
- **Learning / open question:** they describe a coherent **partial-coverage OCR / scanned-BDE**
  design (OCR a page sample of a large scan, keep it non-searchable, extrapolate a BDE count) that is
  **not built** in this version. Does the code exist elsewhere, or was it never written? Tracked in
  [`BACKLOG.md`](BACKLOG.md) E2.

---

*Keep this log current: when a hypothesis is newly tested or a result changes, add a row with the
code version and the learning. Pair with [`BACKLOG.md`](BACKLOG.md) for what's still open.*
