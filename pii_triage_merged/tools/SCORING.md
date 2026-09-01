# Scoring a combined run — `tools/score_combined.py`

One scorecard: OCR cost, LLM cost, NR/R accuracy for stage 1 / stage 2 / both, and BDE
accuracy. Replaces running `score_bde_by_count.py` and `metrics_vs_manual.py` separately.
Stdlib only (reads `.xlsx` without openpyxl), read-only.

```
python tools/score_combined.py \
    --inventory inventory.csv \
    --entities "C:\Users\AnnaPoutanen\Desktop\07222026 re gk CNG_Entities Export.csv" \
    --bde-threshold 6 \
    --price-per-1k-in 0.10 --price-per-1k-out 0.40 --price-per-1k-pages 10.0 \
    --out-dir .
```

## The entities export does double duty

Because it gives a per-file `Total Entities`, one sheet supplies **both** ground truths:

| Truth | Rule |
|---|---|
| Responsive | `Total Entities > 0` — your "zero entities means no PII" |
| BDE | `Total Entities > --bde-threshold` (default 6, i.e. 7+) |

Add `--manual <sheet>` with a `Data Entry Status` column to cross-check the entities-derived
R/NR truth against reviewer status. If those two disagree materially, one sheet is stale and
every NR number is suspect — the script says so.

## The one setting to get right: `--absent-means`

A file in the inventory but **not** in the entities export means one of two opposite things:

- `--absent-means zero` — the export lists only files that *have* entities, so absent = 0
  entities = genuinely non-responsive. Scores the whole corpus.
- `--absent-means unreviewed` — the export includes zero-count rows, so absent = never
  reviewed. Scores the intersection only.

`auto` (default) infers it from whether the export contains any `0` rows and **prints which
way it went**. Getting it backwards inverts every NR figure, so check that line.

## Control ID matching

IDs are matched to `file_name` by stripping a trailing extension with an anchored
`\.[A-Za-z0-9]{1,5}$` regex — the rule from your `metrics_vs_manual.py`.

**Do not use `os.path.splitext` for this.** `Q00897.01-0000000003` has no extension but does
contain a dot, and `splitext` cuts it to `Q00897`, silently collapsing tens of thousands of
IDs onto a handful. I hit exactly that while building this. The script now also **warns loudly
if the match rate drops below 50%** and prints sample IDs from both sides, because a broken
match produces plausible-looking but meaningless metrics.

## What it prints

**1. Run cost.** DI calls, billable pages (full-file + embedded), per-stage tokens, priced if
you pass rates. Plus the single-pass saving: how many DI calls a second pass over the stage-1
survivors would have repeated. Prices are optional and nothing is invented without them —
and since only `total_tokens` is recorded, LLM cost is reported as a **range** across your
in/out prices rather than a fake point estimate.

**2. NR/R accuracy**, five blocks:

| Block | Population | What it answers |
|---|---|---|
| Stage 1 (Anna's) | all scored files | the tracker number — is NR removal safe? |
| Stage 2 (Daniel's) | only files stage 2 graded | how good is the overview? |
| Stage 1 restricted to stage 2's population | same as above | **the apples-to-apples comparison** |
| Both — sequential pipeline | all scored files | what a reviewer actually receives |
| Both — union | all scored files | recall ceiling; shows what gating costs |

The third block matters: stage 1 runs on everything and stage 2 only on survivors, so
comparing their headline numbers directly is misleading. Compare blocks 2 and 3.

Each reports `NR accuracy` (of files **cleared**, share actually responsive — target < 5%) and
`R accuracy` (of files **flagged**, share actually non-responsive — target < 50%), in your
tracker's wording.

**3. BDE accuracy** — your four definitions plus stage 2's own flag, and count accuracy
against the human count. Note the off-by-one it prints: truth is `count > 6` (≥7) while the
scanner flags `is_bde` at `count >= bde_threshold`, so run the scan with
`--bde-threshold 7` to line them up.

The "net change from bde_person_count" line can be **negative**, which is not necessarily
wrong: for structured files the effective count is `bde_person_count` alone (the
distinct-subject tier), not `max(est, bpc)`, so the LLM count is allowed to correct an
inflated token estimate downward. If your human counts say those files really are large, the
counter is under-reading them.

**4. By file type** — R/NR plus DI calls and tokens per type, so cost and accuracy sit side
by side.

**5. OCR yield** — how many files gained text from embedded-image OCR, what share of DI spend
that path consumed, and how those files were routed. Suggestive, not causal: for the real
answer re-run with `--no-image-ocr` and diff the lanes.

## Outputs

- `scorecard_summary.csv` — every metric as `section, metric, value`, easy to paste into the
  tracker.
- `scorecard_misses.csv` — the individual files behind the numbers: stage-1 misses and
  over-calls, pipeline misses, BDE misses and over-calls, each with human count, tool count,
  `bde_person_count`, lane, stage-2 level and `detail`.

**Read the misses first.** A stage-1 false negative is a document with real PII that the tool
cleared — a missed notification, and the only error class here that nothing downstream catches.

## Works on old runs too

Point it at a 2.10.2 inventory and it degrades gracefully: stage-2 blocks are omitted and it
tells you OCR spend is unmeasurable from that CSV. Useful for scoring your existing
`test_ai.csv` with the same code path.
