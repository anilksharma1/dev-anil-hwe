# Running 3.0.0 — Windows quickstart

Written for your setup: corpus on `Z:`, working folder on the Desktop, existing 2.10.2 build at
`C:\Users\AnnaPoutanen\Desktop\New-HWE\pii_triage`.

---

## 0. Put it somewhere, keep the old build

Extract the zip and rename the folder — **don't overwrite `New-HWE`.** You want the old build
intact to compare against.

```
C:\Users\AnnaPoutanen\Desktop\
    New-HWE\pii_triage\        <- your existing 2.10.2, leave alone
    HWE-3.0\                   <- extract here (the zip's "merged" folder, renamed)
        pii_triage\            <- the package
        tools\
        tests\
        rulepacks\
        README.md
```

Everything below runs from `C:\Users\AnnaPoutanen\Desktop\HWE-3.0` — the folder that *contains*
`pii_triage`, not the package itself.

```bat
cd /d C:\Users\AnnaPoutanen\Desktop\HWE-3.0
```

## 1. Install

```bat
pip install -r requirements.txt
```

Legacy `.doc`/`.xls`/`.ppt` conversion needs LibreOffice installed separately (the `soffice`
binary on PATH, or `SOFFICE_PATH` pointing at it) — not a pip package. Without it, those files
fall back to whatever `extractors.py` can manage natively (`antiword`/`catdoc` for `.doc`,
`xlrd` for `.xls`) or are flagged `needs_conversion`.

Then **verify Pillow specifically**, because it's the one that fails silently:

```bat
python -c "import PIL; print('Pillow', PIL.__version__)"
```

If that errors, `pip install Pillow` and try again. Without it, OCR of images embedded in text
PDFs is switched off and nothing tells you.

## 2. Bring your `.env` across

```bat
copy C:\Users\AnnaPoutanen\Desktop\New-HWE\pii_triage\.env .
```

Your existing one loads 4 settings. Nothing about the format changed.

## 3. Verify the install — 3 commands, ~10 seconds

```bat
python -m pytest tests\ -q
python tools\check_nr_frozen.py check
python -m pii_triage --version
```

Expect `282 passed, 4 skipped`, `NR FROZEN CHECK PASSED`, and `pii_triage 3.0.0`.

**Exactly 4 skips, all naming `PROJECT_PLAN.md Q-2`.** Those are deliberate (see the README).
`python -m pytest tests\ -q -rs` lists the reasons.

If you also see three `reportlab not installed` skips, `pip install reportlab` — it's now in
`requirements.txt`. Those three are not incidental: they cover the **OCR routing decision**
itself (an image-dominated PDF must go to OCR; a letterhead logo must *not*, or you pay for DI
calls on every letterhead) and AcroForm extraction, which is how an SSN typed into a fillable
form gets seen at all. Worth having them run.

If the frozen check reports **DRIFT on both `detection.py` and `routing.py` at once**, that is
almost certainly line endings, not a code change — the zip ships LF and Windows tooling sometimes
converts to CRLF. The checker normalises for this now, so on this build it just passes. If you
ever see it on an older copy, don't run `capture` to make it go away; that's how a real regression
gets blessed.

Any *other* frozen-check failure: stop. Something modified the non-responsive decision path, and
that's the one thing worth halting for.

## 4. Smoke test on a small folder first

Use the 100-file sample folder you already have:

```bat
python -m pii_triage scan ^
  "Z:\Managed Review Service\1.0 Cyber Incident Response\3.0 Cognicion\002. CNG\07. BDE\01. Native Documents\2026.05.21 Review Population_export1_sample_100" ^
  --out smoke.csv --workers 4 --bde-threshold 7 --ocr --llm --restart
```

Read the summary block it prints. You should see:

```
pipeline: stage 1 (NR/BDE) + stage 2 (graded overview) on stage-1 survivors
OCR: N Document Intelligence calls over M files (P billable pages)
  full-file : ... embedded : ...
stage 2: graded N file(s) (responsive X, NR Y); Z tokens
  levels: clear_yes=.., likely_yes=.., borderline=..
  skipped: stage1_nr=..
```

**If it says `OCR: enabled but 0 Document Intelligence calls were made`**, either the sample has
no images at all, or Pillow is missing. Check step 1 again.

## 5. The important run: prove stage 1 didn't move

Before you trust anything, confirm the merged build reproduces your existing stage-1 numbers.
`--no-stage2` runs stage 1 alone.

```bat
python -m pii_triage scan ^
  "Z:\Managed Review Service\1.0 Cyber Incident Response\3.0 Cognicion\002. CNG\07. BDE\01. Native Documents\2026.05.21 Review Population_export1\2026.05.21 Review Population_export" ^
  --out stage1_only.csv --workers 8 --bde-threshold 7 --ner --ocr --llm --restart
```

