# pii_triage — Demo Day Deck (content spec)

Paste this into Claude chat and ask it to design a polished slide deck. It's the full copy +
speaker notes; the visual design is up to you.

**Context for the designer:** `pii_triage` v3.0.0 is the "HWE Bucketing & Tagging" stage of a legal-discovery
pipeline. It scans a corpus of files for PII, routes each file to a lane, and never stores PII values.
Audience is **mixed** (stakeholders + engineers). ~11 slides, ~10 minutes, and it **ends by handing off to a
live demo of the operator UI**. Suggested accent colors: blue `#2a78d6` (stage-1 / Document Intelligence),
orange `#eb6834` (stage-2 / LLM), dark `#232a31` for title/section slides. Keep it clean and confident.

---

## Slide 1 — Title
- **pii_triage**
- Subtitle: *Scan a corpus for PII. Route every file. Never store a value.*
- Tags: v3.0.0 · local CLI + Azure fleet · live UI demo to follow →
- **Speaker notes:** Welcome. This is pii_triage — the Bucketing & Tagging stage of our HWE pipeline. In ~10 minutes I'll cover the problem, how it works, how it scales, and how we prove it's accurate. Then a live demo of the operator UI. One sentence to anchor everything: we scan a corpus for PII, route every file to the right lane, and never store a single PII value.

## Slide 2 — The problem
- Headline: **A matter arrives with hundreds of thousands of files.**
- Lead: Someone has to decide, for *every* file: does it contain personal information, how much, and where should it go?
- Three points:
  - **Too big to read** — 100k+ files, every format (email, Office, PDF, images, archives)
  - **Manual review is slow** — costs $$ and weeks, and it's inconsistent between reviewers
  - **The risk is asymmetric** — a *missed responsive file* is the failure mode that matters
- Closer: **pii_triage does the first pass automatically** — so humans only review what actually needs judgement.
- **Speaker notes:** A legal matter lands with a huge corpus. Every file needs a decision — is there PII, how much, where does it route. By hand that's slow, expensive, inconsistent. And the costliest mistake isn't over-flagging — it's *missing* a responsive file. pii_triage automates the first pass so reviewers only spend time on the ambiguous middle.

## Slide 3 — What it produces (+ the guarantee)
- Headline: **One inventory row per file — counts and a label, nothing else.**
- Two outputs:
  - **Table 1 — searchable files:** per-file detail (entity-type counts + routing lane), ready for downstream
  - **Table 2 — non-searchable:** sample → review → extrapolate (stratified 5% sample, humans code it, project to the population)
- **Guarantee callout (make this prominent):** 🔒 *No PII value is ever stored, logged, or output.* Detection counts occurrences into a local set that is **discarded on return**. Only entity **type counts** and a **routing label** ever leave the process.
- **Speaker notes:** The output is deliberately boring: one row per file, holding entity-type counts and a routing label. That feeds Table 1 (searchable) and Table 2 (non-searchable, which we sample, have humans review, and extrapolate). The guarantee that shapes every decision: we never store a PII value — we count into a set that's thrown away on return. Only counts and labels survive. That's what lets us run it anywhere without creating new data-exposure risk.

## Slide 4 — How it works: the pipeline
- Headline: **Every file runs one decision tree.**
- Flow (render as a clean diagram/flowchart):
  1. `file too big` → **manual_oversize** (exit)
  2. **Extract** text + metadata (read-only, 25+ formats) → unsupported/error/container/legacy each exit to their own lane (needs_parser · review_error · container_expand · convert_lane)
  3. `image-only?` → +OCR runs Azure **Document Intelligence**; no OCR → **nonsearchable_sample** (exit)
  4. **Detect** entities by type *(values discarded)*
  5. **Classify ambiguity:** no signals → **likely_non_responsive**; strong id (SSN) → skip LLM; ambiguous → **Azure OpenAI** grades responsiveness
  6. **Route** to a final lane: spreadsheet ≥ threshold → **structured_bde**; entities ≥ threshold → **bde**; else → **standard**
