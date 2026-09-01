# Benchmarks & Comparison Results

What ground truth **pii_triage** is benchmarked against, how the comparison is measured, and how
the tool's output actually compares — with real numbers from the committed CNG scorecards.

> **Provenance & method.** The figures below come from the scorecards under `outputs/*/` — real
> runs on the **CNG corpus** (2,000-file samples), produced by the team on 2026-08-10 … 2026-08-19
> and scored with `tools/score_combined.py`. The scorecard `.xlsx` files store the confusion
> matrix (TP/FP/FN/TN) as literal values but the derived metrics as spreadsheet formulas with no
> cached result, so **recall / precision / F1 / accuracy here were recomputed directly from each
> scorecard's confusion counts** (uniform method across runs). `outputs/scaling-1/scorecard_summary.csv`
> is an alternate scoring pass of the same run and may differ by a point or two.
>
> **Reproduce:** `python pii_triage_merged/tools/score_combined.py --inventory <inv> --entities "<CNG Entities Export>.csv" --bde-threshold 6`

---

## 1. Benchmarks (ground truth) used

| Benchmark | What it provides | Used by |
|---|---|---|
| **CNG Entities Export** (per-file `Control ID` → `Total Entities`) | The primary ground truth: **responsive ⇔ count > 0**, **BDE ⇔ count > threshold** | `tools/score_combined.py --entities` |
| **Reviewer gold sheet** (xlsx/csv, yes/no) | A yes/no responsiveness benchmark | `python -m pii_triage benchmark` |
| **Manual-review coded sample** (`gold_responsive`/`gold_bde`) | Truth for the non-searchable population (Table 2) | `python -m pii_triage estimate` |
| **Prior tool versions** (2.10.2 / 2.9.9) | Regression baseline for build-to-build comparison | diff of legacy columns; see §5 |

One export does double duty (responsive **and** BDE truth). Matching is by `Control ID` reconciled
against the inventory's `file_name` / `rel_path` (minus extension).

## 2. How the comparison is measured

- **Metrics** (positive class = *responsive*): Recall, Precision, F1, Accuracy, and **NR-error**
  = `FN/(FN+TN)` — the *inaccurate-clear rate*, the metric that matters most (a wrongly-cleared
  responsive file = a missed notification). The scorecard sets a **target of < 5%**.
