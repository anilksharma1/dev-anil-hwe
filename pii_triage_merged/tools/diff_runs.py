#!/usr/bin/env python3
"""diff_runs.py — compare two pii_triage inventory CSVs and report files whose
final verdict changed between runs.

Final verdict (pipeline logic):
  - Stage 1 NR  →  NR  (stage 1 clears a file; stage 2 never overrides this)
  - Otherwise   →  stage 2's call if it ran, else stage 1's call
  - Neither stage reached a clear lane  →  undetermined

Only files whose final verdict differs are included.  Every shared column from
both runs is written to the output CSV side-by-side.

Usage:
    python tools/diff_runs.py inventory_a.csv inventory_b.csv [options]

Options:
    --out PATH          Output CSV path (default: diff_<A>_vs_<B>.csv)
    --key rel_path|file_name
                        Join key column (default: rel_path, falls back to file_name)
    --label-a LABEL     Label for the first inventory in output (default: A)
    --label-b LABEL     Label for the second inventory in output (default: B)
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import Counter

RESP_LANES = {"standard", "bde", "structured_bde"}
NR_LANE    = "likely_non_responsive"

_EXT_RE = re.compile(r"\.(?=[A-Za-z0-9]{1,5}$)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{1,5}$")


def _norm(name: str) -> str:
    return _EXT_RE.sub("", os.path.basename(str(name or "").strip())).strip().lower()


def _canon(val) -> str:
    return (val or "").strip().lower()


def _load(path: str, key_col: str) -> tuple[dict[str, dict], list[str]]:
    rows: dict[str, dict] = {}
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        rd = csv.DictReader(fh)
        fieldnames = list(rd.fieldnames or [])
        available = {(c or "").strip() for c in fieldnames}
        if key_col in available:
            join_col = key_col
        elif "rel_path" in available:
            join_col = "rel_path"
        elif "file_name" in available:
            join_col = "file_name"
        else:
            sys.exit(f"ERROR: {path!r} has neither 'rel_path' nor 'file_name'.")
        for r in rd:
            raw_key = (r.get(join_col) or "").strip()
            if not raw_key:
                continue
            norm = _norm(raw_key) or raw_key.lower()
            if norm not in rows:
                rows[norm] = {"_raw_key": raw_key, **r}
    return rows, fieldnames


def _verdict(row: dict) -> str:
    """Compute the pipeline final verdict for one inventory row."""
    lane = _canon(row.get("suggested_lane", ""))
    if lane == NR_LANE:
        return "nr"
    s2_lane = _canon(row.get("s2_lane", ""))
    if s2_lane == NR_LANE:
        return "nr"
    if s2_lane in RESP_LANES:
        return "responsive"
    # Stage 2 did not run or returned no clear lane — fall back to stage 1.
    if lane in RESP_LANES:
        return "responsive"
    return "undetermined"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Diff two pii_triage inventories — files whose pipeline verdict changed."
    )
    parser.add_argument("inventory_a", help="First (baseline) inventory CSV")
    parser.add_argument("inventory_b", help="Second (comparison) inventory CSV")
    parser.add_argument("--out", default="", help="Output CSV path")
    parser.add_argument("--key", default="rel_path",
                        choices=["rel_path", "file_name"],
                        help="Join key column (default: rel_path)")
    parser.add_argument("--label-a", default="A", help="Label suffix for the first inventory")
    parser.add_argument("--label-b", default="B", help="Label suffix for the second inventory")
    args = parser.parse_args(argv)

    path_a, path_b = args.inventory_a, args.inventory_b
    label_a, label_b = args.label_a, args.label_b

    for p in (path_a, path_b):
        if not os.path.isfile(p):
            sys.exit(f"ERROR: file not found: {p!r}")

    rows_a, fields_a = _load(path_a, args.key)
    rows_b, fields_b = _load(path_b, args.key)

    # All shared columns, in the order they appear in file A, excluding the join key.
    _skip = {"rel_path", "file_name", "_raw_key"}
    shared_cols = [c for c in fields_a if c in set(fields_b) and c not in _skip]

    common_keys = set(rows_a) & set(rows_b)
    only_a = set(rows_a) - set(rows_b)
    only_b = set(rows_b) - set(rows_a)

    diff_rows: list[dict] = []
    verdict_transitions: Counter = Counter()
    per_col_changes: Counter = Counter()

    for key in sorted(common_keys):
        ra, rb = rows_a[key], rows_b[key]
        va, vb = _verdict(ra), _verdict(rb)
        if va == vb:
            continue

        verdict_transitions[(va, vb)] += 1
        changed = [c for c in shared_cols if _canon(ra.get(c, "")) != _canon(rb.get(c, ""))]
        per_col_changes.update(changed)

        out_row: dict = {
            "file":       ra.get("_raw_key") or rb.get("_raw_key"),
            f"verdict_{label_a}": va,
            f"verdict_{label_b}": vb,
            "changed_cols": ",".join(changed),
        }
        for c in shared_cols:
            out_row[f"{c}_{label_a}"] = ra.get(c, "")
            out_row[f"{c}_{label_b}"] = rb.get(c, "")

        diff_rows.append(out_row)

    # Output path.
    if args.out:
        out_path = args.out
    else:
        stem_a = os.path.splitext(os.path.basename(path_a))[0]
        stem_b = os.path.splitext(os.path.basename(path_b))[0]
        out_path = f"diff_{stem_a}_vs_{stem_b}.csv"

    out_fields = (
        ["file", f"verdict_{label_a}", f"verdict_{label_b}", "changed_cols"]
        + [f"{c}_{label_a}" for c in shared_cols]
        + [f"{c}_{label_b}" for c in shared_cols]
    )

    if diff_rows:
        with open(out_path, "w", newline="", encoding="utf-8") as fh:
            wr = csv.DictWriter(fh, fieldnames=out_fields, extrasaction="ignore")
            wr.writeheader()
            wr.writerows(diff_rows)

    # ---- summary ----
    n_a, n_b, n_common = len(rows_a), len(rows_b), len(common_keys)
    n_diff = len(diff_rows)

    print(f"\n{'='*66}")
    print(f"  diff_runs  {os.path.basename(path_a)}  vs  {os.path.basename(path_b)}")
    print(f"{'='*66}")
    print(f"  {label_a}: {n_a:,} rows    {label_b}: {n_b:,} rows    common: {n_common:,}")
    if only_a:
        print(f"  only in {label_a}: {len(only_a):,} files")
    if only_b:
        print(f"  only in {label_b}: {len(only_b):,} files")

    if n_common:
        print(f"\n  Files with changed verdict: {n_diff:,} of {n_common:,} ({n_diff/n_common:.1%})")
    else:
        print("\n  No common files.")

    if verdict_transitions:
        print(f"\n  Verdict transitions ({label_a} → {label_b}):")
        for (va, vb), cnt in verdict_transitions.most_common():
            print(f"    {va:<16s} → {vb:<16s}  {cnt:,}")

    if per_col_changes:
        print(f"\n  Columns that changed most often (among differing files):")
        for col, cnt in per_col_changes.most_common(20):
            print(f"    {col:<34s}  {cnt:,}")

    if diff_rows:
        print(f"\n  Output: {out_path}  ({len(shared_cols)} shared columns × 2 labels)")
    else:
        print("\n  No verdict differences found — no output file written.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