Note `--ner`: your 2.10.2 run had NER on, and detection has to match for the comparison to mean
anything. Then diff the 27 legacy columns against your existing output:

```bat
python tools\diff_legacy.py ^
  C:\Users\AnnaPoutanen\Desktop\New-HWE\pii_triage\test_ai.csv ^
  stage1_only.csv
```

**Expect zero differences**, except on files affected by OCR — and there should be almost none,
since only 3 PDFs in that corpus were image-only. If a file with no OCR shows a different lane,
stop and tell me; that's a merge bug in the one place it matters.

## 6. The real combined run

```bat
python -m pii_triage scan ^
  "Z:\Managed Review Service\1.0 Cyber Incident Response\3.0 Cognicion\002. CNG\07. BDE\01. Native Documents\2026.05.21 Review Population_export1\2026.05.21 Review Population_export" ^
  --out inventory.csv --workers 8 --bde-threshold 7 --ner --ocr --llm ^
  --protocol protocols\cognicion.txt ^
  --price-per-1k-pages 10.0
```

Set `--price-per-1k-pages` to your actual contracted Document Intelligence rate so the run prints
real money. Add `--price-per-1k-in` / `--price-per-1k-out` for the LLM range in the manifest.

**Why `--bde-threshold 7`:** your entities export defines BDE as `Total Entities > 6`, i.e. 7 or
more. The scanner flags at `>= threshold`. So 7 lines them up. This is the off-by-one your
existing scorer compensates for with `threshold = bde_threshold - 1`.

It resumes automatically — if it dies or you Ctrl-C, rerun the identical command and it picks up.
Only `--restart` throws progress away.

## 7. Score it

```bat
python tools\score_combined.py ^
  --inventory inventory.csv ^
  --entities "C:\Users\AnnaPoutanen\Desktop\07222026 re gk CNG_Entities Export.csv" ^
  --bde-threshold 6 ^
  --price-per-1k-pages 10.0 --price-per-1k-in 0.10 --price-per-1k-out 0.40 ^
  --out-dir .
```

Note the thresholds differ on purpose: `--bde-threshold 7` on the **scan** (flags at `>=`),
`--bde-threshold 6` on the **score** (truth is `> 6`). Same boundary, two conventions.

**Check the `absent-means` line it prints.** It decides what a file missing from the entities
export means — 0 entities, or never reviewed — and that choice inverts every non-responsive
figure. It auto-detects and says which way it went. Override with `--absent-means zero` or
`--absent-means unreviewed` if it guessed wrong.

Then read `scorecard_misses.csv` before the summary. Stage-1 false negatives are documents with
real PII the tool cleared, and nothing downstream catches them.

## 8. The HWE deliverables, unchanged

```bat
python -m pii_triage report inventory.csv --out table1_searchable.csv
python -m pii_triage sample inventory.csv --out sample.csv --rate 0.05
:: reviewers fill gold_responsive / gold_bde in sample.csv, then:
python -m pii_triage estimate inventory.csv sample.csv --out table2_nonsearchable.csv
```

---

## Things worth knowing

**Excel locks the CSV.** If `inventory.csv` is open in Excel, the scan exits with a plain message
rather than a traceback. Close it and rerun — progress is kept.

**The watchdog needs `--workers 2` or more.** It works by killing and replacing a stuck worker, so
it can't help at `--workers 1`. Default is a 120-second per-file limit; `set PII_WATCHDOG_S=180`
to change it, `set PII_WATCHDOG_S=0` to disable (don't).

**Sizing `--workers`.** In-flight Azure calls ≈ workers. Too many and you'll see
`llm_failed:RateLimitError` in `detail`. Retries with backoff are built in, but they can't create
RPM headroom you don't have. 8 is a reasonable start; check the failure count in the run summary.

**Measuring what embedded-image OCR is worth.** It's ~95% of DI calls and nobody has established
what it changes. Run a slice twice, once with `--no-image-ocr`, and diff the lanes:

```bat
python -m pii_triage scan "Z:\...\_sample_100" --out with_img.csv --ocr --llm --workers 4 --restart
python -m pii_triage scan "Z:\...\_sample_100" --out no_img.csv  --ocr --llm --workers 4 --restart --no-image-ocr
python tools\diff_legacy.py with_img.csv no_img.csv
```

If few lanes move, tightening the image qualification threshold saves more than the merge did.

**Flags you probably won't need:** `--stage2-on-all` (grades files stage 1 cleared — costs extra
LLM calls, use only to reproduce an ungated overview), `--ocr-max-pages 0` (whole-document OCR
instead of the first 15 pages; correct but a behaviour change, validate separately),
`--s2-bde-threshold` (defaults to `--bde-threshold`).
