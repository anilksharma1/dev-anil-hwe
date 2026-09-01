"""Non-searchable estimation workflow: sample -> code -> extrapolate (Table 2).

For files that cannot be searched, the HWE spec samples 2-5% of each complexity
bucket, has reviewers code responsiveness/BDE on the sample, then extrapolates
percentages to the whole bucket population.
"""
from __future__ import annotations

import csv
import math
import random
from collections import defaultdict


def _nonsearchable_by_bucket(inventory_csv: str) -> dict:
    buckets = defaultdict(list)
    with open(inventory_csv, "r", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            if r.get("suggested_lane") == "nonsearchable_sample":
                buckets[r.get("complexity_bucket", "unknown")].append(r)
    return buckets


def draw_sample(inventory_csv: str, out_csv: str, rate: float = 0.05,
                seed: int = 12345) -> int:
    """Write a per-bucket random sample for reviewers to code."""
    rng = random.Random(seed)
    buckets = _nonsearchable_by_bucket(inventory_csv)
    cols = ["rel_path", "complexity_bucket", "file_type", "gold_responsive", "gold_bde"]
    n_total = 0
    with open(out_csv, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for bucket, rows in sorted(buckets.items()):
            k = max(1, math.ceil(len(rows) * rate))
            for r in rng.sample(rows, min(k, len(rows))):
                w.writerow({"rel_path": r["rel_path"], "complexity_bucket": bucket,
                            "file_type": r["ext"], "gold_responsive": "", "gold_bde": ""})
                n_total += 1
    return n_total


def estimate(inventory_csv: str, coded_sample_csv: str, out_table2_csv: str) -> list:
    """Extrapolate coded-sample percentages to the full bucket populations."""
    buckets = _nonsearchable_by_bucket(inventory_csv)

    sampled = defaultdict(lambda: {"n": 0, "resp": 0, "bde": 0})
    with open(coded_sample_csv, "r", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            b = r.get("complexity_bucket", "unknown")
            sampled[b]["n"] += 1
            if str(r.get("gold_responsive", "")).strip() in ("1", "true", "True"):
                sampled[b]["resp"] += 1
            if str(r.get("gold_bde", "")).strip() in ("1", "true", "True"):
                sampled[b]["bde"] += 1

    table = []
    for i, (bucket, rows) in enumerate(sorted(buckets.items()), start=1):
        n_files = len(rows)
        ftype = _most_common(r["ext"] for r in rows)
        s = sampled.get(bucket, {"n": 0, "resp": 0, "bde": 0})
        pct_resp = (s["resp"] / s["n"]) if s["n"] else 0.0
        pct_bde = (s["bde"] / s["n"]) if s["n"] else 0.0
        table.append({
            "Bucket ID": i, "Bucket": bucket, "File Type": ftype,
            "Searchable": "No",
            "Programmatic": "Yes" if any(r.get("programmatic") in ("True", "true")
                                         for r in rows) else "No",
            "# of Files": n_files,
            "% Responsive": f"{pct_resp*100:.0f}%",
            "% BDE": f"{pct_bde*100:.0f}%",
            "# of Responsive (Predicted)": round(pct_resp * n_files),
            "# of BDEs (Predicted)": round(pct_bde * n_files),
            "Sample Size": s["n"],
        })

    cols = ["Bucket ID", "Bucket", "File Type", "Searchable", "Programmatic",
            "# of Files", "% Responsive", "% BDE",
            "# of Responsive (Predicted)", "# of BDEs (Predicted)", "Sample Size"]
    with open(out_table2_csv, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(table)
    return table


def _most_common(it):
    counts = defaultdict(int)
    for x in it:
        counts[x] += 1
    return max(counts, key=counts.get) if counts else ""
