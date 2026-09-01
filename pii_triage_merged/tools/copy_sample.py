#!/usr/bin/env python3
r"""
copy_sample.py -- Step B of building the validation sample.

Reads the manifest produced by select_sample.py and copies each listed file
from SOURCE_DIR to DEST_DIR.  Files may live anywhere inside SOURCE_DIR; the
tool indexes all filenames on first run so subdirectories are handled
transparently.  Optionally organises the destination into responsive/ and
non_responsive/ subfolders.

Manifest columns (as produced by select_sample.py on the nr-precision branch):
    control_id, label, type_category, size_bucket, size_bytes, rel_path, dest_name

Run:
    python copy_sample.py manifest.csv SOURCE_DIR DEST_DIR [options]

Options:
    --by-class          Create responsive/ and non_responsive/ subfolders
    --dry-run           Print what would be copied; don't touch the filesystem
    --missing-ok        Exit 0 even when some files aren't found (default: exit 1)
"""

import argparse
import csv
import os
import shutil
import sys
from collections import defaultdict


# ── manifest column names (must match select_sample.py nr-precision output) ─
COL_REL_PATH  = "rel_path"    # relative path from source root (or bare filename)
COL_DEST_NAME = "dest_name"   # filename to use at destination
COL_LABEL     = "label"       # "responsive" | "non_responsive"
COL_CTRL_ID   = "control_id"


def build_index(source_dir: str) -> dict[str, list[str]]:
    """Walk source_dir and map each bare filename (case-folded) -> [full paths]."""
    index: dict[str, list[str]] = defaultdict(list)
    for dirpath, _, filenames in os.walk(source_dir):
        for fname in filenames:
            index[fname.lower()].append(os.path.join(dirpath, fname))
    return index


def read_manifest(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    if not rows:
        sys.exit(f"error: manifest is empty: {path}")
    headers = list(rows[0].keys())
    if COL_REL_PATH not in headers:
        sys.exit(
            f"error: '{COL_REL_PATH}' column not found in manifest.\n"
            f"  columns present: {headers}\n"
            f"  make sure this manifest was produced by select_sample.py (nr-precision branch)"
        )
    return rows


def main():
    ap = argparse.ArgumentParser(
        description="Copy files listed in a select_sample.py manifest."
    )
    ap.add_argument("manifest", help="Path to sample_manifest.csv")
    ap.add_argument("source_dir", help="Root folder that contains the original files")
    ap.add_argument("dest_dir",   help="Destination folder for copied files")
    ap.add_argument("--by-class", action="store_true",
                    help="Put files in responsive/ or non-responsive/ subfolders")
    ap.add_argument("--dry-run",  action="store_true",
                    help="Show what would happen without copying anything")
    ap.add_argument("--missing-ok", action="store_true",
                    help="Exit 0 even when some manifest files are not found")
    args = ap.parse_args()

    if not os.path.isfile(args.manifest):
        sys.exit(f"error: manifest not found: {args.manifest}")
    if not os.path.isdir(args.source_dir):
        sys.exit(f"error: source_dir not found: {args.source_dir}")

    rows = read_manifest(args.manifest)
    print(f"manifest rows:  {len(rows)}")
    print(f"source:         {args.source_dir}")
    print(f"destination:    {args.dest_dir}")
    if args.dry_run:
        print("*** DRY RUN — no files will be copied ***")
    print("indexing source directory …")
    index = build_index(args.source_dir)
    print(f"  {sum(len(v) for v in index.values())} files indexed")
    print()

    copied = skipped_dup = 0
    missing: list[str] = []

    for row in rows:
        rel_path  = (row.get(COL_REL_PATH)  or "").strip()
        dest_name = (row.get(COL_DEST_NAME) or "").strip()
        if not rel_path:
            continue

        # Use dest_name as output filename; fall back to the basename of rel_path.
        out_fname = dest_name or os.path.basename(rel_path)

        # Resolution order:
        #   1. rel_path as a path relative to source_dir (handles subdirectory layouts)
        #   2. filename-only index search (handles flat or unknown layouts)
        direct = os.path.join(args.source_dir, rel_path)
        if os.path.isfile(direct):
            src_path = direct
        else:
            key = os.path.basename(rel_path).lower()
            candidates = index.get(key, [])
            if not candidates:
                missing.append(rel_path)
                print(f"  MISSING  {rel_path}")
                continue
            if len(candidates) > 1:
                print(f"  WARN     {key} found {len(candidates)}x — using first match")
                print(f"           {candidates[0]}")
            src_path = candidates[0]

        if args.by_class:
            cls = (row.get(COL_LABEL) or "unknown").strip()
            dst_folder = os.path.join(args.dest_dir, cls)
        else:
            dst_folder = args.dest_dir

        dst_path = os.path.join(dst_folder, out_fname)

        if os.path.exists(dst_path):
            skipped_dup += 1
            print(f"  EXISTS   {out_fname}")
            continue

        print(f"  copy     {out_fname}")
        if not args.dry_run:
            os.makedirs(dst_folder, exist_ok=True)
            shutil.copy2(src_path, dst_path)
        copied += 1

    # ── summary ──────────────────────────────────────────────────────────────
    print()
    print("=" * 56)
    print("COPY SUMMARY")
    print("=" * 56)
    print(f"copied:          {copied}")
    print(f"already existed: {skipped_dup}")
    print(f"not found:       {len(missing)}")
    if missing:
        print()
        print("missing files:")
        for f in missing:
            print(f"  {f}")

    if args.dry_run:
        print()
        print("(dry run — nothing was written)")

    if missing and not args.missing_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
