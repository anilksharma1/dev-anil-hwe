# pii_triage 3.0.0

Read-only, parallel, crash-safe PII triage scanner for breach-notification review. Implements
the **HWE Bucketing & Tagging** stage and emits the two HWE deliverables (the per-file table for
searchable files, the sampled/extrapolated bucket table for non-searchable files).

It does **not** store PII values. Per file it reports whether the file is searchable and
programmatic, the entity *types* found (`Name | SSN | Address`), the protocol PI categories
present, an entity-count estimate and bucket, the BDE flag, and the routing lane.

**New in 3.0.0: one pass, two decision stages.** Previous practice was to run one build to strip
non-responsive documents, then run a second build over the survivors for the dataset overview —
which parsed and OCR'd every surviving file twice. 3.0.0 does the expensive work once and lets
both stages read the same text.

---

## Contents

- [The pipeline](#the-pipeline)
- [Install](#install) · [Pillow is not optional](#pillow-is-not-optional)
- [The workflow](#the-workflow)
- [Azure enrichment](#azure-enrichment-ocr--llm)
- [Controlling OCR cost](#controlling-ocr-cost)
- [Output: the inventory CSV](#output-the-inventory-csv)
- [The run manifest](#the-run-manifest)
- [Scoring a run](#scoring-a-run)
- [Command reference](#command-reference)
- [The frozen NR path](#the-frozen-nr-path)
- [Safety guarantees](#safety-guarantees)
- [Troubleshooting](#troubleshooting)
- [Tests](#tests)
- [What changed](#what-changed-from-2102--299)

---

## The pipeline

```
discover files
  └─ per file, in one worker:

     ┌───────────────── SHARED PASS — runs ONCE ──────────────────┐
     │  extract  →  apply_ocr  →  apply_image_ocr  →  detect      │
     │  (this is where the cost is, and where the saving is)      │
     └────────────────────────────────────────────────────────────┘

     ┌── STAGE 1 — the NR-removal decision ───────────────────────┐
     │  ambiguity gate → LLM (responsive y/n) → BDE person-count  │
     │  → lane                                                    │
     │  nr_stage1 = (lane == "likely_non_responsive")             │
     └────────────────────────────────────────────────────────────┘
                │                              │
        nr_stage1 = True              nr_stage1 = False
                │                              │
       stage 2 SKIPPED         ┌── STAGE 2 — the dataset overview ──┐
       s2_skip_reason=         │  graded LLM → one of five levels:  │
         "stage1_nr"           │  clear_yes | likely_yes |          │
                              │  borderline | likely_no | clear_no │
                              │  → s2_lane, s2_nr                  │
                              └────────────────────────────────────┘
                └──────── one CSV row, 49 columns ──────────┘
```

**Stage 1 owns NR removal. Stage 2 only describes what survived it.** That ordering is not
arbitrary: the two stages are given *different uncertainty policies*. Stage 1's prompt rounds
genuine uncertainty **up** to responsive. Stage 2's expresses it as `borderline`, and routing
then treats `borderline` as non-responsive. So **stage 2 clears on uncertainty where stage 1
flags** — which makes stage 1 the safe gate and stage 2 the informative summary, never the
reverse. Stage 2 is structurally incapable of changing a stage-1 answer.

---

## Install

```bash
pip install -r requirements.txt
# legacy .doc (optional): apt-get install antiword     (or catdoc)
```

No machine-learning libraries are needed for the core scan. Every parser is optional and
degrades gracefully — a missing library means affected files are recorded as `no_parser` or
`needs_conversion`, never silently skipped. `scan` prints which optional libraries are absent
at startup.

### Pillow is not optional

If you use `--ocr`, **install Pillow.** pypdf needs it to decode images embedded in PDFs, and
without it every one of those images is silently skipped: `apply_image_ocr` becomes a no-op and
OCR of content images inside text PDFs — normally the bulk of the OCR workload — simply doesn't
happen. No error, no warning in earlier builds.

This is not hypothetical. On the CNG corpus one build logged 24,223 embedded-image OCR calls and
another logged **zero** on the same files, because Pillow was missing and nothing said so. The
second run looked cheaper and was actually reading less text.

3.0.0 makes it visible three ways: Pillow is declared in `requirements.txt`, it appears in the
startup optional-dependency line, and `scan --ocr` prints an explicit warning when it's absent.
The `img_decode_failed` column counts images pypdf couldn't decode, so a broken install shows up
per file in the inventory.

> **Never compare a with-Pillow run against a without-Pillow one.** The DI counts differ by
> orders of magnitude and it will look like a regression.

---

## The workflow

```bash
# 1. scan — one pass, both stages
python -m pii_triage scan /corpus --out inventory.csv --workers 16 \
    --ocr --llm --bde-threshold 7 \
    --protocol "Cognicion_CIR_Review_Protocol.pdf"

# 2. HWE Table 1 — per-file, searchable
python -m pii_triage report inventory.csv --out table1_searchable.csv

# 3. HWE Table 2 — sample the non-searchable files, reviewers code them, extrapolate
python -m pii_triage sample inventory.csv --out sample.csv --rate 0.05
#    reviewers fill gold_responsive / gold_bde in sample.csv, then:
python -m pii_triage estimate inventory.csv sample.csv --out table2_nonsearchable.csv

# 4. score the run against the entities export
python tools/score_combined.py --inventory inventory.csv \
    --entities "07222026 re gk CNG_Entities Export.csv" --bde-threshold 6
```

**Resume** is automatic: rerun the same `scan` command to continue after a crash or Ctrl-C. The
output CSV *is* the progress record — a file is done iff it has a complete row. Any partial
trailing line from a hard crash is dropped via an atomic rewrite. `--restart` forces a fresh run.

**Stage 1 only.** `--no-stage2` reproduces 2.10.2's 27 legacy column *values* exactly
(verified: zero differences on a shared corpus). The CSV still carries all 49 columns — the
stage-2 block is blank with `s2_skip_reason = stage2_disabled`. Useful for re-establishing a
stage-1 baseline, or if you only need the NR gate.

---

## Azure enrichment (OCR + LLM)

Both tiers are opt-in and off by default. The rules pass makes **no network calls**.

**`--llm` enables TWO different calls.** This trips people up, so read the table twice.

*Call 1 — responsiveness.* Gated on the ambiguity classifier:

| Condition | Ambiguity | Responsiveness call? |
|---|---|---|
| A strong identifier (validated SSN) is present | `clear_responsive` | No — and the AI can never clear it |
| Nothing detected at all | `clear_non_responsive` | No |
| Signals present, no strong identifier | `ambiguous` | **Yes** |
| A structured file with rows to read | `ambiguous` | **Yes** |

*Call 2 — the BDE person-count.* A separate, count-only prompt that makes **no**
responsiveness judgment. It is **not gated on ambiguity**: it fires on any searchable file
that is structured, or whose candidate-entity estimate reaches `bde_count_min_entities`
(default 7). So a `clear_responsive` file that the responsiveness call never sees can still
incur an Azure call here.

Two consequences for reading the CSV:

- `llm_tokens` is the **sum of both calls**, so a row can show `llm_consulted = False` with
  `llm_tokens > 0`. `llm_consulted` tracks the responsiveness call only.
- The `detail` column records `bde_llm_count` (or `bde_llm_count_roster`) when call 2 ran.
  That is the marker to filter on if you need to attribute count-call spend.

`bde_person_count` is where call 2's answer lands. It never feeds the NR decision — by
design, so the BDE tier can be corrected without touching responsiveness.

`--ocr` sends non-searchable (image-only) files to Azure Document Intelligence
(`prebuilt-layout`, so scanned tables come back as tables and are row-counted), turning them into
text that flows through normal detection instead of the sampling lane. It also OCRs
content-sized images embedded in *text* PDFs and appends what it finds.

`--llm` consults Azure OpenAI on ambiguous files only. `--protocol <doc>` injects the matter
protocol as judgment context (PDF/DOCX/TXT). Failures degrade per file — the row gets an
`ocr_failed` / `llm_failed` note and falls back to the rules result. One bad call never stops
a run.

### Auth and configuration

No endpoint or credential is hardcoded — they come from environment variables or Key Vault,
with credentials from `DefaultAzureCredential`. (The one hardcoded value is a *deployment name*
fallback of `gpt-4.5-nano`, used only if neither `--llm-deployment` nor
`AZURE_OPENAI_DEPLOYMENT` is set.)

| Purpose | Env var | Key Vault secret |
|---|---|---|
| OCR endpoint | `AZURE_DOCUMENTINTELLIGENCE_ENDPOINT` | `AZURE-DOCUMENTINTELLIGENCE-ENDPOINT` |
| OpenAI endpoint | `AZURE_OPENAI_ENDPOINT` | `AZURE-OPENAI-ENDPOINT` |
| OpenAI deployment | `AZURE_OPENAI_DEPLOYMENT` (or `--llm-deployment`) | — |
| OpenAI API version | `AZURE_OPENAI_API_VERSION` (default `2024-10-21`) | — |
| Key Vault (optional) | `AZURE_KEY_VAULT_URL` | — |

`DefaultAzureCredential` picks up a managed identity, or
`AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET`.

**Settings via `.env`.** Drop a `.env` beside where you run `scan`, or point at one with
`--env-file PATH`. Plain `KEY=VALUE`, one per line; `#` comments on their own line; quotes
optional. A real shell variable wins over the file. The tool prints only *how many* settings
loaded, never their values.

```
AZURE_DOCUMENTINTELLIGENCE_ENDPOINT=https://your-di-resource.cognitiveservices.azure.com/
AZURE_OPENAI_ENDPOINT=https://your-openai-resource.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-4.5-nano
# only for a service principal — these ARE sensitive:
AZURE_TENANT_ID=...
AZURE_CLIENT_ID=...
AZURE_CLIENT_SECRET=...
```

**Concurrency.** Workers use multiprocessing, one file each, so in-flight Azure calls ≈
`--workers`. Size it to your deployment's requests-per-minute headroom. Every call also retries
with exponential backoff and jitter on throttling (429) and transient 5xx/timeout/connection
errors, under a hard wall-clock cap. That retry layer matters: a build without it saw a 77%
`RateLimitError` failure rate on a sample where this one saw 13 failures in 187,690 calls.

**Locked-down VMs.** If the VM blocks public PyPI you cannot `pip install` the Azure SDKs at run
time — have IT pre-install `azure-ai-documentintelligence`, `openai`, `azure-identity`, plus
`azure-keyvault-secrets` if you use Key Vault, and **`Pillow`**. If they're absent, `--ocr` /
`--llm` warn and the scan proceeds rules-only.

---

## Controlling OCR cost

OCR is billed per page by Document Intelligence, and there are two distinct paths:

| Path | Trigger | Cost shape |
|---|---|---|
| Full-file OCR | the file is image-only (a scan, or any image file) | pages in the document — capped by `--ocr-max-pages` **on PDFs only**; a multi-page TIFF is sent whole and billed for every page |
| **Embedded-image OCR** | a *text* PDF containing content-sized images | **one call per qualifying image** |

The second path is easy to underestimate. On the CNG corpus it was ~95% of all DI calls — 24,223
of ~25,473 — across 10,896 ordinary text PDFs, averaging 2.22 calls each. Qualifying images
already exclude logos (`width < 400`) and full-page scans (`area ≥ 550×550`).

```bash
--ocr-max-pages 0     # whole document (default is 15, inherited from 2.10.2)
--no-image-ocr        # skip embedded-image OCR entirely
```

**`--no-image-ocr` is the measurement tool.** Nothing has ever established how often the text
recovered from embedded images changes a routing decision. Run a slice with the flag and diff
the lanes against a normal run: that isolates what the path actually contributes. If it's a
handful of files, tightening the qualification threshold is a one-line change worth more than
any other optimisation here.

Every DI call is now counted per file (`di_calls`) and rolled up in the manifest, so **sum
`di_calls` over the CSV to get your billable call total** and reconcile it against the Azure
portal. `--price-per-1k-pages` makes the run print DI cost on stderr; the LLM cost range lands
in the manifest's `cost` block (and `tools/score_combined.py` prints both).

---

## Output: the inventory CSV

One row per file, 49 columns. **Columns 1–27 are unchanged from 2.10.2** in name, meaning and
order, so `report` and `benchmark` keep working — as does your existing external
`score_bde_by_count.py`, which reads `bde_person_count`, `estimated_entities`, `is_bde`,
`is_structured` and `suggested_lane` by name.

### The three answers

| Column | Meaning |
|---|---|
| **`nr_stage1`** | Stage 1 cleared this file as non-responsive. **This is the NR-removal decision.** |
| **`bde_stage1`** | Stage 1 called this a BDE (alias of `is_bde`, named so the owner is unambiguous). |
| **`s2_nr`** | Stage 2 called this non-responsive. **This is the R/NR-by-stage-2 decision.** |

### Stage 1 — the 27 legacy columns

`rel_path, file_name, ext, size_bytes, status, searchable, programmatic, text_extractable,
is_structured, page_or_sheet_count, attachment_count, estimated_entities, estimate_truncated,
bde_person_count, bde_confirmed, entity_bucket, entities_found, value_signal, pi_categories,
is_bde, complexity_bucket, ambiguity, llm_consulted, llm_responsive, llm_tokens, suggested_lane,
detail`

`ambiguity` / `llm_consulted` / `llm_responsive` make every **responsiveness** call traceable:
which files the AI was asked to judge, and what it said. They do *not* account for the BDE-count
call — that one leaves its mark in `detail` (`bde_llm_count`) and in `bde_person_count`, while
contributing to `llm_tokens`.

### OCR accounting

| Column | Meaning |
|---|---|
| `ocr_attempted` | a full-file DI call was made — **billable even if it failed** |
| `ocr` | …and it returned text |
| `ocr_pages` | pages DI reported for the full-file call |
| `img_ocr_qualifying` | embedded content images found |
| `img_ocr_calls` | of those, DI calls actually attempted (excludes sub-1KB pre-call skips) |
| `img_ocr_ok` | …that returned text |
| `img_decode_failed` | images pypdf couldn't decode — **almost always missing Pillow** |
| `di_calls` | `ocr_attempted + img_ocr_calls` — this file's billable DI calls |
| `elapsed_s` | wall-clock for this file, all stages |

`img_ocr_qualifying` vs `img_ocr_calls` matters when comparing against historical figures: older
builds recorded images *found*, which over-counts calls by the number of sub-1KB skips.

### Stage 2

| Column | Meaning |
|---|---|
| `s2_ran` | stage 2 executed |
| `s2_skip_reason` | `""` \| `stage1_nr` \| `not_searchable` \| `no_text` \| `stage2_disabled` \| `no_parser` \| `skipped_too_large` \| `timeout` \| `extract_error` \| `error` (worker died) |
| `s2_llm_consulted` | the graded call reached the model |
| `s2_llm_responsiveness` | `clear_yes` \| `likely_yes` \| `borderline` \| `likely_no` \| `clear_no` |
| `s2_llm_responsive` | `yes` / `no` — the level collapsed |
| `s2_llm_tokens` | tokens for the stage-2 call only |
| `s2_is_bde` | BDE at the stage-2 threshold (`--s2-bde-threshold`) |
| `s2_lane` | stage 2's own lane |
| `s2_nr` | `s2_lane == "likely_non_responsive"` |
| `s2_detail` | stage-2 notes, including `llm_failed:*` |
| `llm_tokens_total` | `llm_tokens + s2_llm_tokens` — the figure for the tracker |

`s2_skip_reason` is **never blank when `s2_ran` is false.** A blank would be indistinguishable
from a broken gate, so files that exit before the stages (unsupported extension, parse error,
timeout) say so explicitly.

### Lanes

`standard`, `bde`, `structured_bde`, `likely_non_responsive`, `structured_unreadable`,
`nonsearchable_sample`, `convert_lane`, `container_expand`, `needs_parser`, `manual_oversize`,
`review_error`.

Only the first three count as responsive for scoring; `likely_non_responsive` is cleared;
everything else is **undetermined** and excluded from metrics rather than guessed.

---

## The run manifest

`<out>.manifest.json` beside the CSV. Rebuilt correctly across resumes, so figures are complete
even after a restart.

```json
{
  "started_at": "...", "root": "...", "grand_total_files": 0, "completed": 0,
  "newly_scanned": 0, "resumed": false, "interrupted": false, "elapsed_s": 0,
  "status_counts": {}, "lane_counts": {}, "llm_stats": {},
  "ocr_stats":    { "di_calls": 0, "pages": 0, "img_qualifying": 0, "img_calls": 0,
                    "img_ok": 0, "img_decode_failed": 0,
                    "full_file_calls": 0, "full_file_ok": 0, "full_file_failed": 0,
                    "files_with_di": 0 },
  "stage2_stats": { "ran": 0, "consulted": 0, "tokens": 0, "responsive": 0, "nr": 0,
                    "level:clear_yes": 0, "skip:stage1_nr": 0 },
  "cost":         { "llm_tokens_total": 0, "llm_tokens_stage1": 0, "llm_tokens_stage2": 0,
                    "di_calls": 0, "di_pages_billable": 0 },
  "config": {}, "tool_version": "3.0.0", "finished_at": "..."
}
```

**These dicts are accumulators, so keys appear only when they have something to report.**
`full_file_calls` / `full_file_ok` / `full_file_failed` appear only if a full-file OCR call was
made; `files_with_di` only if some file made a DI call; the `level:` and `skip:` keys only for
levels and reasons that actually occurred. Don't write a parser that assumes a fixed key set.

In `cost`, the money keys are added **only when the matching price flag is supplied** —
`di_cost` with `--price-per-1k-pages`, `llm_cost_low` / `llm_cost_high` / `llm_cost_note` with
`--price-per-1k-in` / `--price-per-1k-out`, and `di_share_of_spend_pct` when both are given.
Nothing is priced at a guessed rate.

LLM cost is a **range**, not a point estimate: the API returns only `total_tokens`, so the
input/output split is unknown and inventing a ratio would be worse than reporting bounds.

---

## Scoring a run

```bash
python tools/score_combined.py --inventory inventory.csv \
    --entities "07222026 re gk CNG_Entities Export.csv" --bde-threshold 6 \
    --price-per-1k-in 0.10 --price-per-1k-out 0.40 --price-per-1k-pages 10.0
```

One scorecard: OCR cost, LLM cost, NR/R accuracy for stage 1 / stage 2 / both, BDE accuracy, and
per-file-type breakdowns. The entities export does double duty — responsive is
`Total Entities > 0`, BDE is `Total Entities > threshold`.

See **[`tools/SCORING.md`](tools/SCORING.md)** for the detail, especially the `--absent-means`
setting: whether a file missing from the export means "0 entities, non-responsive" or "never
reviewed, exclude". That choice inverts every NR figure, so the script auto-detects it and prints
which way it went.

`benchmark` remains available for scoring against a yes/no gold sheet:

```bash
python -m pii_triage benchmark inventory.csv my_results.xlsx
```

It auto-detects the id and responsive/BDE columns (`--id-col`, `--responsive-col`, `--bde-col`,
`--sheet`), matches by relative path or file name, scores the tool's actual routing lane, and
lists every disagreement by name — **misses first**, since those are the ones that matter.

---

## Command reference

### `scan`

| Flag | Default | Notes |
|---|---|---|
| `--out` | `inventory.csv` | also writes `<out>.manifest.json` |
| `--rulepack` | built-in | Master List JSON/YAML; see `rulepacks/` |
| `--workers` | CPU count | in-flight Azure calls ≈ this |
| `--bde-threshold` | 51 (or rulepack) | `is_bde` when the count **≥** this |
| `--ner` | off | spaCy names, *if* installed |
| `--ocr` / `--llm` | off | Azure enrichment |
| `--llm-deployment` | env | Azure OpenAI deployment name |
| `--protocol` | — | matter protocol doc, injected as LLM context |
| `--env-file` | `.env` | `KEY=VALUE` settings |
| `--no-stage2` | off | stage 1 only |
| `--stage2-on-all` | off | grade stage-1-cleared files too (ungated; extra LLM cost) |
| `--s2-bde-threshold` | = `--bde-threshold` | stage-2 BDE threshold |
| `--jurisdiction` | auto | `us` / `non-us`, applied in the stage-2 prompt |
| `--ocr-max-pages` | 15 | `0` = whole document |
| `--no-image-ocr` | off | skip embedded-image OCR |
| `--price-per-1k-in` / `-out` / `--price-per-1k-pages` | 0 | cost summary |
| `--timeout` | 60 | per-file extraction timeout, seconds |
| `--max-bytes` | 1 GiB | skip larger files |
| `--max-scan-chars` / `--max-scan-rows` | 5M / 200k | bounded work |
| `--chunksize` | 16 | pool batch size; inert while the watchdog path is active |
| `--progress-interval` | 0.5 | seconds between progress lines |
| `--restart` | off | discard progress and start fresh |

Env: `PII_WATCHDOG_S` overrides the per-file hard timeout (default **120s**, `0` disables).
Needs `--workers > 1`, since it works by killing and replacing a wedged worker.

### Others

| Command | Purpose |
|---|---|
| `report <inventory>` | HWE Table 1 — per-file, searchable only |
| `sample <inventory>` | draw a per-bucket sample of non-searchable files (`--rate`, `--seed`) |
| `estimate <inventory> <sample>` | HWE Table 2 — extrapolate from the coded sample |
| `benchmark <inventory> <gold>` | precision / recall / F1 vs your coding |

---

## Master List = the protocol

Detection is data-driven by a Master List of entity *definitions* — never values. The built-in
default covers the protocol PI-Type categories (Contact, Government ID, Birth, Financial
Account, Access Credentials, Health, Biometric, Family, Demographic, Student, Work) plus Name and
Address. Copy `rulepacks/default.yaml`, edit per matter, pass with `--rulepack`.

`rulepacks/cognicion-cir.yaml` is a client-protocol pack with 56 entities — the coarse default
categories decomposed to protocol leaf level (e.g. `HEALTH` → history / condition / treatment /
insurance policy / subscriber / claims), with a `weak:` convention so a bare name never
auto-flags.

### Name detection is structural — no name lists

Names are detected only from reliable structure: a title (`Dr. Jane Smith`), an explicit field
label (`Name:`, `Patient:`, `Employee:`), or a salutation (`Dear Jane`). There is deliberately
**no list of common first names** — that approach both misses real names and false-matches
companies and places. Bare capitalised word pairs are left for the LLM, which reads context. This
also matches the protocol: a bare name alone is not responsive. `--ner` adds spaCy
`en_core_web_sm` for higher recall if you bundle it.

---

## Formats

**Parsed:** `txt`/`log`/`text`, `csv`/`tsv`/`tab`, `docx`/`docm`, `pptx`/`pptm`, `xlsx`/`xlsm`,
`odt`/`odp`, `pdf`, `msg`, `eml`, `html`/`htm`/`xml`, `rtf`, legacy `doc` (via converter), `xls`.

**Flagged for prep:** images (`png`, `jpg`, `jpeg`, `tif`, `tiff`, `bmp`, `gif`) and image-PDFs →
OCR if `--ocr`, else non-searchable sampling; legacy `ppt`/`ods` → `convert_lane`;
`zip`/`pst`/`ost`/`nsf`/`mbox`/`7z`/`rar` → `container_expand`.

PDF handling is hardened: a 30-second watchdog on `pypdf` open (roughly 1% of scanned PDFs hang
there and would never reach OCR), a raw page-count fallback, an image-only early-exit probe, and
a `pdf_unreadable` path that records the failure class rather than losing the file.

---

## The frozen NR path

Clearing a document that contains real PII is the one error this tool cannot afford — nothing
downstream catches it. So the stage-1 decision path is pinned by hash:

```bash
python tools/check_nr_frozen.py check     # exit 0 clean, 1 on drift, 2 if the golden is missing
```

Five surfaces are frozen: `detection.py` and `routing.py` whole-file, `enrich.apply_llm` and
`azure_clients.llm_classify` at function scope, and the `_SYSTEM_PROMPT` constant by value.

**Run `check` after every change.** A clean run is the proof that an edit didn't touch NR
behaviour. Adding new code — as stage 2 does — does not drift it; *modifying* any of those five
does.

**Line endings do not count as drift.** The whole-file hash normalises CRLF/CR to LF, because
Python is newline-agnostic and a converted file is not a changed file. This mattered on the first
Windows run: the check reported DRIFT on *both* frozen files at once, and both hashes proved to be
the CRLF rendering of the unchanged source. If you see that on an older copy of the checker, it is
a false alarm — `tests/test_frozen_check.py` pins the behaviour, including that real one-token
edits and whitespace changes are still caught.

`capture` re-blesses the golden. Treat that as a reviewed action, not a fix: it overwrites with
no diff and no confirmation, so anyone can silence a real regression with it. The current golden
records why it was last re-captured and carries a `reviewer: PENDING` field awaiting sign-off.
`tools/nr_frozen_golden.v2_10_2_base.json` is the pre-merge baseline, kept for reference.

---

## Safety guarantees

Verified by tests, not just asserted:

- **Read-only.** No file is written, moved or modified in the corpus. No code execution.
- **No PII values in any output.** `entities_found` / `pi_categories` are labels only. The
  model's returned names and reasoning have **no column to land in**: stage 2's client drops
  `reasoning` before returning it, and for stage 1 the callers (`apply_llm`,
  `apply_bde_count`) simply never read it. `tests/test_integration.py::TestNoPiiInOutput` proves
  this end to end by planting sentinel values in a fixture corpus and asserting they appear
  nowhere in the produced CSV or manifest.
- **Logs are a different matter, so be deliberate.** `runner.py` carries `logger.debug` calls
  that dump full extracted text and OCR text. **No CLI path enables DEBUG**, so a normal run
  logs nothing sensitive — but if you attach a DEBUG handler to `pii_triage.*` yourself, that
  text lands wherever the handler points. `--debug-log`, which did exactly that, is not in this
  build.
- **Bounded work.** Per-file extraction timeout, hard watchdog, size cap, scan caps, zip-bomb
  ratio guard.
- **Fault isolation.** One unreadable file, failed OCR call or failed LLM call degrades that
  row and never stops the run.
- **Crash-safe resume.** The CSV is the progress record; partial trailing rows are repaired.
- **Deterministic offline rules.** The rules pass makes no network calls. Network is used only
  under `--ocr` / `--llm`, and the LLM runs at `temperature=0`.
- **Stage isolation.** Stage 2 cannot write any stage-1 column. Tested adversarially: stage 2
  is handed a response containing every stage-1 field name and garbage values, and all 27 legacy
  columns must be unchanged.

---

## Troubleshooting

**`0 Document Intelligence calls were made` with `--ocr` on.** Either nothing in the corpus was
image-only and no PDF carried qualifying content images, or Pillow is missing. Check the startup
dependency line and the `img_decode_failed` column.

**OCR volume suddenly jumped.** Someone installed Pillow. That is the correct number; the
earlier one was low because embedded-image OCR was silently disabled.

**`ERROR: 'inventory.csv' is open in another program`.** Excel holds a lock on Windows. Close it
and rerun — progress is preserved.

**A run stalls with steady CPU and no completions.** A parser is spinning on a malformed file.
The watchdog (default 120s) kills and replaces the worker and records the file as `timeout`. If
you disabled it with `PII_WATCHDOG_S=0`, re-enable it.

**`--llm` set but files show `llm_failed:RateLimitError`.** `--workers` exceeds your deployment's
RPM headroom. Lower it; retries with backoff are already in place.

**Stage 2 shows `skip:not_searchable` for many files.** Those files never yielded text, so there
is nothing to grade — check whether `--ocr` should be on.

**Scoring says the ID match rate is low.** The Control IDs and `file_name` aren't lining up. See
`tools/SCORING.md`; note that `os.path.splitext` is the wrong tool for IDs like
`Q00897.01-0000000003`.

---

## Tests

```bash
python -m pytest tests/ -q          # 273 passed, 4 skipped
```

`pytest.ini` and `conftest.py` are included. The suite is `unittest`-based with OCR and LLM
injected as fake callables, so it runs offline with no Azure and no corpus.

Notable suites: `test_stage_split.py` (the shared pass runs exactly once; stage 2 is gated and
isolated), `test_stage2_levels.py` (the five levels and the recall-safe defaults),
`test_ocr_accounting.py` (including the missing-Pillow case), `test_csv_contract.py` (the 49
columns pinned by name and order), `test_integration.py` (end to end over a real directory).

**The 4 skips are deliberate.** `test_scanned_bde.py`, `test_bde_tier_distinct.py` and two tests
in `test_pdf_open_bypass.py` import `apply_scanned_bde`, `structured_subject_count`,
`_first_n_pages_pdf` and expect a `meta["ocr_sample_text"]` contract. None of those exist in any
available version. They describe a coherent *partial-coverage OCR* design — OCR a page sample of
a large scan, keep the file non-searchable so the sample never reaches the NR decision, and
extrapolate a BDE count from sample plus page ratio. That is relevant to OCR cost but is not part
of this build, so the tests are skipped with reasons rather than deleted, which would lose the
specification. **Open question: does that code exist somewhere, or was it never built?**

---

## What changed from 2.10.2 / 2.9.9

**Merged in.** Single shared extract/OCR/detect pass; stage 2's graded responsiveness as new code
(`_S2_PROMPT_*`, `llm_classify_graded`, `get_stage2_fn`); the superset `benchmark.py` with NR
precision/recall/F1; `rulepacks/cognicion-cir.yaml`; `--jurisdiction`, now actually read
(`Config.jurisdiction` existed in both prior builds and neither stage-1 path consumed it).

**Fixed.**
- **The `scaling_lib` dependency is gone.** It was undeclared, absent from both requirements
  files, and its two guard probes made OCR and the LLM silently return `None` — so that build
  could never OCR or call the model regardless of Azure config. This build uses the
  self-contained client with its own retry, backoff and hard-timeout layer.
- **`llm_reasoning` removed.** The prior build wrote the model's reasoning to the CSV while its
  README promised no PII in output, and the prompt asks the model to state the value it found.
  It is now dropped inside the client, so it cannot reach the inventory.
- **OCR became measurable.** `apply_ocr` overwrites `text_extractable` from `image_only` to
  `text` on success, so that column counted OCR *failures*; and `apply_image_ocr` recorded
  nothing at all when its images yielded no text. Both are fixed and rolled up in the manifest.
- **Pillow declared and probed** (see above).
- **`s2_skip_reason` is always populated**, including on early-exit paths.
- **Test suite repaired.** It previously had 2 collection errors and 8 failures, six of which
  asserted v2.9 semantics against v2.10 code — a bare `pytest tests/` aborted on collection
  rather than reporting.

**Deliberately excluded.**
- **Table-aware detection.** `detect()` accepted a `tables` argument and never read it;
  `detect_labeled_value_cells()` had zero call sites; no rulepack declares that method. Dead
  code, removed along with the `pdfplumber` dependency and its per-PDF table pass.
- **The `route` label-widening** in the other build's `detection.py`. It emits extra labels for
  `labeled_value` entities whose label and value are non-adjacent, which changes
  `entities_found` / `pi_categories` for many files and widens LLM volume. A real idea, but it
  belongs in its own change with its own before/after.
- **`--filter-inventory` / `--filter-exclude-lanes`.** With both stages in one pass they are no
  longer the workflow. Say the word if you want them back as a resume path.
- **`--debug-log`.** It wrote full extracted text, OCR text and complete LLM prompts and
  responses to a plaintext file, which contradicts the no-PII guarantee.

**Not yet ported.** The per-project layer — `projects.py`, `ingest.py`, `score_bde.py`, the
`ingest` and `score` subcommands, and the per-project settings. It is genuinely orthogonal
(`detection.py`, `routing.py`, `enrich.py` and `runner.py` are byte-identical between the two
Anna builds), so porting it later costs nothing extra. Note that what it makes per-project is the
**protocol text injected into the prompts** plus a few numeric settings — not the regex layer,
which remains project-agnostic.

See **[`MERGE_NOTES.md`](MERGE_NOTES.md)** for the full rationale, the verification evidence, and
the decisions taken.