- Caption: The LLM is consulted *only* on genuinely ambiguous files — never when the answer is already clear. 10 lanes total.
- **Speaker notes:** The whole engine on one slide. Every file walks the same tree. We short-circuit early — too big, unsupported, an archive, a legacy format each get a lane and stop. Images with OCR on run Azure Document Intelligence; with OCR off they go to the non-searchable sample. We detect entities and immediately discard the values. Then the key move: classify *ambiguity*. No signals = non-responsive. A strong identifier like an SSN = clearly responsive, skip the LLM. Only the ambiguous middle goes to Azure OpenAI. Then route to a final lane. Cheap deterministic rules handle clear cases; the expensive model only runs where it earns its keep.

## Slide 5 — Detection & routing
- Two columns.
- **Left — 8 detection methods:** regex (email, phone, card +Luhn); ssn (validated); name (heuristic / NER); address (street / city-state-zip); labeled_value (passport, license, DOB); keyword (health, biometric); money (currency amounts). Caption: against an 11-category Master List (rulepack) — definitions, never values.
- **Right — 10 routing lanes** (show as chips): standard, bde, structured_bde, likely_non_responsive, nonsearchable_sample, container_expand, convert_lane, needs_parser, manual_oversize, review_error. Plus a callout: **BDE = "big data entity"** — files with a lot of PII (default ≥ 51 entities); threshold is configurable per run and per score.
- **Speaker notes:** Two pieces of vocabulary for later. Left: eight detection methods — regex for emails/cards with a Luhn check, validated SSN, name and address detection, labeled values like passports, keyword matching for health/biometric, and money. All driven by a configurable Master List rulepack that holds definitions, not values. Right: the ten lanes. The responsive searchable outcomes are standard / bde / structured_bde. "BDE" — big data entity — is just a file with a lot of PII, default threshold 51, tunable. Keep "responsive," "BDE," and "lanes" in mind — they show up in the demo.

## Slide 6 — Scale
- Headline: **Same engine, two ways to run it.**
- Two cards:
  - **Local — one machine:** CLI, multiprocessing. `python -m pii_triage scan /corpus …`. The output CSV *is* the progress record — crash-safe resume, idempotent.
  - **Scaled — Azure fleet:** Container Apps + Storage Queue. Linux workers poll one queue, one file each, autoscaling with queue depth and multi-threaded per replica. Legacy Office (.doc/.xls/.ppt) converts inline via LibreOffice — no separate Windows worker.
- Callout: Built on **scaling-lib** — polling, retries, rate-limited AI clients, dead-lettering, status table, autoscaling. A matter is `<job_dir>/files/` + a sibling `protocol.pdf` the workers pick up automatically.
- **Speaker notes:** Same code, two ways. Locally it's a multiprocess CLI, and the neat trick is the output CSV *is* the progress record — crash, re-run, it skips finished rows. At scale it's a fleet of Docker workers on Azure Container Apps, each pulling one file off a queue, autoscaling with the backlog and multi-threading within each replica sized to its own CPU allocation. Legacy Office files convert inline via LibreOffice headless on the same fleet — no dedicated Windows worker any more. All the plumbing — retries, rate limiting, dead-lettering, status — comes from scaling-lib. Each matter is a folder of files plus an optional protocol doc the workers read automatically.

## Slide 7 — Why you can trust the numbers
- Headline: **Integrity is designed in, not asserted.**
- Four cards:
  - **Deterministic rules pass** — a file decided *without* a model call produces a byte-identical row every run. The Compare screen proves it.
  - **"Not measured" ≠ zero** — an unrecorded value renders as a dash, never a fake zero.
  - **Read-only, everywhere** — no file is ever modified; no network unless OCR/LLM is explicitly on.
  - **Archive before destroy** — clearing a run archives every job's rows and re-reads them to verify, or it refuses.
- Footer: Tests: **111** on the core library · **46** on the UI · store flows validated end-to-end against a local Azurite stack.
- **Speaker notes:** The slide I care most about for a mixed room. Trust is built in, not claimed. The rules-based path is fully deterministic — a file that didn't need the model produces a byte-identical row every run, and the Compare screen diffs two runs to prove no rules-decided row moved. We distinguish "not measured" from zero — a dash, never a misleading zero. The whole thing is read-only and makes no network calls unless you turn on OCR or the LLM. And the destructive reset archives and verifies before clearing. Backed by 111 core tests, 46 UI tests, and end-to-end validation against a local Azure emulator.