- **Four views** of the same run: **Stage 1** (the NR gate, all scored files), **Stage 2** (graded,
  on survivors only), **Pipeline** (S1 clears → gone; else S2's call — *what a reviewer receives*),
  **Union** (recall ceiling — shows the cost of gating).
- **BDE accuracy** is scored several ways (the `is_bde` flag/lane, the effective count, the
  lane-OR-`bde_person_count` recovery, and stage-2's `s2_is_bde`), plus a count-accuracy pass.
- **`--absent-means`** decides how files absent from the export are treated (`zero` = non-responsive
  vs `unreviewed` = excluded); it inverts every NR figure, so the scorer auto-detects and prints
  which way it went. All runs below resolved to **`auto → unreviewed`** (the export contains
  zero-count rows), i.e. only files present in the export are scored (~1,960 of 2,000).
- Output lists **misses first** (coded-responsive files the tool cleared).

## 3. Worked example — `scaling-1` (CNG 2k sample, truth: entities > 0)

**NR / R accuracy** (recomputed from the scorecard's confusion matrix):

| View | N | Recall | Precision | F1 | Accuracy | NR-error (target <5%) |
|---|---:|---:|---:|---:|---:|---:|
| **Stage 1** (all scored) | 1,964 | **0.887** | 0.433 | 0.582 | 0.694 | **5.3%** |
| **Stage 2** (graded, 977) | 977 | 0.864 | 0.707 | 0.778 | 0.788 | 12.3% |
| **Pipeline** (what a reviewer gets) | 1,964 | 0.766 | 0.706 | **0.735** | 0.868 | 7.6% |
| **Union** (recall ceiling) | 1,975 | 0.888 | 0.433 | 0.582 | 0.696 | 5.3% |

**Reading it:** Stage 1 is the deliberately recall-safe gate — recall **0.887**, NR-error **5.3%**
(at target), precision only 0.43 (it over-flags on purpose). Stage 2 raises precision on the
survivors (0.43 → **0.71**). The pipeline balances the two (F1 **0.735**, accuracy 0.87). This is
exactly the intended shape: catch nearly everything at the gate, then describe/refine.

**BDE accuracy** (truth: entities > 6; recomputed from confusion counts):

| Definition | Recall | Precision | F1 | Accuracy |
|---|---:|---:|---:|---:|
| `is_bde` flag / BDE lane | 0.595 | 0.618 | 0.606 | 0.939 |
| effective count > 6 | 0.835 | 0.238 | 0.371 | 0.776 |
| **lane OR `bde_person_count` > 6** (recovery) | **0.848** | 0.513 | **0.641** | 0.925 |
| stage-2 `s2_is_bde` | 0.582 | 0.453 | 0.510 | 0.912 |

**Finding:** the LLM person-count *recovers* BDEs the flag alone misses — recall jumps 0.595 →
**0.848** with the `bde_person_count` recovery path, at a modest precision cost. Count accuracy on
the 158 true BDEs: the raw estimate ≥ 7 catches **138 (87.3%)**; **median |tool − human| = 23**
(mean is skewed to 202 by a few huge rosters); the 26 still-missed are mostly PDFs (18) and Excel (7).
Note the **off-by-one**: truth is `> 6` (≥ 7) but the scanner flags at `≥ bde_threshold` — run with
`--bde-threshold 7` (or score with `6`) to align.

**Cost / efficiency** (from the same scorecard): 632 DI calls, 20.87M LLM tokens (14.38M stage-1 +
6.49M stage-2); total **$23.65** (OCR 80.5% / LLM 19.5%, LLM exact $4.61 from the token split). The
one-pass design **saved 38.8% of DI calls** (400 of 1,032) vs the old two-pass workflow — 50.1% of
files were cleared by stage 1 and never re-OCR'd.

## 4. Cross-run comparison

Ten CNG scorecards, same ground truth and method (2,000-file samples, `auto → unreviewed`, truth
entities > 0 / BDE > 6). **Pipeline** view, recomputed from each run's confusion matrix:

| Run | Date | Recall | Precision | F1 | Accuracy | NR-error |
|---|---|---:|---:|---:|---:|---:|
| scaling-1 | 08-10 | 0.766 | 0.706 | 0.735 | 0.868 | 7.6% |
| scaling-2 | 08-10 | 0.754 | 0.703 | 0.727 | 0.865 | 8.0% |
| scaling-variance-1 | 08-10 | 0.756 | 0.642 | 0.694 | 0.866 | 6.4% |
| scaling-variance-2 | 08-10 | 0.779 | 0.650 | 0.709 | 0.871 | 5.9% |
| scaling-sts | 08-11 | 0.779 | 0.650 | 0.709 | 0.871 | 5.9% |
| scaling-api-version | 08-13 | 0.766 | 0.641 | 0.698 | 0.866 | 6.2% |
| scaling-bug-fix | 08-13 | 0.762 | 0.642 | 0.697 | 0.867 | 6.3% |
| scaling-output-limit | 08-13 | 0.779 | 0.625 | 0.694 | 0.861 | 6.0% |
| scaling-graphs | 08-15 | 0.766 | 0.641 | 0.698 | 0.866 | 6.2% |
| scaling-file-detail | 08-19 | 0.771 | 0.621 | 0.688 | 0.859 | 6.2% |
| **Range** | | **0.754–0.779** | **0.621–0.706** | **0.688–0.735** | **0.859–0.871** | **5.9–8.0%** |

**Observations:**
- **Stable across config changes.** Accuracy stays in a 1.2-point band (0.859–0.871) and F1 within
  ~5 points across API-version, bug-fix, output-limit, and scorecard-feature runs — the pipeline
  isn't fragile to those changes.
- **LLM run-to-run variance is real but small.** The two repeatability runs (`variance-1` vs
  `variance-2`, same config) differ by ~2 points of recall (0.756 vs 0.779) — the temperature-0 LLM
  still introduces minor non-determinism, so single-run deltas under ~2 points aren't signal.
- **Against the 5% NR-error target:** the pipeline lands at **5.9–8.0%** on these samples — slightly
  above target, with **stage-1-alone at ~5.3%** (at target). The gate itself is recall-safe; the
  pipeline's few extra misses come from letting stage 2 clear some survivors.
- Some runs share inputs (`scaling-graphs` reuses the `scaling-api-version` inventory; `scaling-sts`
  and `scaling-variance-2` report identical figures) — treat those as one data point, not two.

## 5. Build-to-build benchmarks (regression comparisons)

Beyond accuracy-vs-ground-truth, the project benchmarks **builds against each other** (from the
README's *What changed* record):

| Comparison | Result | Takeaway |
|---|---|---|
| **With vs without the retry layer** | **77% `RateLimitError`** failure vs **13 failures in 187,690 calls** | The backoff/jitter layer is load-bearing at scale |
| **With vs without Pillow** (embedded-image OCR) | **24,223 vs 0** DI calls on the *identical* corpus | Never compare across a Pillow boundary; the "cheaper" run was reading less |
| **3.0.0 stage-1 vs 2.10.2** (`--no-stage2`) | **Zero differences** in the 27 legacy columns on a shared corpus | The stage split didn't move the NR decision |
| **Single-pass vs old two-pass workflow** | **38.8% of DI calls saved** (`scaling-1`) | Reusing one extract/OCR pass across both stages is the 3.0.0 win |

## 6. Caveats

- **Sample, not census.** These are 2,000-file slices of one matter (CNG), not the full population.
- **Single-vendor ground truth.** Responsive/BDE truth is the CNG entities export; its own coding is
  taken as correct.
- **`auto → unreviewed`** means files absent from the export aren't scored — NR figures would change
  under `--absent-means zero`.
- **Recomputed metrics.** Recall/precision/F1 here were derived from the scorecards' confusion
  counts because the `.xlsx` cached only the counts; open a scorecard in Excel (or run the scorer)
  for its own formula-computed values and the full per-type / OCR-yield / misses detail.

See [`TESTING_RESULTS.md`](TESTING_RESULTS.md) for the hypothesis/learning log and
[`pii_triage_merged/tools/SCORING.md`](pii_triage_merged/tools/SCORING.md) for scorer detail.
