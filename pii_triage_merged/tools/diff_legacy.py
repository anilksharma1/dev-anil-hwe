#!/usr/bin/env python3
r"""
diff_legacy.py -- diff two inventory CSVs, column by column, keyed on rel_path.

Built for the one comparison that gates the merge: does 3.0.0's stage 1 reproduce 2.10.2's
27 legacy columns exactly? Also useful for any before/after on the same corpus -- e.g. a run
with and without --no-image-ocr, to see what embedded-image OCR actually changes.

    python tools\diff_legacy.py OLD.csv NEW.csv
    python tools\diff_legacy.py a.csv b.csv --columns suggested_lane,is_bde
    python tools\diff_legacy.py a.csv b.csv --all-columns --out-dir diffreport

By default it compares the 27 legacy columns and ignores the ones expected to differ between
runs (timings, and token counts, which move with the model). Rows present in only one file are
reported separately and never counted as differences.

Exit code is 0 if there are no differences in the compared columns, 1 otherwise -- so it can
gate a script.

Stdlib only. Read-only. Python 3.10+.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter, defaultdict

LEGACY = [
    "rel_path", "file_name", "ext", "size_bytes", "status", "searchable", "programmatic",
    "text_extractable", "is_structured", "page_or_sheet_count", "attachment_count",
    "estimated_entities", "estimate_truncated", "bde_person_count", "bde_confirmed",
    "entity_bucket", "entities_found", "value_signal", "pi_categories", "is_bde",
    "complexity_bucket", "ambiguity", "llm_consulted", "llm_responsive", "llm_tokens",
    "suggested_lane", "detail",
]

# Never meaningful to compare across two runs.
ALWAYS_IGNORE = {"elapsed_s"}
# Move with the model / with retries, not with the code. Ignored unless --strict.
NOISY = {"llm_tokens", "s2_llm_tokens", "llm_tokens_total", "detail"}


def load(path):
    rows = {}
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        rd = csv.DictReader(fh)
        fields = list(rd.fieldnames or [])
        for r in rd:
            key = (r.get("rel_path") or r.get("file_name") or "").strip()
            if key:
                rows[key] = r
    return rows, fields


def main():
    ap = argparse.ArgumentParser(description="Diff two inventory CSVs column by column.")
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--columns", default=None,
                    help="comma-separated columns to compare (default: the 27 legacy columns)")
    ap.add_argument("--all-columns", action="store_true",
                    help="compare every column present in BOTH files")
    ap.add_argument("--ignore-columns", default="",
                    help="comma-separated columns to skip")
    ap.add_argument("--strict", action="store_true",
                    help="also compare token counts and detail (noisy across runs)")
    ap.add_argument("--examples", type=int, default=8,
                    help="differing rows to print per column")
    ap.add_argument("--out-dir", default=None,
                    help="write differences.csv / only_in_old.csv / only_in_new.csv here")
    a = ap.parse_args()

    for p in (a.old, a.new):
        if not os.path.exists(p):
            sys.exit(f"not found: {p}")

    old, of = load(a.old)
    new, nf = load(a.new)
    common_fields = [c for c in of if c in nf]

    if a.columns:
        cols = [c.strip() for c in a.columns.split(",") if c.strip()]
    elif a.all_columns:
        cols = list(common_fields)
    else:
        cols = [c for c in LEGACY if c in common_fields]
        missing = [c for c in LEGACY if c not in common_fields]
        if missing:
            print(f"note: {len(missing)} legacy column(s) absent from one file, skipped: "
                  f"{', '.join(missing)}")

    skip = set(ALWAYS_IGNORE) | {c.strip() for c in a.ignore_columns.split(",") if c.strip()}
    if not a.strict:
        skip |= NOISY
    ignored = [c for c in cols if c in skip]
    cols = [c for c in cols if c not in skip]

    keys_old, keys_new = set(old), set(new)
    both = keys_old & keys_new
    only_old = sorted(keys_old - keys_new)
    only_new = sorted(keys_new - keys_old)

    print("=" * 72)
    print("INVENTORY DIFF")
    print("=" * 72)
    print(f"old : {a.old}   ({len(old):,} rows, {len(of)} columns)")
    print(f"new : {a.new}   ({len(new):,} rows, {len(nf)} columns)")
    print(f"rows in both        : {len(both):,}")
    print(f"only in old         : {len(only_old):,}")
    print(f"only in new         : {len(only_new):,}")
    print(f"columns compared    : {len(cols)}")
    if ignored:
        print(f"columns ignored     : {', '.join(ignored)}"
              + ("   (use --strict to include)" if not a.strict else ""))

    per_col = Counter()
    transitions = defaultdict(Counter)
    examples = defaultdict(list)
    changed_rows = set()
    for k in sorted(both):
        o, n = old[k], new[k]
        for c in cols:
            ov, nv = (o.get(c) or ""), (n.get(c) or "")
            if ov != nv:
                per_col[c] += 1
                changed_rows.add(k)
                transitions[c][(ov[:40], nv[:40])] += 1
                if len(examples[c]) < a.examples:
                    examples[c].append((k, ov, nv))

    total = sum(per_col.values())
    print("-" * 72)
    if not total:
        print(f"NO DIFFERENCES across {len(both):,} rows x {len(cols)} columns.")
        if not only_old and not only_new:
            print("The two runs are identical on the compared columns.")
    else:
        print(f"{total:,} cell difference(s) over {len(changed_rows):,} row(s) "
              f"({len(changed_rows) / max(len(both), 1):.2%} of rows)")
        print()
        print(f"{'COLUMN':<24}{'rows differing':>16}{'% of rows':>12}")
        print("-" * 52)
        for c, n in per_col.most_common():
            print(f"{c:<24}{n:>16,}{n / max(len(both), 1):>11.2%}")
        for c, n in per_col.most_common():
            print()
            print(f"--- {c} ({n:,} differing) ---")
            top = transitions[c].most_common(6)
            for (ov, nv), cnt in top:
                print(f"    {ov!r:<44} -> {nv!r:<44} x{cnt:,}")
            if len(transitions[c]) > len(top):
                print(f"    ... {len(transitions[c]) - len(top):,} more distinct transitions")
            for k, ov, nv in examples[c]:
                print(f"    e.g. {k}: {ov!r} -> {nv!r}")

    if only_old or only_new:
        print()
        print("ROWS PRESENT IN ONLY ONE FILE (not counted as differences)")
        for label, ids in (("only in old", only_old), ("only in new", only_new)):
            if ids:
                print(f"  {label} ({len(ids):,}): " + ", ".join(ids[:5])
                      + (" ..." if len(ids) > 5 else ""))

    if a.out_dir:
        os.makedirs(a.out_dir, exist_ok=True)
        p = os.path.join(a.out_dir, "differences.csv")
        with open(p, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["rel_path", "column", "old_value", "new_value"])
            for k in sorted(changed_rows):
                for c in cols:
                    ov, nv = (old[k].get(c) or ""), (new[k].get(c) or "")
                    if ov != nv:
                        w.writerow([k, c, ov, nv])
        print(f"\nwrote {p}")
        for label, ids, src in (("only_in_old.csv", only_old, old),
                                ("only_in_new.csv", only_new, new)):
            if ids:
                q = os.path.join(a.out_dir, label)
                with open(q, "w", newline="", encoding="utf-8-sig") as fh:
                    w = csv.DictWriter(fh, fieldnames=list(src[ids[0]].keys()))
                    w.writeheader()
                    for k in ids:
                        w.writerow(src[k])
                print(f"wrote {q}")

    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