## Slide 8 — Accuracy / scoring
- Headline: **We score every run against human review.**
- Lead: The **vs manual review** step compares the pipeline's decisions to a reviewer's entity export and produces a full scorecard.
- Three metrics:
  - **Missed notifications** (drive to zero, **target < 5%**) — % of *cleared* files that were actually responsive. The number that matters.
  - **Over-flagged** (**target < 50%**) — % of *flagged* files that were actually non-responsive; extra review cost.
  - **Recall · Precision · F1** — plus cost, timing, and per-entity-type breakdowns, all in one workbook.
- Caption: "Read the misses first." The scorecard leads with the responsive files the pipeline cleared — the only error class that can hurt.
- **Speaker notes:** Does it work? We don't guess. Every run is scored against a reviewer's export. The scorecard leads with the number that matters — missed notifications, files we cleared that a human said were responsive. That's the asymmetric risk from slide 2; we drive it toward zero, target under 5%. We also track over-flagging (just wasted review cost) and standard recall/precision/F1, cost, and timing. The philosophy is "read the misses first" — surface the dangerous errors before anything else.

## Slide 9 — The operator console
- Headline: **One localhost UI drives the whole scaled run.**
- Eight screens (grid): **Setup** (is the machine + store configured?) · **New run** (validate a corpus → submit) · **Runs** (history + archive & reset) · **Monitor** (live progress from the store) · **Results** (lanes, cost, entity types) · **vs manual review** (the scorecard) · **Compare runs** (prove nothing drifted) · **Build & deploy** (readiness checks).
- Callout: Every screen is a **view over artefacts the pipeline already writes**. The UI builds a command, shows it before it runs, and reads the results back — it never holds authoritative state, and its numbers **can't disagree** with the CLI. No PII values, ever — only labels and counts.
- **Speaker notes:** This brings us to the UI I'm about to demo. A single localhost console — no install, pure standard library — that drives a whole scaled run through eight screens: set up and validate, submit, monitor live, see results, score against manual review, compare runs, check build readiness. Same principle as the pipeline: every screen is a *view* over artefacts the pipeline already writes. It builds a command, shows it to you before running, reads results back. It never invents a number, so it can't disagree with the CLI — and it only shows labels and counts, never PII.

## Slide 10 — Demo handoff (section slide, dark background)
- **Let's open the UI.**
- Demo path:
  1. **Setup** — the machine & store are green
  2. **New run** — validate a corpus, see the exact command
  3. **Monitor** — watch files complete, live
  4. **Results** — lanes, cost, entity types
  5. **vs manual review** — the scorecard
- Footer: `./HWE_Scaled.sh` → http://127.0.0.1:<port>/
- **Speaker notes:** Now the real thing. I'll launch the console and walk the operator's path: Setup to confirm green, New run to validate a corpus and show the command, Monitor to watch it live, Results to see where files landed and cost, and the scorecard against manual review. [Switch to the UI now.]

## Slide 11 — Recap
- Headline: **What pii_triage gives you.**
- Five bullets:
  - **An automatic first pass** over a whole corpus — humans review only the ambiguous middle.
  - **A hard privacy guarantee** — counts and labels leave the system; PII values never do.
  - **Scales from a laptop to a 21-worker Azure fleet** — same engine, crash-safe, autoscaling.
  - **Provable accuracy** — scored against human review, misses surfaced first, runs compared for drift.
  - **One console** to run, watch, score, and compare — a view over what the pipeline already writes.
- Footer: Docs in the repo — PROJECT_OVERVIEW.md · UI_OVERVIEW.md · RUNBOOK.md · CLI_ONLY.md
- End with: **Questions?**
- **Speaker notes:** To bring it home — five things. Automatic first pass so humans only touch the hard cases. A hard privacy guarantee: values never leave. Scales from a laptop to a full Azure fleet on the same code. Accuracy is provable, not asserted — scored against human review with dangerous misses first. All driven from one honest console. Everything's documented in the repo. Happy to take questions — thanks.
