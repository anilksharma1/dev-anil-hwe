"""Build the HWE bucket-tagging deliverables from an inventory.

Table 1 (searchable files, per-file): File ID, File Type, Searchable,
Programmatic, Entities Found, Entity Bucket.
Table 2 is produced by `sampling.estimate` (it needs the coded sample).
"""
from __future__ import annotations

import csv


def build_table1(inventory_csv: str, out_csv: str) -> int:
    rows_out = []
    with open(inventory_csv, "r", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            if r.get("searchable") not in ("True", "true"):
                continue
            if r.get("status") != "ok":
                continue
            rows_out.append({
                "File ID": r["rel_path"],
                "File Type": r["ext"],
                "Searchable": "Yes",
                "Programmatic": "Yes" if r.get("programmatic") in ("True", "true") else "No",
                "Entities Found": r.get("entities_found", ""),
                "Entity Bucket": r.get("entity_bucket", ""),
            })
    cols = ["File ID", "File Type", "Searchable", "Programmatic",
            "Entities Found", "Entity Bucket"]
    with open(out_csv, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows_out)
    return len(rows_out)
